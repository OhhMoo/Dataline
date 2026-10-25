"""Export the 3PL settlement extract for the Atlas billing handoff.

Reads warehouse.threepl_settlement_extract from the world warehouse and writes
<out>/threepl_settlement.csv. Deterministic: explicit column list, fixed row
order, LF line endings, no timestamps or floats.
"""
import argparse
import csv
import sys
from pathlib import Path

import duckdb

COLUMNS = [
    "settlement_id", "settlement_date", "warehouse_id", "sku_id", "threepl_id",
    "received_today_each", "shipped_today_each", "storage_each",
    "receipt_handling_cents", "outbound_handling_cents", "storage_charge_cents",
    "total_charge_cents", "currency", "settlement_status",
]


def _connect(db_path: Path):
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + db_path.as_posix().replace("'", "''") + "' AS wh")
    con.execute("USE wh")
    return con


def export(db_path: Path, out_dir: Path) -> int:
    con = _connect(db_path)
    try:
        rows = con.execute(
            "SELECT " + ", ".join(COLUMNS)
            + " FROM warehouse.threepl_settlement_extract"
            + " ORDER BY settlement_date, warehouse_id, sku_id").fetchall()
    finally:
        con.close()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "threepl_settlement.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(COLUMNS)
        writer.writerows(rows)
    print(f"threepl_settlement.csv: {len(rows)} rows")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="export_threepl")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.db.is_file():
        print(f"warehouse not found: {args.db}", file=sys.stderr)
        return 2
    return export(args.db, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
