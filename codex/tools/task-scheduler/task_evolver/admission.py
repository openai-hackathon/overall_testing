import argparse
import asyncio
import json
import logging
import math
import time
from pathlib import Path

from .learning import LearningQueue


class AdmissionService:
    def __init__(
        self, slots=1, rate=1.0, snapshot=None, *, trace=None, on_missing=None
    ):
        if type(slots) is not int or slots < 1 or not math.isfinite(rate) or rate < 0:
            raise ValueError("slots must be positive and rate finite and nonnegative")
        self.slots = slots
        self.rate = rate
        self.snapshot = Path(snapshot) if snapshot else None
        self.version = -1
        self.scores = {}
        self.bindings = []
        self.calls = {}
        self.trace = Path(trace) if trace else None
        self.on_missing = on_missing

    def refresh(self):
        if self.snapshot is None:
            return
        try:
            with self.snapshot.open("rb") as file:
                data = file.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError("snapshot exceeds 1 MiB")
            value = json.loads(data)
            version, scores, bindings = (
                value["fit_version"],
                value["scores"],
                value["bindings"],
            )
            if (
                type(version) is not int
                or version < 0
                or not isinstance(scores, dict)
                or not isinstance(bindings, list)
            ):
                raise ValueError("invalid snapshot")
            if any(
                type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 100
                for v in scores.values()
            ):
                raise ValueError("invalid scores")
            parsed = [(Path(item["cwd"]), item["key"]) for item in bindings]
            if any(
                not cwd.is_absolute() or not isinstance(key, str) or not key
                for cwd, key in parsed
            ):
                raise ValueError("invalid bindings")
            parsed = [(cwd.resolve(), key) for cwd, key in parsed]
            if len({cwd for cwd, _ in parsed}) != len(parsed):
                raise ValueError("duplicate bindings")
            if version >= self.version:
                self.version, self.scores, self.bindings = version, scores, parsed
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return

    def key_for(self, cwd):
        cwd = Path(cwd).resolve()
        matches = [
            (path, key)
            for path, key in self.bindings
            if cwd == path or path in cwd.parents
        ]
        return max(matches, key=lambda pair: len(pair[0].parts))[1] if matches else None

    def record(self, event, key, call):
        if self.trace is None:
            return
        now = time.monotonic()
        value = dict(zip(("client_id", "thread_id", "turn_id", "call_id"), key))
        value.update(
            event=event,
            at=now,
            fit_version=self.version,
            cwd=call["cwd"],
            tool=call["tool"],
            wait_seconds=call.get("granted_at", now) - call["queued_at"],
            hold_seconds=now - call["granted_at"] if "granted_at" in call else 0,
        )
        try:
            with self.trace.open("a") as file:
                file.write(json.dumps(value) + "\n")
        except OSError:
            logging.getLogger(__name__).warning("Cannot write scheduler trace")

    def dispatch(self):
        self.refresh()
        running = sum(call["state"] == "granted" for call in self.calls.values())
        now = time.monotonic()

        def priority(call):
            key = self.key_for(call["cwd"])
            return self.scores.get(key, 50.0) + self.rate * (now - call["queued_at"])

        waiting = sorted(
            (c for c in self.calls.values() if c["state"] == "queued"),
            key=lambda c: (-priority(c), c["queued_at"]),
        )
        for call in waiting[: max(0, self.slots - running)]:
            call["state"] = "granted"
            call["granted_at"] = now
            self.record("grant", call["identity"], call)
            call["writer"].write(b'{"status":"granted"}\n')

    async def handle(self, reader, writer):
        owned = None
        terminal = False
        try:
            while line := await reader.readline():
                message = json.loads(line)
                operation = message.get("op")
                if operation == "enqueue":
                    fields = [
                        message.get(name)
                        for name in (
                            "client_id",
                            "thread_id",
                            "turn_id",
                            "call_id",
                            "cwd",
                            "tool",
                        )
                    ]
                    if any(
                        not isinstance(value, str) or not value or len(value) > 4096
                        for value in fields
                    ):
                        raise ValueError("invalid enqueue fields")
                    key = tuple(fields[:4])
                    if owned is not None:
                        if key != owned or terminal:
                            raise ValueError("connection already owns a call")
                        existing = self.calls[key]
                        if fields[4:] != [existing["cwd"], existing["tool"]]:
                            raise ValueError("enqueue payload changed")
                        writer.write(
                            json.dumps({"status": existing["state"]}).encode() + b"\n"
                        )
                    else:
                        if key in self.calls or len(self.calls) >= 4096:
                            raise ValueError(
                                "duplicate call or service capacity reached"
                            )
                        owned = key
                        self.calls[key] = {
                            "identity": key,
                            "cwd": fields[4],
                            "tool": fields[5],
                            "writer": writer,
                            "queued_at": time.monotonic(),
                            "state": "queued",
                        }
                        writer.write(b'{"status":"queued"}\n')
                        self.record("enqueue", key, self.calls[key])
                        self.dispatch()
                        task_key = self.key_for(fields[4])
                        if (
                            self.on_missing is not None
                            and task_key is not None
                            and task_key not in self.scores
                        ):
                            self.on_missing(task_key)
                elif operation in ("cancel", "release") and owned is not None:
                    removed = self.calls.pop(owned, None)
                    if removed is not None:
                        self.record(operation, owned, removed)
                    terminal = True
                    writer.write(b'{"status":"released"}\n')
                    self.dispatch()
                else:
                    raise ValueError("invalid operation")
                await writer.drain()
        except (ValueError, TypeError, AttributeError, ConnectionError, OSError):
            writer.write(b'{"status":"error"}\n')
        finally:
            if owned is not None and not terminal:
                removed = self.calls.pop(owned, None)
                if removed is not None:
                    self.record("disconnect", owned, removed)
                self.dispatch()
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass


async def serve(args):
    service = AdmissionService(args.slots, args.rate, args.snapshot, trace=args.trace)
    if args.db:
        if not args.reference or not args.snapshot:
            raise ValueError("learning requires --reference and --snapshot")
        service.refresh()
        bindings = [(str(path), key) for path, key in service.bindings]
        if not bindings:
            raise ValueError("learning requires a published snapshot with bindings")
        loop = asyncio.get_running_loop()
        learner = LearningQueue(
            args.db,
            args.reference,
            args.snapshot,
            bindings,
            lambda: loop.call_soon_threadsafe(service.dispatch),
        )
        service.on_missing = learner.request
    server = await asyncio.start_server(
        service.handle, "127.0.0.1", args.port, limit=65536
    )
    print(json.dumps({"port": server.sockets[0].getsockname()[1]}), flush=True)
    async with server:
        await server.serve_forever()


def main():
    parser = argparse.ArgumentParser(description="Shared local tool admission service")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--slots", type=int, default=1)
    parser.add_argument("--rate", type=float, default=1.0)
    parser.add_argument("--snapshot")
    parser.add_argument("--trace")
    parser.add_argument("--db")
    parser.add_argument("--reference")
    args = parser.parse_args()
    asyncio.run(serve(args))


if __name__ == "__main__":
    main()
