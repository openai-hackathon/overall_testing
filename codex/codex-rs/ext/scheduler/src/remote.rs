use std::collections::HashMap;
use std::future::pending;
use std::io;
use std::io::BufRead;
use std::io::BufReader;
use std::io::Read;
use std::io::Write;
use std::net::Shutdown;
use std::net::SocketAddr;
use std::net::TcpStream;
use std::sync::Arc;
use std::sync::Mutex;
use std::time::Duration;
use std::time::SystemTime;
use std::time::UNIX_EPOCH;

use serde_json::Value;
use serde_json::json;
use tokio::sync::oneshot;

use crate::CallKey;
use crate::TaskFeatures;

#[derive(Default)]
struct Lease {
    stream: Option<TcpStream>,
    closed: bool,
}

pub(crate) struct RemoteScheduler {
    address: SocketAddr,
    client_id: String,
    calls: Mutex<HashMap<CallKey, Arc<Mutex<Lease>>>>,
}

struct PendingLease<'a> {
    scheduler: &'a RemoteScheduler,
    key: Option<CallKey>,
}

impl Drop for PendingLease<'_> {
    fn drop(&mut self) {
        if let Some(key) = &self.key {
            self.scheduler
                .release_matching(|candidate| candidate == key);
        }
    }
}

impl RemoteScheduler {
    pub(crate) fn new(address: SocketAddr) -> Self {
        let pid = std::process::id();
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        Self {
            address,
            client_id: format!("{pid}-{nonce}"),
            calls: Mutex::new(HashMap::new()),
        }
    }

    pub(crate) async fn admit(&self, key: CallKey, features: TaskFeatures) {
        let lease = Arc::new(Mutex::new(Lease::default()));
        let inserted = {
            let mut calls = self
                .calls
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            if calls.contains_key(&key) {
                false
            } else {
                calls.insert(key.clone(), lease.clone());
                true
            }
        };
        if !inserted {
            pending::<()>().await;
            return;
        }
        let mut guard = PendingLease {
            scheduler: self,
            key: Some(key.clone()),
        };
        let (tx, rx) = oneshot::channel();
        let address = self.address;
        let message = json!({
            "op": "enqueue", "client_id": self.client_id,
            "thread_id": key.thread_id, "turn_id": key.turn_id, "call_id": key.call_id,
            "cwd": features.cwd, "tool": features.tool_name,
        });
        std::thread::spawn(move || {
            let result = (|| -> io::Result<()> {
                let mut stream = TcpStream::connect_timeout(&address, Duration::from_secs(3))?;
                stream.set_write_timeout(Some(Duration::from_secs(3)))?;
                {
                    let mut state = lease
                        .lock()
                        .unwrap_or_else(std::sync::PoisonError::into_inner);
                    if state.closed {
                        return Err(io::Error::other("admission cancelled"));
                    }
                    state.stream = Some(stream.try_clone()?);
                }
                writeln!(stream, "{message}")?;
                let mut reader = BufReader::new(stream);
                loop {
                    let mut line = String::new();
                    if reader.by_ref().take(65536).read_line(&mut line)? == 0
                        || !line.ends_with('\n')
                    {
                        return Err(io::Error::other("admission service disconnected"));
                    }
                    let reply: Value = serde_json::from_str(&line)?;
                    match reply["status"].as_str() {
                        Some("queued") => {}
                        Some("granted") => return Ok(()),
                        _ => return Err(io::Error::other("admission service rejected call")),
                    }
                }
            })();
            if let Err(err) = &result {
                tracing::warn!(%err, "remote admission failed; tool remains blocked");
            }
            let _ = tx.send(result);
        });
        if matches!(rx.await, Ok(Ok(()))) {
            let calls = self
                .calls
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            if calls.contains_key(&key) {
                guard.key = None;
                return;
            }
        }
        pending::<()>().await;
    }

    pub(crate) fn release_matching(&self, matches: impl Fn(&CallKey) -> bool) {
        let mut calls = self
            .calls
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        calls.retain(|key, lease| {
            if !matches(key) {
                return true;
            }
            let mut lease = lease
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            lease.closed = true;
            if let Some(stream) = lease.stream.take() {
                let _ = stream.shutdown(Shutdown::Both);
            }
            false
        });
    }
}

#[cfg(test)]
#[path = "remote_tests.rs"]
mod tests;
