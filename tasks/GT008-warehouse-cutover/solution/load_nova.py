#!/usr/bin/env python3
"""Nightly load of the orders feed into NOVA, built to source contract v2.

    python pipelines/load_nova.py <nova.duckdb> <workspace>

Full reload every night from the immutable feed: every partition under
data/orders_feed into raw.order_events, Finance's adjustments file into
raw.finance_adjustments, then both marts rebuilt from scratch.

Current state of an order (contract section 3): the event with the HIGHEST
version, whatever partition it arrived in. A re-delivered event is identical
to the original and changes nothing. An order whose highest version is a
delete is absent. Approved adjustments (section 4) are added to the net of the
day the order currently belongs to.
"""
import sys
from pathlib import Path

import duckdb


def main(argv):
    if len(argv) < 2:
        print("usage: load_nova.py <nova.duckdb> <workspace>", file=sys.stderr)
        return 2
    db, workspace = Path(argv[0]), Path(argv[1])
    feed = (workspace / "data" / "orders_feed").as_posix().replace("'", "''")
    adjustments = (workspace / "data" / "finance" / "adjustments.csv").as_posix() \
        .replace("'", "''")
    partitions = sorted(p for p in (workspace / "data" / "orders_feed").glob("dt=*")
                        if p.is_dir())
    if not partitions:
        print("no dt= partitions under data/orders_feed", file=sys.stderr)
        return 1
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh")
    con.execute("CREATE SCHEMA IF NOT EXISTS wh.raw")
    con.execute("CREATE SCHEMA IF NOT EXISTS wh.warehouse")
    con.execute("USE wh.warehouse")
    con.execute(f"""
        CREATE OR REPLACE TABLE raw.order_events AS
        SELECT event_id::VARCHAR              AS event_id,
               order_id::VARCHAR              AS order_id,
               version::INTEGER               AS version,
               event_type::VARCHAR            AS event_type,
               order_day::DATE                AS order_day,
               customer_id::VARCHAR           AS customer_id,
               amount::DECIMAL(12,2)          AS amount,
               emitted_at::TIMESTAMP          AS emitted_at,
               dt::DATE                       AS delivered_dt
        FROM read_csv('{feed}/dt=*/events.csv', header=true, all_varchar=true,
                      hive_partitioning=true, union_by_name=true)
    """)
    con.execute(f"""
        CREATE OR REPLACE TABLE raw.finance_adjustments AS
        SELECT adjustment_id::VARCHAR AS adjustment_id,
               order_id::VARCHAR      AS order_id,
               amount_delta::DECIMAL(12,2) AS amount_delta,
               status::VARCHAR        AS status,
               requested_on::DATE     AS requested_on,
               NULLIF(approved_on, '')::DATE AS approved_on
        FROM read_csv('{adjustments}', header=true, all_varchar=true)
    """)
    # contract section 3: the highest version is current, whatever arrived last
    con.execute("""
        CREATE OR REPLACE TABLE warehouse.fct_orders AS
        SELECT order_id, version, order_day, customer_id, amount
        FROM (
            SELECT *, ROW_NUMBER() OVER (
                       PARTITION BY order_id
                       ORDER BY version DESC, emitted_at DESC, event_id) AS rn
            FROM raw.order_events
        )
        WHERE rn = 1 AND event_type <> 'delete'
        ORDER BY order_id
    """)
    con.execute("""
        CREATE OR REPLACE TABLE warehouse.rpt_revenue_daily AS
        WITH adj AS (
            SELECT order_id, SUM(amount_delta) AS delta
            FROM raw.finance_adjustments
            WHERE status = 'approved'
            GROUP BY order_id
        )
        SELECT o.order_day,
               COUNT(*)::BIGINT                                   AS orders,
               SUM(o.amount)::DECIMAL(12,2)                       AS gross_amount,
               COALESCE(SUM(a.delta), 0)::DECIMAL(12,2)           AS adjustments,
               (SUM(o.amount) + COALESCE(SUM(a.delta), 0))::DECIMAL(12,2) AS net_amount
        FROM warehouse.fct_orders o
        LEFT JOIN adj a USING (order_id)
        GROUP BY o.order_day
        ORDER BY o.order_day
    """)
    events = con.execute("SELECT COUNT(*) FROM raw.order_events").fetchone()[0]
    orders = con.execute("SELECT COUNT(*) FROM warehouse.fct_orders").fetchone()[0]
    days = con.execute("SELECT COUNT(*) FROM warehouse.rpt_revenue_daily").fetchone()[0]
    con.close()
    print(f"nova: {events} event(s) from {len(partitions)} partition(s); "
          f"{orders} current order(s); {days} day(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
