use std::fs;
use std::fs::OpenOptions;
use std::future::Future;
use std::future::poll_fn;
use std::path::PathBuf;
use std::sync::Arc;
use std::task::Poll;
use std::time::Duration;
use std::time::Instant;
use std::time::SystemTime;
use std::time::UNIX_EPOCH;

use pretty_assertions::assert_eq;
use serde_json::Value;
use serde_json::json;
use tokio::sync::mpsc;

use super::CallKey;
use super::Scheduler;
use crate::CHAIN_THRESHOLD;
use crate::POINTS_PER_TIER;
use crate::scorer::ChainScorer;
use crate::scorer::LinearScorer;
use crate::scorer::RuleScorer;
use crate::scorer::TaskFeatures;

fn call_key(call_id: &str) -> CallKey {
    CallKey {
        thread_id: "thread".to_string(),
        turn_id: "turn".to_string(),
        call_id: call_id.to_string(),
    }
}

fn task(cwd: &str) -> TaskFeatures {
    TaskFeatures {
        cwd: PathBuf::from(cwd),
        tool_name: "shell".to_string(),
        waiting_since: Instant::now(),
    }
}

fn scheduler(timeout: Duration) -> Arc<Scheduler> {
    let rules = RuleScorer::new(
        vec![(PathBuf::from("/incident"), 0), (PathBuf::from("/oss"), 3)],
        Vec::new(),
    );
    Arc::new(Scheduler::new(Arc::new(rules), 1, timeout))
}

#[tokio::test]
async fn grants_higher_priority_waiter_first() {
    let scheduler = scheduler(Duration::from_secs(5));
    scheduler.admit(call_key("running"), task("/oss/a")).await;

    let (tx, mut rx) = mpsc::unbounded_channel();
    for (call_id, cwd) in [("oss", "/oss/b"), ("incident", "/incident/x")] {
        let scheduler = Arc::clone(&scheduler);
        let tx = tx.clone();
        tokio::spawn(async move {
            scheduler.admit(call_key(call_id), task(cwd)).await;
            tx.send(call_id).ok();
        });
    }
    while scheduler.waiting_len() < 2 {
        tokio::task::yield_now().await;
    }

    scheduler.release(&call_key("running"));
    assert_eq!(rx.recv().await, Some("incident"));
    scheduler.release(&call_key("incident"));
    assert_eq!(rx.recv().await, Some("oss"));
}

#[tokio::test]
async fn linear_chain_grants_higher_score_with_small_gap() {
    let tiers = vec![(PathBuf::from("/incident"), 0), (PathBuf::from("/oss"), 3)];
    let rate = 4.0;
    let linear = LinearScorer::new(tiers, POINTS_PER_TIER, rate);
    let chain = ChainScorer::new(vec![Arc::new(linear)], CHAIN_THRESHOLD);
    let slots = 1;
    let grant_timeout = Duration::from_secs(5);
    let scheduler = Scheduler::new(Arc::new(chain), slots, grant_timeout);
    scheduler.admit(call_key("running"), task("/oss/a")).await;

    let now = Instant::now();
    let mut older = task("/oss/b");
    older.waiting_since = now - Duration::from_secs(7);
    let mut newer = task("/incident/x");
    newer.waiting_since = now;
    let mut oss = Box::pin(scheduler.admit(call_key("oss"), older));
    let mut incident = Box::pin(scheduler.admit(call_key("incident"), newer));
    poll_fn(|cx| {
        assert!(oss.as_mut().poll(cx).is_pending());
        assert!(incident.as_mut().poll(cx).is_pending());
        Poll::Ready(())
    })
    .await;

    scheduler.release(&call_key("running"));
    let first = tokio::select! {
        biased;
        _ = &mut oss => "oss",
        _ = &mut incident => "incident",
    };
    assert_eq!(first, "incident");
    scheduler.release(&call_key("incident"));
    oss.await;
    scheduler.release(&call_key("oss"));
}

#[tokio::test]
async fn fails_open_after_grant_timeout() {
    let scheduler = scheduler(Duration::from_millis(20));
    scheduler.admit(call_key("running"), task("/oss/a")).await;
    scheduler.admit(call_key("late"), task("/oss/b")).await;
    assert_eq!(scheduler.waiting_len(), 0);
}

#[tokio::test]
async fn timeout_trace_records_overflow_and_release_restores_capacity()
-> Result<(), Box<dyn std::error::Error>> {
    let nonce = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
    let process_id = std::process::id();
    let path = std::env::temp_dir().join(format!("scheduler-{process_id}-{nonce}.jsonl"));
    let file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&path)?;
    drop(file);
    let slots = 1;
    let scheduler = Scheduler::new(
        Arc::new(RuleScorer::default()),
        slots,
        Duration::from_millis(20),
    )
    .with_trace(&path);
    scheduler.admit(call_key("running"), task("/oss")).await;
    scheduler.admit(call_key("late"), task("/oss")).await;
    assert_eq!(scheduler.lock().running.len(), 2);
    let mut next = Box::pin(scheduler.admit(call_key("next"), task("/oss")));
    poll_fn(|cx| {
        assert!(next.as_mut().poll(cx).is_pending());
        Poll::Ready(())
    })
    .await;
    scheduler.release(&call_key("late"));
    poll_fn(|cx| {
        assert!(next.as_mut().poll(cx).is_pending());
        Poll::Ready(())
    })
    .await;
    scheduler.release(&call_key("running"));
    poll_fn(|cx| {
        assert_eq!(next.as_mut().poll(cx), Poll::Ready(()));
        Poll::Ready(())
    })
    .await;
    drop(next);
    scheduler.release(&call_key("next"));
    scheduler.release(&call_key("next"));
    assert!(scheduler.lock().running.is_empty());
    drop(scheduler);
    let contents = fs::read_to_string(&path)?;
    fs::remove_file(&path)?;
    let mut records: Vec<Value> = contents
        .lines()
        .map(serde_json::from_str)
        .collect::<Result<_, _>>()?;
    for record in &mut records {
        let fields = record.as_object_mut().unwrap();
        assert!(fields.remove("wait_ms").unwrap().as_u64().is_some());
        let timing_fields: &[&str] = if fields["event"] == "grant_timeout" {
            &["timestamp_ms"]
        } else {
            &["run_ms", "released_at_ms"]
        };
        for name in timing_fields {
            assert!(fields.remove(*name).unwrap().as_u64().is_some());
        }
    }
    let mut expected = vec![json!({
        "event": "grant_timeout",
        "thread_id": "thread",
        "turn_id": "turn",
        "call_id": "late",
        "cwd": PathBuf::from("/oss"),
        "tool": "shell",
        "running_count": 2,
        "max_concurrent": 1,
    })];
    for call_id in ["late", "running", "next"] {
        expected.push(json!({
            "event": "release",
            "thread_id": "thread",
            "turn_id": "turn",
            "call_id": call_id,
            "cwd": PathBuf::from("/oss"),
            "tool": "shell",
        }));
    }
    assert_eq!(records, expected);
    Ok(())
}

#[tokio::test]
async fn dropping_waiting_admission_removes_it_immediately() {
    let scheduler = scheduler(Duration::from_secs(5));
    scheduler.admit(call_key("running"), task("/oss")).await;
    let mut cancelled = Box::pin(scheduler.admit(call_key("cancelled"), task("/oss")));
    poll_fn(|cx| {
        assert!(cancelled.as_mut().poll(cx).is_pending());
        Poll::Ready(())
    })
    .await;
    drop(cancelled);
    assert_eq!(scheduler.waiting_len(), 0);
    scheduler.release(&call_key("running"));
    assert!(scheduler.lock().running.is_empty());
}

#[tokio::test]
async fn dropping_unobserved_grant_releases_slot_for_next_waiter() {
    let scheduler = scheduler(Duration::from_secs(5));
    scheduler.admit(call_key("running"), task("/oss")).await;
    let mut cancelled = Box::pin(scheduler.admit(call_key("cancelled"), task("/incident")));
    let mut next = Box::pin(scheduler.admit(call_key("next"), task("/oss")));
    poll_fn(|cx| {
        assert!(cancelled.as_mut().poll(cx).is_pending());
        assert!(next.as_mut().poll(cx).is_pending());
        Poll::Ready(())
    })
    .await;
    scheduler.release(&call_key("running"));
    drop(cancelled);
    poll_fn(|cx| {
        assert_eq!(next.as_mut().poll(cx), Poll::Ready(()));
        Poll::Ready(())
    })
    .await;
    scheduler.release(&call_key("cancelled"));
    assert!(scheduler.lock().running.contains_key(&call_key("next")));
    scheduler.release(&call_key("next"));
    assert!(scheduler.lock().running.is_empty());
}

#[tokio::test]
async fn release_before_admission_resumes_never_grants_cancelled_call() {
    for release_running_first in [false, true] {
        let scheduler = scheduler(Duration::from_millis(20));
        scheduler.admit(call_key("running"), task("/oss")).await;
        let mut cancelled = Box::pin(scheduler.admit(call_key("cancelled"), task("/oss")));
        poll_fn(|cx| {
            assert!(cancelled.as_mut().poll(cx).is_pending());
            Poll::Ready(())
        })
        .await;
        if release_running_first {
            scheduler.release(&call_key("running"));
        }
        scheduler.release(&call_key("cancelled"));
        assert_eq!(scheduler.waiting_len(), 0);
        assert!(
            tokio::time::timeout(Duration::from_millis(40), &mut cancelled)
                .await
                .is_err()
        );
        drop(cancelled);
        scheduler.release(&call_key("running"));
        assert!(scheduler.lock().running.is_empty());
    }
}

#[tokio::test]
async fn reused_call_ids_keep_thread_and_turn_slots_separate() {
    for (thread_id, turn_id) in [("other-thread", "turn"), ("thread", "other-turn")] {
        let slots = 2;
        let grant_timeout = Duration::from_secs(5);
        let scheduler = Scheduler::new(Arc::new(RuleScorer::default()), slots, grant_timeout);
        let first = call_key("shared");
        let second = CallKey {
            thread_id: thread_id.to_string(),
            turn_id: turn_id.to_string(),
            call_id: first.call_id.clone(),
        };
        scheduler.admit(first.clone(), task("/oss")).await;
        scheduler.admit(second.clone(), task("/oss")).await;
        let mut next = Box::pin(scheduler.admit(call_key("next"), task("/oss")));
        poll_fn(|cx| {
            assert!(next.as_mut().poll(cx).is_pending());
            Poll::Ready(())
        })
        .await;

        let unstarted = CallKey {
            thread_id: "unstarted-thread".to_string(),
            ..first.clone()
        };
        scheduler.release(&unstarted);
        poll_fn(|cx| {
            assert!(next.as_mut().poll(cx).is_pending());
            Poll::Ready(())
        })
        .await;

        scheduler.release(&first);
        poll_fn(|cx| {
            assert!(next.as_mut().poll(cx).is_ready());
            Poll::Ready(())
        })
        .await;
        let mut last = Box::pin(scheduler.admit(call_key("last"), task("/oss")));
        scheduler.release(&first);
        poll_fn(|cx| {
            assert!(last.as_mut().poll(cx).is_pending());
            Poll::Ready(())
        })
        .await;
        scheduler.release(&second);
        poll_fn(|cx| {
            assert!(last.as_mut().poll(cx).is_ready());
            Poll::Ready(())
        })
        .await;
        scheduler.release(&call_key("next"));
        scheduler.release(&call_key("last"));
    }
}

#[tokio::test]
async fn lost_waiter_fails_open_instead_of_hanging() {
    let scheduler = scheduler(Duration::from_millis(20));
    scheduler.admit(call_key("running"), task("/oss")).await;
    let mut lost = Box::pin(scheduler.admit(call_key("lost"), task("/incident")));
    poll_fn(|cx| {
        assert!(lost.as_mut().poll(cx).is_pending());
        Poll::Ready(())
    })
    .await;
    scheduler.lock().waiting.clear();
    tokio::time::timeout(Duration::from_millis(200), &mut lost)
        .await
        .expect("lost waiter must fail open");
    assert!(scheduler.lock().running.contains_key(&call_key("lost")));
    scheduler.release(&call_key("lost"));
    scheduler.release(&call_key("running"));
    assert!(scheduler.lock().running.is_empty());
}
