use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use std::time::Instant;

use pretty_assertions::assert_eq;

use super::AgingScorer;
use super::ChainScorer;
use super::LinearScorer;
use super::PairTableScorer;
use super::Preference;
use super::RuleScorer;
use super::Scorer;
use super::TaskFeatures;

fn task(cwd: &str, tool: &str) -> TaskFeatures {
    TaskFeatures {
        cwd: PathBuf::from(cwd),
        tool_name: tool.to_string(),
        waiting_since: Instant::now(),
    }
}

fn rules() -> RuleScorer {
    RuleScorer::new(
        vec![
            (PathBuf::from("/home/fab-incident"), 0),
            (PathBuf::from("/home/oss"), 3),
        ],
        vec!["read_file".to_string()],
    )
}

#[test]
fn rule_scorer_prefers_lower_tier() {
    let scorer = rules();
    let incident = task("/home/fab-incident/logs", "shell");
    let oss = task("/home/oss/airflow", "shell");
    assert_eq!(scorer.score(&incident, &oss), Preference::certain(true));
    assert_eq!(scorer.score(&oss, &incident), Preference::certain(false));
}

#[test]
fn rule_scorer_falls_back_to_cheap_tool() {
    let scorer = rules();
    let read = task("/home/oss/a", "read_file");
    let shell = task("/home/oss/b", "shell");
    let preference = scorer.score(&read, &shell);
    assert!(preference.first_wins());
    assert!(preference.confidence < 1.0);
    assert_eq!(
        scorer.score(&shell, &task("/home/oss/c", "shell")),
        Preference::UNKNOWN
    );
}

#[test]
fn pair_table_learns_from_records() {
    let scorer = PairTableScorer::default();
    let a = task("/x", "shell");
    let b = task("/y", "shell");
    assert_eq!(scorer.score(&a, &b), Preference::UNKNOWN);
    scorer.record(&a, &b, true);
    assert_eq!(scorer.score(&a, &b), Preference::certain(true));
    assert_eq!(scorer.score(&b, &a), Preference::certain(false));
    scorer.record(&b, &a, true);
    assert_eq!(scorer.score(&a, &b), Preference::UNKNOWN);
}

#[test]
fn pair_table_combines_conflicting_votes_in_either_order() {
    let a = task("/x", "shell");
    let b = task("/y", "shell");
    for (first, second) in [(&a, &b), (&b, &a)] {
        let scorer = PairTableScorer::default();
        scorer.record(first, second, true);
        scorer.record(second, first, true);
        scorer.record(first, second, true);
        assert_eq!(scorer.score(first, second), Preference::certain(true));
        assert_eq!(scorer.score(second, first), Preference::certain(false));
    }
}

#[test]
fn chain_uses_first_confident_scorer() {
    let pairs = Arc::new(PairTableScorer::default());
    let chain = ChainScorer::new(vec![Arc::new(rules()), pairs.clone()], 0.7);
    let a = task("/unknown/a", "shell");
    let b = task("/unknown/b", "shell");
    assert_eq!(chain.score(&a, &b), Preference::UNKNOWN);
    for _ in 0..3 {
        pairs.record(&b, &a, true);
    }
    assert!(!chain.score(&a, &b).first_wins());
}

#[test]
fn aging_scorer_prefers_stale_waiter() {
    let scorer = AgingScorer::new(Duration::from_millis(50));
    let mut old = task("/a", "shell");
    old.waiting_since = Instant::now() - Duration::from_millis(100);
    let fresh = task("/b", "shell");
    assert_eq!(scorer.score(&fresh, &fresh), Preference::UNKNOWN);
    assert_eq!(scorer.score(&fresh, &old), Preference::certain(false));
    assert_eq!(scorer.score(&old, &fresh), Preference::certain(true));
}

#[test]
fn linear_scorer_lets_wait_overtake_tier() {
    let tiers = vec![(PathBuf::from("/incident"), 0), (PathBuf::from("/oss"), 3)];
    let scorer = LinearScorer::new(tiers, 10.0, 5.0);
    let incident = task("/incident/x", "shell");
    let mut oss = task("/oss/y", "shell");
    assert!(scorer.score(&incident, &oss).first_wins());
    oss.waiting_since = Instant::now() - Duration::from_secs(10);
    let preference = scorer.score(&incident, &oss);
    assert!(!preference.first_wins());
    assert_eq!(preference.confidence, 1.0);
}
