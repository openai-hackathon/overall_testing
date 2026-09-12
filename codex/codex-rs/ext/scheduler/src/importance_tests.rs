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
use serde_json::json;

use super::ImportanceScorer;
use crate::CallKey;
use crate::Preference;
use crate::Scheduler;
use crate::Scorer;
use crate::TaskFeatures;

struct SnapshotFile(PathBuf);

impl SnapshotFile {
    fn new() -> Self {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let process_id = std::process::id();
        let path = std::env::temp_dir().join(format!("importance-{process_id}-{nonce}.json"));
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .unwrap();
        Self(path)
    }

    fn write(&self, version: u64, a_score: f64, b_score: f64) {
        fs::write(
            &self.0,
            json!({
                "fit_version": version,
                "scores": {"a": a_score, "b": b_score},
                "bindings": [
                    {"cwd": std::env::temp_dir().join("repo-a"), "key": "a"},
                    {"cwd": std::env::temp_dir().join("repo-b"), "key": "b"}
                ]
            })
            .to_string(),
        )
        .unwrap();
    }
}

impl Drop for SnapshotFile {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.0);
    }
}

fn task(repo: &str) -> TaskFeatures {
    TaskFeatures {
        cwd: std::env::temp_dir().join(repo),
        tool_name: "shell".to_string(),
        waiting_since: Instant::now(),
    }
}

fn key(call: &str) -> CallKey {
    CallKey {
        thread_id: call.to_string(),
        turn_id: "turn".to_string(),
        call_id: call.to_string(),
    }
}

#[tokio::test]
async fn refreshed_importance_changes_waiting_order() {
    let file = SnapshotFile::new();
    file.write(1, 80.0, 20.0);
    let rate = 0.0;
    let scorer = Arc::new(ImportanceScorer::new(file.0.clone(), rate).unwrap());
    scorer.refresh();
    let a = task("repo-a");
    let b = task("repo-b");
    assert_eq!(scorer.score(&a, &b), Preference::certain(true));
    let slots = 1;
    let scheduler = Scheduler::new(scorer, slots, Duration::from_secs(5));
    scheduler.admit(key("running"), task("other")).await;
    let mut first = Box::pin(scheduler.admit(key("a"), a));
    let mut second = Box::pin(scheduler.admit(key("b"), b));
    poll_fn(|cx| {
        assert!(first.as_mut().poll(cx).is_pending());
        assert!(second.as_mut().poll(cx).is_pending());
        Poll::Ready(())
    })
    .await;
    file.write(2, 20.0, 80.0);
    scheduler.release(&key("running"));
    poll_fn(|cx| {
        assert!(first.as_mut().poll(cx).is_pending());
        assert_eq!(second.as_mut().poll(cx), Poll::Ready(()));
        Poll::Ready(())
    })
    .await;
    scheduler.release(&key("b"));
    first.await;
    scheduler.release(&key("a"));
}

#[test]
fn reload_preserves_waiting_age_and_last_valid_scores() {
    let file = SnapshotFile::new();
    let rate = 4.0;
    let scorer = ImportanceScorer::new(file.0.clone(), rate).unwrap();
    let mut old = task("repo-a");
    old.waiting_since = Instant::now() - Duration::from_secs(20);
    let fresh = task("repo-b");
    scorer.refresh();
    assert_eq!(scorer.score(&old, &fresh), Preference::certain(true));
    file.write(2, 20.0, 80.0);
    scorer.refresh();
    assert_eq!(scorer.score(&old, &fresh), Preference::certain(true));
    let a = task("repo-a/subdir");
    assert_eq!(scorer.score(&a, &fresh), Preference::certain(false));
    for contents in ["broken", "{}"] {
        fs::write(&file.0, contents).unwrap();
        scorer.refresh();
        assert_eq!(scorer.score(&a, &fresh), Preference::certain(false));
    }
    file.write(3, 101.0, 0.0);
    scorer.refresh();
    assert_eq!(scorer.score(&a, &fresh), Preference::certain(false));
    file.write(1, 100.0, 0.0);
    scorer.refresh();
    assert_eq!(scorer.score(&a, &fresh), Preference::certain(false));
}

#[test]
fn invalid_rate_is_rejected() {
    for rate in [-1.0, f64::NAN, f64::INFINITY] {
        assert!(ImportanceScorer::new(PathBuf::new(), rate).is_err());
    }
}

#[cfg(unix)]
#[test]
fn symlink_task_uses_canonical_binding() {
    let file = SnapshotFile::new();
    let real = file.0.with_extension("directory");
    let alias = file.0.with_extension("link");
    fs::create_dir(&real).unwrap();
    std::os::unix::fs::symlink(&real, &alias).unwrap();
    fs::write(&file.0, json!({"fit_version": 1, "scores": {"a": 90.0}, "bindings": [{"cwd": real.canonicalize().unwrap(), "key": "a"}]}).to_string()).unwrap();
    let rate = 0.0;
    let scorer = ImportanceScorer::new(file.0.clone(), rate).unwrap();
    scorer.refresh();
    let first = TaskFeatures {
        cwd: alias.clone(),
        tool_name: "shell".into(),
        waiting_since: Instant::now(),
    };
    let second = task("unbound");
    assert_eq!(scorer.score(&first, &second), Preference::certain(true));
    fs::write(&file.0, json!({"fit_version": 2, "scores": {"a": 0.0}, "bindings": [{"cwd": real.canonicalize().unwrap(), "key": "a"}, {"cwd": alias, "key": "a"}]}).to_string()).unwrap();
    scorer.refresh();
    let actual = scorer.score(&first, &second);
    fs::remove_file(alias).unwrap();
    fs::remove_dir(real).unwrap();
    assert_eq!(actual, Preference::certain(true));
}
