#!/usr/bin/env python3
"""
Fixture contract verification.

A stale generator process once silently overwrote expected.duckdb AFTER a
validated run. The baselines still executed, every verdict still printed PASS,
and the scores were quietly wrong against a reference that no longer matched the
spec. Nothing detected it.

So: never score against a fixture without first proving it is the fixture the
contract describes. This runs before any baseline and refuses to continue on
drift.

    python fixture_contract.py --fixtures ./fixtures/build

The last three assertions are the ones that matter. `dup_keys == 0` in the BROKEN
world is the property the whole task rests on: defect B leaves no trace anywhere
until defect A is repaired. If a regenerated world ever shows a duplicate before
the repair, the task has silently stopped being the task.
"""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

import duckdb

from warehouse_conn import deployed_copy, open_warehouse

# Contract values from SEED_SPEC.md at seed 2029.
CONTRACT = {
    "world": {"rows": 407155, "dup_keys": 0, "dup_order_ids": 139, "dlq_rows": 41, "max_order_dt": "2026-03-09"},
    "expected": {"rows": 430240, "dup_keys": 0, "dup_order_ids": 1240, "dlq_rows": 67, "max_order_dt": "2026-03-13"},
}
TRUTH = {
    "log_start_offset": 4000000,
    "log_end_offset": 4150411,
    "committed_offset_at_start": 4126000,
    "loaded_uncommitted_overlap": 1300,
    "produced_gap": 24411,
    "gap_loadable_rows": 24385,
    "unrestorable_rows": 282221,
    "n_dead_letter": 26,
    "n_legit_revisions": 1100,
    "n_blast_radius_partitions": 54
}


class FixtureDrift(Exception):
    pass


def _stats(db: Path) -> dict:
    con = open_warehouse(db, read_only=True)
    try:
        return {
            "rows": con.execute(
                "SELECT COUNT(*) FROM warehouse.fct_orders").fetchone()[0],
            "dup_keys": con.execute(
                "SELECT COUNT(*) FROM (SELECT order_id, revision "
                "FROM warehouse.fct_orders GROUP BY 1,2 HAVING COUNT(*) > 1)").fetchone()[0],
            "dup_order_ids": con.execute(
                "SELECT COUNT(*) FROM (SELECT order_id FROM warehouse.fct_orders "
                "GROUP BY 1 HAVING COUNT(*) > 1)").fetchone()[0],
            "dlq_rows": con.execute(
                "SELECT COUNT(*) FROM raw.orders_dlq").fetchone()[0],
            "max_order_dt": str(con.execute(
                "SELECT MAX(order_dt) FROM warehouse.fct_orders").fetchone()[0]),
        }
    finally:
        con.close()


def verify(fx: Path, quiet: bool = False) -> None:
    """Raise FixtureDrift unless the fixture matches the contract exactly."""
    problems: list[str] = []

    for name in ("world", "expected"):
        db = fx / (name + ".duckdb")
        if not db.exists():
            problems.append("MISSING: " + str(db))
            continue
        want, got = CONTRACT[name], _stats(db)
        if got != want:
            problems.append(
                name + ".duckdb does not match the contract"
                + "\n      expected " + repr(want)
                + "\n      got      " + repr(got))

    tp = fx / "harness_truth.json"
    if not tp.exists():
        problems.append("MISSING: " + str(tp))
    else:
        t = json.loads(tp.read_text(encoding="utf-8"))
        got = {
            "log_start_offset": t["log_start_offset"],
            "log_end_offset": t["log_end_offset"],
            "committed_offset_at_start": t["committed_offset_at_start"],
            "loaded_uncommitted_overlap": t["loaded_uncommitted_overlap"],
            "produced_gap": t["produced_gap"],
            "gap_loadable_rows": t["gap_loadable_rows"],
            "n_dead_letter": len(t["dlq_expected_offsets"]),
            "n_legit_revisions": len(t["legit_revisions"]),
            "n_blast_radius_partitions": len(t["pre_incident_partitions"]),
            "unrestorable_rows": t["unrestorable_rows"],
        }
        if got != TRUTH:
            problems.append(
                "harness_truth.json does not match the contract"
                + "\n      expected " + repr(TRUTH)
                + "\n      got      " + repr(got))
        elif (t["gap_loadable_rows"] + len(t["dlq_expected_offsets"])
              != t["produced_gap"]):
            problems.append(
                "the conservation identity does not hold in the reference itself: "
                f"{t['gap_loadable_rows']} + {len(t['dlq_expected_offsets'])} != "
                f"{t['produced_gap']}. The star check cannot be passable.")

    # The world's own pipeline must actually be blocked. If the sink no longer
    # raises on the world it ships with, defect A is not present and every
    # downstream number is meaningless.
    ws = fx.parent / "workspace"
    sink = ws / "pipelines" / "orders_sink.sql"
    if not problems and sink.exists():
        # a writable scratch copy: read-only would reject the INSERT before the
        # cast is ever evaluated, which proves nothing about the poison record
        tmp = Path(tempfile.mkdtemp(prefix="gt002-contract-"))
        try:
            # the CONTRACT filename, so the catalog/schema collision is
            # reproduced rather than dodged by a convenient file stem
            probe = deployed_copy(fx / "world.duckdb", tmp)
            con = open_warehouse(probe)
            try:
                con.execute(sink.read_text(encoding="utf-8"))
                problems.append(
                    "the shipped sink does NOT raise on the shipped world - the "
                    "consumer is not stalled and defect A is absent")
            except duckdb.Error as exc:
                if "Conversion Error" not in str(exc):
                    problems.append(f"the sink failed for the wrong reason: "
                                    f"{str(exc).splitlines()[0][:120]}")
            finally:
                con.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    if problems:
        raise FixtureDrift(
            "FIXTURE DOES NOT MATCH CONTRACT\n\n  "
            + "\n  ".join(problems)
            + "\n\n  Regenerate:\n    python fixtures/make_fixture_world.py --out "
            + str(fx) + " --seed 2029"
            + "\n\n  Scores computed against a drifted fixture are meaningless.")

    if not quiet:
        print("  fixture verified against contract "
              "(0 duplicate business keys pre-repair; the sink raises on the world "
              "it ships with)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures",
                    default=str(Path(__file__).parent / "fixtures" / "build"))
    a = ap.parse_args()
    try:
        verify(Path(a.fixtures))
    except FixtureDrift as e:
        print(str(e))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
