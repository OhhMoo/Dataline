"""Nightly shop extracts -> raw.* in the warehouse.

The shop platform exports one change-data-capture extract per business day:

    data/landing/<entity>/dt=YYYY-MM-DD/<entity>.csv

for entity in orders, order_lines, customers, products. An order shows up again
in a later partition every time its status changes (cancelled the next day,
refunded weeks later), and an account every time it changes segment. Picking
the latest version of each is staging's job (dbt_project/models/staging), not
this script's: raw keeps every version, as landed, so a restatement can always
be traced back to the partition that caused it.

Full reload, every night. Each raw table is rebuilt with CREATE OR REPLACE over
every partition on disk, so re-running is safe, a re-landed partition replaces
itself, and a partition that landed late is picked up by the next run without
anybody doing anything. It also means anything on disk under dt=*/ that ends in
.csv is loaded - including a file nobody meant to be there.

Everything is VARCHAR at this layer. Casts happen in staging, where a bad value
fails a dbt test with a row to look at, instead of failing here as a null.

Usage: load_extracts.py --landing <data/landing> --db <warehouse.duckdb>
"""
import argparse
from pathlib import Path

import duckdb

# entity -> (columns in file order, ORDER BY for a deterministic table layout)
ENTITIES = {
    "orders": (["order_id", "customer_id", "ordered_at", "status", "subtotal",
                "shipping", "currency", "updated_at"],
               ["dt", "order_id", "updated_at"]),
    "order_lines": (["order_line_id", "order_id", "product_id", "quantity",
                     "unit_price", "line_amount", "created_at"],
                    ["dt", "order_line_id"]),
    "customers": (["customer_id", "customer_name", "email", "segment", "region",
                   "country", "signup_date", "updated_at"],
                  ["dt", "customer_id", "updated_at"]),
    "products": (["product_id", "sku", "product_name", "category", "unit_price",
                  "is_active", "updated_at"],
                 ["dt", "product_id", "updated_at"]),
}


def load(con, landing: Path, entity: str) -> int:
    columns, order_by = ENTITIES[entity]
    glob = (landing / entity).as_posix() + "/dt=*/*.csv"
    spec = ", ".join(f"'{c}': 'VARCHAR'" for c in columns)
    # `dt` comes from the folder name (hive partitioning) and is the only typed
    # column here: it is the business date the extract covers, and the
    # partition a re-landing or a backfill is addressed by.
    con.execute(f"""
        CREATE OR REPLACE TABLE raw.{entity} AS
        SELECT {", ".join(columns)}, dt
        FROM read_csv('{glob}', header = true, hive_partitioning = true,
                      hive_types = {{'dt': 'DATE'}}, columns = {{{spec}}})
        ORDER BY {", ".join(order_by)}""")
    return con.execute(f"SELECT count(*) FROM raw.{entity}").fetchone()[0]


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="load_extracts")
    p.add_argument("--landing", type=Path, required=True,
                   help="landing root holding one folder per entity")
    p.add_argument("--db", type=Path, required=True,
                   help="path to the warehouse duckdb file")
    p.add_argument("--only", choices=sorted(ENTITIES), action="append",
                   help="reload just these entities (default: all four)")
    a = p.parse_args(argv)
    con = duckdb.connect(str(a.db))
    try:
        # threads=1 + ORDER BY: one row order, every rebuild.
        con.execute("PRAGMA threads=1")
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        for entity in a.only or ENTITIES:
            n = load(con, a.landing, entity)
            print(f"raw.{entity}: {n} rows")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
