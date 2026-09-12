import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from task_evolver.admission import AdmissionService

CLIENT = """import json, socket, sys
s=socket.create_connection(('127.0.0.1',int(sys.argv[1])))
f=s.makefile('rb')
s.sendall((json.dumps({'op':'enqueue','client_id':sys.argv[2],'thread_id':'same','turn_id':'same','call_id':'same','cwd':'/repo','tool':'shell'})+'\\n').encode())
print(f.readline().decode().strip(),flush=True)
print(f.readline().decode().strip(),flush=True)
sys.stdin.readline()
f.close()
s.close()
"""


class AdmissionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = AdmissionService()
        self.server = await asyncio.start_server(
            self.service.handle, "127.0.0.1", 0, limit=65536
        )
        self.port = self.server.sockets[0].getsockname()[1]
        self.addAsyncCleanup(self.server.wait_closed)
        self.addCleanup(self.server.close)

    async def client(self, name):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        message = {
            "op": "enqueue",
            "client_id": name,
            "thread_id": "thread",
            "turn_id": "turn",
            "call_id": "call",
            "cwd": "/repo",
            "tool": "shell",
        }
        writer.write(json.dumps(message).encode() + b"\n")
        await writer.drain()
        self.addAsyncCleanup(writer.wait_closed)
        self.addCleanup(writer.close)
        return reader, writer, message

    async def read(self, reader):
        return json.loads(await asyncio.wait_for(reader.readline(), 3))["status"]

    async def test_two_independent_processes_share_one_slot(self):
        processes = []
        try:
            for name in ["a", "b"]:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    CLIENT,
                    str(self.port),
                    name,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                )
                processes.append(process)
                self.assertEqual(
                    json.loads(await asyncio.wait_for(process.stdout.readline(), 3))[
                        "status"
                    ],
                    "queued",
                )
                if name == "a":
                    self.assertEqual(
                        json.loads(
                            await asyncio.wait_for(process.stdout.readline(), 3)
                        )["status"],
                        "granted",
                    )
            self.assertEqual(
                sorted(c["state"] for c in self.service.calls.values()),
                ["granted", "queued"],
            )
            processes[0].stdin.write(b"finish\n")
            await processes[0].stdin.drain()
            self.assertEqual(
                json.loads(await asyncio.wait_for(processes[1].stdout.readline(), 3))[
                    "status"
                ],
                "granted",
            )
        finally:
            for process in processes:
                if process.returncode is None:
                    process.stdin.close()
                await asyncio.wait_for(process.wait(), 3)

    async def test_cancel_and_duplicate_operations_do_not_duplicate_slots(self):
        first, owner, message = await self.client("a")
        self.assertEqual(await self.read(first), "queued")
        self.assertEqual(await self.read(first), "granted")
        owner.write(json.dumps(message).encode() + b"\n")
        self.assertEqual(await self.read(first), "granted")
        second, waiter, _ = await self.client("b")
        self.assertEqual(await self.read(second), "queued")
        waiter.write(b'{"op":"cancel"}\n{"op":"cancel"}\n')
        self.assertEqual(await self.read(second), "released")
        self.assertEqual(await self.read(second), "released")
        self.assertEqual(len(self.service.calls), 1)
        owner.write(b'{"op":"release"}\n{"op":"release"}\n')
        self.assertEqual(await self.read(first), "released")
        self.assertEqual(await self.read(first), "released")
        self.assertEqual(self.service.calls, {})

    async def test_symlink_cwd_matches_canonical_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real"
            real.mkdir()
            alias = root / "alias"
            try:
                alias.symlink_to(real, target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation is unavailable")
            self.service.bindings = [(real.resolve(), "incident")]
            self.assertEqual(self.service.key_for(str(alias)), "incident")

    async def test_unknown_bound_key_requests_learning_without_blocking(self):
        self.service.bindings = [(Path("/repo"), "unknown")]
        self.service.on_missing = Mock()
        first, owner, _ = await self.client("a")
        self.assertEqual(await self.read(first), "queued")
        self.assertEqual(await self.read(first), "granted")
        self.service.on_missing.assert_called_once_with("unknown")
        self.assertNotIn("unknown", self.service.scores)
        owner.close()
        await owner.wait_closed()

    async def test_trace_failure_does_not_hold_slot(self):
        with (
            patch("task_evolver.admission.Path.open", side_effect=OSError),
            patch("task_evolver.admission.logging.getLogger"),
        ):
            self.service.trace = Path("unwritable")
            first, owner, _ = await self.client("a")
            self.assertEqual(await self.read(first), "queued")
            self.assertEqual(await self.read(first), "granted")
            owner.write(b'{"op":"release"}\n')
            self.assertEqual(await self.read(first), "released")
            self.assertEqual(self.service.calls, {})

    async def test_duplicate_connection_cannot_release_original(self):
        first, owner, _ = await self.client("a")
        await self.read(first)
        await self.read(first)
        duplicate, _, _ = await self.client("a")
        self.assertEqual(await self.read(duplicate), "error")
        self.assertEqual(len(self.service.calls), 1)
        owner.close()
        await owner.wait_closed()

    async def test_updated_scores_reorder_waiters(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "importance.json"
            self.service.snapshot = snapshot
            snapshot.write_text(
                json.dumps(
                    {
                        "fit_version": 1,
                        "scores": {"a": 90, "b": 10},
                        "bindings": [
                            {"cwd": "/a", "key": "a"},
                            {"cwd": "/b", "key": "b"},
                        ],
                    }
                )
            )
            first, owner, _ = await self.client("owner")
            await self.read(first)
            await self.read(first)
            connections = []
            for name in ["a", "b"]:
                reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
                self.addAsyncCleanup(writer.wait_closed)
                self.addCleanup(writer.close)
                writer.write(
                    json.dumps(
                        {
                            "op": "enqueue",
                            "client_id": name,
                            "thread_id": "thread",
                            "turn_id": "turn",
                            "call_id": "call",
                            "cwd": "/" + name,
                            "tool": "shell",
                        }
                    ).encode()
                    + b"\n"
                )
                self.assertEqual(await self.read(reader), "queued")
                connections.append((reader, writer))
            snapshot.write_text(
                json.dumps(
                    {
                        "fit_version": 2,
                        "scores": {"a": 10, "b": 90},
                        "bindings": [
                            {"cwd": "/a", "key": "a"},
                            {"cwd": "/b", "key": "b"},
                        ],
                    }
                )
            )
            owner.close()
            self.assertEqual(await self.read(connections[1][0]), "granted")
            self.assertEqual(
                sorted(c["state"] for c in self.service.calls.values()),
                ["granted", "queued"],
            )
