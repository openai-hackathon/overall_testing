use std::path::Path;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::OnceLock;
use std::time::Duration;
use std::time::Instant;

use codex_code_mode_protocol::PUBLIC_TOOL_NAME;
use codex_code_mode_protocol::WAIT_TOOL_NAME;
use codex_extension_api::ExtensionFuture;
use codex_extension_api::ExtensionRegistryBuilder;
use codex_extension_api::ThreadLifecycleContributor;
use codex_extension_api::ThreadStartInput;
use codex_extension_api::ThreadStopInput;
use codex_extension_api::ToolFinishInput;
use codex_extension_api::ToolLifecycleContributor;
use codex_extension_api::ToolLifecycleFuture;
use codex_extension_api::ToolStartInput;

mod importance;
mod remote;
mod scheduler;
mod scorer;

#[cfg(test)]
#[path = "extension_tests.rs"]
mod tests;

pub use scheduler::CallKey;
pub use scheduler::Scheduler;
pub use scorer::AgingScorer;
pub use scorer::ChainScorer;
pub use scorer::LinearScorer;
pub use scorer::PairTableScorer;
pub use scorer::Preference;
pub use scorer::RuleScorer;
pub use scorer::Scorer;
pub use scorer::TaskFeatures;

pub const TIERS_ENV: &str = "CODEX_SCHEDULER_TIERS";
pub const SLOTS_ENV: &str = "CODEX_SCHEDULER_SLOTS";
pub const TRACE_ENV: &str = "CODEX_SCHEDULER_TRACE";
pub const POLICY_ENV: &str = "CODEX_SCHEDULER_POLICY";
pub const AGING_MS_ENV: &str = "CODEX_SCHEDULER_AGING_MS";
pub const SERVICE_ENV: &str = "CODEX_SCHEDULER_SERVICE";
pub const IMPORTANCE_ENV: &str = "CODEX_SCHEDULER_IMPORTANCE";
pub const RATE_ENV: &str = "CODEX_SCHEDULER_RATE";
const GRANT_TIMEOUT: Duration = Duration::from_secs(300);
const CHAIN_THRESHOLD: f32 = 0.5;
const POINTS_PER_TIER: f32 = 10.0;
const CHEAP_TOOLS: [&str; 4] = ["read_file", "list_dir", "grep_files", "tool_search"];

struct ThreadTag {
    cwd: PathBuf,
}

struct SchedulerExtension<C> {
    scheduler: Arc<Scheduler>,
    cwd_of: Box<dyn Fn(&C) -> PathBuf + Send + Sync>,
}

impl<C: Sync> ThreadLifecycleContributor<C> for SchedulerExtension<C> {
    fn on_thread_start<'a>(&'a self, input: ThreadStartInput<'a, C>) -> ExtensionFuture<'a, ()> {
        input.thread_store.insert(ThreadTag {
            cwd: (self.cwd_of)(input.config),
        });
        Box::pin(std::future::ready(()))
    }

    fn on_thread_stop<'a>(&'a self, input: ThreadStopInput<'a>) -> ExtensionFuture<'a, ()> {
        self.scheduler.release_thread(input.thread_store.level_id());
        Box::pin(std::future::ready(()))
    }
}

impl<C: Send + Sync> ToolLifecycleContributor for SchedulerExtension<C> {
    fn on_tool_start<'a>(&'a self, input: ToolStartInput<'a>) -> ToolLifecycleFuture<'a> {
        if input.tool_name.is_default_namespace()
            && [PUBLIC_TOOL_NAME, WAIT_TOOL_NAME].contains(&input.tool_name.name.as_str())
        {
            return Box::pin(std::future::ready(()));
        }
        let features = TaskFeatures {
            cwd: input
                .thread_store
                .get::<ThreadTag>()
                .map(|tag| tag.cwd.clone())
                .unwrap_or_default(),
            tool_name: input.tool_name.to_string(),
            waiting_since: Instant::now(),
        };
        let key = CallKey {
            thread_id: input.thread_store.level_id().to_string(),
            turn_id: input.turn_id.to_string(),
            call_id: input.call_id.to_string(),
        };
        Box::pin(self.scheduler.admit(key, features))
    }

    fn on_tool_finish<'a>(&'a self, input: ToolFinishInput<'a>) -> ToolLifecycleFuture<'a> {
        self.scheduler.release(&CallKey {
            thread_id: input.thread_store.level_id().to_string(),
            turn_id: input.turn_id.to_string(),
            call_id: input.call_id.to_string(),
        });
        Box::pin(std::future::ready(()))
    }
}

pub fn install<C>(
    registry: &mut ExtensionRegistryBuilder<C>,
    scheduler: Arc<Scheduler>,
    cwd_of: impl Fn(&C) -> PathBuf + Send + Sync + 'static,
) where
    C: Send + Sync + 'static,
{
    let extension = Arc::new(SchedulerExtension {
        scheduler,
        cwd_of: Box::new(cwd_of),
    });
    registry.thread_lifecycle_contributor(extension.clone());
    registry.tool_lifecycle_contributor(extension);
}

pub fn shared_from_env() -> Option<Arc<Scheduler>> {
    static SHARED: OnceLock<Option<Arc<Scheduler>>> = OnceLock::new();
    SHARED
        .get_or_init(|| {
            let service = std::env::var(SERVICE_ENV).ok();
            let snapshot_path = std::env::var_os(IMPORTANCE_ENV);
            let tier_spec = std::env::var(TIERS_ENV).ok();
            if service.is_none() && snapshot_path.is_none() && tier_spec.is_none() {
                return None;
            }
            let tiers = parse_tiers(tier_spec.as_deref().unwrap_or_default());
            let slots = std::env::var(SLOTS_ENV)
                .ok()
                .and_then(|value| value.parse().ok())
                .unwrap_or(1);
            let rules = RuleScorer::new(
                tiers.clone(),
                CHEAP_TOOLS.iter().map(|tool| (*tool).to_string()).collect(),
            );
            let learned: Vec<Arc<dyn Scorer>> =
                vec![Arc::new(rules), Arc::new(PairTableScorer::default())];
            let scorers: Vec<Arc<dyn Scorer>> = match std::env::var(POLICY_ENV).as_deref() {
                Ok("fifo") => Vec::new(),
                Ok("linear") => {
                    let rate = std::env::var(RATE_ENV)
                        .ok()
                        .and_then(|value| value.parse().ok())
                        .unwrap_or(1.0);
                    vec![Arc::new(LinearScorer::new(tiers, POINTS_PER_TIER, rate))]
                }
                Ok("aging") => {
                    let max_wait = std::env::var(AGING_MS_ENV)
                        .ok()
                        .and_then(|value| value.parse().ok())
                        .map_or(Duration::from_secs(4), Duration::from_millis);
                    let mut scorers: Vec<Arc<dyn Scorer>> =
                        vec![Arc::new(AgingScorer::new(max_wait))];
                    scorers.extend(learned);
                    scorers
                }
                _ => learned,
            };
            let scorer: Arc<dyn Scorer> = if let Some(path) = snapshot_path {
                let rate = match std::env::var(RATE_ENV) {
                    Ok(value) => value
                        .parse::<f64>()
                        .ok()
                        .filter(|rate| rate.is_finite() && *rate >= 0.0)
                        .unwrap_or_else(|| {
                            tracing::warn!("invalid scheduler rate; using 1 point per second");
                            1.0
                        }),
                    Err(_) => 1.0,
                };
                match importance::ImportanceScorer::new(PathBuf::from(path), rate) {
                    Ok(scorer) => Arc::new(scorer),
                    Err(err) => {
                        tracing::warn!(%err, "invalid importance scheduler configuration");
                        return None;
                    }
                }
            } else {
                Arc::new(ChainScorer::new(scorers, CHAIN_THRESHOLD))
            };
            let mut scheduler = Scheduler::new(scorer, slots, GRANT_TIMEOUT);
            if let Some(service) = service {
                let address = match service.parse::<std::net::SocketAddr>() {
                    Ok(address) if address.ip().is_loopback() => address,
                    Ok(_) | Err(_) => {
                        tracing::warn!(
                            "CODEX_SCHEDULER_SERVICE must be a loopback IP:port; scheduler disabled"
                        );
                        return None;
                    }
                };
                scheduler = scheduler.with_remote(address);
            }
            if let Some(path) = std::env::var_os(TRACE_ENV) {
                scheduler = scheduler.with_trace(Path::new(&path));
            }
            Some(Arc::new(scheduler))
        })
        .clone()
}

fn parse_tiers(spec: &str) -> Vec<(PathBuf, u8)> {
    spec.split(';')
        .filter_map(|entry| {
            let (path, tier) = entry.rsplit_once('=')?;
            Some((PathBuf::from(path.trim()), tier.trim().parse().ok()?))
        })
        .collect()
}
