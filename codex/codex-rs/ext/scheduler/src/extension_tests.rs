use std::future::Future;
use std::future::poll_fn;
use std::path::PathBuf;
use std::sync::Arc;
use std::task::Poll;
use std::time::Instant;

use codex_extension_api::ConversationHistorySnapshot;
use codex_extension_api::ExtensionData;
use codex_extension_api::ResponseItem;
use codex_extension_api::ThreadLifecycleContributor;
use codex_extension_api::ThreadStopInput;
use codex_extension_api::ToolCallOutcome;
use codex_extension_api::ToolCallSource;
use codex_extension_api::ToolFinishInput;
use codex_extension_api::ToolLifecycleContributor;
use codex_extension_api::ToolName;
use codex_extension_api::ToolPayload;
use codex_extension_api::ToolStartInput;
use pretty_assertions::assert_eq;

use super::CallKey;
use super::GRANT_TIMEOUT;
use super::PUBLIC_TOOL_NAME;
use super::RuleScorer;
use super::Scheduler;
use super::SchedulerExtension;
use super::TaskFeatures;
use super::WAIT_TOOL_NAME;

struct EmptyHistory;

fn features() -> TaskFeatures {
    TaskFeatures {
        cwd: PathBuf::new(),
        tool_name: "shell".to_string(),
        waiting_since: Instant::now(),
    }
}

fn key(thread_id: &str, call_id: &str) -> CallKey {
    CallKey {
        thread_id: thread_id.to_string(),
        turn_id: "turn".to_string(),
        call_id: call_id.to_string(),
    }
}

impl ConversationHistorySnapshot for EmptyHistory {
    fn history_version(&self) -> u64 {
        0
    }

    fn user_message_revision(&self) -> u64 {
        0
    }

    fn items(&self) -> Box<dyn Iterator<Item = &ResponseItem> + Send + '_> {
        Box::new(std::iter::empty())
    }
}

#[tokio::test]
async fn only_default_namespace_code_mode_wrappers_bypass_queue() {
    for name in [PUBLIC_TOOL_NAME, WAIT_TOOL_NAME] {
        for (tool_name, bypasses_queue) in [
            (ToolName::namespaced("mcp__example", name), false),
            (ToolName::plain(name), true),
            (ToolName::namespaced("functions", name), true),
            (ToolName::namespaced("", name), true),
        ] {
            let slots = 1;
            let scheduler = Arc::new(Scheduler::new(
                Arc::new(RuleScorer::new(Vec::new(), Vec::new())),
                slots,
                GRANT_TIMEOUT,
            ));
            let running = CallKey {
                thread_id: "thread".to_string(),
                turn_id: "turn".to_string(),
                call_id: "running".to_string(),
            };
            scheduler
                .admit(
                    running.clone(),
                    TaskFeatures {
                        cwd: PathBuf::new(),
                        tool_name: "shell".to_string(),
                        waiting_since: Instant::now(),
                    },
                )
                .await;
            let extension = SchedulerExtension {
                scheduler: scheduler.clone(),
                cwd_of: Box::new(|_: &()| PathBuf::new()),
            };
            let session_store = ExtensionData::new("session");
            let thread_store = ExtensionData::new("thread");
            let turn_store = ExtensionData::new("turn");
            let payload = ToolPayload::Function {
                arguments: "{}".to_string(),
            };
            let mut admission = extension.on_tool_start(ToolStartInput {
                session_store: &session_store,
                thread_store: &thread_store,
                turn_store: &turn_store,
                turn_id: "turn",
                root_turn_id: None,
                call_id: "candidate",
                originating_item_id: None,
                tool_name: &tool_name,
                mcp_tool: None,
                payload: &payload,
                conversation_history: Arc::new(EmptyHistory),
                source: ToolCallSource::Direct,
            });
            poll_fn(|cx| {
                assert_eq!(
                    admission.as_mut().poll(cx).is_ready(),
                    bypasses_queue,
                    "{tool_name:?}"
                );
                Poll::Ready(())
            })
            .await;
            scheduler.release(&running);
            if !bypasses_queue {
                poll_fn(|cx| {
                    assert_eq!(admission.as_mut().poll(cx), Poll::Ready(()));
                    Poll::Ready(())
                })
                .await;
            }
        }
    }
}

#[tokio::test]
async fn thread_stop_removes_its_calls_and_preserves_other_threads() {
    let slots = 2;
    let scheduler = Arc::new(Scheduler::new(
        Arc::new(RuleScorer::default()),
        slots,
        GRANT_TIMEOUT,
    ));
    let extension = SchedulerExtension {
        scheduler: scheduler.clone(),
        cwd_of: Box::new(|_: &()| PathBuf::new()),
    };
    let session_store = ExtensionData::new("session");
    let thread_store = ExtensionData::new("stopped");
    scheduler.admit(key("stopped", "running"), features()).await;
    scheduler.admit(key("other", "running"), features()).await;
    let mut cancelled = Box::pin(scheduler.admit(key("stopped", "waiting"), features()));
    let mut next = Box::pin(scheduler.admit(key("other", "waiting"), features()));
    poll_fn(|cx| {
        assert!(cancelled.as_mut().poll(cx).is_pending());
        assert!(next.as_mut().poll(cx).is_pending());
        Poll::Ready(())
    })
    .await;
    extension
        .on_thread_stop(ThreadStopInput {
            session_store: &session_store,
            thread_store: &thread_store,
        })
        .await;
    poll_fn(|cx| {
        assert!(cancelled.as_mut().poll(cx).is_pending());
        assert_eq!(next.as_mut().poll(cx), Poll::Ready(()));
        Poll::Ready(())
    })
    .await;
    drop(cancelled);
    extension
        .on_thread_stop(ThreadStopInput {
            session_store: &session_store,
            thread_store: &thread_store,
        })
        .await;
    let mut last = Box::pin(scheduler.admit(key("other", "last"), features()));
    poll_fn(|cx| {
        assert!(last.as_mut().poll(cx).is_pending());
        Poll::Ready(())
    })
    .await;
    scheduler.release(&key("other", "running"));
    poll_fn(|cx| {
        assert_eq!(last.as_mut().poll(cx), Poll::Ready(()));
        Poll::Ready(())
    })
    .await;
    scheduler.release(&key("other", "waiting"));
    scheduler.release(&key("other", "last"));
    assert_eq!(scheduler.waiting_len(), 0);
}

#[tokio::test]
async fn terminal_outcomes_release_once_without_affecting_other_calls() {
    for outcome in [
        ToolCallOutcome::Completed { success: true },
        ToolCallOutcome::Completed { success: false },
        ToolCallOutcome::Blocked,
        ToolCallOutcome::Failed {
            handler_executed: false,
        },
        ToolCallOutcome::Failed {
            handler_executed: true,
        },
        ToolCallOutcome::Aborted,
    ] {
        let slots = 1;
        let scheduler = Arc::new(Scheduler::new(
            Arc::new(RuleScorer::default()),
            slots,
            GRANT_TIMEOUT,
        ));
        let extension = SchedulerExtension {
            scheduler: scheduler.clone(),
            cwd_of: Box::new(|_: &()| PathBuf::new()),
        };
        let session_store = ExtensionData::new("session");
        let thread_store = ExtensionData::new("thread");
        let turn_store = ExtensionData::new("turn");
        let tool_name = ToolName::plain("shell");
        scheduler.admit(key("thread", "running"), features()).await;
        let mut next = Box::pin(scheduler.admit(key("other", "next"), features()));
        poll_fn(|cx| {
            assert!(next.as_mut().poll(cx).is_pending());
            Poll::Ready(())
        })
        .await;
        for call_id in ["never-started", "running", "running"] {
            extension
                .on_tool_finish(ToolFinishInput {
                    session_store: &session_store,
                    thread_store: &thread_store,
                    turn_store: &turn_store,
                    turn_id: "turn",
                    call_id,
                    tool_name: &tool_name,
                    source: ToolCallSource::Direct,
                    outcome,
                })
                .await;
            if call_id == "never-started" {
                poll_fn(|cx| {
                    assert!(next.as_mut().poll(cx).is_pending());
                    Poll::Ready(())
                })
                .await;
            }
        }
        poll_fn(|cx| {
            assert_eq!(next.as_mut().poll(cx), Poll::Ready(()));
            Poll::Ready(())
        })
        .await;
        let mut last = Box::pin(scheduler.admit(key("other", "last"), features()));
        poll_fn(|cx| {
            assert!(last.as_mut().poll(cx).is_pending());
            Poll::Ready(())
        })
        .await;
        scheduler.release(&key("other", "next"));
        poll_fn(|cx| {
            assert_eq!(last.as_mut().poll(cx), Poll::Ready(()));
            Poll::Ready(())
        })
        .await;
        scheduler.release(&key("other", "last"));
    }
}
