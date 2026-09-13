#!/usr/bin/env python3
"""
Fixture contract verification.

A stale generator process once silently overwrote expected.duckdb AFTER a
validated run. The baselines still executed, every verdict still printed PASS,
and the scores were quietly wrong against a reference that no longer matched
the spec. Nothing detected it.

So: never score against a fixture without first proving it is the fixture the
contract describes. This runs before any baseline and refuses to continue on
drift.

    python fixture_contract.py --fixtures ./fixtures/build
"""
from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

# Contract values from SEED_SPEC.md at seed 1337.
CONTRACT = {
    "world": {"rows": 419675, "adj": 824, "dupkeys": 59995},
    "expected": {"rows": 359680, "adj": 412, "dupkeys": 0},
    "aov_ratio": 0.496,
    "aov_tolerance": 0.01,
}

WIN_START, WIN_END = "2026-02-01", "2026-03-02"


class FixtureDrift(Exception):
    pass


def _stats(db: Path) -> dict:
    con = duckdb.connect(str(db))
    con.execute("SET threads TO 1;")
    try:
        return {
            "rows": con.execute(
                "SELECT COUNT(*) FROM warehouse.fct_orders").fetchone()[0],
            "adj": con.execute(
                "SELECT COUNT(*) FROM warehouse.fct_orders "
                "WHERE is_manual_adjustment").fetchone()[0],
            "dupkeys": con.execute(
                "SELECT COUNT(*) FROM (SELECT order_id, line_nbr "
                "FROM warehouse.fct_orders GROUP BY 1,2 HAVING COUNT(*) > 1)"
            ).fetchone()[0],
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

    ratio = None
    if not problems:
        con = duckdb.connect(str(fx / "world.duckdb"))
        con.execute("SET threads TO 1;")
        a, b = con.execute(
            "SELECT ROUND(AVG(CASE WHEN dt BETWEEN DATE '" + WIN_START
            + "' AND DATE '" + WIN_END + "' THEN aov END), 2),"
            " ROUND(AVG(CASE WHEN dt < DATE '" + WIN_START + "' THEN aov END), 2)"
            " FROM warehouse.kpi_daily").fetchone()
        con.close()
        ratio = round(float(a) / float(b), 3)
        if abs(ratio - CONTRACT["aov_ratio"]) > CONTRACT["aov_tolerance"]:
            problems.append(
                "AOV ratio " + str(ratio) + ", contract "
                + str(CONTRACT["aov_ratio"])
                + " - the symptom the task is built around is not present")

    if problems:
        raise FixtureDrift(
            "FIXTURE DOES NOT MATCH CONTRACT" + "\n\n  "
            + "\n  ".join(problems)
            + "\n\n  Regenerate:\n    python fixtures/make_fixture_world.py --out "
            + str(fx) + " --seed 1337"
            + "\n\n  Scores computed against a drifted fixture are meaningless.")

    if not quiet:
        print("  fixture verified against contract (AOV ratio " + str(ratio) + ")")


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
