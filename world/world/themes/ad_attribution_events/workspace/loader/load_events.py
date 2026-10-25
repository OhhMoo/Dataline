"""Full deterministic reload of ad-event CSV exports into the raw schema."""
import argparse
import sys
from pathlib import Path

import duckdb

DAILY = {
    "impressions": {
        "columns": {"impression_id": "VARCHAR", "event_time": "VARCHAR",
                    "user_id": "VARCHAR", "partner_id": "VARCHAR",
                    "campaign_id": "VARCHAR", "placement": "VARCHAR",
                    "ip_prefix": "VARCHAR", "user_agent": "VARCHAR"},
        "select": "impression_id, cast(event_time as timestamp) as event_time, "
                  "user_id, partner_id, campaign_id, placement, ip_prefix, user_agent, "
                  "cast(dt as date) as dt",
        "order": "event_time, impression_id",
    },
    "clicks": {
        "columns": {"click_id": "VARCHAR", "impression_id": "VARCHAR",
                    "click_time": "VARCHAR", "user_id": "VARCHAR",
                    "partner_id": "VARCHAR", "campaign_id": "VARCHAR",
                    "ip_prefix": "VARCHAR", "user_agent": "VARCHAR",
                    "referrer": "VARCHAR"},
        "select": "click_id, impression_id, cast(click_time as timestamp) as click_time, "
                  "user_id, partner_id, campaign_id, ip_prefix, user_agent, referrer, "
                  "cast(dt as date) as dt",
        "order": "click_time, click_id",
    },
    "conversions": {
        "columns": {"postback_id": "VARCHAR", "conversion_id": "VARCHAR",
                    "event_time": "VARCHAR", "user_id": "VARCHAR",
                    "order_id": "VARCHAR", "revenue_cents": "VARCHAR",
                    "currency": "VARCHAR", "received_at": "VARCHAR"},
        "select": "postback_id, conversion_id, cast(event_time as timestamp) as event_time, "
                  "user_id, order_id, cast(revenue_cents as integer) as revenue_cents, "
                  "currency, cast(received_at as timestamp) as received_at, "
                  "cast(dt as date) as dt",
        "order": "event_time, conversion_id, postback_id",
    },
}
STATIC = {
    "partners": (
        {"partner_id": "VARCHAR", "partner_name": "VARCHAR",
         "revshare_bps": "VARCHAR", "currency": "VARCHAR"},
        "partner_id, partner_name, cast(revshare_bps as integer) as revshare_bps, currency",
        "partner_id",
    ),
    "bot_exclusions": (
        {"exclusion_id": "VARCHAR", "exclusion_type": "VARCHAR",
         "exclusion_value": "VARCHAR", "reason": "VARCHAR", "active": "VARCHAR"},
        "exclusion_id, exclusion_type, exclusion_value, reason, "
        "cast(active as boolean) as active",
        "exclusion_id",
    ),
    "attribution_rules": (
        {"rule_id": "VARCHAR", "lookback_days": "VARCHAR",
         "billing_lag_days": "VARCHAR"},
        "rule_id, cast(lookback_days as integer) as lookback_days, "
        "cast(billing_lag_days as integer) as billing_lag_days",
        "rule_id",
    ),
}


def sql_string(path: Path) -> str:
    return str(path).replace("'", "''")


def column_map(columns: dict) -> str:
    return "{" + ", ".join(f"'{key}': '{value}'" for key, value in columns.items()) + "}"


def load(landing: Path, config: Path, db_path: Path) -> None:
    con = duckdb.connect(str(db_path))
    try:
        con.execute("PRAGMA threads=1")
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        for table, spec in DAILY.items():
            glob = sql_string(landing / table) + "/dt=*/*.csv"
            con.execute(f"""
                CREATE OR REPLACE TABLE raw.{table} AS
                SELECT {spec['select']}
                FROM read_csv('{glob}', hive_partitioning=true,
                              columns={column_map(spec['columns'])})
                ORDER BY {spec['order']}
            """)
        for table, (columns, select, order) in STATIC.items():
            path = sql_string(config / f"{table}.csv")
            con.execute(f"""
                CREATE OR REPLACE TABLE raw.{table} AS
                SELECT {select}
                FROM read_csv('{path}', columns={column_map(columns)})
                ORDER BY {order}
            """)
    finally:
        con.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="load_events")
    parser.add_argument("--landing", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.landing.is_dir() or not args.config.is_dir():
        print("landing and config roots must exist", file=sys.stderr)
        return 2
    load(args.landing, args.config, args.db)
    con = duckdb.connect(str(args.db), read_only=True)
    try:
        for table in (*DAILY, *STATIC):
            count = con.execute(f"SELECT count(*) FROM raw.{table}").fetchone()[0]
            print(f"raw.{table}: {count} rows")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
