use std::collections::HashMap;
use std::fs::File;
use std::io;
use std::io::Read;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Instant;

use serde_json::Value;
use tracing::warn;

use crate::Preference;
use crate::Scorer;
use crate::TaskFeatures;

const MAX_SNAPSHOT_BYTES: u64 = 1024 * 1024;

#[derive(Default)]
struct Snapshot {
    version: u64,
    scores: HashMap<String, f64>,
    bindings: Vec<(PathBuf, String)>,
}

pub(crate) struct ImportanceScorer {
    path: PathBuf,
    rate: f64,
    snapshot: Mutex<Snapshot>,
}

impl ImportanceScorer {
    pub(crate) fn new(path: PathBuf, rate: f64) -> io::Result<Self> {
        if !rate.is_finite() || rate < 0.0 {
            return Err(io::Error::other("rate must be finite and nonnegative"));
        }
        Ok(Self {
            path,
            rate,
            snapshot: Mutex::new(Snapshot::default()),
        })
    }

    fn load(&self) -> io::Result<Snapshot> {
        let file = File::open(&self.path)?;
        let metadata = file.metadata()?;
        if !metadata.is_file() || metadata.len() > MAX_SNAPSHOT_BYTES {
            return Err(io::Error::other("snapshot must be a file of at most 1 MiB"));
        }
        let value: Value = serde_json::from_reader(file.take(MAX_SNAPSHOT_BYTES))?;
        let invalid = || io::Error::other("invalid importance snapshot");
        let version = value["fit_version"].as_u64().ok_or_else(invalid)?;
        let mut scores = HashMap::new();
        for (key, value) in value["scores"].as_object().ok_or_else(invalid)? {
            let score = value.as_f64().ok_or_else(invalid)?;
            if !score.is_finite() || !(0.0..=100.0).contains(&score) {
                return Err(invalid());
            }
            scores.insert(key.clone(), score);
        }
        let mut bindings = Vec::new();
        for binding in value["bindings"].as_array().ok_or_else(invalid)? {
            let cwd = PathBuf::from(binding["cwd"].as_str().ok_or_else(invalid)?);
            let key = binding["key"].as_str().ok_or_else(invalid)?.to_string();
            if !cwd.is_absolute() || key.is_empty() {
                return Err(invalid());
            }
            let cwd = cwd.canonicalize().unwrap_or(cwd);
            if bindings.iter().any(|(path, _)| path == &cwd) {
                return Err(invalid());
            }
            bindings.push((cwd, key));
        }
        Ok(Snapshot {
            version,
            scores,
            bindings,
        })
    }
}

impl Scorer for ImportanceScorer {
    fn refresh(&self) {
        match self.load() {
            Ok(next) => {
                let mut snapshot = self
                    .snapshot
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner);
                if next.version >= snapshot.version {
                    *snapshot = next;
                }
            }
            Err(err) => warn!(%err, "keeping previous importance snapshot"),
        }
    }

    fn score(&self, first: &TaskFeatures, second: &TaskFeatures) -> Preference {
        let snapshot = self
            .snapshot
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        let now = Instant::now();
        let importance = |task: &TaskFeatures| {
            let path = task.cwd.canonicalize().unwrap_or_else(|_| task.cwd.clone());
            snapshot
                .bindings
                .iter()
                .filter(|(cwd, _)| path.starts_with(cwd))
                .max_by_key(|(cwd, _)| cwd.components().count())
                .and_then(|(_, key)| snapshot.scores.get(key))
                .copied()
                .unwrap_or(50.0)
        };
        let diff = importance(first) - importance(second)
            + self.rate
                * (now.duration_since(first.waiting_since).as_secs_f64()
                    - now.duration_since(second.waiting_since).as_secs_f64());
        if diff == 0.0 {
            Preference::UNKNOWN
        } else {
            Preference::certain(diff > 0.0)
        }
    }
}

#[cfg(test)]
#[path = "importance_tests.rs"]
mod tests;
