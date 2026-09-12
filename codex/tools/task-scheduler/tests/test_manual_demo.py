import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from task_evolver import ImportanceTable, PairStore, TaskEvolver

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "experiments/manual_demo.py"
REFERENCE = "開源專案例行文件維護,沒有服務中斷"
INCIDENT = "生產系統故障,服務全面中斷,需要立即修復"


@unittest.skipUnless(os.name == "posix", "The SSH demo supports Linux and macOS")
class ManualDemoTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.binary = self.root / "codex"
        self.binary.write_text(
            f"#!{sys.executable}\n"
            "import socket\nimport sys\n"
            "port = int(sys.argv[-1].rsplit(':', 1)[1])\n"
            "server = socket.socket()\n"
            "server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
            "server.bind(('127.0.0.1', port))\nserver.listen()\n"
            "while True:\n    client, _ = server.accept()\n    client.close()\n"
        )
        self.binary.chmod(0o700)
        self.sockets = [socket.socket() for _ in range(3)]
        for connection in self.sockets:
            connection.bind(("127.0.0.1", 0))
            self.addCleanup(connection.close)
        self.ports = [connection.getsockname()[1] for connection in self.sockets]
        self.command = [
            sys.executable,
            str(SCRIPT),
            "serve",
            "--codex",
            str(self.binary),
            "--state-dir",
            str(self.root / "state"),
            "--ports",
            *map(str, self.ports),
        ]
        self.env = dict(
            os.environ,
            PYTHONPATH=str(ROOT),
            OPENAI_API_KEY="demo-test-secret",
            TASK_EVOLVER_MODEL="test-model",
        )

    def test_shutdown_and_restart_preserve_learning(self):
        for connection in self.sockets:
            connection.close()
        for stop in (signal.SIGINT, signal.SIGHUP):
            with self.subTest(signal=stop):
                process = subprocess.Popen(
                    self.command,
                    env=self.env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    output = b""
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        while b'{"port":' not in output:
                            self.assertTrue(
                                selector.select(30), "demo startup timed out"
                            )
                            chunk = os.read(process.stdout.fileno(), 4096)
                            if not chunk:
                                self.fail(process.stderr.read())
                            output += chunk
                    for port in self.ports:
                        with socket.create_connection(("127.0.0.1", port), timeout=2):
                            pass
                    state = self.root / "state"
                    snapshot = json.loads((state / "importance.json").read_text())
                    if stop == signal.SIGINT:
                        self.assertEqual(snapshot["scores"], {})
                    else:
                        self.assertGreater(
                            snapshot["scores"][INCIDENT], snapshot["scores"][REFERENCE]
                        )
                    process.send_signal(stop)
                    process.communicate(timeout=15)
                    self.assertEqual(process.returncode, 0)
                    for port in self.ports:
                        with self.assertRaises(OSError):
                            socket.create_connection(("127.0.0.1", port), timeout=0.2)
                    if stop == signal.SIGINT:
                        store = PairStore(state / "pairs.sqlite3")
                        table = ImportanceTable(state / "pairs.sqlite3")
                        try:
                            store.record(INCIDENT, REFERENCE, 0.9, "human", weight=1.0)
                            TaskEvolver(store, table, None, REFERENCE).refit()
                        finally:
                            store.conn.close()
                            table.conn.close()
                    for config in state.glob("*-home/config.toml"):
                        self.assertNotIn("demo-test-secret", config.read_text())
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.communicate(timeout=15)

    def test_occupied_port_does_not_create_state(self):
        result = subprocess.run(
            self.command,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "state").exists())
