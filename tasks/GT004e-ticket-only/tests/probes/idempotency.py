#!/usr/bin/env python3
"""
GT004 gate G2 - idempotency, with a mandatory liveness clause.

    from probes import idempotency
    result = idempotency.run(world_root, contract=CONTRACT,
                             config=manifest.get("idempotency_probe"),
                             max_points=6)

Behavioural, not static.  Copies the graded world, plants a partition the
pipeline has NEVER seen, and executes the agent's own pipeline files at the paths
the project is wired to - twice.

Three clauses, and the first is the whole trick:

  * LIVENESS (fatal).  The planted partition is ingested exactly ONCE.  A load
    "fixed" into a permanent skip - a watermark pinned in the past, a
    `WHERE 1=0`, a guard that decides the table is already loaded - is idempotent
    by construction and repairs nothing.  It must score ZERO, not full marks, so
    this clause is marked fatal and a failure zeroes the probe outright.

  * NO COLLATERAL.  The window the agent repaired is unchanged by a run that had
    nothing to do with it.  A repair that re-derives the whole table on every run
    fails here even when its arithmetic is right.

  * IDEMPOTENCE.  A second identical run changes no BUSINESS column.  Audit
    columns (load_ts, batch_id, ...) are excluded by contract: they legitimately
    differ after any repair, and including them would turn this into "wrote
    nothing", which is the failure the liveness clause exists to catch.

NOT through `sched_trigger`.  `FileScheduler.trigger` reports success without
executing for every command that is not dbt_build/dbt_run
(gateway/src/alkera_gateway/surfaces.py:69-82), so a run count of two there
proves nothing ran at all.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from ._common import (Clause, ProbeNotConfigured, business_columns,
                      business_fingerprint, copy_world, crashed, key_count,
                      normalise_steps, not_configured, open_world, per_date,
                      pipeline_failed, probe_config, require, run_pipeline,
                      score_clauses, table_exists, warehouse_path, workspace_path,
                      write_text, world_config)

PROBE_ID = "G2"


def _plant(world_root: Path, plant: dict) -> str:
    """Materialise a partition the pipeline has never seen.

    `landing` writes a CSV into the agent-visible landing zone - the arrival
    surface an A009 loader actually reads - with newline="" so the planted bytes
    are identical on every host.  `sql` inserts into a raw table for worlds whose
    arrival surface is a table rather than a folder.
    """
    mode = plant.get("mode", "landing")
    date = plant["date"]
    rows = int(plant["rows"])
    if mode == "landing":
        require(plant, "dir", "filename", "header", "row_template",
                where="idempotency.plant (mode=landing)")
        target = (workspace_path(world_root) / plant["dir"].format(date=date)
                  / plant["filename"])
        lines = [plant["header"]]
        for i in range(rows):
            lines.append(plant["row_template"].format(
                n=i + 1, date=date, id=int(plant.get("id_base", 990000001)) + i,
                amount=plant.get("amount", "10.00")))
        write_text(target, "\n".join(lines) + "\n")
        return f"wrote {rows} row(s) to {target.relative_to(world_root).as_posix()}"
    if mode == "sql":
        require(plant, "sql", where="idempotency.plant (mode=sql)")
        con = open_world(warehouse_path(world_root))
        try:
            for i in range(rows):
                con.execute(plant["sql"].format(
                    n=i + 1, date=date,
                    id=int(plant.get("id_base", 990000001)) + i,
                    amount=plant.get("amount", "10.00")))
        finally:
            con.close()
        return f"inserted {rows} row(s) through idempotency.plant.sql"
    raise ProbeNotConfigured(f"unknown idempotency.plant.mode {mode!r}")


def run(world_root, *, contract: dict | None = None, config: dict | None = None,
        max_points: float = 6.0, keep_dir: Path | None = None) -> "object":
    """Gate G2.

    world_root  - the post-episode world root (contains var/ and workspace/).
                  Never written to; copied first.
    contract    - fixtures/fixture_contract.py::CONTRACT, or None.
    config      - the grading manifest's `idempotency_probe` block; overlays
                  CONTRACT['probes']['idempotency'].
    max_points  - the gate's weight in the rubric.
    keep_dir    - optional: build the probe world here instead of a temp dir, so
                  a failure can be opened afterwards.  Diagnostics only.
    """
    world_root = Path(world_root)
    try:
        w = world_config(contract, config)
        cfg = probe_config(contract, config, "idempotency")
        require(cfg, "plant", where="probes.idempotency")
        plant = cfg["plant"]
        require(plant, "date", "rows", "predicate", where="probes.idempotency.plant")
        steps = normalise_steps(cfg.get("steps")
                                or ((contract or {}).get("probes") or {})
                                .get("pipeline", {}).get("steps")
                                or (config or {}).get("steps"))
        if not steps:
            return not_configured(PROBE_ID, max_points,
                                  "no pipeline steps given (probes.pipeline.steps)")
    except ProbeNotConfigured as exc:
        return not_configured(PROBE_ID, max_points, str(exc))

    tmp = Path(keep_dir) if keep_dir else Path(tempfile.mkdtemp(prefix="gt004-g2-"))
    try:
        root = copy_world(world_root, tmp / "world")
        db = warehouse_path(root)
        table, key, dcol = w["fact_table"], w["business_key"], w["date_column"]
        win, audit = w["window"], w.get("audit_columns") or []

        con = open_world(db, read_only=True)
        try:
            if not table_exists(con, table):
                return score_clauses(PROBE_ID, [Clause(
                    "liveness", False, 1.0,
                    f"{table} does not exist in the graded world - there is "
                    "nothing for a pipeline to be idempotent about", fatal=True)],
                    max_points)
            cols = business_columns(con, table, audit)
            before_window = per_date(con, table, dcol, cols, win["start"], win["end"])
            before_all = per_date(con, table, dcol, cols)
            before_planted = key_count(con, table, key, plant["predicate"])
        finally:
            con.close()

        if plant["date"] in before_all:
            return not_configured(
                PROBE_ID, max_points,
                f"idempotency.plant.date {plant['date']} ALREADY exists in "
                f"{table} - a planted partition the pipeline has seen cannot "
                "prove liveness.  Fixture bug, not an agent failure.")
        if before_planted:
            return not_configured(
                PROBE_ID, max_points,
                f"idempotency.plant.predicate already matches {before_planted} "
                "row(s) before anything was planted; the predicate does not "
                "isolate the planted partition")

        planted_note = _plant(root, plant)

        log1 = run_pipeline(root, steps)
        con = open_world(db, read_only=True)
        try:
            after1_planted = key_count(con, table, key, plant["predicate"])
            after1_window = per_date(con, table, dcol, cols, win["start"], win["end"])
            fp1 = business_fingerprint(con, table, cols)
        finally:
            con.close()

        log2 = run_pipeline(root, steps)
        con = open_world(db, read_only=True)
        try:
            after2_planted = key_count(con, table, key, plant["predicate"])
            after2_window = per_date(con, table, dcol, cols, win["start"], win["end"])
            fp2 = business_fingerprint(con, table, cols)
        finally:
            con.close()

        n = int(plant["rows"])
        err1, err2 = pipeline_failed(log1), pipeline_failed(log2)

        live_ok = after1_planted == n
        if after1_planted == 0:
            live_detail = (f"the planted partition {plant['date']} ({n} row(s), never "
                           f"seen by this pipeline) landed 0 rows - idempotent by "
                           f"never loading anything, which repairs nothing"
                           + (f"; run 1 failed [{err1}]" if err1 else ""))
        elif after1_planted != n:
            live_detail = (f"the planted partition landed {after1_planted} rows where "
                           f"{n} were planted - the first load already duplicates")
        else:
            live_detail = f"the planted partition landed exactly {n} row(s) on run 1"

        moved = sorted(d for d in set(before_window) | set(after1_window)
                       if before_window.get(d) != after1_window.get(d))
        coll_ok = not moved
        coll_detail = ("the repaired window is byte-identical after a run that had "
                       f"nothing to do with it ({len(before_window)} day(s) checked)"
                       if coll_ok else
                       f"{len(moved)} day(s) of the repaired window changed on a run "
                       f"that only planted a new partition: {moved[:6]}")

        window_moved_2 = sorted(d for d in set(after1_window) | set(after2_window)
                                if after1_window.get(d) != after2_window.get(d))
        idem_ok = (after2_planted == n and fp1 == fp2 and not window_moved_2)
        if after2_planted != n and after1_planted == 0:
            idem_detail = (f"nothing loaded on either run ({after1_planted} then "
                           f"{after2_planted} rows) - vacuously stable")
        elif after2_planted != n:
            idem_detail = (f"a second identical run took the planted partition from "
                           f"{after1_planted} to {after2_planted} rows - the load is "
                           "still append-only")
        elif fp1 != fp2:
            idem_detail = ("a second identical run changed a business column "
                           f"somewhere in {table} (audit columns excluded)"
                           + (f"; days that moved: {window_moved_2[:6]}"
                              if window_moved_2 else ""))
        else:
            idem_detail = ("a second identical run changed no business column "
                           f"anywhere in {table}")
        if err2 and not idem_ok:
            idem_detail += f"; run 2 failed [{err2}]"

        return score_clauses(PROBE_ID, [
            Clause("liveness", live_ok, 0.45, live_detail, fatal=True),
            # GATES, does not pay - the same rule the liveness clause already
            # obeys, applied to the clause it was missed on. "The repaired
            # window did not move while an unrelated partition loaded" is TRUE
            # of a world nobody repaired: the shipped loader only ever touches
            # the newest landing day, so an agent that did nothing at all
            # satisfies it by construction. MEASURED 2026-09-14: that paid the
            # no_op baseline 2.91 of G2's 8 points - its entire score, and the
            # only thing standing between this rubric and "no check may be
            # satisfiable by doing nothing". As a gate it still carries its full
            # meaning in the direction that matters: a loader that disturbs the
            # repaired window while ingesting an unrelated partition is the
            # original defect recurring, and it takes the gate to zero.
            Clause("no-collateral", coll_ok, 0.20, coll_detail, fatal=True),
            Clause("idempotence", idem_ok, 0.35, idem_detail),
        ], max_points, {
            "planted": planted_note,
            "planted_rows_after_run1": after1_planted,
            "planted_rows_after_run2": after2_planted,
            "window_days": len(before_window),
            "run1": log1, "run2": log2,
        })
    except Exception as exc:                            # noqa: BLE001 - scored
        return crashed(PROBE_ID, max_points, exc)
    finally:
        if keep_dir is None:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
