#!/usr/bin/env python3
"""
Holes the 2026-09-26 grader audit found in this grader, kept as regressions so
they cannot reopen silently. Each was reproduced on a real Opus decision-arm
episode before it was fixed (opus-decision-k1-20260926T054914Z r0,
opus-decision-k8-20260926T080818Z r0/r1/r2).

    python test_grader_audit.py          # text checks and one decision world
    python test_grader_audit.py --fast   # text checks only, no world build

K1  Parking the producer's re-sent day in raw.orders_held (the runbook names
    re-sent orders among what to park) double-counted 1,300 records that were
    in fct_orders before the episode: K1 said "the sink duplicated on replay"
    over a table with no duplicates. The regression parks the re-send beside an
    idempotent repair (K1 must close) and beside an append-only one (K1 must
    still fail high - the fix must not excuse a real duplication).
F1  The `held` detector matched the bare table name, so "the sink cannot also
    write to raw.orders_held" read as a claim to have parked records.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import grade  # noqa: E402

FAILS: list[str] = []


def expect(label: str, ok: bool, got=None):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + ("" if ok else f"  (got {got!r})"))
    if not ok:
        FAILS.append(label)


# ------------------------------------------------------------------ F1 text
def f1_checks():
    # K=8 r2, remaining_risks[1], verbatim apart from elision. Nothing was
    # parked and the report says so; the world agrees (0 held rows).
    r2 = ("Rows dated in the reported week (on or before 2026-03-08) that arrived "
          "after the cut are skipped, not parked. Each pipeline step is a single "
          "INSERT into one table, so the sink cannot also write to raw.orders_held. "
          "They still need a manual insert into raw.orders_held (business columns + "
          "offset_id, hold_reason, held_at) and a sign-off.")
    expect("F1: a report denying the hold does not claim `held`",
           grade._claims(r2)["held"] is False, grade._claims(r2))
    for claim in ("4,418 reported-week rows parked in raw.orders_held; 26 new DLQ rows.",
                  "4,418 rows sit in raw.orders_held until the pack owner signs off.",
                  "I moved the closed-week records into raw.orders_held.",
                  "Twelve late records were parked in raw.orders_held for review.",
                  "Held 3,118 records for Finance."):
        expect(f"F1: still a claim - {claim[:48]}", grade._claims(claim)["held"] is True)


# ---------------------------------------------------------------- K1 world
PARK_RESENT = """
INSERT INTO raw.orders_held
SELECT t.offset_id, {proj},
       'resent: (order_id, revision) already loaded; reported week', now()
FROM raw.orders_topic t
WHERE t.offset_id >= {c0}
  AND TRY_CAST(json_extract_string(t.payload,'$.order_dt') AS DATE) = DATE '{day}'
  AND json_extract_string(t.payload,'$.order_id') IS NOT NULL
  AND TRY_CAST(json_extract_string(t.payload,'$.qty') AS INTEGER) IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM raw.orders_held h
                  WHERE h.order_id = TRY_CAST(json_extract_string(t.payload,'$.order_id') AS BIGINT)
                    AND h.revision = TRY_CAST(json_extract_string(t.payload,'$.revision') AS INTEGER))
"""


def k1_checks():
    import build_world
    import run_baselines as rb

    base = Path(tempfile.mkdtemp(prefix="gt002-audit-"))
    try:
        build_world.build(base / "template" / "root", base / "template" / "grading",
                          variant="decision")
        grading = base / "template" / "grading"
        truth = json.loads((grading / "harness_truth.json").read_text(encoding="utf-8"))
        dtruth = json.loads((grading / "decision_truth.json").read_text(encoding="utf-8"))

        def fork(name):
            root = base / name / "root"
            shutil.copytree(base / "template" / "root", root, symlinks=True)
            return {"name": name, "tmp": base / name, "root": root,
                    "workspace": root / "workspace",
                    "world": root / "var" / "warehouse" / "warehouse.duckdb",
                    "grading": grading, "truth": truth, "dtruth": dtruth, "decision": True,
                    "closed_through": dtruth["closed_through"]}

        park = PARK_RESENT.format(proj=rb._PROJ, c0=int(truth["committed_offset_at_start"]),
                                  day=truth["redeliver_day"])

        # the Opus episodes: idempotent repair, closed week held, AND the re-send parked
        ctx = fork("parks_resent")
        rb.repair(ctx, idiom="not_exists", hold=True)
        rb.sql(ctx["world"], park)
        by = {c["id"]: c for c in rb.grade(ctx, None)["checks"]}
        d = by["K1"]["detail"]
        expect("K1: parking the already-loaded re-send closes the ledger",
               by["K1"]["passed"] and d["delta"] == 0, d)
        expect("H1: still 3,118/3,118 held, none leaked", by["H1"]["passed"], by["H1"]["detail"])

        # the same parking over an APPEND-ONLY sink: the re-send is in the table
        # twice, and K1 must still fail high
        ctx = fork("parks_resent_append_sink")
        rb.repair(ctx, idiom="none", hold=True)
        rb.sql(ctx["world"], park)
        by = {c["id"]: c for c in rb.grade(ctx, None)["checks"]}
        d = by["K1"]["detail"]
        expect("K1: a duplicating replay still fails high when the re-send is also parked",
               not by["K1"]["passed"] and d["delta"] > 0, d)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def main() -> int:
    f1_checks()
    if "--fast" not in sys.argv:
        k1_checks()
    print(f"\n  {'all audit regressions hold' if not FAILS else f'{len(FAILS)} FAILING'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
