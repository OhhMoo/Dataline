"""Rebuild raw EDI and provider tables from every landed CSV partition."""
import argparse
from pathlib import Path

import duckdb

ENTITIES = {
    "edi_837_claims": (
        ["claim_id", "claim_version", "frequency_code", "transaction_control_number",
         "provider_id", "member_id", "service_date", "received_date", "service_code",
         "diagnosis_code", "billed_amount", "other_payer_id", "other_payer_paid_amount"],
        ["claim_id", "claim_version", "dt"],
    ),
    "edi_835_remits": (
        ["remit_id", "claim_id", "claim_version", "provider_id", "remit_date",
         "adjudication_status", "allowed_amount", "payer_paid_amount",
         "member_responsibility_amount", "contractual_adjustment_amount",
         "cob_adjustment_amount", "other_payer_paid_amount", "denial_reason_code"],
        ["claim_id", "claim_version", "remit_id", "dt"],
    ),
    "provider_roster": (
        ["provider_id", "provider_name", "specialty", "network_tier",
         "capitated_members", "capitation_pmpm", "contract_start_date"],
        ["provider_id", "dt"],
    ),
}


def load(con, landing: Path, entity: str) -> int:
    columns, order_by = ENTITIES[entity]
    glob = (landing / entity).as_posix() + "/dt=*/*.csv"
    spec = ", ".join(f"'{column}': 'VARCHAR'" for column in columns)
    con.execute(f"""
        CREATE OR REPLACE TABLE raw.{entity} AS
        SELECT {", ".join(columns)}, dt
        FROM read_csv('{glob}', header=true, hive_partitioning=true,
                      hive_types={{'dt': 'DATE'}}, columns={{{spec}}})
        ORDER BY {", ".join(order_by)}
    """)
    return con.execute(f"SELECT count(*) FROM raw.{entity}").fetchone()[0]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="load_edi")
    parser.add_argument("--landing", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args(argv)
    con = duckdb.connect(str(args.db))
    try:
        con.execute("PRAGMA threads=1")
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        for entity in ENTITIES:
            count = load(con, args.landing, entity)
            print(f"raw.{entity}: {count} rows")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
