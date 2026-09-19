#!/usr/bin/env python3
"""Finance's revenue book - the platform job `finance_month_end_book`.

    python finance_month_end_book.py <nova.duckdb> <world_root>

Registered by Platform for Finance systems (DATA-8121). It is NOT in
orchestration/schedule.yml: Data does not run, hold or re-time it. Every
morning of the cutover window, at 06:00, it books into the APPEND-ONLY table
warehouse.finance_revenue_book in nova what Finance's two sources hold that
morning, and the platform writes a publication receipt for the whole book:

    source = 'dashboard'   every row of warehouse.v_revenue_daily - what the
                           revenue dashboard renders that morning
    source = 'legacy'      every row of legacy.warehouse.rpt_revenue_daily -
                           the old mart, which stays on until Finance signs
                           the reconciliation off

under booked_on = the morning the run is for (the calendar day after the
scheduler's current day, read from var/sched/state.json; the job runs at
06:00 the next morning). A morning already booked is not booked twice. A
source that cannot be read books nothing for that source and says so: the job
does not fail because Data's view is missing - Finance's book records that
the dashboard showed nothing that morning.

Do NOT run this by hand. A manual trigger books the morning after the world's
current day; the scheduled run for that morning then finds it booked and
records nothing, and the receipt Finance relies on carries whatever the hand
run saw.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

BOOK = "warehouse.finance_revenue_book"
LEGACY_RELPATH = Path("var") / "warehouse" / "legacy.duckdb"
STATE_RELPATH = Path("var") / "sched" / "state.json"
DDL = f"""
CREATE TABLE IF NOT EXISTS {BOOK} (
    booked_on    DATE          NOT NULL,
    source       VARCHAR       NOT NULL,
    order_day    DATE          NOT NULL,
    orders       BIGINT        NOT NULL,
    gross_amount DECIMAL(12,2) NOT NULL,
    adjustments  DECIMAL(12,2) NOT NULL,
    net_amount   DECIMAL(12,2) NOT NULL
)
"""
SOURCES = (
    ("dashboard", "wh.warehouse.v_revenue_daily"),
    ("legacy", "legacy.warehouse.rpt_revenue_daily"),
)


def morning_for(root: Path) -> date:
    state = json.loads((root / STATE_RELPATH).read_text(encoding="utf-8"))
    vnow = datetime.fromisoformat(str(state["vnow"]).replace("Z", "+00:00"))
    return (vnow.astimezone(timezone.utc) + timedelta(days=1)).date()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: finance_month_end_book.py <nova.duckdb> <world_root>",
              file=sys.stderr)
        return 2
    db, root = Path(argv[0]), Path(argv[1])
    morning = morning_for(root)
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh")
    legacy = root / LEGACY_RELPATH
    legacy_attached = legacy.is_file()
    if legacy_attached:
        con.execute("ATTACH '" + str(legacy).replace("'", "''")
                    + "' AS legacy (READ_ONLY)")
    con.execute("CREATE SCHEMA IF NOT EXISTS wh.warehouse")
    con.execute("USE wh")
    con.execute(DDL)
    already = con.execute(
        f"SELECT COUNT(*) FROM {BOOK} WHERE booked_on = ?", [morning]).fetchone()[0]
    if already:
        print(f"finance book: {morning.isoformat()} is already booked "
              f"({already} row(s)); nothing recorded")
        con.close()
        return 0
    booked = {}
    for source, relation in SOURCES:
        if source == "legacy" and not legacy_attached:
            print(f"finance book: {source} unreadable (the legacy warehouse is "
                  f"gone); nothing booked for it")
            booked[source] = 0
            continue
        try:
            con.execute(f"""
                INSERT INTO {BOOK}
                SELECT CAST(? AS DATE)                    AS booked_on,
                       CAST(? AS VARCHAR)                 AS source,
                       CAST(order_day AS DATE)            AS order_day,
                       CAST(orders AS BIGINT)             AS orders,
                       CAST(gross_amount AS DECIMAL(12,2)) AS gross_amount,
                       CAST(adjustments AS DECIMAL(12,2))  AS adjustments,
                       CAST(net_amount AS DECIMAL(12,2))   AS net_amount
                FROM {relation}
                ORDER BY order_day
            """, [morning, source])
            n = con.execute(
                f"SELECT COUNT(*) FROM {BOOK} WHERE booked_on = ? AND source = ?",
                [morning, source]).fetchone()[0]
            booked[source] = int(n)
        except duckdb.Error as exc:
            first = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            print(f"finance book: {source} unreadable ({first}); nothing booked "
                  f"for it")
            booked[source] = 0
    con.close()
    Path(str(db) + ".wal").unlink(missing_ok=True)
    print(f"finance book: {morning.isoformat()} booked - "
          + ", ".join(f"{s}={n} day(s)" for s, n in booked.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
