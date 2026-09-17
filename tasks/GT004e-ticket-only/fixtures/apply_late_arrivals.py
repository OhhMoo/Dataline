#!/usr/bin/env python3
"""
Advance the clock: land the late-arrival payload in a COPIED world.

This is the G3 mechanism. `late_arrival` faults in the world engine are inert
(`_kind_scan` globs the cumulative folder with min:1, so after day one a poll
cannot fail) and `sched_trigger` reports success without executing anything that
is not dbt_build/dbt_run. So G3 must be delivered through the arrival stream and
the clock, by hand, against a copied world — the pattern GT002's K5 and GT003's
D6 already use.

    python apply_late_arrivals.py --world <copy>/var/warehouse/warehouse.duckdb \
                                  --payload <fixtures>/build/late_arrivals

What it does, and only this:

  1. appends every row of every `dt=*/orders.csv` partition to
     `raw.orders_landing`, stamped with the payload's arrival_ts and batch_id;
  2. copies the partitions into `<world_root>/data/landing/orders/` if
     --world-root is given, so a file-based loader sees them too.

What it deliberately does NOT do:

  * it does not touch `raw.orders_snapshot`. The snapshot is a frozen vendor
    extract; the agent does not control it and it will never learn about these
    events. That is precisely what makes a repair of the form "DELETE the window
    and reinsert from the snapshot" go stale the moment the clock advances: it
    wipes the late arrivals it just merged, and the manual adjustments with them.
    A merge on (order_id, line_nbr) from the landing stream survives, and
    survives a second run.
  * it does not touch `warehouse.*`. Loading the delta is the agent's pipeline's
    job; if this script did it, G3 would be measuring this script.
  * it does not advance `warehouse.pipeline_watermark`. An agent whose load is
    watermark-driven has to move its own watermark, and an agent whose load
    permanently skips has to fail the liveness clause rather than pass the
    idempotency one.

Applying twice raises. A probe bug that double-applies would look exactly like
an agent that re-doubled the window, and those two must never be confusable.
"""
from __future__ import annotations

import argparse
import csv
import shutil
from pathlib import Path

import duckdb

# One import site for the payload's shape. Never hardcode these.
try:                                            # running as a module
    from .make_fixture_world import LATE_ARRIVAL_TS, LATE_BATCH, LATE_CSV_HEADER
except ImportError:                             # running as a script
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from make_fixture_world import (LATE_ARRIVAL_TS, LATE_BATCH,  # noqa: E402
                                    LATE_CSV_HEADER)

LANDING_COLS = ["arrival_ts", "batch_id", "order_id", "line_nbr", "order_dt",
                "order_ts", "customer_id", "sku", "qty", "unit_price", "promo_id",
                "source_file"]


class PayloadError(Exception):
    pass


def read_payload(payload: Path) -> list[dict]:
    """Every partition, in dt order, as dicts keyed by LATE_CSV_HEADER."""
    payload = Path(payload)
    parts = sorted(p for p in payload.glob("dt=*") if p.is_dir())
    if not parts:
        raise PayloadError(f"no dt=* partitions under {payload}")
    rows: list[dict] = []
    for part in parts:
        f = part / "orders.csv"
        if not f.exists():
            raise PayloadError(f"partition without orders.csv: {part}")
        with f.open("r", newline="", encoding="utf-8") as fh:
            rdr = csv.reader(fh)
            header = next(rdr)
            if header != LATE_CSV_HEADER:
                raise PayloadError(
                    f"{f} header {header} != contract {LATE_CSV_HEADER}")
            for rec in rdr:
                d = dict(zip(header, rec))
                d["source_file"] = f"landing/orders/{part.name}/orders.csv"
                rows.append(d)
    return rows


def apply(world: Path, payload: Path, world_root: Path | None = None) -> dict:
    """Land the payload in `world` (a COPY — never the cached fixture)."""
    rows = read_payload(payload)

    # Aliased ATTACH, never a direct open (F622). This runs against the deployed
    # copy at var/warehouse/warehouse.duckdb, where the catalog and the schema
    # would otherwise both be named `warehouse` and every two-part reference
    # below would raise Binder Error - Ambiguous reference.
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute(f"ATTACH '{Path(world).as_posix()}' AS wh")
    con.execute("USE wh.warehouse")
    try:
        already = con.execute(
            "SELECT COUNT(*) FROM raw.orders_landing WHERE batch_id = ?",
            [LATE_BATCH]).fetchone()[0]
        if already:
            raise PayloadError(
                f"batch {LATE_BATCH} is already present in raw.orders_landing "
                f"({already} rows). Applying twice would be indistinguishable "
                f"from the agent re-doubling the window. Use a fresh copy.")
        con.executemany(
            "INSERT INTO raw.orders_landing (" + ", ".join(LANDING_COLS) + ") "
            "VALUES (?::TIMESTAMP, ?, ?::BIGINT, ?::INTEGER, ?::DATE, "
            "?::TIMESTAMP, ?::BIGINT, ?, ?::INTEGER, ?::DECIMAL(10,2), "
            "NULLIF(?, ''), ?)",
            [[r["arrival_ts"], r["batch_id"], r["order_id"], r["line_nbr"],
              r["order_dt"], r["order_ts"], r["customer_id"], r["sku"],
              r["qty"], r["unit_price"], r["promo_id"], r["source_file"]]
             for r in rows])
        landed = con.execute(
            "SELECT COUNT(*) FROM raw.orders_landing WHERE batch_id = ?",
            [LATE_BATCH]).fetchone()[0]
        con.execute("CHECKPOINT")
    finally:
        con.close()

    copied = 0
    if world_root is not None:
        dst = Path(world_root) / "data" / "landing" / "orders"
        dst.mkdir(parents=True, exist_ok=True)
        for part in sorted(p for p in Path(payload).glob("dt=*") if p.is_dir()):
            shutil.copytree(part, dst / part.name, dirs_exist_ok=True)
            copied += 1

    return {"batch_id": LATE_BATCH, "arrival_ts": LATE_ARRIVAL_TS,
            "rows_landed": landed, "partitions_copied": copied}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", required=True,
                    help="path to the COPIED warehouse .duckdb to advance")
    ap.add_argument("--payload", required=True,
                    help="path to <fixtures>/build/late_arrivals")
    ap.add_argument("--world-root", default=None,
                    help="optional: also drop the partitions under "
                         "<root>/data/landing/orders/")
    a = ap.parse_args()
    try:
        r = apply(Path(a.world), Path(a.payload),
                  Path(a.world_root) if a.world_root else None)
    except PayloadError as e:
        print(f"LATE ARRIVAL NOT APPLIED: {e}")
        return 1
    print(f"  landed {r['rows_landed']} rows as batch {r['batch_id']} "
          f"at {r['arrival_ts']}"
          + (f", {r['partitions_copied']} partitions copied"
             if r["partitions_copied"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
