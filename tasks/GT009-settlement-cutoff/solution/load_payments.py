#!/usr/bin/env python3
"""Nightly payments load - the published source contract, section 3.

    python load_payments.py <warehouse.duckdb> <workspace>

Three changes from the shipped version, each for a stated reason.

1. The feed is immutable and re-deliverable, so the landing table is rebuilt
   from the WHOLE feed every night rather than appended to from the partitions
   after a checkpoint. A partition that lands behind the checkpoint - which is
   how the vendor re-delivers a night it missed - is then read like any other.
   Duplicate deliveries of the same event are collapsed on `event_id`: the
   publisher's retry sends the same partition twice under two names, and two
   copies of one event is not two payments.

2. The current state of a payment is its HIGHEST VERSION, not its most
   recently delivered event. Ties - which the feed does not currently produce -
   fall back to delivery order and then to `event_id`, so the fold is total.

3. The checkpoint records the newest partition that was actually READ. It is a
   statement about data, and advancing it past a partition nobody read is how a
   three-day gap survives a working nightly.
"""
import sys
from pathlib import Path

import duckdb

FEED = "data/payments_feed"


def partitions(workspace: Path):
    """(day, csv path) for every delivered file, oldest partition first."""
    out = []
    for folder in sorted((workspace / FEED).glob("dt=*")):
        day = folder.name.split("=", 1)[1]
        for path in sorted(folder.glob("*.csv")):
            out.append((day, path))
    return out


def main(argv):
    db, workspace = Path(argv[0]), Path(argv[1])
    found = partitions(workspace)
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + db.as_posix() + "' AS wh")
    con.execute("USE wh")
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        con.execute("CREATE SCHEMA IF NOT EXISTS warehouse")
        con.execute("DROP TABLE IF EXISTS raw.payments_events_staging")
        con.execute(
            "CREATE TABLE raw.payments_events_staging (event_id VARCHAR, "
            "delivered_at TIMESTAMP, payment_day DATE, payment_id VARCHAR, "
            "version INTEGER, event_type VARCHAR, merchant_id VARCHAR, "
            "amount_cents BIGINT, source_partition VARCHAR)")
        for day, path in found:
            rel = FEED + "/dt=" + day + "/" + path.name
            con.execute(
                "INSERT INTO raw.payments_events_staging SELECT event_id, "
                "CAST(delivered_at AS TIMESTAMP), CAST(payment_day AS DATE), "
                "payment_id, CAST(version AS INTEGER), event_type, merchant_id, "
                "CAST(amount_cents AS BIGINT), '" + rel + "' "
                "FROM read_csv('" + path.as_posix() + "', header = true, "
                "all_varchar = true)")
        # one row per delivered event, whichever file carried it first
        con.execute("""
            CREATE OR REPLACE TABLE raw.payments_events AS
            SELECT event_id, delivered_at, payment_day, payment_id, version,
                   event_type, merchant_id, amount_cents, source_partition
            FROM (SELECT *, row_number() OVER (PARTITION BY event_id
                             ORDER BY source_partition) AS rn
                  FROM raw.payments_events_staging)
            WHERE rn = 1""")
        con.execute("DROP TABLE raw.payments_events_staging")
        # version precedence: highest version wins; a void at the top removes
        con.execute("""
            CREATE OR REPLACE TABLE warehouse.fct_payments AS
            SELECT payment_id, payment_day, merchant_id, amount_cents, version
            FROM (SELECT *, row_number() OVER (
                             PARTITION BY payment_id
                             ORDER BY version DESC, delivered_at DESC,
                                      event_id DESC) AS rn
                  FROM raw.payments_events)
            WHERE rn = 1 AND event_type <> 'void'""")
        read_through = max([day for day, _ in found] or ["1970-01-01"])
        con.execute("DELETE FROM warehouse.load_checkpoint")
        con.execute(
            "INSERT INTO warehouse.load_checkpoint VALUES (DATE '" + read_through
            + "', CAST('" + read_through + "T22:04:00' AS TIMESTAMP))")
        con.execute("CHECKPOINT")
        print("payments: read " + str(len(found)) + " file(s) through "
              + read_through)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
