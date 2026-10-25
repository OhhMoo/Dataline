"""Rebuild all raw inventory tables from the immutable EDI landing partitions."""
import argparse
import sys
from pathlib import Path

import duckdb

TABLES = {
    "asn_receipts": (
        ["event_id", "asn_id", "asn_line_id", "edi_control_id", "warehouse_id",
         "sku_id", "supplier_id", "event_type", "event_date", "quantity", "uom",
         "expected_receipt_date", "customs_declaration_id"],
        ["event_date", "event_id"],
    ),
    "dc_movements": (
        ["movement_id", "movement_date", "warehouse_id", "sku_id", "movement_type",
         "quantity", "uom", "reference_id", "threepl_id"],
        ["movement_date", "movement_id"],
    ),
    "cycle_counts": (
        ["count_id", "count_date", "warehouse_id", "sku_id", "book_quantity_each",
         "counted_quantity", "uom", "count_method", "counter_id"],
        ["count_date", "count_id"],
    ),
    "uom_conversions": (
        ["effective_date", "warehouse_id", "sku_id", "product_family", "case_uom",
         "each_uom", "units_per_case", "receipt_rate_cents", "outbound_rate_cents",
         "storage_rate_cents"],
        ["effective_date", "warehouse_id", "sku_id"],
    ),
    "customs_declarations": (
        ["declaration_id", "asn_id", "warehouse_id", "entry_type", "declaration_status",
         "declared_at", "expected_arrival_date", "freeze_start_at", "freeze_end_at",
         "document_version"],
        ["declaration_id"],
    ),
}


def load(landing: Path, db_path: Path) -> None:
    con = duckdb.connect(str(db_path))
    try:
        con.execute("PRAGMA threads=1")
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        for table, (columns, order) in TABLES.items():
            path = landing.as_posix() + f"/dt=*/{table}.csv"
            declared = ", ".join(f"'{column}': 'VARCHAR'" for column in columns)
            selected = ", ".join(columns)
            ordered = ", ".join(order)
            con.execute(f"""
                CREATE OR REPLACE TABLE raw.{table} AS
                SELECT {selected}, cast(dt as date) AS dt
                FROM read_csv('{path}', hive_partitioning=true,
                              columns={{{declared}}})
                ORDER BY {ordered}
            """)
    finally:
        con.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="load_inventory")
    parser.add_argument("--landing", type=Path, required=True)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.landing.is_dir():
        print(f"landing root not found: {args.landing}", file=sys.stderr)
        return 2
    load(args.landing, args.db)
    con = duckdb.connect(str(args.db), read_only=True)
    try:
        for table in TABLES:
            count = con.execute(f"SELECT count(*) FROM raw.{table}").fetchone()[0]
            print(f"raw.{table}: {count} rows")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
