import argparse
import asyncio
import json
import os
import shlex
import sys
import tempfile
from functools import partial
from pathlib import Path
from unittest.mock import Mock

from task_evolver import ImportanceTable, PairStore, TaskEvolver
from task_evolver.admission import AdmissionService


class Client:
    def __init__(self, process):
        self.process = process
        self.pending = {}
        self.events = asyncio.Queue()
        self.counter = 0
        self.reader = asyncio.create_task(self.read())

    async def read(self):
        while line := await self.process.stdout.readline():
            message = json.loads(line)
            if "id" in message and message["id"] in self.pending:
                self.pending.pop(message["id"]).set_result(message)
            else:
                self.events.put_nowait(message)

    async def rpc(self, method, params):
        self.counter += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.counter] = future
        self.process.stdin.write(
            (
                json.dumps({"id": self.counter, "method": method, "params": params})
                + "\n"
            ).encode()
        )
        await self.process.stdin.drain()
        result = await asyncio.wait_for(future, 30)
        if "error" in result:
            raise RuntimeError(result["error"])
        return result["result"]

    async def completed(self):
        while True:
            event = await asyncio.wait_for(self.events.get(), 30)
            if event.get("method") == "turn/completed":
                return event["params"]["turn"]["status"]

    async def close(self):
        self.process.stdin.close()
        await asyncio.wait_for(self.process.wait(), 30)
        await self.reader


async def model(reader, writer, outputs):
    header = await reader.readuntil(b"\r\n\r\n")
    length = next(
        int(line.split(b":", 1)[1])
        for line in header.split(b"\r\n")
        if line.lower().startswith(b"content-length:")
    )
    request = json.loads(await reader.readexactly(length))
    events = [{"type": "response.created", "response": {"id": "mock-response"}}]
    items = request.get("input", [])
    last_user = max(
        (index for index, item in enumerate(items) if item.get("role") == "user"),
        default=-1,
    )
    completed = [
        item
        for item in items[last_user + 1 :]
        if item.get("type") == "function_call_output"
    ]
    outputs.extend(str(item["output"]) for item in completed)
    if not completed:
        events.append(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": "demo-call",
                    "name": "exec_command",
                    "arguments": json.dumps(
                        {
                            "cmd": shlex.join(
                                [
                                    sys.executable,
                                    "-c",
                                    "import time; time.sleep(0.2); print('scheduler-demo-ok')",
                                ]
                            ),
                            "yield_time_ms": 1000,
                            "max_output_tokens": 1000,
                        }
                    ),
                },
            }
        )
    events.append(
        {
            "type": "response.completed",
            "response": {
                "id": "mock-response",
                "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            },
        }
    )
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
    writer.write(
        f"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        + body
    )
    await writer.drain()
    writer.close()
    await writer.wait_closed()


async def wait_calls(service, count):
    async with asyncio.timeout(30):
        while len(service.calls) != count:
            await asyncio.sleep(0.01)


async def blocker(port):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        (
            json.dumps(
                {
                    "op": "enqueue",
                    "client_id": "blocker",
                    "thread_id": "blocker",
                    "turn_id": "blocker",
                    "call_id": "blocker",
                    "cwd": "/blocker",
                    "tool": "controlled",
                }
            )
            + "\n"
        ).encode()
    )
    await writer.drain()
    assert json.loads(await reader.readline())["status"] == "queued"
    assert json.loads(await reader.readline())["status"] == "granted"
    return writer


async def run(binary, output):
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        paths = [root / "incident", root / "oss"]
        for path in paths:
            path.mkdir()
        snapshot = root / "importance.json"
        store = PairStore()
        table = ImportanceTable(
            snapshot_path=snapshot, bindings=zip(paths, ["incident", "oss"])
        )
        expander = Mock()
        expander.expand.side_effect = lambda key: [key + " equivalent"]
        evolver = TaskEvolver(store, table, expander, "oss")
        evolver.answer("incident", "oss", 0.1)
        trace = output / "app-server-trace.jsonl"
        trace.write_text("")
        service = AdmissionService(snapshot=snapshot, trace=trace)
        admission = await asyncio.start_server(service.handle, "127.0.0.1", 0)
        outputs = []
        provider = await asyncio.start_server(
            partial(model, outputs=outputs), "127.0.0.1", 0, limit=2**24
        )
        port = admission.sockets[0].getsockname()[1]
        provider_port = provider.sockets[0].getsockname()[1]
        clients = []
        logs = []
        hold = None
        try:
            for index, path in enumerate(paths):
                home = root / f"client-{index}"
                home.mkdir()
                (home / "config.toml").write_text(
                    f'model = "gpt-5.1"\nmodel_provider = "mock"\napproval_policy = "never"\nsandbox_mode = "danger-full-access"\n[features]\ncode_mode = false\nunified_exec = false\n[model_providers.mock]\nname = "mock"\nbase_url = "http://127.0.0.1:{provider_port}/v1"\nwire_api = "responses"\nrequires_openai_auth = false\nsupports_websockets = false\n'
                )
                env = {
                    key: value
                    for key, value in os.environ.items()
                    if not key.startswith("CODEX_SCHEDULER_")
                }
                env.update(
                    CODEX_HOME=str(home), CODEX_SCHEDULER_SERVICE=f"127.0.0.1:{port}"
                )
                log = (output / f"app-server-{index}.log").open("w")
                logs.append(log)
                process = await asyncio.create_subprocess_exec(
                    str(binary),
                    "app-server",
                    "--listen",
                    "stdio://",
                    env=env,
                    cwd=path,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=log,
                    limit=2**24,
                )
                client = Client(process)
                clients.append(client)
                await client.rpc(
                    "initialize",
                    {
                        "clientInfo": {"name": "scheduler-demo", "version": "1"},
                        "capabilities": {"experimentalApi": True},
                    },
                )
                process.stdin.write(b'{"method":"initialized"}\n')
                thread = await client.rpc(
                    "thread/start",
                    {
                        "cwd": str(path),
                        "model": "gpt-5.1",
                        "modelProvider": "mock",
                        "approvalPolicy": "never",
                        "sandbox": "danger-full-access",
                    },
                )
                client.thread_id = thread["thread"]["id"]
            hold = await blocker(port)
            for index, client in enumerate(reversed(clients)):
                await client.rpc(
                    "turn/start",
                    {
                        "threadId": client.thread_id,
                        "input": [
                            {
                                "type": "text",
                                "text": "Run the controlled shell tool once.",
                                "text_elements": [],
                            }
                        ],
                    },
                )
                await wait_calls(service, index + 2)
            assert [service.key_for(str(path)) for path in paths] == ["incident", "oss"]
            queued = {key: call["queued_at"] for key, call in service.calls.items()}
            result = evolver.answer("incident", "oss", 0.9)
            service.dispatch()
            assert queued == {
                key: call["queued_at"] for key, call in service.calls.items()
            }
            hold.close()
            await hold.wait_closed()
            hold = None
            statuses = await asyncio.gather(*(client.completed() for client in clients))
            assert statuses == ["completed", "completed"], statuses
            await wait_calls(service, 0)
            events = [json.loads(line) for line in trace.read_text().splitlines()]
            grants = [
                event
                for event in events
                if event["event"] == "grant" and event["client_id"] != "blocker"
            ]
            assert [event["cwd"] for event in grants] == list(map(str, paths)), grants
            assert len({event["client_id"] for event in grants}) == 2
            hold = await blocker(port)
            turn = await clients[0].rpc(
                "turn/start",
                {
                    "threadId": clients[0].thread_id,
                    "input": [
                        {"type": "text", "text": "Run again.", "text_elements": []}
                    ],
                },
            )
            await clients[1].rpc(
                "turn/start",
                {
                    "threadId": clients[1].thread_id,
                    "input": [
                        {"type": "text", "text": "Run again.", "text_elements": []}
                    ],
                },
            )
            await wait_calls(service, 3)
            await clients[0].rpc(
                "turn/interrupt",
                {"threadId": clients[0].thread_id, "turnId": turn["turn"]["id"]},
            )
            assert await clients[0].completed() == "interrupted"
            await wait_calls(service, 2)
            hold.close()
            await hold.wait_closed()
            hold = None
            await wait_calls(service, 0)
            assert await clients[1].completed() == "completed"
            assert len(outputs) == 3 and all(
                "scheduler-demo-ok" in value for value in outputs
            ), outputs
            events = [json.loads(line) for line in trace.read_text().splitlines()]
            assert not any(
                event["event"] == "grant" and event["turn_id"] == turn["turn"]["id"]
                for event in events
            )
            report = {
                "app_server_processes": 2,
                "model": "fixed local SSE mock",
                "lm_expansion": "deterministic fake",
                "fit_version": result.fit_version,
                "grant_order": ["incident", "oss"],
                "waiting_timestamps_preserved": True,
                "queued_cancellation": "passed",
                "other_waiter_after_cancellation": "passed",
                "successful_tool_outputs": len(outputs),
                "statuses": statuses,
            }
            (output / "app-server-report.json").write_text(
                json.dumps(report, indent=2) + "\n"
            )
            print(json.dumps(report), flush=True)
        finally:
            if hold is not None:
                hold.close()
                await hold.wait_closed()
            await asyncio.gather(*(client.close() for client in clients))
            admission.close()
            provider.close()
            await admission.wait_closed()
            await provider.wait_closed()
            store.conn.close()
            table.conn.close()
            for log in logs:
                log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.codex.resolve(), args.output))
