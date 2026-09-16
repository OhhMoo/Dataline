"""Divergence suite: run one probe list against a Snowflake-shaped socket and classify.

The third column is the one that matters. A probe is scored as:

  pass      the fake returned what real Snowflake returns;
  loud      the fake refused, and an agent can attribute the refusal (safe);
  SILENT    the fake reported success while doing something else (disqualifying);
  slow      the fake answered, but only after a retry storm (see --slow-s).

Usage:
    python snowflake_divergence.py --port 54301 [--json out.json]

Scope is semantics, not the socket; snowflake_acceptance.py owns TLS/DNS/wire.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

SLOW_S = 5.0

# (id, sql, class, expectation) - expectation is what REAL Snowflake does, prose.
PROBES: list[tuple[str, str, str, str]] = [
    # --- setup -------------------------------------------------------------
    ("setup.db", "create database if not exists db1", "setup", "creates"),
    ("setup.schema", "create schema if not exists db1.sch1", "setup", "creates"),
    ("setup.use", "use schema db1.sch1", "setup", "sets session schema"),
    ("setup.t1", "create or replace table t1 (id int, v varchar)", "setup", "creates"),
    ("setup.rows", "insert into t1 values (1,'a'),(2,'b')", "setup", "2 rows"),
    # --- R004 2.2: the blocking defect -------------------------------------
    (
        "temp.unqualified",
        "create temporary table tmp_unqualified as select 1 as a",
        "blocking",
        "creates a temp table",
    ),
    (
        "temp.three_part",
        "create temporary table db1.sch1.tmp_three as select 1 as a",
        "blocking",
        "creates a temp table; Snowflake accepts the qualified name",
    ),
    (
        "temp.two_part",
        "create temporary table sch1.tmp_two as select 1 as a",
        "blocking",
        "creates a temp table; Snowflake accepts the qualified name",
    ),
    (
        "temp.three_part_read",
        "select a from db1.sch1.tmp_three",
        "blocking",
        "reads the temp table back by its qualified name",
    ),
    (
        "temp.transient",
        "create transient table db1.sch1.tr1 as select 1 as a",
        "blocking",
        "creates a transient table",
    ),
    # The shape dbt-snowflake's incremental materialization actually emits, taken
    # verbatim from GT004's dbt.log. R004 §2.2 named CREATE TEMPORARY TABLE; the
    # statement that breaks the second run is a temporary VIEW.
    (
        "temp.dbt_view",
        "create or replace temporary view db1.sch1.m__dbt_tmp as (select 1 as a)",
        "blocking",
        "creates the tmp relation dbt merges from",
    ),
    (
        "temp.dbt_view_read",
        "select a from db1.sch1.m__dbt_tmp",
        "blocking",
        "the merge reads the tmp relation by its qualified name",
    ),
    (
        "temp.dbt_view_drop",
        "drop view if exists db1.sch1.m__dbt_tmp cascade",
        "blocking",
        "dbt drops the tmp relation at the end of the model",
    ),
    # --- the second blocker, found only by running dbt ----------------------
    # dbt-snowflake wraps its incremental merge in an explicit begin/commit. In
    # server mode fakesnow describes the last statement it ran, and DESCRIBE BEGIN
    # is a catalog lookup for a table called BEGIN.
    ("tx.begin", "begin", "blocking", "opens a transaction"),
    (
        "tx.begin_commented",
        'begin\n/* {"app": "dbt"} */',
        "blocking",
        "opens a transaction; the trailing comment is dbt's query header",
    ),
    ("tx.commit", "commit", "blocking", "commits"),
    # --- R004 2.3: the retry storm -----------------------------------------
    ("storm.use_warehouse", "use warehouse wh_etl", "landmine", "sets the session warehouse"),
    (
        "storm.file_format",
        "create file format if not exists ff1 type = 'CSV'",
        "landmine",
        "creates a file format",
    ),
    (
        "storm.values_dollar",
        "select $1 from values (1),(2)",
        "landmine",
        "returns 1 and 2",
    ),
    # --- R004 3.2: the silent-wrong set ------------------------------------
    ("wrong.div_zero", "select 1/0 as x", "silent", "100051 Division by zero"),
    (
        "wrong.div_zero_agg",
        "select sum(id/0) as x from t1",
        "silent",
        "100051 Division by zero",
    ),
    (
        "wrong.number_div",
        "select 1.0::number(38,10)/3 as x",
        "silent",
        "fixed-point NUMBER 0.3333333333",
    ),
    (
        "wrong.cluster_by",
        "alter table t1 cluster by (id)",
        "silent",
        "sets a clustering key that is visible afterwards",
    ),
    (
        "wrong.dynamic_table",
        "create dynamic table dt1 target_lag='1 minute' warehouse=wh_etl as select * from t1",
        "silent",
        "creates a self-refreshing table",
    ),
    (
        "wrong.dynamic_table_show",
        "show tables like 'DT1'",
        "silent",
        "lists DT1 with kind DYNAMIC TABLE, not TABLE",
    ),
    ("wrong.explain", "explain select * from t1", "silent", "a Snowflake query plan"),
    # --- R004 3.2 item 6: session identity ---------------------------------
    ("id.account", "select current_account() as v", "identity", "the account locator"),
    ("id.region", "select current_region() as v", "identity", "eg AWS_US_WEST_2"),
    ("id.warehouse", "select current_warehouse() as v", "identity", "the session warehouse"),
    ("id.session", "select current_session() as v", "identity", "a numeric session id"),
    ("id.role", "select current_role() as v", "identity", "the session role"),
    ("id.user", "select current_user() as v", "identity", "the login name"),
    ("id.version", "select current_version() as v", "identity", "eg 9.x.x"),
    ("id.client", "select current_client() as v", "identity", "eg PythonConnector 4.7.3"),
    ("id.last_query_id", "select last_query_id() as v", "identity", "the previous query's id"),
    ("id.show_warehouses", "show warehouses", "identity", "the account's warehouses"),
]


def run(host: str, port: int, network_timeout: int, slow_s: float) -> dict:
    import snowflake.connector as sc

    conn = sc.connect(
        account="acme",
        user="alkera",
        password="local",
        host=host,
        port=port,
        protocol="http",
        database="db1",
        schema="sch1",
        login_timeout=10,
        network_timeout=network_timeout,
    )
    results = []
    cur = conn.cursor()
    for pid, sql, cls, expect in PROBES:
        started = time.monotonic()
        try:
            cur.execute(sql)
            rows = cur.fetchall()
            elapsed = time.monotonic() - started
            outcome = {
                "id": pid,
                "class": cls,
                "sql": sql,
                "ok": True,
                "rows": [[str(v) for v in r] for r in rows][:5],
                "seconds": round(elapsed, 3),
            }
        except Exception as exc:  # noqa: BLE001 - the probe records every failure shape
            elapsed = time.monotonic() - started
            outcome = {
                "id": pid,
                "class": cls,
                "sql": sql,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}"[:220],
                "seconds": round(elapsed, 3),
            }
        outcome["real_snowflake"] = expect
        outcome["slow"] = elapsed >= slow_s
        results.append(outcome)
    cur.close()
    conn.close()
    return {"host": host, "port": port, "probes": results}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--network-timeout", type=int, default=25)
    ap.add_argument("--slow-s", type=float, default=SLOW_S)
    ap.add_argument("--json")
    args = ap.parse_args()

    report = run(args.host, args.port, args.network_timeout, args.slow_s)
    for p in report["probes"]:
        mark = "OK " if p["ok"] else "ERR"
        slow = " SLOW" if p["slow"] else ""
        detail = p.get("rows") if p["ok"] else p.get("error")
        print(f"{mark} {p['seconds']:7.3f}s{slow:5} {p['id']:26} {detail}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
