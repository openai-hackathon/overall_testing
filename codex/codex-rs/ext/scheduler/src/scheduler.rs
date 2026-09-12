use std::collections::HashMap;
use std::fs::File;
use std::fs::OpenOptions;
use std::future::pending;
use std::io::Write;
use std::path::Path;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::Mutex;
use std::time::Duration;
use std::time::Instant;
use std::time::SystemTime;
use std::time::UNIX_EPOCH;

use serde_json::json;
use tokio::sync::oneshot;
use tracing::info;
use tracing::warn;

use crate::scorer::Scorer;
use crate::scorer::TaskFeatures;

#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct CallKey {
    pub thread_id: String,
    pub turn_id: String,
    pub call_id: String,
}

pub struct Scheduler {
    remote: Option<crate::remote::RemoteScheduler>,
    scorer: Arc<dyn Scorer>,
    max_concurrent: usize,
    grant_timeout: Duration,
    state: Mutex<State>,
    trace: Option<Mutex<File>>,
}

#[derive(Default)]
struct State {
    running: HashMap<CallKey, Running>,
    waiting: Vec<Waiter>,
}

struct Running {
    features: TaskFeatures,
    granted_at: Instant,
}

#[derive(Debug)]
enum Grant {
    Granted,
    Cancelled,
}

struct Waiter {
    key: CallKey,
    features: TaskFeatures,
    grant: oneshot::Sender<Grant>,
}

struct AdmissionGuard<'a> {
    scheduler: &'a Scheduler,
    key: &'a CallKey,
    armed: bool,
}

impl Drop for AdmissionGuard<'_> {
    fn drop(&mut self) {
        if self.armed {
            self.scheduler.release(self.key);
        }
    }
}

impl Scheduler {
    pub fn new(scorer: Arc<dyn Scorer>, max_concurrent: usize, grant_timeout: Duration) -> Self {
        Self {
            remote: None,
            scorer,
            max_concurrent: max_concurrent.max(1),
            grant_timeout,
            state: Mutex::new(State::default()),
            trace: None,
        }
    }

    pub(crate) fn with_remote(mut self, address: std::net::SocketAddr) -> Self {
        self.remote = Some(crate::remote::RemoteScheduler::new(address));
        self
    }

    pub fn with_trace(mut self, path: &Path) -> Self {
        match OpenOptions::new().create(true).append(true).open(path) {
            Ok(file) => self.trace = Some(Mutex::new(file)),
            Err(err) => warn!(path = %path.display(), %err, "scheduler trace file unavailable"),
        }
        self
    }

    pub async fn admit(&self, key: CallKey, features: TaskFeatures) {
        if let Some(remote) = &self.remote {
            remote.admit(key, features).await;
            return;
        }
        let grant = {
            let mut state = self.lock();
            if state.running.len() < self.max_concurrent {
                info!(?key, tool = features.tool_name, cwd = %features.cwd.display(), "scheduler admitted");
                state.start(key, features);
                return;
            }
            let (tx, rx) = oneshot::channel();
            info!(?key, tool = features.tool_name, cwd = %features.cwd.display(), "scheduler waiting");
            state.waiting.push(Waiter {
                key: key.clone(),
                features,
                grant: tx,
            });
            rx
        };
        let mut guard = AdmissionGuard {
            scheduler: self,
            key: &key,
            armed: true,
        };
        let outcome = tokio::time::timeout(self.grant_timeout, grant).await;
        let reason = {
            let mut state = self.lock();
            if state.running.contains_key(&key) {
                guard.armed = false;
                return;
            }
            match outcome {
                Ok(Ok(Grant::Cancelled)) => "cancelled while waiting",
                Ok(Ok(Grant::Granted)) => "released before admission resumed",
                Ok(Err(_)) | Err(_) => {
                    self.fail_open(&mut state, &key);
                    guard.armed = false;
                    return;
                }
            }
        };
        info!(?key, reason, "scheduler call will not run");
        guard.armed = false;
        pending::<()>().await;
    }

    fn fail_open(&self, state: &mut State, key: &CallKey) {
        let (features, reason) = match state.waiting.iter().position(|waiter| waiter.key == *key) {
            Some(index) => (state.waiting.swap_remove(index).features, "grant_timeout"),
            None => (
                TaskFeatures {
                    cwd: PathBuf::new(),
                    tool_name: String::new(),
                    waiting_since: Instant::now(),
                },
                "waiter_lost",
            ),
        };
        warn!(
            ?key,
            reason, "scheduler could not grant, running tool call anyway"
        );
        self.write_trace(json!({
            "event": reason,
            "thread_id": key.thread_id,
            "turn_id": key.turn_id,
            "call_id": key.call_id,
            "cwd": features.cwd,
            "tool": features.tool_name,
            "wait_ms": features.waiting_since.elapsed().as_millis(),
            "running_count": state.running.len() + 1,
            "max_concurrent": self.max_concurrent,
            "timestamp_ms": SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_millis(),
        }));
        state.start(key.clone(), features);
    }

    pub fn release(&self, key: &CallKey) {
        self.release_matching(|candidate| candidate == key, "release");
    }

    pub(crate) fn release_thread(&self, thread_id: &str) {
        self.release_matching(|key| key.thread_id == thread_id, "cancelled");
    }

    fn release_matching(&self, matches: impl Fn(&CallKey) -> bool, event: &str) {
        if let Some(remote) = &self.remote {
            remote.release_matching(matches);
            return;
        }
        let mut state = self.lock();
        let mut index = 0;
        while index < state.waiting.len() {
            if matches(&state.waiting[index].key) {
                let waiter = state.waiting.swap_remove(index);
                let _ = waiter.grant.send(Grant::Cancelled);
            } else {
                index += 1;
            }
        }
        state.running.retain(|key, running| {
            if !matches(key) {
                return true;
            }
            let held = running.granted_at.elapsed();
            info!(
                ?key,
                tool = running.features.tool_name,
                held_ms = held.as_millis(),
                event,
                "scheduler released"
            );
            self.record(key, running, held, event);
            false
        });
        while state.running.len() < self.max_concurrent {
            let Some(index) = self.pick(&state.waiting) else {
                break;
            };
            let waiter = state.waiting.swap_remove(index);
            if waiter.grant.send(Grant::Granted).is_ok() {
                info!(key = ?waiter.key, tool = waiter.features.tool_name, cwd = %waiter.features.cwd.display(), "scheduler granted");
                state.start(waiter.key, waiter.features);
            }
        }
    }

    pub fn waiting_len(&self) -> usize {
        self.lock().waiting.len()
    }

    fn record(&self, key: &CallKey, running: &Running, held: Duration, event: &str) {
        let line = json!({
            "event": event,
            "thread_id": key.thread_id,
            "turn_id": key.turn_id,
            "call_id": key.call_id,
            "cwd": running.features.cwd,
            "tool": running.features.tool_name,
            "wait_ms": (running.granted_at - running.features.waiting_since).as_millis(),
            "run_ms": held.as_millis(),
            "released_at_ms": SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_millis(),
        });
        self.write_trace(line);
    }

    fn write_trace(&self, line: serde_json::Value) {
        let Some(trace) = &self.trace else {
            return;
        };
        let mut file = trace
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if let Err(err) = writeln!(file, "{line}") {
            warn!(%err, "scheduler trace write failed");
        }
    }

    fn pick(&self, waiting: &[Waiter]) -> Option<usize> {
        self.scorer.refresh();
        let mut best = 0;
        for index in 1..waiting.len() {
            if self.prefers(&waiting[index].features, &waiting[best].features) {
                best = index;
            }
        }
        (!waiting.is_empty()).then_some(best)
    }

    fn prefers(&self, first: &TaskFeatures, second: &TaskFeatures) -> bool {
        let preference = self.scorer.score(first, second);
        if preference.confidence > 0.0 {
            preference.first_wins()
        } else {
            first.waiting_since < second.waiting_since
        }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, State> {
        self.state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }
}

impl State {
    fn start(&mut self, key: CallKey, features: TaskFeatures) {
        self.running.insert(
            key,
            Running {
                features,
                granted_at: Instant::now(),
            },
        );
    }
}

#[cfg(test)]
#[path = "scheduler_tests.rs"]
mod tests;
