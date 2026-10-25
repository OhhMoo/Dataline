"""Reload the immutable card-network feed and its control tables.

Usage: python pipelines/load_payments.py --workspace . --db ../var/warehouse/warehouse.duckdb
"""
import argparse
from pathlib import Path

import duckdb

FEED_COLUMNS = {
    "event_id": "VARCHAR", "delivered_at": "VARCHAR", "payment_day": "VARCHAR",
    "payment_id": "VARCHAR", "version": "VARCHAR", "event_type": "VARCHAR",
    "merchant_id": "VARCHAR", "amount_cents": "VARCHAR",
}


def _columns(columns: dict) -> str:
    return "{" + ", ".join(f"'{name}': '{kind}'" for name, kind in columns.items()) + "}"


def _path(path: Path) -> str:
    return path.as_posix().replace("'", "''")


def load(workspace: Path, db_path: Path) -> None:
    feed = _path(workspace / "data" / "payments_feed") + "/dt=*/*.csv"
    control = workspace / "data" / "control"
    con = duckdb.connect(str(db_path))
    try:
        con.execute("PRAGMA threads=1")
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        con.execute(f"""
            CREATE OR REPLACE TABLE raw.payments_events AS
            SELECT event_id,
                   CAST(delivered_at AS TIMESTAMP) AS delivered_at,
                   CAST(payment_day AS DATE) AS payment_day,
                   payment_id,
                   CAST(version AS INTEGER) AS version,
                   lower(event_type) AS event_type,
                   merchant_id,
                   CAST(amount_cents AS BIGINT) AS amount_cents,
                   CAST(dt AS DATE) AS dt,
                   regexp_extract(filename, 'data/payments_feed/.*', 0) AS source_partition
            FROM read_csv('{feed}', hive_partitioning=true, filename=true,
                          header=true, union_by_name=true, columns={_columns(FEED_COLUMNS)})
            ORDER BY payment_day, payment_id, version, event_id, delivered_at, source_partition
        """)
        con.execute(f"""
            CREATE OR REPLACE TABLE raw.merchant_terms AS
            SELECT merchant_id, merchant_name, CAST(interchange_bps AS INTEGER) AS interchange_bps
            FROM read_csv('{_path(control / 'merchant_terms.csv')}', header=true,
                          columns={_columns({'merchant_id': 'VARCHAR', 'merchant_name': 'VARCHAR', 'interchange_bps': 'VARCHAR'})})
            ORDER BY merchant_id
        """)
        con.execute(f"""
            CREATE OR REPLACE TABLE raw.settlement_config AS
            SELECT CAST(cutoff_day AS DATE) AS cutoff_day,
                   CAST(window_days AS INTEGER) AS window_days,
                   CAST(interchange_bps AS INTEGER) AS interchange_bps
            FROM read_csv('{_path(control / 'settlement_config.csv')}', header=true,
                          columns={_columns({'cutoff_day': 'VARCHAR', 'window_days': 'VARCHAR', 'interchange_bps': 'VARCHAR'})})
            ORDER BY cutoff_day
        """)
        con.execute(f"""
            CREATE OR REPLACE TABLE raw.settlement_corrections AS
            SELECT CAST(settlement_day AS DATE) AS settlement_day,
                   ticket, approved_by, CAST(approved_at AS DATE) AS approved_at
            FROM read_csv('{_path(control / 'settlement_corrections.csv')}', header=true,
                          columns={_columns({'settlement_day': 'VARCHAR', 'ticket': 'VARCHAR', 'approved_by': 'VARCHAR', 'approved_at': 'VARCHAR'})})
            ORDER BY settlement_day, approved_at, ticket
        """)
    finally:
        con.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="load_payments")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args(argv)
    load(args.workspace.resolve(), args.db.resolve())
    con = duckdb.connect(str(args.db), read_only=True)
    try:
        counts = {table: con.execute(f"SELECT count(*) FROM raw.{table}").fetchone()[0]
                  for table in ("payments_events", "merchant_terms", "settlement_corrections")}
    finally:
        con.close()
    print("raw load: " + ", ".join(f"{table}={count}" for table, count in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
