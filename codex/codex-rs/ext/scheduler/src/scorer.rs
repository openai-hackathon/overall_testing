use std::collections::HashMap;
use std::path::Path;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::Mutex;
use std::time::Duration;
use std::time::Instant;

#[derive(Clone, Debug)]
pub struct TaskFeatures {
    pub cwd: PathBuf,
    pub tool_name: String,
    pub waiting_since: Instant,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Preference {
    pub p_first_wins: f32,
    pub confidence: f32,
}

impl Preference {
    pub const UNKNOWN: Self = Self {
        p_first_wins: 0.5,
        confidence: 0.0,
    };

    pub fn certain(first_wins: bool) -> Self {
        Self {
            p_first_wins: if first_wins { 1.0 } else { 0.0 },
            confidence: 1.0,
        }
    }

    pub fn first_wins(&self) -> bool {
        self.p_first_wins > 0.5
    }
}

pub trait Scorer: Send + Sync {
    fn refresh(&self) {}
    fn score(&self, first: &TaskFeatures, second: &TaskFeatures) -> Preference;
}

fn tier_of(cwd_tiers: &[(PathBuf, u8)], cwd: &Path) -> Option<u8> {
    cwd_tiers
        .iter()
        .filter(|(prefix, _)| cwd.starts_with(prefix))
        .max_by_key(|(prefix, _)| prefix.as_os_str().len())
        .map(|(_, tier)| *tier)
}

#[derive(Default)]
pub struct RuleScorer {
    cwd_tiers: Vec<(PathBuf, u8)>,
    cheap_tools: Vec<String>,
}

impl RuleScorer {
    pub fn new(cwd_tiers: Vec<(PathBuf, u8)>, cheap_tools: Vec<String>) -> Self {
        Self {
            cwd_tiers,
            cheap_tools,
        }
    }

    fn tier(&self, cwd: &Path) -> Option<u8> {
        tier_of(&self.cwd_tiers, cwd)
    }

    fn is_cheap(&self, tool_name: &str) -> bool {
        self.cheap_tools.iter().any(|tool| tool == tool_name)
    }
}

impl Scorer for RuleScorer {
    fn score(&self, first: &TaskFeatures, second: &TaskFeatures) -> Preference {
        if let (Some(a), Some(b)) = (self.tier(&first.cwd), self.tier(&second.cwd))
            && a != b
        {
            return Preference::certain(a < b);
        }
        match (
            self.is_cheap(&first.tool_name),
            self.is_cheap(&second.tool_name),
        ) {
            (true, false) => Preference {
                p_first_wins: 0.8,
                confidence: 0.6,
            },
            (false, true) => Preference {
                p_first_wins: 0.2,
                confidence: 0.6,
            },
            _ => Preference::UNKNOWN,
        }
    }
}

#[derive(Clone, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
struct PairKey {
    cwd: PathBuf,
    tool_name: String,
}

impl PairKey {
    fn of(features: &TaskFeatures) -> Self {
        Self {
            cwd: features.cwd.clone(),
            tool_name: features.tool_name.clone(),
        }
    }

    fn canonical_pair(first: &TaskFeatures, second: &TaskFeatures) -> ((Self, Self), bool) {
        let first = Self::of(first);
        let second = Self::of(second);
        if first > second {
            ((second, first), true)
        } else {
            ((first, second), false)
        }
    }
}

#[derive(Default)]
pub struct PairTableScorer {
    pairs: Mutex<HashMap<(PairKey, PairKey), (u32, u32)>>,
}

impl PairTableScorer {
    pub fn record(&self, first: &TaskFeatures, second: &TaskFeatures, first_wins: bool) {
        let (key, reversed) = PairKey::canonical_pair(first, second);
        let mut pairs = self
            .pairs
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let entry = pairs.entry(key).or_insert((0, 0));
        entry.0 += u32::from(first_wins != reversed);
        entry.1 += 1;
    }

    fn lookup(&self, first: &TaskFeatures, second: &TaskFeatures) -> Option<(u32, u32)> {
        let (key, reversed) = PairKey::canonical_pair(first, second);
        let pairs = self
            .pairs
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        pairs.get(&key).map(|(wins, total)| {
            let wins = if reversed { total - wins } else { *wins };
            (wins, *total)
        })
    }
}

impl Scorer for PairTableScorer {
    fn score(&self, first: &TaskFeatures, second: &TaskFeatures) -> Preference {
        match self.lookup(first, second) {
            Some((wins, total)) if total > 0 && wins * 2 != total => {
                Preference::certain(wins * 2 > total)
            }
            _ => Preference::UNKNOWN,
        }
    }
}

pub struct AgingScorer {
    max_wait: Duration,
}

impl AgingScorer {
    pub fn new(max_wait: Duration) -> Self {
        Self { max_wait }
    }
}

impl Scorer for AgingScorer {
    fn score(&self, first: &TaskFeatures, second: &TaskFeatures) -> Preference {
        let now = Instant::now();
        let first_stale = now.duration_since(first.waiting_since) >= self.max_wait;
        let second_stale = now.duration_since(second.waiting_since) >= self.max_wait;
        match (first_stale, second_stale) {
            (false, false) => Preference::UNKNOWN,
            _ => Preference::certain(
                first_stale && (!second_stale || first.waiting_since <= second.waiting_since),
            ),
        }
    }
}

pub struct LinearScorer {
    cwd_tiers: Vec<(PathBuf, u8)>,
    points_per_tier: f32,
    points_per_sec: f32,
}

impl LinearScorer {
    pub fn new(cwd_tiers: Vec<(PathBuf, u8)>, points_per_tier: f32, points_per_sec: f32) -> Self {
        Self {
            cwd_tiers,
            points_per_tier,
            points_per_sec,
        }
    }

    fn score_at(&self, task: &TaskFeatures, now: Instant) -> f32 {
        let tier = tier_of(&self.cwd_tiers, &task.cwd).unwrap_or(u8::MAX / 2);
        let waited = now.duration_since(task.waiting_since).as_secs_f32();
        waited * self.points_per_sec - f32::from(tier) * self.points_per_tier
    }
}

impl Scorer for LinearScorer {
    fn score(&self, first: &TaskFeatures, second: &TaskFeatures) -> Preference {
        let now = Instant::now();
        let diff = self.score_at(first, now) - self.score_at(second, now);
        if diff == 0.0 {
            return Preference::UNKNOWN;
        }
        Preference::certain(diff > 0.0)
    }
}

pub struct ChainScorer {
    scorers: Vec<Arc<dyn Scorer>>,
    threshold: f32,
}

impl ChainScorer {
    pub fn new(scorers: Vec<Arc<dyn Scorer>>, threshold: f32) -> Self {
        Self { scorers, threshold }
    }
}

impl Scorer for ChainScorer {
    fn refresh(&self) {
        for scorer in &self.scorers {
            scorer.refresh();
        }
    }

    fn score(&self, first: &TaskFeatures, second: &TaskFeatures) -> Preference {
        self.scorers
            .iter()
            .map(|scorer| scorer.score(first, second))
            .find(|preference| preference.confidence >= self.threshold)
            .unwrap_or(Preference::UNKNOWN)
    }
}

#[cfg(test)]
#[path = "scorer_tests.rs"]
mod tests;
