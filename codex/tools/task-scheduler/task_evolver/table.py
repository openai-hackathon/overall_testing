import json
import os
import sqlite3
import tempfile
from pathlib import Path

from .validation import finite_number


class ImportanceTable:
    def __init__(self, path=":memory:", *, snapshot_path=None, bindings=()):
        self.snapshot_path = Path(snapshot_path) if snapshot_path else None
        if (
            self.snapshot_path is not None
            and path != ":memory:"
            and self.snapshot_path.resolve() == Path(path).resolve()
        ):
            raise ValueError("snapshot must not replace the SQLite database")
        self.bindings = [
            {"cwd": str(Path(cwd).resolve()), "key": key} for cwd, key in bindings
        ]
        if len({item["cwd"] for item in self.bindings}) != len(self.bindings):
            raise ValueError("each working directory must have one task key")
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS scores (key TEXT PRIMARY KEY, importance REAL NOT NULL, fit_version INTEGER NOT NULL)"
        )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value INTEGER NOT NULL)"
        )
        self.conn.commit()

    def fit_version(self):
        row = self.conn.execute(
            "SELECT value FROM meta WHERE name = 'fit_version'"
        ).fetchone()
        return row[0] if row else 0

    def lookup(self, key):
        row = self.conn.execute(
            "SELECT importance FROM scores WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    def update(self, importance):
        version = self.fit_version() + 1
        try:
            importance = {
                key: finite_number(value) for key, value in importance.items()
            }
            if any(not 0 <= value <= 100 for value in importance.values()):
                raise ValueError("importance must be between 0 and 100")
            with self.conn:
                self.conn.execute("DELETE FROM scores")
                self.conn.executemany(
                    "INSERT INTO scores (key, importance, fit_version) VALUES (?, ?, ?)",
                    [(k, v, version) for k, v in importance.items()],
                )
                self.conn.execute(
                    "INSERT OR REPLACE INTO meta (name, value) VALUES ('fit_version', ?)",
                    (version,),
                )
        except (sqlite3.Error, TypeError, ValueError):
            return self.fit_version()
        if self.snapshot_path is not None:
            self.publish()
        return version

    def publish(self):
        if self.snapshot_path is None:
            raise ValueError("snapshot path is required")
        with self.conn:
            self.conn.execute("BEGIN")
            version = self.fit_version()
            scores = dict(self.conn.execute("SELECT key, importance FROM scores"))
        payload = json.dumps(
            {
                "fit_version": version,
                "scores": scores,
                "bindings": self.bindings,
            },
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        if len(payload) > 1024 * 1024:
            raise ValueError("importance snapshot exceeds 1 MiB")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.snapshot_path.parent, delete=False
            ) as file:
                temporary = Path(file.name)
                file.write(payload)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.snapshot_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
