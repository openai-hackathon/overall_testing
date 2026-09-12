import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from task_evolver import ImportanceTable, OpenAIExpander, PairStore, TaskEvolver
from task_evolver.__main__ import main


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.store = PairStore()
        self.table = ImportanceTable()
        self.addCleanup(self.store.conn.close)
        self.addCleanup(self.table.conn.close)
        self.expander = Mock()
        self.expander.expand.side_effect = lambda key: [
            key,
            f"{key} alternative",
            f"{key} alternative",
        ]
        self.evolver = TaskEvolver(self.store, self.table, self.expander, "a")

    def test_soft_score_expands_in_both_directions_and_refits(self):
        result = self.evolver.answer("z", "a", 0.7)
        pairs = self.store.effective()
        human = next(pair for pair in pairs if pair.source == "human")
        self.assertEqual((human.a_key, human.b_key), ("a", "z"))
        self.assertAlmostEqual(human.p_a_wins, 0.3)
        children = [pair for pair in pairs if pair.source == "expanded"]
        self.assertEqual(len(children), 2)
        self.assertAlmostEqual(sum(pair.weight for pair in children), 0.2)
        self.assertEqual({pair.parent_pair_id for pair in children}, {result.pair_id})
        self.assertEqual(
            [(p.a_key, p.b_key) for p in children],
            [("a", "z alternative"), ("a alternative", "z")],
        )
        for pair in children:
            self.assertAlmostEqual(pair.p_a_wins, 0.3)
        self.assertGreater(self.evolver.lookup("z"), self.evolver.lookup("a"))
        self.assertEqual(
            (result.fit_version, result.expanded_count, result.expansion_error),
            (2, 2, None),
        )
        self.assertEqual(self.expander.expand.call_count, 2)

    def test_correction_invalidates_old_children_and_changes_ranking(self):
        first = self.evolver.answer("z", "a", 0.7)
        second = self.evolver.answer("z", "a", 0.2)
        self.assertGreater(second.fit_version, first.fit_version)
        self.assertLess(self.evolver.lookup("z"), self.evolver.lookup("a"))
        self.assertEqual(
            {
                p.parent_pair_id
                for p in self.store.effective()
                if p.source == "expanded"
            },
            {second.pair_id},
        )

    def test_lm_failure_preserves_human_fit_and_lookup_never_calls_lm(self):
        self.expander.expand.side_effect = TimeoutError("OpenAI request timed out")
        result = self.evolver.answer("z", "a", 0.8)
        self.assertEqual(
            (result.fit_version, result.expanded_count, result.expansion_error),
            (1, 0, "TimeoutError"),
        )
        self.assertGreater(self.evolver.lookup(" z  "), 50)
        self.assertIsNone(self.evolver.lookup("missing"))
        self.assertEqual(self.expander.expand.call_count, 1)

    def test_invalid_score_and_disconnected_pair_do_not_write(self):
        for score in [-0.1, 1.1, float("nan")]:
            with self.assertRaises(ValueError):
                self.evolver.answer("z", "a", score)
        self.assertEqual(self.store.effective(), [])
        self.assertEqual(self.table.fit_version(), 0)
        self.expander.expand.return_value = []
        self.expander.expand.side_effect = None
        self.evolver.answer("z", "a", 0.5)
        before = self.store.effective()
        with self.assertRaises(ValueError):
            self.evolver.answer("x", "y", 0.6)
        self.assertEqual(self.store.effective(), before)
        self.assertEqual(self.evolver.lookup("z"), 50)

    def test_existing_keys_are_not_relabelled_by_expansion(self):
        self.expander.expand.side_effect = lambda key: ["a", "z", "same", " same "]
        result = self.evolver.answer("z", "a", 0.7)
        self.assertEqual(result.expanded_count, 1)
        self.assertEqual(len(self.store.effective()), 2)

    def test_reference_cannot_change(self):
        with self.assertRaises(ValueError):
            TaskEvolver(self.store, self.table, self.expander, "other")

    def test_refit_rejects_disconnected_imported_pairs(self):
        self.expander.expand.side_effect = None
        self.expander.expand.return_value = []
        self.evolver.answer("z", "a", 0.7)
        previous = self.table.fit_version()
        self.store.record("x", "y", 0.8, "seed")
        with self.assertRaises(ValueError):
            self.evolver.refit()
        self.assertEqual(self.table.fit_version(), previous)
        self.assertIsNone(self.evolver.lookup("x"))


class OpenAIExpanderTest(unittest.TestCase):
    def test_reasoning_setting_is_sent_only_when_configured(self):
        response = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": '{"keys": []}'}],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            dotenv = Path(directory) / ".env"
            dotenv.write_text("TASK_EVOLVER_REASONING_EFFORT=medium\n")
            for environment, expected in [
                ({}, {"effort": "medium"}),
                ({"TASK_EVOLVER_REASONING_EFFORT": "low"}, {"effort": "low"}),
                ({"TASK_EVOLVER_REASONING_EFFORT": ""}, None),
            ]:
                with (
                    patch("task_evolver.expansion.os.environ", environment),
                    patch("task_evolver.expansion.urlopen") as send,
                ):
                    send.return_value.__enter__.return_value.read.return_value = (
                        json.dumps(response).encode()
                    )
                    OpenAIExpander(
                        "test-model", api_key="test-key", dotenv_path=dotenv
                    ).expand("task")
                    payload = json.loads(send.call_args.args[0].data)
                    self.assertEqual(payload.get("reasoning"), expected)
                    self.assertEqual("reasoning" in payload, expected is not None)

    def test_dotenv_and_override_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            dotenv = Path(directory) / ".env"
            dotenv.write_text(
                'export OPENAI_API_KEY="file-key" # key\nTASK_EVOLVER_MODEL=file-model\n'
            )
            with patch("task_evolver.expansion.os.environ", {}):
                expander = OpenAIExpander(dotenv_path=dotenv)
                self.assertEqual(
                    (expander.api_key, expander.model), ("file-key", "file-model")
                )
            with patch(
                "task_evolver.expansion.os.environ",
                {"OPENAI_API_KEY": "env-key", "TASK_EVOLVER_MODEL": "env-model"},
            ):
                expander = OpenAIExpander(dotenv_path=dotenv)
                self.assertEqual(
                    (expander.api_key, expander.model), ("env-key", "env-model")
                )
                expander = OpenAIExpander(
                    "arg-model", api_key="arg-key", dotenv_path=dotenv
                )
                self.assertEqual(
                    (expander.api_key, expander.model), ("arg-key", "arg-model")
                )

    def test_invalid_dotenv_does_not_expose_value(self):
        with tempfile.TemporaryDirectory() as directory:
            dotenv = Path(directory) / ".env"
            dotenv.write_text('OPENAI_API_KEY="secret')
            with (
                patch("task_evolver.expansion.os.environ", {}),
                self.assertRaisesRegex(ValueError, "^invalid OPENAI_API_KEY in .env$"),
            ):
                OpenAIExpander("test-model", dotenv_path=dotenv)

    def test_request_and_response(self):
        response = {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps({"keys": [" new task ", "new  task"]}),
                        }
                    ],
                }
            ],
        }
        with patch("task_evolver.expansion.urlopen") as send:
            send.return_value.__enter__.return_value.read.return_value = json.dumps(
                response
            ).encode()
            self.assertEqual(
                OpenAIExpander("test-model", api_key="test-key").expand("task"),
                ["new task"],
            )
            request = send.call_args.args[0]
            payload = json.loads(request.data)
            self.assertEqual(request.full_url, "https://api.openai.com/v1/responses")
            self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
            self.assertEqual(payload["model"], "test-model")
            self.assertEqual(payload["input"], '"task"')
            self.assertTrue(payload["text"]["format"]["strict"])
            self.assertFalse(payload["store"])

    def test_incomplete_refusal_and_invalid_keys_are_rejected(self):
        responses = [
            {"status": "incomplete"},
            {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "refusal"}]}],
            },
        ]
        for keys in ["wrong", ["x"] * 5, [""]]:
            responses.append(
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": json.dumps({"keys": keys}),
                                }
                            ],
                        }
                    ],
                }
            )
        for response in responses:
            with (
                self.subTest(response=response),
                patch("task_evolver.expansion.urlopen") as send,
            ):
                send.return_value.__enter__.return_value.read.return_value = json.dumps(
                    response
                ).encode()
                with self.assertRaises(ValueError):
                    OpenAIExpander("test-model", api_key="test-key").expand("task")


class CommandTest(unittest.TestCase):
    def test_interactive_numeric_answer_persists_scores(self):
        with tempfile.TemporaryDirectory() as directory:
            db = str(Path(directory) / "evolver.sqlite3")
            arguments = [
                "task_evolver",
                "--db",
                db,
                "--reference",
                "oss",
                "compare",
                "incident",
                "oss",
            ]
            output = io.StringIO()
            with (
                patch("sys.argv", arguments),
                patch("builtins.input", return_value="0.7"),
                patch.object(OpenAIExpander, "expand", return_value=[]),
                redirect_stdout(output),
            ):
                main()
            self.assertEqual(
                json.loads(output.getvalue()),
                {
                    "pair_id": 1,
                    "fit_version": 1,
                    "expanded_count": 0,
                    "expansion_error": None,
                },
            )
            table = ImportanceTable(db)
            try:
                self.assertGreater(table.lookup("incident"), table.lookup("oss"))
            finally:
                table.conn.close()
