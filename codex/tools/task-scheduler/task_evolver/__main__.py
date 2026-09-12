import argparse
import json
from dataclasses import asdict

from .expansion import OpenAIExpander, normalize_key
from .learning import ask_score, ensure_key
from .store import PairStore
from .table import ImportanceTable
from .workflow import TaskEvolver


def main():
    parser = argparse.ArgumentParser(
        description="Learn task importance from pair scores."
    )
    parser.add_argument("--db", default="task-evolver.sqlite3")
    parser.add_argument("--reference", required=True)
    parser.add_argument("--model")
    parser.add_argument("--snapshot")
    parser.add_argument(
        "--bind", nargs=2, action="append", default=[], metavar=("CWD", "KEY")
    )
    commands = parser.add_subparsers(dest="command", required=True)
    compare = commands.add_parser("compare")
    compare.add_argument("a")
    compare.add_argument("b")
    compare.add_argument("--score", type=float)
    lookup = commands.add_parser("lookup")
    lookup.add_argument("key")
    lookup.add_argument("--no-ask", action="store_true")
    initialize = commands.add_parser("init")
    initialize.add_argument("keywords", nargs="+")
    commands.add_parser("export")
    args = parser.parse_args()
    store = None
    table = None
    try:
        table = ImportanceTable(
            args.db,
            snapshot_path=args.snapshot,
            bindings=[(cwd, normalize_key(key)) for cwd, key in args.bind],
        )
        store = PairStore(args.db)
        evolver = TaskEvolver(store, table, OpenAIExpander(args.model), args.reference)
        if args.command == "export":
            table.publish()
            output = {"fit_version": table.fit_version(), "snapshot": args.snapshot}
        elif args.command == "init":
            comparisons = []
            for key in dict.fromkeys(normalize_key(key) for key in args.keywords):
                learned = ensure_key(evolver, key)
                if learned is not None:
                    comparisons.append(asdict(learned))
            output = {"fit_version": table.fit_version(), "comparisons": comparisons}
        elif args.command == "lookup":
            learned = ensure_key(evolver, args.key) if not args.no_ask else None
            output = {"importance": evolver.lookup(args.key)}
            if learned is not None:
                output["learning"] = asdict(learned)
        else:
            score = args.score
            if score is None:
                score = ask_score(args.a, args.b)
            output = asdict(evolver.answer(args.a, args.b, score))
        print(json.dumps(output, ensure_ascii=False))
    except (ValueError, TypeError, RuntimeError, OSError, EOFError) as exc:
        parser.error(str(exc))
    finally:
        if store is not None:
            store.conn.close()
        if table is not None:
            table.conn.close()


if __name__ == "__main__":
    main()
