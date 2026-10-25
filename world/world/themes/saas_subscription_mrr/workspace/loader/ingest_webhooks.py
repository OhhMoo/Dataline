"""Rebuild the billing raw layer from the landed webhook CSV partitions."""
import argparse
from pathlib import Path

import duckdb

EVENT_COLUMNS = {
    "event_id": "VARCHAR",
    "subscription_id": "VARCHAR",
    "account_id": "VARCHAR",
    "account_name": "VARCHAR",
    "segment": "VARCHAR",
    "event_type": "VARCHAR",
    "occurred_at": "VARCHAR",
    "effective_date": "VARCHAR",
    "plan_code": "VARCHAR",
    "seats": "VARCHAR",
    "unit_price_cents": "VARCHAR",
    "revrec_days": "VARCHAR",
    "reason": "VARCHAR",
}
INVOICE_COLUMNS = {
    "webhook_id": "VARCHAR",
    "invoice_id": "VARCHAR",
    "subscription_id": "VARCHAR",
    "account_id": "VARCHAR",
    "webhook_type": "VARCHAR",
    "invoice_type": "VARCHAR",
    "status": "VARCHAR",
    "invoice_date": "VARCHAR",
    "service_period_start": "VARCHAR",
    "service_period_end": "VARCHAR",
    "amount_cents": "VARCHAR",
    "currency": "VARCHAR",
}


def _mapping(columns: dict) -> str:
    body = ", ".join(f"'{name}': '{kind}'" for name, kind in columns.items())
    return "{" + body + "}"


def main() -> int:
    parser = argparse.ArgumentParser(prog="ingest_webhooks")
    parser.add_argument("--landing", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args()
    root = args.landing.resolve().as_posix()
    con = duckdb.connect(str(args.db))
    try:
        con.execute("PRAGMA threads=1")
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        con.execute(f"""
            CREATE OR REPLACE TABLE raw.subscription_events AS
            SELECT event_id, subscription_id, account_id, account_name, segment,
                   event_type, cast(occurred_at AS timestamp) AS occurred_at,
                   cast(effective_date AS date) AS effective_date,
                   plan_code, cast(seats AS integer) AS seats,
                   cast(unit_price_cents AS bigint) AS unit_price_cents,
                   cast(revrec_days AS integer) AS revrec_days, reason,
                   cast(dt AS date) AS source_date
            FROM read_csv('{root}/subscription_events/dt=*/*.csv',
                          hive_partitioning=true, columns={_mapping(EVENT_COLUMNS)})
            ORDER BY source_date, event_id
        """)
        con.execute(f"""
            CREATE OR REPLACE TABLE raw.invoice_webhooks AS
            SELECT webhook_id, invoice_id, subscription_id, account_id,
                   webhook_type, invoice_type, status,
                   cast(invoice_date AS date) AS invoice_date,
                   cast(service_period_start AS date) AS service_period_start,
                   cast(service_period_end AS date) AS service_period_end,
                   cast(amount_cents AS bigint) AS amount_cents, currency,
                   cast(dt AS date) AS source_date
            FROM read_csv('{root}/invoice_webhooks/dt=*/*.csv',
                          hive_partitioning=true, columns={_mapping(INVOICE_COLUMNS)})
            ORDER BY source_date, webhook_id
        """)
        counts = con.execute(
            "SELECT (SELECT count(*) FROM raw.subscription_events), "
            "(SELECT count(*) FROM raw.invoice_webhooks)"
        ).fetchone()
    finally:
        con.close()
    print(f"ingested {counts[0]} subscription events and {counts[1]} invoice webhooks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
