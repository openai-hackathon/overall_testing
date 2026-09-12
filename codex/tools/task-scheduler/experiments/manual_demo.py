import argparse
import asyncio
import json
import os
import signal
import socket
from pathlib import Path

from task_evolver import ImportanceTable, PairStore, TaskEvolver
from task_evolver.admission import serve
from task_evolver.expansion import OpenAIExpander

REFERENCE = "開源專案例行文件維護,沒有服務中斷"
INCIDENT = "生產系統故障,服務全面中斷,需要立即修復"


async def run(args, env, expander):
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    for signum in (signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(signum, task.cancel)
    processes = []
    logs = []
    reservations = []
    try:
        for port in args.ports:
            connection = socket.socket()
            reservations.append(connection)
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            connection.bind(("127.0.0.1", port))
        args.state_dir.mkdir(parents=True, exist_ok=True)
        paths = [args.state_dir / role for role in ("incident", "oss")]
        for path in paths:
            path.mkdir(exist_ok=True)
        db = args.state_dir / "pairs.sqlite3"
        snapshot = args.state_dir / "importance.json"
        store = PairStore(db)
        table = ImportanceTable(
            db, snapshot_path=snapshot, bindings=zip(paths, [INCIDENT, REFERENCE])
        )
        try:
            TaskEvolver(store, table, expander, REFERENCE)
            table.publish()
        finally:
            table.conn.close()
            store.conn.close()
        for connection in reservations:
            connection.close()
        for role, port in zip(("incident", "oss"), args.ports[1:]):
            home = args.state_dir / f"{role}-home"
            home.mkdir(mode=0o700, exist_ok=True)
            (home / "config.toml").write_text(
                f"model = {json.dumps(expander.model)}\n"
                f"model_reasoning_effort = {json.dumps(expander.reasoning_effort or 'medium')}\n"
                'model_provider = "live"\napproval_policy = "on-request"\n'
                'sandbox_mode = "workspace-write"\n'
                "[features]\ncode_mode = false\nunified_exec = false\n"
                '[model_providers.live]\nname = "OpenAI scheduling demo"\n'
                'base_url = "https://api.openai.com/v1"\nwire_api = "responses"\n'
                'env_key = "OPENAI_API_KEY"\nrequires_openai_auth = false\n'
                "supports_websockets = false\n"
            )
            log = (args.state_dir / f"{role}-server.log").open("ab")
            logs.append(log)
            process = await asyncio.create_subprocess_exec(
                str(args.codex),
                "app-server",
                "--listen",
                f"ws://127.0.0.1:{port}",
                cwd=args.state_dir / role,
                env=dict(env, CODEX_HOME=str(home)),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            processes.append(process)
            async with asyncio.timeout(30):
                while True:
                    if process.returncode is not None:
                        raise RuntimeError(f"{role} server exited; check {log.name}")
                    try:
                        _reader, writer = await asyncio.open_connection(
                            "127.0.0.1", port
                        )
                        writer.close()
                        await writer.wait_closed()
                        break
                    except OSError:
                        await asyncio.sleep(0.1)
        print(
            f"State: {args.state_dir}\nBackground app servers ready. Ctrl+C stops both.",
            flush=True,
        )
        await serve(
            argparse.Namespace(
                slots=1,
                rate=1.0,
                snapshot=str(snapshot),
                trace=str(args.state_dir / "scheduler.jsonl"),
                db=str(db),
                reference=REFERENCE,
                port=args.ports[0],
            )
        )
    finally:
        for connection in reservations:
            connection.close()
        for process in processes:
            if process.returncode is None:
                process.terminate()
        for process in processes:
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()
                await process.wait()
        for log in logs:
            log.close()


def main():
    parser = argparse.ArgumentParser(
        description="Run a scheduling demo in three terminals."
    )
    parser.add_argument("mode", choices=("serve", "incident", "oss"))
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path.home() / ".local/share/codex-scheduling-demo/repo-demo",
    )
    parser.add_argument(
        "--codex",
        type=Path,
        default=Path(__file__).resolve().parents[3] / "codex-rs/target/debug/codex",
    )
    parser.add_argument(
        "--ports",
        type=int,
        nargs=3,
        default=[8765, 4501, 4502],
        metavar=("SCHEDULER", "INCIDENT", "OSS"),
    )
    args = parser.parse_args()
    args.state_dir = args.state_dir.expanduser().resolve()
    args.codex = args.codex.expanduser().resolve()
    if not args.codex.is_file():
        parser.error("build codex-cli first or pass --codex")
    if len(set(args.ports)) != 3 or any(not 1 <= port <= 65535 for port in args.ports):
        parser.error("--ports requires three distinct ports from 1 to 65535")
    expander = OpenAIExpander()
    if not expander.api_key or not expander.model:
        parser.error(
            "set OPENAI_API_KEY and TASK_EVOLVER_MODEL in codex/.env or the environment"
        )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CODEX_SCHEDULER_")
    }
    env.update(
        OPENAI_API_KEY=expander.api_key,
        TASK_EVOLVER_MODEL=expander.model,
        TASK_EVOLVER_REASONING_EFFORT=expander.reasoning_effort or "medium",
        CODEX_SCHEDULER_SERVICE=f"127.0.0.1:{args.ports[0]}",
    )
    os.environ.update(
        {
            key: env[key]
            for key in (
                "OPENAI_API_KEY",
                "TASK_EVOLVER_MODEL",
                "TASK_EVOLVER_REASONING_EFFORT",
            )
        }
    )
    if args.mode != "serve":
        home = args.state_dir / f"{args.mode}-home"
        if not (home / "config.toml").exists():
            parser.error("start serve with the same --state-dir first")
        port = args.ports[1 if args.mode == "incident" else 2]
        env = dict(env, CODEX_HOME=str(home))
        command = [
            str(args.codex),
            "--remote",
            f"ws://127.0.0.1:{port}",
            "-C",
            str(args.state_dir / args.mode),
            "--no-alt-screen",
        ]
        os.execve(str(args.codex), command, env)
    try:
        asyncio.run(run(args, env, expander))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
