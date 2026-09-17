#!/usr/bin/env python3
"""LIVENESS: the pipeline still ingests tomorrow's data, and re-running it
changes nothing."""
from __future__ import annotations

import tempfile
from pathlib import Path

from . import _world as W
from ._types import Clause, ContractNotEvaluable, Context, not_evaluable

NAME = "liveness"

DOC = """INVARIANT   a delivery never seen lands on the next real run, exactly once,
            disturbing nothing it has nothing to do with; a second identical
            run changes no business column.
STRESSED BY any repair to a loader - watermark, merge key, filter, quality
            rule - because "stop loading" satisfies every check-shaped gate.
SHORTCUT    the permanent skip: a watermark pinned in the past, WHERE 1=0, a
            guard that decides the table is already loaded, a rule tightened
            until nothing passes.  All are idempotent by construction and
            repair nothing, so the ingestion clause is FATAL.
LIFTED FROM GT004 G2; GT006 G1c; GT007's canary-liveness rule.
"""


def evaluate(ctx: Context) -> list:
    try:
        return _evaluate(ctx)
    except ContractNotEvaluable as exc:
        return not_evaluable(f"{NAME}.not_evaluable", str(exc))


def _evaluate(ctx: Context) -> list:
    runner = ctx.param("runner", "pipeline")
    if runner != "pipeline":
        raise ContractNotEvaluable(
            f"runner={runner!r} is declared but only 'pipeline' is implemented. "
            f"Triggering through the scheduler reports success WITHOUT executing "
            f"for every command that is not dbt_build/dbt_run, so a run count "
            f"there would prove nothing ran at all.")

    table = ctx.param("table", required=True)
    date_col = ctx.param("date_column", required=True)
    spec = ctx.param("plant", required=True)
    predicate = spec.get("predicate")
    if not predicate:
        raise ContractNotEvaluable("plant.predicate is required: without it the "
                                   "planted delivery cannot be isolated")
    rows = int(spec.get("rows", 0))
    if rows <= 0:
        raise ContractNotEvaluable("plant.rows must be > 0; a delivery of nothing "
                                   "cannot demonstrate that anything is alive")
    steps = W.steps_from(ctx)
    window = ctx.param("window") or {}
    audit = ctx.param("audit_columns") or []

    tmp = Path(tempfile.mkdtemp(prefix="contract-liveness-"))
    try:
        root = W.copy_world(ctx.world_root, tmp / "world")
        db = W.warehouse_path(root)

        con = W.open_world(db, read_only=True)
        try:
            if not W.table_exists(con, table):
                return [Clause.make(
                    f"{NAME}.ingests", False,
                    f"{table} does not exist in the graded world - there is "
                    f"nothing for a pipeline to be alive about",
                    weight=1.0, fatal=True)]
            cols = W.business_columns(con, table, audit)
            before_window = W.per_date(con, table, date_col, cols,
                                       window.get("start"), window.get("end"))
            before_all = W.per_date(con, table, date_col, cols)
            before_planted = W.key_count(con, table, predicate)
        finally:
            con.close()

        if spec.get("date") and str(spec["date"]) in before_all:
            raise ContractNotEvaluable(
                f"plant.date {spec['date']} ALREADY exists in {table} - a "
                f"partition the pipeline has seen cannot prove liveness")
        if before_planted:
            raise ContractNotEvaluable(
                f"plant.predicate already matches {before_planted} row(s) before "
                f"anything was planted; it does not isolate the planted delivery")

        planted = W.plant(root, spec)

        log1 = W.run_pipeline(root, steps)
        con = W.open_world(db, read_only=True)
        try:
            after1 = W.key_count(con, table, predicate)
            after1_window = W.per_date(con, table, date_col, cols,
                                       window.get("start"), window.get("end"))
            fp1 = W.business_fingerprint(con, table, cols)
        finally:
            con.close()

        log2 = W.run_pipeline(root, steps)
        con = W.open_world(db, read_only=True)
        try:
            after2 = W.key_count(con, table, predicate)
            after2_window = W.per_date(con, table, date_col, cols,
                                       window.get("start"), window.get("end"))
            fp2 = W.business_fingerprint(con, table, cols)
        finally:
            con.close()
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    err1, err2 = W.pipeline_failed(log1), W.pipeline_failed(log2)

    live_ok = after1 == rows
    if after1 == 0:
        live_detail = (f"the planted delivery ({rows} row(s), never seen by this "
                       f"pipeline) landed 0 rows - idempotent by never loading "
                       f"anything, which repairs nothing"
                       + (f"; run 1 failed [{err1}]" if err1 else ""))
    elif not live_ok:
        live_detail = (f"the planted delivery landed {after1} row(s) where {rows} "
                       f"were planted - the first load already duplicates")
    else:
        live_detail = f"the planted delivery landed exactly {rows} row(s) on run 1"

    moved = sorted(d for d in set(before_window) | set(after1_window)
                   if before_window.get(d) != after1_window.get(d))
    coll_detail = (f"the protected window is byte-identical after a run that had "
                   f"nothing to do with it ({len(before_window)} day(s) checked)"
                   if not moved else
                   f"{len(moved)} day(s) of the protected window changed on a run "
                   f"that only planted a new delivery: {moved[:6]}")

    window_moved_2 = sorted(d for d in set(after1_window) | set(after2_window)
                            if after1_window.get(d) != after2_window.get(d))
    idem_ok = (after2 == rows and fp1 == fp2 and not window_moved_2)
    if after2 != rows and after1 == 0:
        idem_detail = (f"nothing loaded on either run ({after1} then {after2} "
                       f"row(s)) - vacuously stable")
    elif after2 != rows:
        idem_detail = (f"a second identical run took the planted delivery from "
                       f"{after1} to {after2} row(s) - the load is still "
                       f"append-only")
    elif fp1 != fp2:
        idem_detail = (f"a second identical run changed a business column "
                       f"somewhere in {table} (audit columns excluded)"
                       + (f"; days that moved: {window_moved_2[:6]}"
                          if window_moved_2 else ""))
    else:
        idem_detail = (f"a second identical run changed no business column "
                       f"anywhere in {table}")
    if err2 and not idem_ok:
        idem_detail += f"; run 2 failed [{err2}]"

    return [
        Clause.make(f"{NAME}.ingests", live_ok,
                    f"{live_detail} [{planted}]",
                    weight=0.45, fatal=True, checkpoint="run_1"),
        # GATES, does not pay.  "The protected window did not move while an
        # unrelated delivery loaded" is TRUE of a world nobody repaired - the
        # stock loader only ever touches the newest landing day - so an agent
        # that did nothing satisfies it by construction.  Measured on GT004:
        # paying for it handed the no_op baseline 2.91 of G2's 8 points.
        Clause.make(f"{NAME}.no_collateral", not moved, coll_detail,
                    weight=0.20, fatal=True, checkpoint="run_1"),
        Clause.make(f"{NAME}.idempotent", idem_ok, idem_detail,
                    weight=0.35, checkpoint="run_2"),
    ]
