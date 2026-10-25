"""Full deterministic reload of merchant risk landing feeds into raw.*."""
import argparse
from pathlib import Path

import duckdb

ENTITIES = {
    "dispute_events": (
        ["event_id", "dispute_id", "merchant_id", "event_type", "event_at",
         "opened_at", "dispute_amount", "reason_code", "representment_case_id"],
        "dt, event_id",
    ),
    "kyc_reviews": (
        ["review_id", "merchant_id", "legal_name", "merchant_category", "country",
         "reviewed_at", "review_type", "kyc_status", "risk_tier", "reviewer_id",
         "next_review_date"],
        "dt, review_id",
    ),
    "merchant_volume": (
        ["merchant_id", "business_date", "settled_transactions", "settled_volume",
         "currency"],
        "dt, merchant_id",
    ),
    "payout_holds": (
        ["event_id", "hold_id", "request_id", "merchant_id", "event_type", "event_at",
         "reserve_amount", "release_amount", "reason_code", "risk_officer_id"],
        "dt, event_id",
    ),
}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="load_risk_feeds")
    parser.add_argument("--landing", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.landing.is_dir():
        parser.error(f"landing root not found: {args.landing}")
    con = duckdb.connect(str(args.db))
    try:
        con.execute("PRAGMA threads=1")
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        for entity in sorted(ENTITIES):
            columns, order_by = ENTITIES[entity]
            glob = (args.landing / entity).as_posix() + "/dt=*/*.csv"
            specification = ", ".join(f"'{column}': 'VARCHAR'" for column in columns)
            con.execute(f"""
                CREATE OR REPLACE TABLE raw.{entity} AS
                SELECT {", ".join(columns)}, dt
                FROM read_csv('{glob}', header=true, hive_partitioning=true,
                              hive_types={{'dt': 'DATE'}},
                              columns={{{specification}}})
                ORDER BY {order_by}""")
            count = con.execute(f"SELECT count(*) FROM raw.{entity}").fetchone()[0]
            print(f"raw.{entity}: {count} rows")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
