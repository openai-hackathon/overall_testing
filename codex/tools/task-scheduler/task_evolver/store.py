import sqlite3
from dataclasses import dataclass

from .validation import validate_pair_values

SOURCE_RANK = {"human": 0, "seed": 1, "expanded": 2}
EXPANDED_WEIGHT_CAP = 0.2


@dataclass(frozen=True)
class Pair:
    pair_id: int
    a_key: str
    b_key: str
    p_a_wins: float
    weight: float
    source: str
    parent_pair_id: int | None
    revision: int


class PairStore:
    def __init__(self, path=":memory:"):
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS pairs ("
            "pair_id INTEGER PRIMARY KEY, a_key TEXT NOT NULL, b_key TEXT NOT NULL,"
            "p_a_wins REAL NOT NULL, weight REAL NOT NULL, source TEXT NOT NULL,"
            "parent_pair_id INTEGER, revision INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1)"
        )
        self.conn.commit()

    @staticmethod
    def canonical(a_key, b_key, p_a_wins):
        if a_key <= b_key:
            return a_key, b_key, p_a_wins
        return b_key, a_key, 1.0 - p_a_wins

    def record(self, a_key, b_key, p_a_wins, source, weight=1.0, parent_pair_id=None):
        if source not in SOURCE_RANK:
            raise ValueError(f"unknown source {source!r}")
        if a_key == b_key:
            raise ValueError("pair keys must differ")
        p_a_wins, weight = validate_pair_values(p_a_wins, weight)
        a, b, p = self.canonical(a_key, b_key, p_a_wins)
        if source != "expanded" and parent_pair_id is not None:
            raise ValueError("only expanded pairs may have a parent")
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            cur = self.conn.cursor()
            if source == "expanded":
                parent = cur.execute(
                    "SELECT a_key, b_key, source FROM pairs WHERE pair_id = ? AND active = 1",
                    (parent_pair_id,),
                ).fetchone()
                if parent is None or parent[2] not in ("human", "seed"):
                    raise ValueError(
                        "expanded parent must be an active human or seed pair"
                    )
                sources = cur.execute(
                    "SELECT source FROM pairs WHERE a_key = ? AND b_key = ? AND active = 1",
                    parent[:2],
                ).fetchall()
                if any(
                    SOURCE_RANK[source] < SOURCE_RANK[parent[2]]
                    for (source,) in sources
                ):
                    raise ValueError("expanded parent must be the effective source")
                used = cur.execute(
                    "SELECT COALESCE(SUM(weight), 0) FROM pairs WHERE parent_pair_id = ? AND active = 1",
                    (parent_pair_id,),
                ).fetchone()[0]
                weight = min(weight, max(EXPANDED_WEIGHT_CAP - used, 0.0))
                if weight <= 0:
                    return None
            else:
                prior = cur.execute(
                    "SELECT pair_id, source FROM pairs WHERE a_key = ? AND b_key = ? AND active = 1",
                    (a, b),
                ).fetchall()
                for old_id, old_source in prior:
                    if SOURCE_RANK[old_source] >= SOURCE_RANK[source]:
                        cur.execute(
                            "UPDATE pairs SET active = 0 WHERE pair_id = ? OR parent_pair_id = ?",
                            (old_id, old_id),
                        )
            revision = cur.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 FROM pairs WHERE a_key = ? AND b_key = ? AND source = ?",
                (a, b, source),
            ).fetchone()[0]
            cur.execute(
                "INSERT INTO pairs (a_key, b_key, p_a_wins, weight, source, parent_pair_id, revision)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (a, b, p, weight, source, parent_pair_id, revision),
            )
            return cur.lastrowid

    def effective(self):
        rows = self.conn.execute(
            "SELECT pair_id, a_key, b_key, p_a_wins, weight, source, parent_pair_id, revision"
            " FROM pairs WHERE active = 1 ORDER BY pair_id"
        ).fetchall()
        best = {}
        for row in rows:
            pair = Pair(*row)
            rank = SOURCE_RANK[pair.source]
            key = (pair.a_key, pair.b_key)
            current = best.get(key)
            if current is None or rank < SOURCE_RANK[current[0].source]:
                best[key] = [pair]
            elif rank == SOURCE_RANK[current[0].source] and pair.source == "expanded":
                current.append(pair)
        return [pair for pairs in best.values() for pair in pairs]

    def keys(self):
        rows = self.conn.execute(
            "SELECT a_key FROM pairs WHERE active = 1 UNION SELECT b_key FROM pairs WHERE active = 1"
        )
        return sorted(row[0] for row in rows)
