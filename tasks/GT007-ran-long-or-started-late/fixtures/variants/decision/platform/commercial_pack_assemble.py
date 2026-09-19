#!/usr/bin/env python3
"""commercial_pack_assemble - platform-owned. Cuts the Commercial pack.

Runs at 04:00 every morning from the scheduler's live plane (it is NOT in
orchestration/schedule.yml; Data does not deploy or re-time it). It copies
whatever warehouse.commercial_extract holds at that moment into
warehouse.commercial_pack for the pack day (the day starting when it runs),
one block per pack day, and the scheduler publishes a receipt for the pack
Commercial get at 06:00. Whatever the extract holds at 04:00 is what
Commercial see. If the extract is unreadable the pack is cut empty - that is
the morning Commercial actually have, and the receipt says so.

Usage: commercial_pack_assemble.py <warehouse.duckdb> <world-root>
Owner: platform on-call (mcarver). Sign-off for hand re-runs of the extract
during the day: DATA-7718.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

PACK = "warehouse.commercial_pack"
EXTRACT = "warehouse.commercial_extract"
DDL = f"""
CREATE TABLE IF NOT EXISTS {PACK} (
    pack_day DATE NOT NULL,
    dt DATE NOT NULL,
    orders BIGINT NOT NULL,
    units BIGINT NOT NULL,
    amount DECIMAL(14, 2) NOT NULL
)
"""


def pack_day_for(root: Path):
    """The pack day: the morning after the scheduler's clock. The scheduler's
    `vnow` is the START of the span being advanced (it moves only when the
    advance completes), so a scheduled 04:00 run inside the advance from the
    14th 09:00 cuts the pack of the 15th; a hand trigger during the day of the
    14th cuts the SAME pack day early, from whatever the extract holds then,
    and the 04:00 run finds it already cut. That is the collision the hold on
    DATA-7718 is about."""
    state = json.loads((root / "var" / "sched" / "state.json").read_text(encoding="utf-8"))
    vnow = datetime.fromisoformat(str(state["vnow"]).replace("Z", "+00:00")).astimezone(timezone.utc)
    return (vnow + timedelta(days=1)).date()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: commercial_pack_assemble.py <warehouse.duckdb> <world-root>", file=sys.stderr)
        return 2
    db, root = Path(argv[0]), Path(argv[1])
    pack_day = pack_day_for(root)
    # The warehouse file is named warehouse.duckdb and its schema is
    # `warehouse`; attached as `wh` the two-part names below are unambiguous.
    con = duckdb.connect(":memory:", config={"threads": 1})
    try:
        con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh")
        con.execute("CREATE SCHEMA IF NOT EXISTS wh.warehouse")
        con.execute("USE wh")
        con.execute(DDL)
        already = con.execute(
            f"SELECT COUNT(*) FROM {PACK} WHERE pack_day = ?", [pack_day]).fetchone()[0]
        if already:
            print(f"commercial_pack_assemble: pack {pack_day} already cut ({already} rows); nothing to do")
            return 0
        try:
            con.execute(
                f"INSERT INTO {PACK} "
                f"SELECT CAST(? AS DATE), CAST(dt AS DATE), CAST(orders AS BIGINT), "
                f"CAST(units AS BIGINT), CAST(amount AS DECIMAL(14, 2)) "
                f"FROM {EXTRACT} ORDER BY dt", [pack_day])
        except duckdb.Error as exc:  # the extract is absent or unreadable
            print(f"commercial_pack_assemble: {EXTRACT} unreadable ({exc.__class__.__name__}); "
                  f"pack {pack_day} cut empty")
            return 0
        rows, days, total = con.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT dt), COALESCE(SUM(amount), 0) FROM {PACK} "
            f"WHERE pack_day = ?", [pack_day]).fetchone()
        print(f"commercial_pack_assemble: pack {pack_day} cut - {rows} rows, {days} days, total {total}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
