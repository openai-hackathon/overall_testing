import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from experiments.replay import replay, workload
from task_evolver.admission import AdmissionService


class ReplayTest(unittest.TestCase):
    def test_linear_replay_matches_service_dispatch(self):
        scores = {"0": 80.0, "1": 50.0, "2": 40.0, "3": 30.0}
        for shape in ["uniform", "long_tail"]:
            for rate in [0.25, 1, 4, 16]:
                calls = workload(3, shape)
                expected = replay(calls, scores, "linear", rate)
                service = AdmissionService(rate=rate)
                service.scores = scores
                service.bindings = [(Path(f"/session-{key}"), key) for key in scores]
                incoming = iter(calls)
                next_call = next(incoming, None)
                for selected in expected:
                    now = selected["start"]
                    while next_call is not None and next_call["arrival"] <= now:
                        key = ("client", "thread", "turn", str(next_call["id"]))
                        service.calls[key] = {
                            "identity": key,
                            "state": "queued",
                            "queued_at": next_call["arrival"],
                            "cwd": f"/session-{next_call['session']}",
                            "writer": Mock(),
                        }
                        next_call = next(incoming, None)
                    with patch(
                        "task_evolver.admission.time.monotonic", return_value=now
                    ):
                        service.dispatch()
                    granted = [
                        key
                        for key, call in service.calls.items()
                        if call["state"] == "granted"
                    ]
                    self.assertEqual(
                        granted, [("client", "thread", "turn", str(selected["id"]))]
                    )
                    service.calls.pop(granted[0])
                self.assertEqual(service.calls, {})
