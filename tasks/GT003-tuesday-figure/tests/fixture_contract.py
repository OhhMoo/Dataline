#!/usr/bin/env python3
"""
Fixture contract verification.

A stale generator process once silently overwrote GT001's expected.duckdb AFTER
a validated run. The baselines still executed, every verdict still printed PASS,
and the scores were quietly wrong against a reference that no longer matched the
spec. Nothing detected it.

So: never score against a fixture without first proving it is the fixture the
contract describes. This runs before any baseline and refuses to continue on
drift.

Three of these checks are not row counts, and they are the ones that matter.

  * THE MECHANISM FIRES. The whole task rests on the claim that a read taken
    between two halves of the publish sees a mart that belongs to no single
    build. That is an assertion about DuckDB, not about our data, so it is
    executed here rather than asserted. If it stops being true - an engine
    upgrade makes renames transactional, say - the task is invalid and this is
    where we find out.

  * THE CONTRACT FILENAME WORKS. The world ships as
    /var/warehouse/warehouse.duckdb, where DuckDB names the catalog after the
    file stem and every warehouse.<table> reference is ambiguous. The fixture
    builds world.duckdb, where the collision cannot happen. GT001's oracle was
    broken at the deployed path for weeks while its baselines printed TASK VALID
    for exactly this reason, so the fixture is staged under the contract name
    and queried there.

  * THE RECORD DIFFERS IN EXACTLY TWO ROWS. If the reference world's reporting
    history diverges anywhere else, the snapshot-integrity check is grading
    something other than the restatement.

    python fixture_contract.py --fixtures ./fixtures/build
"""
from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

import duckdb

TORN_TS = "2026-03-03 08:15:00"
PREV_TS = "2026-03-03 08:00:00"
NEXT_TS = "2026-03-03 08:30:00"
FX0, FX1 = "2026-02-16", "2026-03-04"

# Contract values from SEED_SPEC.md at seed 4211.
CONTRACT = {
    "world": {
        "lines": 160501, "customers": 10180, "fact_rows": 131730,
        "fact_reseller_rows": 0, "snapshot_rows": 8980,
        "backup_dim_rows": 11063, "backup_dim_dup_customers": 883,
        "live_dim_rows": 10180, "live_dim_dup_customers": 0,
        "fx_jpy_rows_in_gap": 0, "raw_duplicate_keys": 31,
    },
    "expected": {
        "lines": 160501, "customers": 10180, "fact_rows": 149203,
        "fact_reseller_rows": 17473, "snapshot_rows": 8980,
        "backup_dim_rows": 11063, "backup_dim_dup_customers": 883,
        "live_dim_rows": 10180, "live_dim_dup_customers": 0,
        "fx_jpy_rows_in_gap": 17, "raw_duplicate_keys": 31,
    },
    "reported_total": "15385785.19",     # the figure Finance escalated
    "clean_total": "13482810.23",        # the first self-consistent read after it
    "published_total": "5537653.33",     # the build that was still published
    "overstatement": "1902974.96",       # == the new resellers' 2026-03-02 revenue
    "jpy_overstatement_pct_min": 8.0,
    "jpy_overstatement_pct_max": 9.5,
    "snapshot_rows_differing": 2,
}

STATS = {
    "lines": "SELECT COUNT(*) FROM staging.stg_revenue_lines",
    "customers": "SELECT COUNT(*) FROM staging.stg_customer",
    "fact_rows": "SELECT COUNT(*) FROM warehouse.mart_revenue_fact",
    "fact_reseller_rows": "SELECT COUNT(*) FROM warehouse.mart_revenue_fact "
                          "WHERE customer_segment = 'RESELLER'",
    "snapshot_rows": "SELECT COUNT(*) FROM warehouse.rev_snapshot",
    "backup_dim_rows": "SELECT COUNT(*) FROM warehouse.mart_revenue_dim__dbt_backup",
    "backup_dim_dup_customers": "SELECT COUNT(*) FROM (SELECT customer_id FROM "
                                "warehouse.mart_revenue_dim__dbt_backup "
                                "GROUP BY 1 HAVING COUNT(*) > 1)",
    "live_dim_rows": "SELECT COUNT(*) FROM warehouse.mart_revenue_dim",
    "live_dim_dup_customers": "SELECT COUNT(*) FROM (SELECT customer_id FROM "
                              "warehouse.mart_revenue_dim GROUP BY 1 HAVING COUNT(*) > 1)",
    "fx_jpy_rows_in_gap": f"SELECT COUNT(*) FROM raw.fx_rates_daily WHERE currency='JPY' "
                          f"AND rate_date BETWEEN DATE '{FX0}' AND DATE '{FX1}'",
    "raw_duplicate_keys": "SELECT COUNT(*) FROM (SELECT order_id, line_nbr FROM "
                          "raw.revenue_lines GROUP BY 1, 2 HAVING COUNT(*) > 1)",
}


class FixtureDrift(Exception):
    pass


def attach(path: Path, alias: str = "wh", read_only: bool = True, con=None):
    """NEVER open a warehouse file directly - see the module docstring."""
    if con is None:
        con = duckdb.connect(":memory:", config={"threads": 1})
    ro = " (READ_ONLY)" if read_only else ""
    con.execute("ATTACH '" + str(path).replace("'", "''") + f"' AS {alias}" + ro)
    return con


def _stats(db: Path) -> dict:
    con = attach(db)
    con.execute("USE wh.warehouse")
    try:
        return {k: con.execute(q).fetchone()[0] for k, q in STATS.items()}
    finally:
        con.close()


def _mechanism_fires(world: Path) -> str | None:
    """Stage the world at its CONTRACT filename and reproduce the planted
    interleave. Returns None on success, or a description of what went wrong."""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "var" / "warehouse"
        d.mkdir(parents=True)
        p = d / "warehouse.duckdb"
        shutil.copy(world, p)
        con = duckdb.connect(":memory:", config={"threads": 1})
        con.execute("ATTACH '" + str(p).replace("'", "''") + "' AS wh")
        con.execute("USE wh.warehouse")

        # the contract filename must not break two-part references
        try:
            con.execute("SELECT COUNT(*) FROM warehouse.mart_revenue_fact").fetchone()
        except Exception as ex:
            return ("the world is unusable at its contract filename "
                    "var/warehouse/warehouse.duckdb: " + str(ex).splitlines()[0])

        writer, reader = con.cursor(), con.cursor()
        for c in (writer, reader):
            c.execute("USE wh.warehouse")

        def read_mix():
            return reader.execute("""
                SELECT COUNT(*) FROM warehouse.mart_revenue_fact f
                JOIN warehouse.mart_revenue_dim d ON d.customer_id = f.customer_id
                WHERE f.order_dt = DATE '2026-03-02'""").fetchone()[0]

        # Reconstruct the fact as the 08:14:58 build published it: the shipped
        # mart carries the analyst's segment predicate, which came later and
        # would hide the very rows the interleave fans out.
        writer.execute("""
            CREATE OR REPLACE TABLE warehouse.mart_revenue_fact AS
            SELECT l.order_dt, l.customer_id, l.customer_segment,
                   ROUND(SUM(l.amount_usd), 2)::DECIMAL(16,2) AS revenue_usd,
                   COUNT(*)::BIGINT AS order_count
            FROM staging.stg_revenue_lines l GROUP BY 1, 2, 3""")
        before = read_mix()
        writer.execute("DROP TABLE IF EXISTS warehouse.mart_revenue_dim__dbt_backup")
        # rebuild the dimension the way the publish does, then stop halfway
        writer.execute("CREATE OR REPLACE TABLE warehouse.mart_revenue_dim__dbt_tmp AS "
                       "SELECT customer_id, region, customer_segment "
                       "FROM staging.stg_customer UNION ALL "
                       "SELECT customer_id, region, customer_segment "
                       "FROM staging.stg_customer WHERE customer_id IN ("
                       "SELECT customer_id FROM raw.customer_master "
                       "WHERE source_batch = 'cm-20260225-0001')")
        writer.execute("ALTER TABLE warehouse.mart_revenue_dim "
                       "RENAME TO mart_revenue_dim__probe_live")
        writer.execute("ALTER TABLE warehouse.mart_revenue_dim__dbt_tmp "
                       "RENAME TO mart_revenue_dim")
        during = read_mix()
        con.close()
        if during <= before:
            return (f"the planted interleave does not fan out: a read against the "
                    f"duplicated dimension returned {during} joined rows, the same "
                    f"or fewer than the {before} a clean read returns. The engine "
                    f"may have changed; the task's mechanism no longer fires.")
    return None


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
            bad = {k: (want[k], got[k]) for k in want if want[k] != got[k]}
            problems.append(
                name + ".duckdb does not match the contract"
                + "".join("\n      " + k + ": expected " + str(v[0])
                          + ", got " + str(v[1]) for k, v in bad.items()))

    if not problems:
        con = attach(fx / "world.duckdb", "w")
        attach(fx / "expected.duckdb", "e", con=con)

        def one(q):
            return con.execute(q).fetchone()

        tot = {}
        for label, ts in (("reported_total", TORN_TS), ("clean_total", NEXT_TS),
                          ("published_total", PREV_TS)):
            tot[label] = str(one("SELECT ROUND(SUM(mtd_revenue_usd), 2) FROM "
                                 "w.warehouse.rev_snapshot WHERE snapshot_ts = "
                                 f"TIMESTAMP '{ts}'")[0])
            if tot[label] != CONTRACT[label]:
                problems.append(f"{label} is {tot[label]}, contract {CONTRACT[label]}"
                                " - the symptom the task is built around moved")

        over = str(one(f"""SELECT ROUND(
            (SELECT SUM(mtd_revenue_usd) FROM w.warehouse.rev_snapshot
             WHERE snapshot_ts = TIMESTAMP '{TORN_TS}')
          - (SELECT SUM(mtd_revenue_usd) FROM w.warehouse.rev_snapshot
             WHERE snapshot_ts = TIMESTAMP '{NEXT_TS}'), 2)""")[0])
        # The duplicated set is addressed the only way the world allows: by the
        # leftover relation itself. Addressing it by a live predicate was tried
        # and removed - see the ablation note in SEED_SPEC section 0.
        launch = str(one("""SELECT ROUND(SUM(amount_usd), 2)
            FROM w.staging.stg_revenue_lines
            WHERE order_dt = DATE '2026-03-02' AND customer_id IN (
              SELECT customer_id FROM w.warehouse.mart_revenue_dim__dbt_backup
              GROUP BY 1 HAVING COUNT(*) > 1)""")[0])
        if over != CONTRACT["overstatement"] or launch != over:
            problems.append(
                f"the arithmetic does not close: the tick's excess is {over}, the "
                f"launch-day revenue of the accounts the leftover relation "
                f"duplicates is {launch}, contract {CONTRACT['overstatement']}. "
                "The agent proves the diagnosis by these two being the same "
                "number, and only the leftover relation names that set.")

        n_diff, ts_diff = one(f"""
            SELECT COUNT(*), COUNT(DISTINCT snapshot_ts) FROM (
              SELECT * FROM w.warehouse.rev_snapshot
              EXCEPT ALL SELECT * FROM e.warehouse.rev_snapshot)""")
        if n_diff != CONTRACT["snapshot_rows_differing"] or ts_diff != 1:
            problems.append(
                f"the reporting record differs in {n_diff} row(s) across "
                f"{ts_diff} tick(s); the contract is "
                f"{CONTRACT['snapshot_rows_differing']} rows at one tick. "
                "Anything else and the record check grades the wrong thing.")

        wj, ej = one(f"""SELECT
            (SELECT ROUND(SUM(amount_usd), 2) FROM w.staging.stg_revenue_lines
             WHERE currency = 'JPY' AND order_dt BETWEEN DATE '{FX0}' AND DATE '{FX1}'),
            (SELECT ROUND(SUM(amount_usd), 2) FROM e.staging.stg_revenue_lines
             WHERE currency = 'JPY' AND order_dt BETWEEN DATE '{FX0}' AND DATE '{FX1}')""")
        pct = 100 * (float(wj) / float(ej) - 1)
        if not (CONTRACT["jpy_overstatement_pct_min"] <= pct
                <= CONTRACT["jpy_overstatement_pct_max"]):
            problems.append(f"APAC is overstated {pct:.2f}%, contract band "
                            f"{CONTRACT['jpy_overstatement_pct_min']}-"
                            f"{CONTRACT['jpy_overstatement_pct_max']}%")
        con.close()

        why = _mechanism_fires(fx / "world.duckdb")
        if why:
            problems.append(why)

    if problems:
        raise FixtureDrift(
            "FIXTURE DOES NOT MATCH CONTRACT\n\n  "
            + "\n  ".join(problems)
            + "\n\n  Regenerate:\n    python fixtures/make_fixture_world.py --out "
            + str(fx) + " --seed 4211"
            + "\n\n  Scores computed against a drifted fixture are meaningless.")

    if not quiet:
        print(f"  fixture verified against contract "
              f"(reported {CONTRACT['reported_total']}, excess "
              f"{CONTRACT['overstatement']}, mechanism fires at the contract filename)")


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
