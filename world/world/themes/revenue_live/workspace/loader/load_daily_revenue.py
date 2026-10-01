"""Load new landing partitions into raw.daily_revenue.

Incremental: only partitions newer than the last one loaded are read, and
raw.daily_revenue is appended to. The position is kept in
raw.load_checkpoint (one row per source), so a run that finds nothing new is
a cheap no-op instead of a re-read of the whole landing zone, which had grown
to a minute a night (DATA-131).

PRAGMA threads=1 and a fixed partition order so the DuckDB page layout is the
same on every run and on every machine.

    python loader/load_daily_revenue.py \
        --landing data/landing/daily_revenue \
        --db ../var/warehouse/warehouse.duckdb
"""
import argparse
import sys
from datetime import date
from pathlib import Path

import duckdb

SOURCE = "daily_revenue"
COLUMNS = {"order_id": "VARCHAR", "order_date": "VARCHAR",
           "customer_id": "VARCHAR", "amount": "VARCHAR",
           "terminal_ref": "VARCHAR"}


def partitions(landing: Path) -> list[date]:
    out = []
    for d in Path(landing).glob("dt=*"):
        if d.is_dir():
            out.append(date.fromisoformat(d.name[3:]))
    return sorted(out)


def _ensure(con) -> None:
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    con.execute("""
        CREATE TABLE IF NOT EXISTS raw.daily_revenue (
            order_id INTEGER, order_date DATE, customer_id VARCHAR,
            amount VARCHAR, terminal_ref VARCHAR, dt DATE)""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS raw.load_checkpoint (
            source VARCHAR, last_dt DATE)""")


def load(landing: Path, db_path: Path) -> dict:
    cols = ", ".join(f"'{k}': '{v}'" for k, v in COLUMNS.items())
    con = duckdb.connect(str(db_path))
    try:
        con.execute("PRAGMA threads=1")
        _ensure(con)
        last = con.execute("SELECT max(last_dt) FROM raw.load_checkpoint "
                           "WHERE source = ?", [SOURCE]).fetchone()[0]
        new = [p for p in partitions(landing) if last is None or p > last]
        for p in new:
            glob = (Path(landing) / f"dt={p.isoformat()}").as_posix() + "/*.csv"
            con.execute(f"""
                INSERT INTO raw.daily_revenue
                SELECT cast(order_id as integer), cast(order_date as date),
                       customer_id, amount, terminal_ref, DATE '{p.isoformat()}'
                FROM read_csv('{glob}', header=true, columns={{{cols}}})
                ORDER BY order_id""")
        if new:
            con.execute("DELETE FROM raw.load_checkpoint WHERE source = ?", [SOURCE])
            con.execute("INSERT INTO raw.load_checkpoint VALUES (?, ?)",
                        [SOURCE, new[-1]])
        return {"loaded": [p.isoformat() for p in new],
                "checkpoint": (new[-1] if new else last)}
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
    print(f"loaded {len(out['loaded'])} partition(s); "
          f"checkpoint {out['checkpoint']}; raw.daily_revenue: {n} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
