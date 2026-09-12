import queue
import threading
from dataclasses import asdict

from .expansion import OpenAIExpander, normalize_key
from .store import PairStore
from .table import ImportanceTable
from .validation import validate_pair_values
from .workflow import TaskEvolver


def ask_score(first, second):
    while True:
        try:
            value = float(
                input(f"A: {first}\nB: {second}\nScore 0–1 (1=A, 0=B, 0.5=equal): ")
            )
            return validate_pair_values(value, 1.0)[0]
        except ValueError:
            print("Enter a finite number from 0 to 1.", flush=True)


def ensure_key(evolver, key, ask=ask_score):
    key = normalize_key(key)
    if evolver.lookup(key) is not None:
        return None
    if key == evolver.ref_key:
        evolver.refit()
        return None
    return evolver.answer(key, evolver.ref_key, ask(key, evolver.ref_key))


class LearningQueue:
    def __init__(self, db, reference, snapshot, bindings, updated):
        self.jobs = queue.Queue(maxsize=4096)
        self.pending = set()
        self.lock = threading.Lock()
        self.closed = False
        self.thread = threading.Thread(
            target=self.run,
            args=(db, reference, snapshot, bindings, updated),
            daemon=True,
        )
        self.thread.start()

    def request(self, key):
        with self.lock:
            if self.closed or key in self.pending:
                return
            try:
                self.jobs.put_nowait(key)
                self.pending.add(key)
            except queue.Full:
                return

    def run(self, db, reference, snapshot, bindings, updated):
        store = None
        table = None
        try:
            store = PairStore(db)
            table = ImportanceTable(db, snapshot_path=snapshot, bindings=bindings)
            evolver = TaskEvolver(store, table, OpenAIExpander(), reference)
            while True:
                key = self.jobs.get()
                try:
                    result = ensure_key(evolver, key)
                    print(asdict(result) if result else {"known": key}, flush=True)
                    updated()
                except EOFError:
                    return
                except (OSError, ValueError, RuntimeError) as exc:
                    print({"learning_error": type(exc).__name__}, flush=True)
                finally:
                    with self.lock:
                        self.pending.discard(key)
        except (OSError, ValueError, RuntimeError) as exc:
            print({"learning_error": type(exc).__name__}, flush=True)
        finally:
            with self.lock:
                self.closed = True
            if store is not None:
                store.conn.close()
            if table is not None:
                table.conn.close()
