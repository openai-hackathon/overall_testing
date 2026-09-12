import math
import sqlite3
import unittest

from task_evolver import (
    ImportanceTable,
    PairStore,
    fit_bradley_terry,
    importance_from_z,
)


class PairStoreTest(unittest.TestCase):
    def test_reverse_direction_merges_into_one_pair(self):
        store = PairStore()
        self.addCleanup(store.conn.close)
        store.record("oss", "fab", 0.0, "human")
        store.record("fab", "oss", 1.0, "seed")
        pairs = store.effective()
        self.assertEqual(
            [(p.a_key, p.b_key, p.p_a_wins, p.source) for p in pairs],
            [("fab", "oss", 1.0, "human")],
        )

    def test_latest_human_answer_replaces_previous(self):
        store = PairStore()
        self.addCleanup(store.conn.close)
        first = store.record("a", "b", 1.0, "human")
        store.record("a", "c", 1.0, "human")
        store.record("a", "b", 0.0, "human")
        pairs = {(p.a_key, p.b_key): p for p in store.effective()}
        self.assertEqual(pairs[("a", "b")].p_a_wins, 0.0)
        self.assertEqual(pairs[("a", "b")].revision, 2)
        self.assertIn(("a", "c"), pairs)
        self.assertEqual(
            store.conn.execute(
                "SELECT active FROM pairs WHERE pair_id = ?", (first,)
            ).fetchone()[0],
            0,
        )

    def test_expanded_weight_is_capped_per_parent(self):
        store = PairStore()
        self.addCleanup(store.conn.close)
        parent = store.record("a", "b", 1.0, "human")
        store.record("a2", "b", 1.0, "expanded", weight=0.15, parent_pair_id=parent)
        second = store.record(
            "a3", "b", 1.0, "expanded", weight=0.15, parent_pair_id=parent
        )
        third = store.record(
            "a4", "b", 1.0, "expanded", weight=0.15, parent_pair_id=parent
        )
        weights = sorted(p.weight for p in store.effective() if p.source == "expanded")
        self.assertAlmostEqual(sum(weights), 0.2)
        self.assertIsNotNone(second)
        self.assertIsNone(third)

    def test_correcting_parent_disables_expanded_children(self):
        store = PairStore()
        self.addCleanup(store.conn.close)
        parent = store.record("a", "b", 1.0, "human")
        store.record("a2", "b", 1.0, "expanded", weight=0.1, parent_pair_id=parent)
        store.record("a", "b", 0.5, "human")
        self.assertEqual([p.source for p in store.effective()], ["human"])

    def test_failed_insert_rolls_back_parent_and_children(self):
        store = PairStore()
        self.addCleanup(store.conn.close)
        parent = store.record("a", "b", 1.0, "human")
        store.record("a2", "b", 1.0, "expanded", parent_pair_id=parent)
        before = store.effective()
        store.conn.execute(
            "CREATE TRIGGER reject_pair BEFORE INSERT ON pairs "
            "BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            store.record("a", "b", 0.0, "human")
        self.assertEqual(store.effective(), before)
        store.conn.execute("DROP TRIGGER reject_pair")
        store.record("c", "d", 1.0, "human")
        self.assertEqual(store.effective()[:2], before)

    def test_human_override_invalidates_seed_expansion(self):
        store = PairStore()
        self.addCleanup(store.conn.close)
        parent = store.record("a", "b", 1.0, "seed")
        store.record("a2", "b", 1.0, "expanded", parent_pair_id=parent)
        store.record("b", "a", 1.0, "human")
        self.assertEqual(
            [(p.a_key, p.b_key, p.p_a_wins, p.source) for p in store.effective()],
            [("a", "b", 0.0, "human")],
        )
        with self.assertRaises(ValueError):
            store.record("a3", "b", 1.0, "expanded", parent_pair_id=parent)

    def test_expansion_rejects_missing_recursive_and_inactive_parents(self):
        store = PairStore()
        self.addCleanup(store.conn.close)
        parent = store.record("a", "b", 1.0, "human")
        child = store.record("a2", "b", 1.0, "expanded", parent_pair_id=parent)
        for invalid in [999, child]:
            with self.subTest(parent=invalid), self.assertRaises(ValueError):
                store.record("a3", "b", 1.0, "expanded", parent_pair_id=invalid)
        store.record("a", "b", 0.0, "human")
        with self.assertRaises(ValueError):
            store.record("a3", "b", 1.0, "expanded", parent_pair_id=parent)

    def test_invalid_pair_numbers_preserve_existing_answer(self):
        store = PairStore()
        self.addCleanup(store.conn.close)
        store.record("a", "b", 1.0, "human")
        before = store.effective()
        for probability in [-1, 2, math.inf, math.nan, None]:
            with (
                self.subTest(probability=probability),
                self.assertRaises((TypeError, ValueError)),
            ):
                store.record("a", "b", probability, "human")
            self.assertEqual(store.effective(), before)
        for weight in [-1, 0, math.inf, math.nan, None]:
            with (
                self.subTest(weight=weight),
                self.assertRaises((TypeError, ValueError)),
            ):
                store.record("a", "b", 0.5, "human", weight=weight)
            self.assertEqual(store.effective(), before)

    def test_later_seed_cannot_invalidate_human_expansion(self):
        store = PairStore()
        self.addCleanup(store.conn.close)
        parent = store.record("a", "b", 1.0, "human")
        store.record("a2", "b", 1.0, "expanded", parent_pair_id=parent)
        before = store.effective()
        seed = store.record("a", "b", 0.0, "seed")
        self.assertEqual(store.effective(), before)
        with self.assertRaises(ValueError):
            store.record("a3", "b", 0.0, "expanded", parent_pair_id=seed)


class FitTest(unittest.TestCase):
    def test_fit_orders_keys_by_preference(self):
        store = PairStore()
        self.addCleanup(store.conn.close)
        store.record("fab", "tooling", 1.0, "human")
        store.record("tooling", "oss", 1.0, "human")
        store.record("fab", "oss", 1.0, "seed")
        z = fit_bradley_terry(store.effective(), ref_key="oss")
        self.assertEqual(z["oss"], 0.0)
        self.assertGreater(z["fab"], z["tooling"])
        self.assertGreater(z["tooling"], z["oss"])
        importance = importance_from_z(z)
        self.assertEqual(importance["oss"], 50.0)
        self.assertLess(importance["fab"], 100.0)

    def test_fit_is_stable_across_refit_with_same_data(self):
        store = PairStore()
        self.addCleanup(store.conn.close)
        store.record("a", "b", 0.8, "human")
        pairs = store.effective()
        self.assertEqual(fit_bradley_terry(pairs, "b"), fit_bradley_terry(pairs, "b"))

    def test_many_soft_preferences_converge_without_reversing_rank(self):
        store = PairStore()
        self.addCleanup(store.conn.close)
        for i in range(100):
            store.record("hub", f"leaf-{i}", 0.6, "human")
        pairs = store.effective()
        z = fit_bradley_terry(pairs, "leaf-0")
        self.assertTrue(all(z["hub"] > z[f"leaf-{i}"] for i in range(100)))
        gradient = {k: 0.1 * v for k, v in z.items()}
        for pair in pairs:
            error = 1 / (1 + math.exp(-(z[pair.a_key] - z[pair.b_key]))) - pair.p_a_wins
            gradient[pair.a_key] += pair.weight * error
            gradient[pair.b_key] -= pair.weight * error
        self.assertLess(max(abs(v) for k, v in gradient.items() if k != "leaf-0"), 1e-5)

    def test_unconverged_fit_does_not_return_scores(self):
        store = PairStore()
        self.addCleanup(store.conn.close)
        store.record("a", "b", 1.0, "human")
        with self.assertRaises(RuntimeError):
            fit_bradley_terry(store.effective(), "b", iters=1)

    def test_extreme_logits_do_not_overflow(self):
        self.assertEqual(
            importance_from_z({"low": -2000, "high": 2000}), {"low": 0.0, "high": 100.0}
        )


class ImportanceTableTest(unittest.TestCase):
    def test_update_bumps_version_and_lookup(self):
        table = ImportanceTable()
        self.addCleanup(table.conn.close)
        self.assertIsNone(table.lookup("fab"))
        self.assertEqual(table.update({"fab": 90.0, "oss": 20.0}), 1)
        self.assertEqual(table.update({"fab": 91.0}), 2)
        self.assertEqual(table.lookup("fab"), 91.0)
        self.assertIsNone(table.lookup("oss"))

    def test_failed_update_keeps_previous_version(self):
        table = ImportanceTable()
        self.addCleanup(table.conn.close)
        table.update({"fab": 90.0})
        self.assertEqual(table.update({"fab": object()}), 1)
        self.assertEqual(table.lookup("fab"), 90.0)
        self.assertEqual(table.fit_version(), 1)

    def test_invalid_scores_keep_previous_version(self):
        table = ImportanceTable()
        self.addCleanup(table.conn.close)
        table.update({"a": 60.0})
        for value in [-1, 101, math.inf, -math.inf, math.nan, None]:
            with self.subTest(value=value):
                self.assertEqual(table.update({"a": value}), 1)
                self.assertEqual(table.lookup("a"), 60.0)

    def test_sql_failure_rolls_back_scores_and_version(self):
        table = ImportanceTable()
        self.addCleanup(table.conn.close)
        table.update({"a": 60.0})
        table.conn.execute(
            "CREATE TRIGGER reject_score BEFORE INSERT ON scores "
            "WHEN NEW.key = 'b' BEGIN SELECT RAISE(ABORT, 'test failure'); END"
        )
        self.assertEqual(table.update({"a": 70.0, "b": 80.0}), 1)
        self.assertEqual(table.lookup("a"), 60.0)
        self.assertIsNone(table.lookup("b"))
        self.assertEqual(table.fit_version(), 1)


if __name__ == "__main__":
    unittest.main()
