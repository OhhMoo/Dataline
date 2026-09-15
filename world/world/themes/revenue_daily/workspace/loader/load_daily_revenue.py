"""Rebuild raw.daily_revenue from the landing zone.

Full reload, not an append: every dt= partition under --landing is read on
every run and raw.daily_revenue is replaced. That is deliberate. An append
loader has to know what it already loaded, and the thing it uses to know
(a watermark, a filename, a run id) is the thing that goes wrong; a reload
over an immutable landing zone cannot double-count.

PRAGMA threads=1 and an explicit ORDER BY so the DuckDB page layout is the
same on every run and on every machine.

    python loader/load_daily_revenue.py \
        --landing data/landing/daily_revenue \
        --db ../var/warehouse/warehouse.duckdb
"""
import argparse
import sys
from pathlib import Path

import duckdb

COLUMNS = {"order_id": "VARCHAR", "order_date": "VARCHAR",
           "customer_id": "VARCHAR", "amount": "VARCHAR"}


def load(landing: Path, db_path: Path) -> None:
    glob = Path(landing).as_posix() + "/dt=*/*.csv"
    cols = ", ".join(f"'{k}': '{v}'" for k, v in COLUMNS.items())
    con = duckdb.connect(str(db_path))
    try:
        con.execute("PRAGMA threads=1")
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        con.execute(f"""
            CREATE OR REPLACE TABLE raw.daily_revenue AS
            SELECT cast(order_id as integer) AS order_id,
                   cast(order_date as date) AS order_date,
                   customer_id, amount, dt
            FROM read_csv('{glob}', hive_partitioning=true, columns={{{cols}}})
            ORDER BY order_date, order_id""")
    finally:
        con.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="load_daily_revenue")
    p.add_argument("--landing", type=Path, required=True,
                   help="landing root holding dt=YYYY-MM-DD partitions")
    p.add_argument("--db", type=Path, required=True,
                   help="path to the warehouse duckdb file")
    a = p.parse_args(argv)
    if not a.landing.is_dir():
        print(f"landing root not found: {a.landing}", file=sys.stderr)
        return 2
    load(a.landing, a.db)
    con = duckdb.connect(str(a.db), read_only=True)
    try:
        n = con.execute("select count(*) from raw.daily_revenue").fetchone()[0]
    finally:
        con.close()
    print(f"raw.daily_revenue: {n} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
