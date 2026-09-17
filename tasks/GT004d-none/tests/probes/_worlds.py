#!/usr/bin/env python3
"""
ADVERSARIAL WORLDS for proving the GT004 probes.

This is NOT part of the graded task and GRADER never imports it.  It exists
because this repo's standing rule is that a check which has never run against the
thing it ships against proves nothing: every probe in this package has to be
demonstrated both passing and failing, and that needs worlds built on purpose to
make it do each.

The worlds here are GT004-SHAPED, not GT004: a landing zone of daily partitions,
a raw table reloaded from it, a fact table with a business key and a date column,
a repaired window, a dashboard over that window, and a scheduler state file.  The
real fixtures are WORLD's to build.  What is proven here is the PROBE's logic -
that liveness is fatal, that a hardcoded repair dies on late arrival, that a
moved dashboard definition is attributed - not GT004's numbers.

Every text file is written with newline="" (determinism discipline) and every
warehouse is opened through an aliased ATTACH at the CONTRACT filename
var/warehouse/warehouse.duckdb, so the catalog/schema collision the deployed
world has is reproduced rather than dodged.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from ._common import open_world, warehouse_path, write_text

WINDOW = ("2026-02-16", "2026-02-22")
OUTSIDE = ("2026-02-23", "2026-02-25")
ROWS_PER_DAY = 5
LANDING = "data/landing/daily_revenue"
HEADER = "event_id,revenue_dt,customer_id,amount"

# ---------------------------------------------------------------- the files
LOADER = '''"""Reload raw.daily_revenue from the landing zone (immutable partitions)."""
import argparse
from pathlib import Path

import duckdb


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--landing", type=Path, required=True)
    p.add_argument("--db", type=Path, required=True)
    a = p.parse_args()
    glob = a.landing.as_posix() + "/dt=*/*.csv"
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + a.db.as_posix() + "' AS wh")
    con.execute("USE wh.warehouse")
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    con.execute(
        "CREATE OR REPLACE TABLE raw.daily_revenue AS "
        "SELECT cast(event_id AS BIGINT) AS event_id, "
        "       cast(revenue_dt AS DATE) AS revenue_dt, "
        "       customer_id, cast(amount AS DECIMAL(12,2)) AS amount "
        "FROM read_csv('" + glob + "', header=true, columns={"
        "'event_id':'VARCHAR','revenue_dt':'VARCHAR',"
        "'customer_id':'VARCHAR','amount':'VARCHAR'}) "
        "ORDER BY revenue_dt, event_id")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

INSERT_COLS = ("event_id, revenue_dt, customer_id, amount, "
               "TIMESTAMP '2026-03-01 00:00:00', 'probe-batch'")

# A genuine merge: everything the landing zone holds is restated by key.  New
# partitions land once, existing ones are rewritten to identical business
# content, and a second run is a no-op.
PIPE_MERGE = f"""-- upsert warehouse.fct_revenue from raw.daily_revenue by business key
DELETE FROM warehouse.fct_revenue
 WHERE event_id IN (SELECT event_id FROM raw.daily_revenue);

INSERT INTO warehouse.fct_revenue
SELECT {INSERT_COLS} FROM raw.daily_revenue;
"""

# The unrepaired defect: append mode.  Every run re-appends the whole landing
# zone, which is how the window doubled in the first place.
PIPE_APPEND = f"""-- append-only load; this is the defect, not the repair
INSERT INTO warehouse.fct_revenue
SELECT {INSERT_COLS} FROM raw.daily_revenue;
"""

# The adversary the liveness clause exists for: idempotent by never loading
# anything.  "Only load when the table is empty" is a real pattern and it is
# perfectly idempotent; it also repairs nothing and must score ZERO.
PIPE_SKIP = f"""-- guard: only load when the target is empty
INSERT INTO warehouse.fct_revenue
SELECT {INSERT_COLS} FROM raw.daily_revenue
 WHERE NOT EXISTS (SELECT 1 FROM warehouse.fct_revenue);
"""

# The adversary G3 exists for: a repair that hardcodes "delete the window and
# reinsert the snapshot I saw".  Correct at the moment it is written, idempotent,
# leaves the window untouched, ingests new partitions - and silently discards
# every event that arrives inside the window afterwards.
PIPE_HARDCODED_TMPL = """-- restate the window from the snapshot captured during the repair
DELETE FROM warehouse.fct_revenue
 WHERE revenue_dt BETWEEN DATE '{w0}' AND DATE '{w1}';

INSERT INTO warehouse.fct_revenue VALUES
{values};

-- everything outside the window is merged normally
DELETE FROM warehouse.fct_revenue
 WHERE event_id IN (SELECT event_id FROM raw.daily_revenue
                     WHERE revenue_dt NOT BETWEEN DATE '{w0}' AND DATE '{w1}');

INSERT INTO warehouse.fct_revenue
SELECT event_id, revenue_dt, customer_id, amount,
       TIMESTAMP '2026-03-01 00:00:00', 'probe-batch'
  FROM raw.daily_revenue
 WHERE revenue_dt NOT BETWEEN DATE '{w0}' AND DATE '{w1}';
"""

TEST_DISCRIMINATING = '''"""No business key may appear twice in the revenue fact."""
import os

import duckdb


def _con():
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + os.environ["ALKERA_WAREHOUSE"].replace("\\\\", "/")
                + "' AS wh (READ_ONLY)")
    con.execute("USE wh.warehouse")
    return con


def test_no_duplicate_event_ids():
    con = _con()
    try:
        n = con.execute(
            "SELECT COUNT(*) FROM (SELECT event_id FROM warehouse.fct_revenue "
            "GROUP BY 1 HAVING COUNT(*) > 1)").fetchone()[0]
    finally:
        con.close()
    assert n == 0, f"{n} event_id(s) appear more than once"
'''

TEST_VACUOUS = '''"""A test that cannot fail: it asserts something true of every world."""


def test_revenue_pipeline_exists():
    assert True
'''

DASHBOARD_LIVE = """dashboard: revenue_daily
panels:
- name: window_total
  sql: SELECT SUM(amount) AS revenue_usd, COUNT(*) AS row_count FROM warehouse.fct_revenue
    WHERE revenue_dt BETWEEN DATE '{w0}' AND DATE '{w1}'
  title: Repaired window
title: Daily revenue
version: 1
"""

# The "shipped the code, forgot the migration" adversary: the mart is right, the
# exposure still reads the snapshot table nobody rebuilt.
DASHBOARD_STALE = """dashboard: revenue_daily
panels:
- name: window_total
  sql: SELECT SUM(amount) AS revenue_usd, COUNT(*) AS row_count FROM warehouse.fct_revenue_snapshot
    WHERE revenue_dt BETWEEN DATE '{w0}' AND DATE '{w1}'
  title: Repaired window
title: Daily revenue
version: 1
"""

DASHBOARD_BROKEN = """dashboard: revenue_daily
panels:
- name: window_total
  sql: SELECT SUM(amount) AS revenue_usd FROM warehouse.table_that_was_dropped
  title: Repaired window
title: Daily revenue
version: 1
"""


def _dates(a: str, b: str):
    d0, d1 = date.fromisoformat(a), date.fromisoformat(b)
    out = []
    while d0 <= d1:
        out.append(d0.isoformat())
        d0 += timedelta(days=1)
    return out


def _rows():
    """The whole landing zone as (event_id, date, customer, amount)."""
    out, eid = [], 1000
    for d in _dates(*WINDOW) + _dates(*OUTSIDE):
        for i in range(ROWS_PER_DAY):
            eid += 1
            out.append((eid, d, f"C-{i:02d}", Decimal("10.00") + i))
    return out


def build(root: Path, *, pipeline: str = "merge", doubled: bool = False,
          test: str | None = "discriminating", dashboard: str = "live",
          snapshot_table: bool = True, damage: bool = False) -> dict:
    """Build one GT004-shaped world and return the numbers it implies.

    pipeline  merge | append | skip | hardcoded - the agent's repaired loader
    doubled   True  -> the window is still doubled (a PRE-REPAIR world)
    test      discriminating | vacuous | None - what the agent left behind
    dashboard live | stale | broken
    damage    True  -> also rewrite the dashboard definition and drop an
                       unrelated table, i.e. collateral the task never asked for
    """
    root = Path(root)
    ws = root / "workspace"
    rows = _rows()
    w0, w1 = WINDOW

    # ---- landing zone (immutable daily partitions)
    by_date = {}
    for eid, d, cust, amt in rows:
        by_date.setdefault(d, []).append(f"{eid},{d},{cust},{amt}")
    for d, lines in by_date.items():
        write_text(ws / LANDING / f"dt={d}" / "part-000.csv",
                   "\n".join([HEADER, *lines]) + "\n")

    # ---- warehouse at the CONTRACT filename
    db = warehouse_path(root)
    db.parent.mkdir(parents=True, exist_ok=True)
    # a fresh database has no `warehouse` schema yet, so `USE wh.warehouse`
    # cannot be the first statement; create it, then enter it the normal way.
    import duckdb
    boot = duckdb.connect(":memory:", config={"threads": 1})
    boot.execute(f"ATTACH '{db.as_posix()}' AS wh")
    boot.execute("CREATE SCHEMA IF NOT EXISTS wh.warehouse")
    boot.close()
    con = open_world(db)
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        con.execute("CREATE SCHEMA IF NOT EXISTS staging")
        con.execute("CREATE TABLE warehouse.fct_revenue ("
                    "event_id BIGINT, revenue_dt DATE, customer_id VARCHAR, "
                    "amount DECIMAL(12,2), load_ts TIMESTAMP, batch_id VARCHAR)")
        con.execute("CREATE TABLE staging.stg_revenue ("
                    "event_id BIGINT, revenue_dt DATE, amount DECIMAL(12,2))")
        con.execute("CREATE TABLE raw.daily_revenue ("
                    "event_id BIGINT, revenue_dt DATE, customer_id VARCHAR, "
                    "amount DECIMAL(12,2))")
        for eid, d, cust, amt in rows:
            con.execute("INSERT INTO warehouse.fct_revenue VALUES "
                        "(?, ?, ?, ?, TIMESTAMP '2026-02-26 02:00:00', 'seed')",
                        [eid, d, cust, amt])
            con.execute("INSERT INTO raw.daily_revenue VALUES (?, ?, ?, ?)",
                        [eid, d, cust, amt])
            con.execute("INSERT INTO staging.stg_revenue VALUES (?, ?, ?)",
                        [eid, d, amt])
        if doubled:
            con.execute(f"INSERT INTO warehouse.fct_revenue SELECT * FROM "
                        f"warehouse.fct_revenue WHERE revenue_dt BETWEEN "
                        f"DATE '{w0}' AND DATE '{w1}'")
        if snapshot_table:
            # a BI snapshot table that still carries the doubled figures - the
            # "mart is right, the exposure is stale" shape
            con.execute("CREATE TABLE warehouse.fct_revenue_snapshot AS "
                        "SELECT * FROM warehouse.fct_revenue")
            con.execute(f"INSERT INTO warehouse.fct_revenue_snapshot SELECT * FROM "
                        f"warehouse.fct_revenue_snapshot WHERE revenue_dt BETWEEN "
                        f"DATE '{w0}' AND DATE '{w1}'")
        if damage:
            con.execute("DROP TABLE staging.stg_revenue")
        total, n = con.execute(
            f"SELECT SUM(amount), COUNT(*) FROM warehouse.fct_revenue "
            f"WHERE revenue_dt BETWEEN DATE '{w0}' AND DATE '{w1}'").fetchone()
        snap_total = None
        if snapshot_table:
            snap_total = con.execute(
                f"SELECT SUM(amount) FROM warehouse.fct_revenue_snapshot "
                f"WHERE revenue_dt BETWEEN DATE '{w0}' AND DATE '{w1}'").fetchone()[0]
        window_rows = con.execute(
            f"SELECT event_id, revenue_dt, customer_id, amount FROM "
            f"warehouse.fct_revenue WHERE revenue_dt BETWEEN DATE '{w0}' "
            f"AND DATE '{w1}' ORDER BY event_id").fetchall()
    finally:
        con.close()

    # ---- the agent's pipeline
    write_text(ws / "pipelines" / "load_landing.py", LOADER)
    if pipeline == "merge":
        write_text(ws / "pipelines" / "fct_upsert.sql", PIPE_MERGE)
    elif pipeline == "append":
        write_text(ws / "pipelines" / "fct_upsert.sql", PIPE_APPEND)
    elif pipeline == "skip":
        write_text(ws / "pipelines" / "fct_upsert.sql", PIPE_SKIP)
    elif pipeline == "hardcoded":
        values = ",\n".join(
            f"({e}, DATE '{d}', '{c}', {a}, TIMESTAMP '2026-03-01 00:00:00', "
            f"'probe-batch')" for e, d, c, a in window_rows)
        write_text(ws / "pipelines" / "fct_upsert.sql",
                   PIPE_HARDCODED_TMPL.format(w0=w0, w1=w1, values=values))
    else:
        raise ValueError(pipeline)

    # ---- the agent's regression test
    if test == "discriminating":
        write_text(ws / "tests" / "test_no_doubling.py", TEST_DISCRIMINATING)
    elif test == "vacuous":
        write_text(ws / "tests" / "test_no_doubling.py", TEST_VACUOUS)

    # ---- surfaces
    body = {"live": DASHBOARD_LIVE, "stale": DASHBOARD_STALE,
            "broken": DASHBOARD_BROKEN}[dashboard]
    write_text(root / "var" / "bi" / "dashboards" / "revenue_daily.yml",
               body.format(w0=w0, w1=w1))
    if damage:
        write_text(root / "var" / "bi" / "dashboards" / "revenue_daily.yml",
                   body.format(w0=w0, w1=w1).replace("Daily revenue",
                                                     "Daily revenue (edited)"))
    write_text(root / "var" / "sched" / "state.json",
               json.dumps({"jobs": {"revenue_daily": {"command": "run_pipeline"}},
                           "runs": [], "vnow": "2026-02-26T09:00:00Z"},
                          sort_keys=True, separators=(",", ":")) + "\n")
    write_text(root / "build" / "provenance.json", "{}\n")

    return {"window": {"start": w0, "end": w1},
            "window_total": str(total), "window_rows": int(n),
            "snapshot_total": None if snap_total is None else str(snap_total)}


def contract(numbers: dict) -> dict:
    """The CONTRACT shape the probes read.  Built FROM the world, never typed in."""
    w0, w1 = numbers["window"]["start"], numbers["window"]["end"]
    return {"probes": {
        "world": {
            "fact_table": "warehouse.fct_revenue",
            "business_key": ["event_id"],
            "date_column": "revenue_dt",
            "audit_columns": ["load_ts", "batch_id"],
            "window": {"start": w0, "end": w1},
        },
        "pipeline": {"steps": [
            {"path": "pipelines/load_landing.py",
             "argv": ["--landing", "{workspace}/" + LANDING, "--db", "{db}"]},
            "pipelines/fct_upsert.sql",
        ]},
        "idempotency": {"plant": {
            "mode": "landing", "dir": LANDING + "/dt={date}",
            "filename": "part-000.csv", "header": HEADER,
            "row_template": "{id},{date},C-PROBE,{amount}",
            "date": "2026-03-01", "rows": 4, "id_base": 990000001,
            "amount": "12.34", "predicate": "customer_id = 'C-PROBE'"}},
        "late_arrival": {
            "payload": {"mode": "landing", "dir": LANDING + "/dt={date}",
                        "filename": "part-late.csv", "header": HEADER,
                        "row_template": "{id},{date},C-LATE,{amount}",
                        "predicate": "customer_id = 'C-LATE'",
                        "events": [{"date": "2026-02-18", "rows": 3,
                                    "id_base": 991000001, "amount": "77.00"},
                                   {"date": "2026-02-20", "rows": 2,
                                    "id_base": 991000101, "amount": "55.00"}]},
            "advance_to": "2026-03-02T09:00:00Z"},
        "exposure": {"dashboard": "revenue_daily", "expect": [
            {"panel": "window_total", "column": "revenue_usd",
             "value": numbers["window_total"], "tolerance": 0.01,
             "stale_value": numbers["snapshot_total"]}]},
        "collateral": {
            "may_move": {
                "warehouse": ["warehouse.fct_revenue", "staging.stg_revenue",
                              "raw.daily_revenue"],
                "workspace": ["pipelines/*", "tests/*",
                              "data/landing/daily_revenue/*", "*/__pycache__/*"]},
            "must_have_moved": ["warehouse"], "penalty_per_item": 2.0},
        "regression": {
            "search": ["tests/test_*.py", "tests/*.sql"],
            "reinjure": [f"INSERT INTO warehouse.fct_revenue SELECT * FROM "
                         f"warehouse.fct_revenue WHERE revenue_dt BETWEEN "
                         f"DATE '{w0}' AND DATE '{w1}'"]},
    }}
