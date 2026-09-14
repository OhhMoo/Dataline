"""Vendor feed loader: SFTP drop folders -> raw.* in the warehouse.

A feed file is loaded only when its companion sentinel (<name>.done) exists,
so a file still being written by the vendor's transfer is never picked up.
Vendors redeliver corrections under the same filename; --on-redelivery
controls whether the new content replaces the earlier load of that file
(overwrite, the default) or is added alongside it (append).

Usage: vendor_load.py --drop <drop_root> --db <warehouse.duckdb>
"""
import argparse
from pathlib import Path

import duckdb

FEEDS = [
    ("acme", "ORDERS_", "vendor_orders",
     {"order_id": "VARCHAR", "order_date": "VARCHAR",
      "customer_id": "VARCHAR", "amount": "VARCHAR"},
     ["order_date", "order_id"]),
    ("globex", "RETURNS_", "vendor_returns",
     {"return_id": "VARCHAR", "order_id": "VARCHAR",
      "return_date": "VARCHAR", "amount": "VARCHAR"},
     ["return_date", "return_id"]),
]


def eligible(folder: Path, prefix: str, require_sentinel: bool) -> list[Path]:
    files = sorted(folder.glob(f"{prefix}*.csv"))
    if require_sentinel:
        files = [f for f in files if f.with_suffix(".done").is_file()]
    return files


def load_feed(con, drop: Path, feed, require_sentinel: bool,
              on_redelivery: str) -> int:
    sub, prefix, table, columns, order_by = feed
    ddl = ", ".join(f"{k} {v}" for k, v in columns.items())
    con.execute(f"CREATE TABLE IF NOT EXISTS raw.{table} "
                f"({ddl}, _source_file VARCHAR)")
    names = ", ".join(columns)
    cols = ", ".join(f"'{k}': '{v}'" for k, v in columns.items())
    files = eligible(drop / sub, prefix, require_sentinel)
    for f in files:
        if on_redelivery == "overwrite":
            con.execute(f"DELETE FROM raw.{table} WHERE _source_file = ?",
                        [f.name])
        con.execute(
            f"INSERT INTO raw.{table} "
            f"SELECT {names}, ? AS _source_file "
            f"FROM read_csv(?, header=true, columns={{{cols}}}) "
            f"ORDER BY {', '.join(order_by)}", [f.name, str(f)])
    return len(files)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="vendor_load")
    p.add_argument("--drop", type=Path, required=True,
                   help="drop root holding one subfolder per vendor")
    p.add_argument("--db", type=Path, required=True,
                   help="path to the warehouse duckdb file")
    p.add_argument("--require-sentinel", default=True,
                   action=argparse.BooleanOptionalAction,
                   help="load a CSV only when its <name>.done sentinel exists "
                        "(default: on)")
    p.add_argument("--on-redelivery", choices=["overwrite", "append"],
                   default="overwrite",
                   help="how to treat a file whose name was loaded before: "
                        "replace its rows (default) or add the new content "
                        "alongside them")
    a = p.parse_args(argv)
    con = duckdb.connect(str(a.db))
    try:
        con.execute("PRAGMA threads=1")
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        for feed in FEEDS:
            n = load_feed(con, a.drop, feed, a.require_sentinel,
                          a.on_redelivery)
            print(f"raw.{feed[2]}: loaded {n} file(s) from {feed[0]}/")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
