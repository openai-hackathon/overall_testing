use std::io::BufRead;
use std::io::BufReader;
use std::io::Write;
use std::net::TcpListener;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use std::time::Instant;

use pretty_assertions::assert_eq;
use serde_json::Value;
use tokio::sync::oneshot;

use super::RemoteScheduler;
use crate::CallKey;
use crate::RuleScorer;
use crate::Scheduler;
use crate::TaskFeatures;

#[tokio::test]
async fn remote_waits_for_grant_and_closes_lease_on_release() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let slots = 1;
    let remote = Arc::new(
        Scheduler::new(
            Arc::new(RuleScorer::default()),
            slots,
            Duration::from_secs(5),
        )
        .with_remote(listener.local_addr().unwrap()),
    );
    let (received_tx, received_rx) = oneshot::channel();
    let (grant_tx, grant_rx) = std::sync::mpsc::channel();
    let worker = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        let request: Value = serde_json::from_str(&line).unwrap();
        assert_eq!(request["op"], "enqueue");
        assert_eq!(request["call_id"], "call");
        stream.write_all(b"{\"status\":\"queued\"}\n").unwrap();
        received_tx.send(()).unwrap();
        grant_rx.recv_timeout(Duration::from_secs(5)).unwrap();
        stream.write_all(b"{\"status\":\"granted\"}\n").unwrap();
        line.clear();
        assert_eq!(reader.read_line(&mut line).unwrap(), 0);
    });
    let key = CallKey {
        thread_id: "thread".into(),
        turn_id: "turn".into(),
        call_id: "call".into(),
    };
    let client = remote.clone();
    let task_key = key.clone();
    let admission = tokio::spawn(async move {
        client
            .admit(
                task_key,
                TaskFeatures {
                    cwd: PathBuf::from("/repo"),
                    tool_name: "shell".into(),
                    waiting_since: Instant::now(),
                },
            )
            .await;
    });
    received_rx.await.unwrap();
    assert!(!admission.is_finished());
    grant_tx.send(()).unwrap();
    tokio::time::timeout(Duration::from_secs(5), admission)
        .await
        .unwrap()
        .unwrap();
    remote.release(&key);
    worker.join().unwrap();
}

#[tokio::test]
async fn cancelling_remote_admission_closes_waiting_connection() {
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let remote = Arc::new(RemoteScheduler::new(listener.local_addr().unwrap()));
    let (received_tx, received_rx) = oneshot::channel();
    let worker = std::thread::spawn(move || {
        let (mut stream, _) = listener.accept().unwrap();
        stream
            .set_read_timeout(Some(Duration::from_secs(5)))
            .unwrap();
        let mut reader = BufReader::new(stream.try_clone().unwrap());
        let mut line = String::new();
        reader.read_line(&mut line).unwrap();
        stream.write_all(b"{\"status\":\"queued\"}\n").unwrap();
        received_tx.send(()).unwrap();
        line.clear();
        assert_eq!(reader.read_line(&mut line).unwrap(), 0);
    });
    let client = remote.clone();
    let admission = tokio::spawn(async move {
        client
            .admit(
                CallKey {
                    thread_id: "thread".into(),
                    turn_id: "turn".into(),
                    call_id: "call".into(),
                },
                TaskFeatures {
                    cwd: PathBuf::from("/repo"),
                    tool_name: "shell".into(),
                    waiting_since: Instant::now(),
                },
            )
            .await;
    });
    received_rx.await.unwrap();
    admission.abort();
    assert!(admission.await.unwrap_err().is_cancelled());
    worker.join().unwrap();
    assert!(remote.calls.lock().unwrap().is_empty());
}
