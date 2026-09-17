#!/usr/bin/env python3
"""
Types shared by every contract in this package.

A CONTRACT is an invariant a data platform must keep while the world moves.
It is not a task and it is not a defect: it is the thing a stressor stresses.
A composed task declares which contracts hold; the engine evaluates them.

Two rules this file exists to enforce:

  1. NOTHING in `tasks/contracts/**` imports from a task directory.  The
     contracts are lifted FROM the tasks, and a library that imports its own
     callers is not a library.  Everything the tasks taught us is COPIED here
     with the reason attached (see _score.py's header for the scoring rule, and
     _world.py's for the ATTACH rule).

  2. ALWAYS aliased ATTACH (F622, agent.md 9.2.1).  duckdb.connect(path) names
     the catalog after the file stem, so warehouse.duckdb holding a schema
     called `warehouse` makes catalog == schema and EVERY two-part reference
     raises `Binder Error: Ambiguous reference to catalog or schema "warehouse"`.
     `Context.query` is the only read path in this package and it always does
     connect(":memory:") + ATTACH AS wh (READ_ONLY) + USE wh.warehouse.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WAREHOUSE_RELPATH = Path("var") / "warehouse" / "warehouse.duckdb"


class ContractNotEvaluable(Exception):
    """The contract cannot be decided from what it was given.

    Raised for a missing parameter, an absent table, a ledger that is not there.
    Every contract turns this into ONE clause with passed=False and fatal=False:
    a contract that could not be evaluated must be VISIBLE in the score rather
    than silently absent, and it must not zero a composition that other
    contracts are still carrying.
    """


@dataclass
class Clause:
    """One assertion inside a contract.

    Shape-compatible with tasks/*/tests/probes/_common.py::Clause under the
    mapping (id -> name, passed -> ok, max -> weight); _score.py folds clauses
    by exactly the rule that file folds them by, and
    tests/test_score_equivalence.py proves the two agree on the same inputs.

    points/max  a clause is binary: points == max when it passed, 0.0 when it
                did not.  `max` doubles as the clause's WEIGHT inside the fold.
    fatal       a gate.  It never PAYS - fatal clauses are excluded from the
                weight base - and a failed one takes the whole contract to zero.
                Measured on GT004: paying for clauses a no-op satisfies by
                construction handed the no_op baseline 6.25 of 33 points.
    checkpoint  which point on the trajectory this clause was decided at
                (a receipt's vnow, a night index, None for end-state).  A good
                final state must not be able to erase a bad intermediate
                publication, so the checkpoint travels with the clause.
    """
    id: str
    passed: bool
    points: float
    max: float
    detail: str
    fatal: bool = False
    checkpoint: str | None = None

    @classmethod
    def make(cls, cid: str, passed: bool, detail: str, *, weight: float = 1.0,
             fatal: bool = False, checkpoint: str | None = None) -> "Clause":
        return cls(id=cid, passed=bool(passed),
                   points=float(weight) if passed else 0.0,
                   max=float(weight), detail=detail, fatal=bool(fatal),
                   checkpoint=checkpoint)

    def as_dict(self) -> dict:
        return {"id": self.id, "passed": self.passed,
                "points": round(float(self.points), 4),
                "max": round(float(self.max), 4), "detail": self.detail,
                "fatal": self.fatal, "checkpoint": self.checkpoint}


def not_evaluable(cid: str, message: str, *, weight: float = 1.0) -> list:
    """The ONE shape every contract uses to say "I could not decide this"."""
    return [Clause.make(cid, False, message, weight=weight, fatal=False)]


@dataclass
class Context:
    """Everything a contract is allowed to look at.

    world_root  the post-episode world root (contains var/ and workspace/).
                Contracts NEVER write to it; the ones that execute anything copy
                it first (see _world.copy_world).
    warehouse   the primary duckdb.  Defaults to world_root/var/warehouse/
                warehouse.duckdb, which is the deployed path agent.md 9.2.1
                freezes.
    before      pre-episode reference facts, loaded from a fixture_contract-like
                JSON.  This is what makes "no free points" mechanical: a
                contract that cannot see that its invariant was STRESSED refuses
                to pay for holding.
    receipts    the consumer ledger, oldest first.  Rows are
                {seq, vnow, day, job, target, kind, canonical_hash, row_count,
                 sample, consumer}.  May be empty; contracts that need it say so
                rather than passing vacuously.
    params      the task's [composition.contracts.<name>] block for THIS
                contract.
    """
    world_root: Path
    warehouse: Path | None = None
    before: dict = field(default_factory=dict)
    receipts: list = field(default_factory=list)
    params: dict = field(default_factory=dict)
    _con: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        self.world_root = Path(self.world_root)
        self.warehouse = (Path(self.warehouse) if self.warehouse
                          else self.world_root / WAREHOUSE_RELPATH)

    # ------------------------------------------------------------ parameters
    def param(self, key: str, default: Any = None, *, required: bool = False) -> Any:
        """A dotted lookup into `params`.  A required key that is absent raises
        ContractNotEvaluable naming the key - never defaulted around, because a
        probe that guessed a number once graded something other than what it
        said it graded."""
        node: Any = self.params
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                if required:
                    raise ContractNotEvaluable(
                        f"params.{key} is not set; this contract cannot be "
                        f"decided without it")
                return default
            node = node[part]
        if required and node in (None, "", [], {}):
            raise ContractNotEvaluable(f"params.{key} is empty")
        return node

    def fact(self, key: str, *, required: bool = False) -> Any:
        """A dotted lookup into `before` - the pre-episode reference facts."""
        node: Any = self.before
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                if required:
                    raise ContractNotEvaluable(
                        f"before.{key} is not published by the fixture contract, "
                        f"so this contract cannot tell whether its invariant was "
                        f"ever stressed")
                return None
            node = node[part]
        return node

    def for_contract(self, name: str) -> "Context":
        """The same world, with `params` narrowed to one contract's block."""
        block = (self.params or {}).get(name)
        return Context(world_root=self.world_root, warehouse=self.warehouse,
                       before=self.before, receipts=self.receipts,
                       params=dict(block or {}))

    # --------------------------------------------------------------- duckdb
    def connect(self, path: Path | None = None, *, read_only: bool = True):
        """The ONE way this package opens a warehouse.  See the module header."""
        import duckdb
        target = Path(path) if path else self.warehouse
        if not Path(target).is_file():
            raise ContractNotEvaluable(f"no warehouse at {target}")
        con = duckdb.connect(":memory:", config={"threads": 1})
        mode = " (READ_ONLY)" if read_only else ""
        con.execute(f"ATTACH '{Path(target).as_posix()}' AS wh{mode}")
        try:
            con.execute("USE wh.warehouse")
        except Exception:                                   # noqa: BLE001
            # a world whose primary schema is not called `warehouse` is legal;
            # every reference this package emits is three-part anyway.
            con.execute("USE wh")
        return con

    def query(self, sql: str, args: list | None = None) -> list:
        """Read-only, aliased ATTACH, one cached connection per Context."""
        if self._con is None:
            self._con = self.connect(read_only=True)
        return self._con.execute(sql, args or []).fetchall()

    def query_one(self, sql: str, args: list | None = None):
        rows = self.query(sql, args)
        return rows[0] if rows else None

    def close(self) -> None:
        if self._con is not None:
            try:
                self._con.close()
            finally:
                self._con = None

    # ----------------------------------------------------------- loading
    @staticmethod
    def load_before(path: Path) -> dict:
        """Pre-episode reference facts from a fixture_contract-like JSON file."""
        return json.loads(Path(path).read_text(encoding="utf-8"))

    @staticmethod
    def load_receipts(path: Path) -> list:
        """The consumer ledger.  JSONL (one receipt per line) or a JSON list.

        A MISSING ledger returns [] and the contracts that need it report
        "no receipts: contract not evaluable".  It is never confused with an
        empty one: both are visible, neither passes.
        """
        p = Path(path)
        if not p.is_file():
            return []
        text = p.read_text(encoding="utf-8").strip()
        if not text:
            return []
        if text.lstrip().startswith("["):
            rows = json.loads(text)
        else:
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        return sorted(rows, key=lambda r: int(r.get("seq", 0)))
