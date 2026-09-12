import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from task_evolver import ImportanceTable, PairStore, TaskEvolver
from task_evolver.__main__ import main
from task_evolver.learning import LearningQueue, ask_score, ensure_key


class LearningTest(unittest.TestCase):
    def test_cli_initialization_deduplicates_and_lookup_asks_on_miss(self):
        with tempfile.TemporaryDirectory() as directory:
            base = [
                "task_evolver",
                "--db",
                str(Path(directory) / "pairs.db"),
                "--reference",
                "reference",
            ]
            with (
                patch("task_evolver.__main__.OpenAIExpander") as adapter,
                patch("builtins.input", return_value="0.8") as answer,
                redirect_stdout(io.StringIO()),
            ):
                adapter.return_value.expand.return_value = []
                with patch("sys.argv", base + ["init", "a", "a", "reference"]):
                    main()
                with patch("sys.argv", base + ["lookup", "a"]):
                    main()
                self.assertEqual(answer.call_count, 1)
                with patch("sys.argv", base + ["lookup", "missing", "--no-ask"]):
                    main()
                self.assertEqual(answer.call_count, 1)
                with patch("sys.argv", base + ["lookup", "missing"]):
                    main()
                self.assertEqual(answer.call_count, 2)
                self.assertEqual(adapter.return_value.expand.call_count, 4)

    def test_cli_init_reports_expansion_failure_and_keeps_human_fit(self):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with (
                patch("task_evolver.__main__.OpenAIExpander") as adapter,
                patch("builtins.input", return_value="0.8"),
                patch(
                    "sys.argv",
                    [
                        "task_evolver",
                        "--db",
                        str(Path(directory) / "pairs.db"),
                        "--reference",
                        "b",
                        "init",
                        "a",
                    ],
                ),
                redirect_stdout(output),
            ):
                adapter.return_value.expand.side_effect = RuntimeError("test failure")
                main()
            result = json.loads(output.getvalue())
            self.assertEqual(result["fit_version"], 1)
            self.assertEqual(
                result["comparisons"][0]["expansion_error"], "RuntimeError"
            )

    def test_missing_key_asks_once_and_connects_to_reference(self):
        store = PairStore()
        table = ImportanceTable()
        self.addCleanup(store.conn.close)
        self.addCleanup(table.conn.close)
        expander = Mock()
        expander.expand.side_effect = lambda key: [key + " alias"]
        evolver = TaskEvolver(store, table, expander, "reference")
        ask = Mock(return_value=0.8)
        result = ensure_key(evolver, " new  key ", ask)
        self.assertEqual(result.expanded_count, 2)
        self.assertGreater(table.lookup("new key"), table.lookup("reference"))
        ensure_key(evolver, "new key", ask)
        ensure_key(evolver, "reference", ask)
        ask.assert_called_once_with("new key", "reference")
        self.assertEqual(expander.expand.call_count, 2)

    def test_invalid_input_retries_and_eof_does_not_write(self):
        with (
            patch("builtins.input", side_effect=["nan", "1.2", "bad", "0.7"]),
            patch("builtins.print"),
        ):
            self.assertEqual(ask_score("a", "b"), 0.7)
        store = PairStore()
        table = ImportanceTable()
        self.addCleanup(store.conn.close)
        self.addCleanup(table.conn.close)
        evolver = TaskEvolver(store, table, Mock(), "b")
        with self.assertRaises(EOFError):
            ensure_key(evolver, "a", Mock(side_effect=EOFError))
        self.assertEqual(store.effective(), [])
        self.assertEqual(table.fit_version(), 0)

    def test_worker_deduplicates_pending_key_and_publishes_fit(self):
        entered = threading.Event()
        release = threading.Event()
        updated = threading.Event()

        def answer(prompt):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test answer timeout")
            return "0.8"

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("builtins.input", side_effect=answer) as ask,
                patch("builtins.print"),
                patch("task_evolver.learning.OpenAIExpander") as adapter,
            ):
                adapter.return_value.expand.return_value = []
                worker = LearningQueue(
                    root / "pairs.db",
                    "reference",
                    root / "importance.json",
                    [(root, "a")],
                    updated.set,
                )
                worker.request("a")
                self.assertTrue(entered.wait(5))
                worker.request("a")
                release.set()
                self.assertTrue(updated.wait(5))
                self.assertEqual(ask.call_count, 1)
                scores = json.loads((root / "importance.json").read_text())["scores"]
                self.assertGreater(scores["a"], scores["reference"])
                ask.side_effect = EOFError
                worker.request("b")
                worker.thread.join(5)
                self.assertFalse(worker.thread.is_alive())
                self.assertTrue(worker.closed)
