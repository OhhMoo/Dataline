#!/usr/bin/env python3
"""
GT004 gate G3 - late arrivals land INSIDE the window the agent already repaired.

    from probes import late_arrival
    result = late_arrival.run(world_root, contract=CONTRACT,
                              config=manifest.get("late_arrival_probe"),
                              max_points=5)

This is the gate that makes GT004 long-horizon.  The agent cannot finish, because
finishing changes what "finished" means: the repair has to still hold after the
world moved.

The `late_arrival` FAULT KIND is inert in this environment - `_kind_scan` globs
the cumulative folder with `min: 1`, so after day one a poll cannot fail.  Late
arrival is therefore modelled where it actually lives: in the ARRIVAL STREAM
(WORLD's payload, dropped into the landing zone or the raw table) and in the
CLOCK (var/sched/state.json's v-now).  No fault is injected and no job is
triggered.

Three clauses:

  * INCORPORATION (fatal).  The late events are in the fact table exactly once.
    A repair that hardcoded "delete the window, reinsert from the snapshot I saw"
    wipes them on its next run and lands zero - it fails here, which is the whole
    point of the gate.  A genuine merge survives.
  * NO RE-DOUBLING.  Every day of the repaired window ends at
    (its pre-arrival count + that day's late events) and not a row more.  The
    original defect - an append-mode re-run doubling a window - reappears here if
    the agent hardened the arithmetic without hardening the write mode.
  * STABILITY.  Running again after the arrival still changes nothing.  G2 proves
    idempotency against a NEW partition; this proves it against a partition that
    already exists, which is the harder case and the one a merge key gets wrong.
"""
from __future__ import annotations

import json
import tempfile
from collections import defaultdict
from pathlib import Path

from ._common import (Clause, ProbeNotConfigured, business_columns, copy_world,
                      crashed, key_count, normalise_steps, not_configured,
                      open_world, per_date, pipeline_failed, probe_config,
                      read_json, require, run_pipeline, score_clauses,
                      table_exists, warehouse_path, workspace_path, write_text,
                      world_config)

PROBE_ID = "G3"


def _apply_payload(world_root: Path, payload: dict) -> str:
    """Drop WORLD's late-arrival payload into the world's arrival surface."""
    mode = payload.get("mode", "landing")
    events = payload["events"]
    notes = []
    if mode == "landing":
        require(payload, "dir", "filename", "header", "row_template",
                where="late_arrival.payload (mode=landing)")
        for ev in events:
            date, rows = ev["date"], int(ev["rows"])
            target = (workspace_path(world_root) / payload["dir"].format(date=date)
                      / payload["filename"])
            lines = [payload["header"]]
            for i in range(rows):
                lines.append(payload["row_template"].format(
                    n=i + 1, date=date, id=int(ev.get("id_base", 991000001)) + i,
                    amount=ev.get("amount", "10.00")))
            write_text(target, "\n".join(lines) + "\n")
            notes.append(f"{date}:{rows}")
        return "landing " + ",".join(notes)
    if mode == "sql":
        require(payload, "sql", where="late_arrival.payload (mode=sql)")
        con = open_world(warehouse_path(world_root))
        try:
            for ev in events:
                for i in range(int(ev["rows"])):
                    con.execute(payload["sql"].format(
                        n=i + 1, date=ev["date"],
                        id=int(ev.get("id_base", 991000001)) + i,
                        amount=ev.get("amount", "10.00")))
                notes.append(f"{ev['date']}:{ev['rows']}")
        finally:
            con.close()
        return "sql " + ",".join(notes)
    raise ProbeNotConfigured(f"unknown late_arrival.payload.mode {mode!r}")


def _event_adds(ev: dict) -> int:
    """How many rows this event ADDS to the fact table.

    Defaults to `rows`, which is right for every payload whose events are
    genuinely new lines - that is every world probes/selftest.py builds, and it
    is what this probe assumed exclusively until 2026-09-14.

    A payload may also carry upstream RESTATEMENTS: a redelivery of a business
    key the fact table already holds.  A restatement REPLACES a row rather than
    adding one, so it moves no day's row count and it cannot be isolated by the
    late-event predicate either - an id-range predicate that matched a restated
    key would already match before the payload was applied, and the guard below
    ("predicate already matches N rows") would correctly reject it.  Counting
    such an event as an arrival made both the incorporation and the
    no-re-doubling clauses expect rows that a CORRECT merge must not create:
    MEASURED on GT004's shipped fixture, a genuine merge landed 126 of 126 new
    lines and was scored `126 where 196 were delivered`, zeroing the gate for
    every baseline including the oracle.

    Restatements still carry the gate.  They are planted in full, so an agent
    that APPENDS them instead of merging them moves the day count to
    (pre-arrival + new + restated) and fails no-re-doubling, which is the
    merge-versus-append distinction they exist to make.
    """
    return int(ev.get("adds", ev["rows"]))


def _advance_clock(world_root: Path, to: str | None) -> str:
    """Move the world's v-clock forward.  Nothing is triggered.

    The scheduler's v-now is the world's clock; advancing it is what makes the
    arrival LATE rather than merely additional.  `sched_trigger` is deliberately
    not used: it reports success without executing.
    """
    if not to:
        return "clock not advanced (no advance_to given)"
    p = Path(world_root) / "var" / "sched" / "state.json"
    state = read_json(p)
    if not isinstance(state, dict) or "vnow" not in state:
        return f"clock not advanced: {p.name} carries no vnow"
    was = state["vnow"]
    state["vnow"] = to
    write_text(p, json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n")
    return f"v-clock {was} -> {to}"


def run(world_root, *, contract: dict | None = None, config: dict | None = None,
        max_points: float = 5.0, keep_dir: Path | None = None) -> "object":
    """Gate G3.  Same signature shape as every other probe in this package.

    world_root  - post-episode world root; copied, never written to.
    contract    - CONTRACT from fixtures/fixture_contract.py.
    config      - manifest `late_arrival_probe` block, overlaying
                  CONTRACT['probes']['late_arrival'].
    max_points  - the gate's rubric weight.
    keep_dir    - diagnostics only.
    """
    world_root = Path(world_root)
    try:
        w = world_config(contract, config)
        cfg = probe_config(contract, config, "late_arrival")
        require(cfg, "payload", where="probes.late_arrival")
        payload = cfg["payload"]
        require(payload, "events", "predicate", where="probes.late_arrival.payload")
        steps = normalise_steps(cfg.get("steps")
                                or ((contract or {}).get("probes") or {})
                                .get("pipeline", {}).get("steps")
                                or (config or {}).get("steps"))
        if not steps:
            return not_configured(PROBE_ID, max_points,
                                  "no pipeline steps given (probes.pipeline.steps)")
    except ProbeNotConfigured as exc:
        return not_configured(PROBE_ID, max_points, str(exc))

    tmp = Path(keep_dir) if keep_dir else Path(tempfile.mkdtemp(prefix="gt004-g3-"))
    try:
        root = copy_world(world_root, tmp / "world")
        db = warehouse_path(root)
        table, key, dcol = w["fact_table"], w["business_key"], w["date_column"]
        win, audit = w["window"], w.get("audit_columns") or []

        expect_by_date = defaultdict(int)
        delivered = 0
        for ev in payload["events"]:
            expect_by_date[str(ev["date"])] += _event_adds(ev)
            delivered += int(ev["rows"])
        n_late = sum(expect_by_date.values())

        outside = [d for d in expect_by_date
                   if not (str(win["start"]) <= d <= str(win["end"]))]
        if outside:
            return not_configured(
                PROBE_ID, max_points,
                f"late_arrival payload dates {sorted(outside)} fall OUTSIDE the "
                f"repaired window {win['start']}..{win['end']} - G3 only means "
                "anything for events that land inside it")

        con = open_world(db, read_only=True)
        try:
            if not table_exists(con, table):
                return score_clauses(PROBE_ID, [Clause(
                    "incorporation", False, 1.0,
                    f"{table} does not exist in the graded world", fatal=True)],
                    max_points)
            cols = business_columns(con, table, audit)
            before_window = per_date(con, table, dcol, cols, win["start"], win["end"])
            before_late = key_count(con, table, key, payload["predicate"])
        finally:
            con.close()

        if before_late:
            return not_configured(
                PROBE_ID, max_points,
                f"late_arrival.payload.predicate already matches {before_late} "
                "row(s) before the payload was applied; it does not isolate the "
                "late events")

        note = _apply_payload(root, payload)
        clock = _advance_clock(root, cfg.get("advance_to"))

        log1 = run_pipeline(root, steps)
        con = open_world(db, read_only=True)
        try:
            after_late = key_count(con, table, key, payload["predicate"])
            after_window = per_date(con, table, dcol, cols, win["start"], win["end"])
        finally:
            con.close()

        log2 = run_pipeline(root, steps)
        con = open_world(db, read_only=True)
        try:
            after2_late = key_count(con, table, key, payload["predicate"])
            after2_window = per_date(con, table, dcol, cols, win["start"], win["end"])
        finally:
            con.close()

        err1 = pipeline_failed(log1)
        err2 = pipeline_failed(log2)

        inc_ok = after_late == n_late
        if after_late == 0:
            inc_detail = (f"{n_late} late event(s) landed inside the repaired window "
                          "and the pipeline incorporated NONE of them - either the "
                          "repair reinstates a snapshot captured before they arrived, "
                          "or the load no longer ingests anything at all"
                          + (f"; run failed [{err1}]" if err1 else ""))
        elif after_late != n_late:
            inc_detail = (f"{after_late} rows carry the late-event marker where "
                          f"{n_late} were delivered - incorporated, but not once")
        else:
            inc_detail = (f"all {n_late} late event(s) incorporated exactly once"
                          + (f" ({delivered} row(s) delivered, {delivered - n_late} "
                             f"of them restatements of keys the fact already held)"
                             if delivered != n_late else ""))

        # expected = pre-arrival count + that day's late events, per day
        bad, doubled = [], []
        for d in sorted(set(before_window) | set(after_window)):
            b = before_window.get(d, [0, None])[0]
            a = after_window.get(d, [0, None])[0]
            want = b + expect_by_date.get(d, 0)
            if a != want:
                bad.append(f"{d}: {b}->{a}, expected {want}")
                if b and a >= 2 * b:
                    doubled.append(d)
        red_ok = not bad
        red_detail = (f"every day of the repaired window ends at its pre-arrival "
                      f"count plus its own late events ({len(before_window)} day(s))"
                      if red_ok else
                      ("the window RE-DOUBLED on " + str(doubled[:6]) + "; "
                       if doubled else "") + f"{len(bad)} day(s) off: {bad[:6]}")

        stab_ok = (after2_late == n_late
                   and all(after_window.get(d) == after2_window.get(d)
                           for d in set(after_window) | set(after2_window)))
        stab_detail = ("running again after the arrival changes nothing"
                       if stab_ok else
                       f"a repeat run took the late events from {after_late} to "
                       f"{after2_late} rows and/or moved the window again"
                       + (f"; run 2 failed [{err2}]" if err2 else ""))

        return score_clauses(PROBE_ID, [
            Clause("incorporation", inc_ok, 0.45, inc_detail, fatal=True),
            Clause("no-re-doubling", red_ok, 0.30, red_detail),
            Clause("stability", stab_ok, 0.25, stab_detail),
        ], max_points, {
            "payload": note, "clock": clock, "late_rows_expected": n_late,
            "late_rows_delivered": delivered,
            "late_rows_after_run1": after_late, "late_rows_after_run2": after2_late,
            "window_days": len(before_window), "run1": log1, "run2": log2,
        })
    except Exception as exc:                            # noqa: BLE001 - scored
        return crashed(PROBE_ID, max_points, exc)
    finally:
        if keep_dir is None:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
