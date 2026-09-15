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
    # READ ONLY, and it matters beyond tidiness. A read-write connection takes
    # duckdb's exclusive file lock, so K rollouts verifying the same cached
    # fixture at once knocked each other over with
    # "The process cannot access the file because it is being used by another
    # process" - the verifier is the one component that must never be the reason
    # a run fails.
    # Aliased ATTACH, never a direct open (F622, agent.md 9.2.1). Opening the
    # file directly names the catalog after its stem, so at the CONTRACT path
    # var/warehouse/warehouse.duckdb the catalog and the schema are both
    # `warehouse` and every two-part reference below raises Binder Error -
    # Ambiguous reference. It has not fired yet only because callers happen to
    # pass fixture paths whose stem is `world`; `db` is an argument, so that is
    # luck, not a property. Under the alias the catalog is `wh` and
    # `warehouse.fct_orders` reads unambiguously as schema.table.
    con = duckdb.connect(":memory:")
    con.execute("SET threads TO 1;")
    con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh (READ_ONLY)")
    con.execute("USE wh.warehouse")
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
        # Same F622 form as _stats above. READ_ONLY too: this block only reads,
        # and a read-write handle takes duckdb's exclusive lock, which is what
        # knocked concurrent rollouts over on the sibling connection.
        con = duckdb.connect(":memory:")
        con.execute("SET threads TO 1;")
        con.execute("ATTACH '" + str(fx / "world.duckdb").replace("'", "''")
                    + "' AS wh (READ_ONLY)")
        con.execute("USE wh.warehouse")
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
