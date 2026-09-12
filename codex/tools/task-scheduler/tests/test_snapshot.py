import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from task_evolver import ImportanceTable, PairStore, TaskEvolver


class SnapshotTest(unittest.TestCase):
    def test_human_refit_publishes_changed_scores_and_bindings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "importance.json"
            store = PairStore()
            table = ImportanceTable(snapshot_path=path, bindings=[(directory, "a")])
            self.addCleanup(store.conn.close)
            self.addCleanup(table.conn.close)
            expander = Mock()
            expander.expand.return_value = []
            evolver = TaskEvolver(store, table, expander, "b")
            evolver.answer("a", "b", 0.8)
            first = json.loads(path.read_text())
            self.assertEqual(
                first["bindings"], [{"cwd": str(Path(directory).resolve()), "key": "a"}]
            )
            self.assertGreater(first["scores"]["a"], first["scores"]["b"])
            evolver.answer("a", "b", 0.2)
            second = json.loads(path.read_text())
            self.assertGreater(second["fit_version"], first["fit_version"])
            self.assertLess(second["scores"]["a"], second["scores"]["b"])
            self.assertEqual(second["scores"]["a"], table.lookup("a"))

    def test_failed_publish_preserves_previous_file_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "importance.json"
            table = ImportanceTable(snapshot_path=path)
            self.addCleanup(table.conn.close)
            table.update({"a": 60.0})
            previous = path.read_bytes()
            with (
                patch(
                    "task_evolver.table.os.replace", side_effect=OSError("test failure")
                ),
                self.assertRaises(OSError),
            ):
                table.update({"a": 70.0})
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(list(Path(directory).iterdir()), [path])
            table.publish()
            self.assertEqual(json.loads(path.read_text())["scores"], {"a": 70.0})

    def test_snapshot_cannot_replace_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evolver.sqlite3"
            with self.assertRaises(ValueError):
                ImportanceTable(path, snapshot_path=path)
            self.assertFalse(path.exists())
