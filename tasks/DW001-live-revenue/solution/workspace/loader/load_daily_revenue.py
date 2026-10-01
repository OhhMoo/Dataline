"""Rebuild raw.daily_revenue from the landing zone.

Full reload over every dt= partition, not an append past a checkpoint. The
incremental loader advanced its checkpoint to the newest partition it SAW, so
a partition that landed after a newer one had already been loaded was behind
the checkpoint and was never read. A reload over an immutable landing zone
cannot skip anything; raw.load_checkpoint is still written (the newest
partition actually read) so anything that reads it keeps working.

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

SOURCE = "daily_revenue"
COLUMNS = {"order_id": "VARCHAR", "order_date": "VARCHAR",
           "customer_id": "VARCHAR", "amount": "VARCHAR",
           "terminal_ref": "VARCHAR"}


def load(landing: Path, db_path: Path) -> dict:
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
                   customer_id, amount, terminal_ref,
                   cast(dt as date) AS dt
            FROM read_csv('{glob}', header=true, hive_partitioning=true,
                          columns={{{cols}}})
            ORDER BY dt, order_id, customer_id, amount, terminal_ref""")
        last = con.execute("SELECT max(dt) FROM raw.daily_revenue").fetchone()[0]
        con.execute("CREATE TABLE IF NOT EXISTS raw.load_checkpoint "
                    "(source VARCHAR, last_dt DATE)")
        con.execute("DELETE FROM raw.load_checkpoint WHERE source = ?", [SOURCE])
        if last is not None:
            con.execute("INSERT INTO raw.load_checkpoint VALUES (?, ?)",
                        [SOURCE, last])
        return {"checkpoint": last}
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
    out = load(a.landing, a.db)
    con = duckdb.connect(str(a.db), read_only=True)
    try:
        n = con.execute("select count(*) from raw.daily_revenue").fetchone()[0]
    finally:
        con.close()
    print(f"full reload; checkpoint {out['checkpoint']}; "
          f"raw.daily_revenue: {n} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
