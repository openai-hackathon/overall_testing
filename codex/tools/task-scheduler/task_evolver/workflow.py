from dataclasses import dataclass

from .expansion import MAX_KEYS, normalize_key
from .fit import fit_bradley_terry, importance_from_z
from .store import EXPANDED_WEIGHT_CAP
from .validation import validate_pair_values


@dataclass(frozen=True)
class AnswerResult:
    pair_id: int
    fit_version: int
    expanded_count: int
    expansion_error: str | None


class TaskEvolver:
    def __init__(self, store, table, expander, ref_key):
        self.store = store
        self.table = table
        self.expander = expander
        self.ref_key = normalize_key(ref_key)
        with self.store.conn:
            self.store.conn.execute(
                "CREATE TABLE IF NOT EXISTS evolver_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self.store.conn.execute(
                "INSERT OR IGNORE INTO evolver_settings VALUES ('reference', ?)",
                (self.ref_key,),
            )
            saved = self.store.conn.execute(
                "SELECT value FROM evolver_settings WHERE name = 'reference'"
            ).fetchone()[0]
            if saved != self.ref_key:
                raise ValueError("reference key cannot change for an existing evolver")

    def lookup(self, key):
        return self.table.lookup(normalize_key(key))

    def refit(self):
        pairs = self.store.effective()
        adjacent = {self.ref_key: set()}
        for pair in pairs:
            adjacent.setdefault(pair.a_key, set()).add(pair.b_key)
            adjacent.setdefault(pair.b_key, set()).add(pair.a_key)
        reached = {self.ref_key}
        pending = [self.ref_key]
        while pending:
            for key in adjacent[pending.pop()] - reached:
                reached.add(key)
                pending.append(key)
        if reached != set(adjacent):
            raise ValueError("all comparisons must connect to the reference key")
        scores = importance_from_z(fit_bradley_terry(pairs, self.ref_key))
        previous = self.table.fit_version()
        version = self.table.update(scores)
        if version == previous:
            raise RuntimeError("importance table update failed")
        return version

    def answer(self, a_key, b_key, score):
        a_key, b_key = normalize_key(a_key), normalize_key(b_key)
        score, weight = validate_pair_values(score, 1.0)
        known = set(self.store.keys())
        if not known and self.ref_key not in (a_key, b_key):
            raise ValueError("the first comparison must include the reference key")
        if known and (
            self.ref_key not in known or not known.intersection((a_key, b_key))
        ):
            raise ValueError("comparison must connect to the existing reference group")
        pair_id = self.store.record(a_key, b_key, score, "human", weight=weight)
        version = self.refit()
        count = 0
        try:
            known = set(self.store.keys())
            candidates = []
            for key, expand_first in [(a_key, True), (b_key, False)]:
                aliases = self.expander.expand(key)
                if not isinstance(aliases, list) or len(aliases) > MAX_KEYS:
                    raise ValueError("expansion must return at most four keys")
                for alias in aliases:
                    alias = normalize_key(alias)
                    if alias in known:
                        continue
                    known.add(alias)
                    candidates.append(
                        (alias, b_key) if expand_first else (a_key, alias)
                    )
            for first, second in candidates:
                child_id = self.store.record(
                    first,
                    second,
                    score,
                    "expanded",
                    weight=EXPANDED_WEIGHT_CAP / len(candidates),
                    parent_pair_id=pair_id,
                )
                count += child_id is not None
            if count:
                version = self.refit()
        except (
            OSError,
            ValueError,
            TypeError,
            RuntimeError,
        ) as exc:
            return AnswerResult(pair_id, version, count, type(exc).__name__)
        return AnswerResult(pair_id, version, count, None)
