#!/usr/bin/env python3
"""EXACTLY-ONCE: every business event is in the target exactly once, and stays
that way when the world redelivers it."""
from __future__ import annotations

import tempfile
from collections import defaultdict
from pathlib import Path

from . import _world as W
from ._types import Clause, ContractNotEvaluable, Context, not_evaluable

NAME = "exactly_once"

DOC = """INVARIANT   one business key, one row; a late or repeated delivery is
            incorporated exactly once and a re-run adds nothing.
STRESSED BY a retried backfill that appends instead of merging (GT004's 15,649
            doubled keys); a redelivery into a window already repaired.
SHORTCUT    "delete the window and reinsert the snapshot I saw" - it passes a
            duplicate count on the state it was written against, then wipes
            every event that arrived after it, so the late clauses zero it.
            Deleting duplicate rows without changing the write mode fails the
            same way on the next delivery.
LIFTED FROM GT004 G1b (duplicate keys) and G3 (late arrivals).
"""


def _apply_payload(root: Path, payload: dict) -> str:
    notes = []
    for ev in payload.get("events") or []:
        spec = {k: v for k, v in payload.items() if k != "events"}
        spec.update(ev)
        W.plant(root, spec)
        notes.append(f"{ev.get('date')}:{ev.get('rows')}")
    if not notes:
        raise ContractNotEvaluable("late.payload.events is empty")
    return payload.get("mode", "sql") + " " + ",".join(notes)


def _adds(ev: dict) -> int:
    """How many rows this event ADDS.

    Defaults to `rows`.  A payload may also carry RESTATEMENTS - a redelivery of
    a key the target already holds - which replace a row rather than adding one.
    Counting a restatement as an arrival made GT004's incorporation clause
    expect rows a correct merge must not create, and zeroed the gate for every
    baseline including the oracle.
    """
    return int(ev.get("adds", ev["rows"]))


def evaluate(ctx: Context) -> list:
    try:
        return _evaluate(ctx)
    except ContractNotEvaluable as exc:
        return not_evaluable(f"{NAME}.not_evaluable", str(exc))


def _evaluate(ctx: Context) -> list:
    table = ctx.param("table", required=True)
    key = list(ctx.param("business_key", required=True))
    late = ctx.param("late")

    clauses = []

    # ---- the gate that stops this contract paying for a world nobody stressed
    before_key = ctx.param("before_key", "duplicate_keys_before")
    was = ctx.fact(before_key)
    if was is None:
        raise ContractNotEvaluable(
            f"before.{before_key} is not published, so this contract cannot tell "
            f"whether {table} ever carried a duplicate key.  A duplicate-key "
            f"check on a world that never had duplicates is a free point.")
    if int(was) <= 0 and not ctx.param("allow_unstressed", False):
        clauses.append(Clause.make(
            f"{NAME}.stressed", False,
            f"before.{before_key} is {was}: the shipped world carries no "
            f"duplicate business keys, so holding this invariant costs nothing "
            f"and must not be paid for", weight=1.0, fatal=True))
        return clauses
    clauses.append(Clause.make(
        f"{NAME}.stressed", True,
        f"the shipped world carried {int(was):,} duplicate business key(s) - the "
        f"invariant was genuinely stressed", weight=1.0, fatal=True))

    # ---- clause 1 (static): no duplicate business keys anywhere in the target
    con = ctx.connect(read_only=True)
    try:
        if not W.table_exists(con, table):
            return clauses + [Clause.make(
                f"{NAME}.target_exists", False,
                f"{table} does not exist in the graded world - there is nothing "
                f"for a business key to be unique in", weight=1.0, fatal=True)]
        n, sample = W.duplicate_keys(con, table, key)
    finally:
        con.close()
    dup_weight = 0.40 if late else 1.0
    clauses.append(Clause.make(
        f"{NAME}.no_duplicate_keys", n == 0,
        (f"no duplicate business keys anywhere in {table} ({int(was):,} before)"
         if n == 0 else
         f"{n:,} duplicate business key(s) remain in {table} ({int(was):,} "
         f"before); e.g. {sample[:2]}"),
        weight=dup_weight))

    if not late:
        return clauses

    # ---- clauses 2-4 (behavioural): the world redelivers, on a COPY
    payload = late.get("payload") or {}
    predicate = late.get("predicate")
    if not predicate:
        raise ContractNotEvaluable("late.predicate is required: without it the "
                                   "late events cannot be isolated from the rest "
                                   "of the target")
    steps = W.steps_from(ctx)
    date_col = ctx.param("date_column", required=True)
    window = ctx.param("window") or {}
    audit = ctx.param("audit_columns") or []

    expect_by_date = defaultdict(int)
    delivered = 0
    for ev in payload.get("events") or []:
        expect_by_date[str(ev["date"])] += _adds(ev)
        delivered += int(ev["rows"])
    n_late = sum(expect_by_date.values())

    tmp = Path(tempfile.mkdtemp(prefix="contract-exactly-once-"))
    try:
        root = W.copy_world(ctx.world_root, tmp / "world")
        db = W.warehouse_path(root)

        con = W.open_world(db, read_only=True)
        try:
            cols = W.business_columns(con, table, audit)
            before_window = W.per_date(con, table, date_col, cols,
                                       window.get("start"), window.get("end"))
            before_late = W.key_count(con, table, predicate)
        finally:
            con.close()
        if before_late:
            raise ContractNotEvaluable(
                f"late.predicate already matches {before_late} row(s) before the "
                f"payload was applied; it does not isolate the late events")

        note = _apply_payload(root, payload)
        clock = W.advance_clock(root, late.get("advance_to"))

        log1 = W.run_pipeline(root, steps)
        con = W.open_world(db, read_only=True)
        try:
            after_late = W.key_count(con, table, predicate)
            after_window = W.per_date(con, table, date_col, cols,
                                      window.get("start"), window.get("end"))
        finally:
            con.close()

        log2 = W.run_pipeline(root, steps)
        con = W.open_world(db, read_only=True)
        try:
            after2_late = W.key_count(con, table, predicate)
            after2_window = W.per_date(con, table, date_col, cols,
                                       window.get("start"), window.get("end"))
        finally:
            con.close()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    err1 = W.pipeline_failed(log1)
    err2 = W.pipeline_failed(log2)

    inc_ok = after_late == n_late
    if after_late == 0:
        inc_detail = (f"{n_late} late event(s) landed and the pipeline "
                      f"incorporated NONE of them - either the repair reinstates "
                      f"a snapshot captured before they arrived, or the load no "
                      f"longer ingests anything"
                      + (f"; run failed [{err1}]" if err1 else ""))
    elif not inc_ok:
        inc_detail = (f"{after_late} row(s) carry the late-event marker where "
                      f"{n_late} were delivered - incorporated, but not once")
    else:
        inc_detail = (f"all {n_late} late event(s) incorporated exactly once"
                      + (f" ({delivered} delivered, {delivered - n_late} of them "
                         f"restatements of keys the target already held)"
                         if delivered != n_late else ""))
    clauses.append(Clause.make(f"{NAME}.late_incorporated_once", inc_ok,
                               f"{inc_detail} [{note}; {clock}]",
                               weight=0.45, fatal=True,
                               checkpoint=late.get("advance_to")))

    bad, doubled = [], []
    for d in sorted(set(before_window) | set(after_window)):
        b = before_window.get(d, [0, None])[0]
        a = after_window.get(d, [0, None])[0]
        want = b + expect_by_date.get(d, 0)
        if a != want:
            bad.append(f"{d}: {b}->{a}, expected {want}")
            if b and a >= 2 * b:
                doubled.append(d)
    clauses.append(Clause.make(
        f"{NAME}.no_re_doubling", not bad,
        (f"every day of the protected window ends at its pre-arrival count plus "
         f"its own late events ({len(before_window)} day(s))" if not bad else
         (("the window RE-DOUBLED on " + str(doubled[:6]) + "; ") if doubled else "")
         + f"{len(bad)} day(s) off: {bad[:6]}"),
        weight=0.30, checkpoint=late.get("advance_to")))

    stab_ok = (after2_late == n_late
               and all(after_window.get(d) == after2_window.get(d)
                       for d in set(after_window) | set(after2_window)))
    clauses.append(Clause.make(
        f"{NAME}.stable_on_replay", stab_ok,
        ("running again after the arrival changes nothing" if stab_ok else
         f"a repeat run took the late events from {after_late} to {after2_late} "
         f"row(s) and/or moved the window again"
         + (f"; run 2 failed [{err2}]" if err2 else "")),
        weight=0.30, checkpoint=late.get("advance_to")))
    return clauses
