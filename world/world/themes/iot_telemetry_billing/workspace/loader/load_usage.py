#!/usr/bin/env python3
"""Full, deterministic reload of the IoT landing zone into raw.*."""
import argparse
from pathlib import Path

import duckdb


def quote(path: Path) -> str:
    return str(path).replace("'", "''")


def load(landing: Path, db: Path) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db))
    try:
        con.execute("PRAGMA threads=1")
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        heartbeat_glob = quote(landing / "meter_heartbeats") + "/dt=*/*.csv"
        rollup_glob = quote(landing / "usage_rollups") + "/dt=*/*.csv"
        contract_glob = quote(landing / "contract_commits") + "/dt=*/*.csv"
        con.execute("BEGIN TRANSACTION")
        con.execute(f"""
            CREATE OR REPLACE TABLE raw.meter_heartbeats AS
            SELECT heartbeat_id, device_id, customer_id, gateway_id, event_kind,
                   nullif(boot_id, '') AS boot_id,
                   cast(nullif(counter_value, '') AS BIGINT) AS counter_value,
                   cast(nullif(delta_units, '') AS BIGINT) AS delta_units,
                   cast(observed_at AS TIMESTAMP) AS observed_at,
                   cast(collected_at AS TIMESTAMP) AS collected_at,
                   cast(dt AS DATE) AS dt
            FROM read_csv('{heartbeat_glob}', hive_partitioning=true, columns={{
                'heartbeat_id': 'VARCHAR', 'device_id': 'VARCHAR',
                'customer_id': 'VARCHAR', 'gateway_id': 'VARCHAR',
                'event_kind': 'VARCHAR', 'boot_id': 'VARCHAR',
                'counter_value': 'VARCHAR', 'delta_units': 'VARCHAR',
                'observed_at': 'VARCHAR', 'collected_at': 'VARCHAR'}})
            ORDER BY observed_at, device_id, heartbeat_id
        """)
        con.execute(f"""
            CREATE OR REPLACE TABLE raw.usage_rollups AS
            SELECT rollup_id, device_id, customer_id, gateway_id,
                   cast(window_start AS TIMESTAMP) AS window_start,
                   cast(window_end AS TIMESTAMP) AS window_end,
                   cast(units AS BIGINT) AS units,
                   cast(source_event_count AS INTEGER) AS source_event_count,
                   cast(collected_at AS TIMESTAMP) AS collected_at,
                   cast(dt AS DATE) AS dt
            FROM read_csv('{rollup_glob}', hive_partitioning=true, columns={{
                'rollup_id': 'VARCHAR', 'device_id': 'VARCHAR',
                'customer_id': 'VARCHAR', 'gateway_id': 'VARCHAR',
                'window_start': 'VARCHAR', 'window_end': 'VARCHAR',
                'units': 'VARCHAR', 'source_event_count': 'VARCHAR',
                'collected_at': 'VARCHAR'}})
            ORDER BY window_start, device_id, rollup_id, collected_at
        """)
        con.execute(f"""
            CREATE OR REPLACE TABLE raw.contract_commits AS
            SELECT commit_id, contract_id, customer_id, customer_name,
                   cast(billing_month AS DATE) AS billing_month, metric,
                   cast(committed_units AS BIGINT) AS committed_units,
                   cast(monthly_minimum_cents AS BIGINT) AS monthly_minimum_cents,
                   cast(overage_cents_per_1000 AS BIGINT) AS overage_cents_per_1000,
                   cast(lock_grace_days AS INTEGER) AS lock_grace_days,
                   currency, cast(effective_at AS TIMESTAMP) AS effective_at,
                   cast(collected_at AS TIMESTAMP) AS collected_at,
                   cast(dt AS DATE) AS dt
            FROM read_csv('{contract_glob}', hive_partitioning=true, columns={{
                'commit_id': 'VARCHAR', 'contract_id': 'VARCHAR',
                'customer_id': 'VARCHAR', 'customer_name': 'VARCHAR',
                'billing_month': 'VARCHAR', 'metric': 'VARCHAR',
                'committed_units': 'VARCHAR', 'monthly_minimum_cents': 'VARCHAR',
                'overage_cents_per_1000': 'VARCHAR', 'lock_grace_days': 'VARCHAR',
                'currency': 'VARCHAR', 'effective_at': 'VARCHAR',
                'collected_at': 'VARCHAR'}})
            ORDER BY billing_month, customer_id, commit_id
        """)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--landing", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args()
    load(args.landing, args.db)


if __name__ == "__main__":
    main()
