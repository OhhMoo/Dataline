#!/usr/bin/env python3
"""Publish the nightly settlement to the clearing bank - the correction protocol.

    python publish_settlement.py <warehouse.duckdb> <workspace>

This is a DECISION, taken per settlement day, and the three branches are the
three answers the record permits:

  never handed over   publish it, late, no approval and no correction_ref. The
                      day already carries an SLA_BREACH row; leaving it
                      unpublished does not undo that, it adds another breach
                      every night the window closes without it.
  open                the clearing window has not recorded a SETTLED row for
                      it yet, so republishing is not a restatement. Publish.
  closed, and moved   a restatement. Publish only if an approval row exists
                      for that day in warehouse.settlement_corrections, and
                      then carry its ticket in correction_ref. Without one,
                      leave the day alone and escalate - an unauthorised
                      restatement is a reconciliation break at the bank.

Nothing here decides whether the figures are right; that is the loader's job
and the source contract's. This decides what the bank is allowed to be told.

The handoff table is loaded IN PLACE - DELETE then INSERT for the days that
move - because its column layout is frozen by the source contract and a
`CREATE OR REPLACE ... AS SELECT` would republish a different layout to the
bank along with the rows.
"""
import sys
from pathlib import Path

import duckdb

DESIRED = """
    SELECT payment_day AS settlement_day, merchant_id,
           CAST(COUNT(*) AS BIGINT) AS payments,
           CAST(SUM(amount_cents) AS BIGINT) AS net_cents
    FROM warehouse.fct_payments GROUP BY 1, 2
"""


def main(argv):
    db = Path(argv[0])
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + db.as_posix() + "' AS wh")
    con.execute("USE wh")
    try:
        desired = {(str(r[0]), r[1]): (int(r[2]), int(r[3]))
                   for r in con.execute(DESIRED).fetchall()}
        published = {(str(r[0]), r[1]): (int(r[2]), int(r[3]))
                     for r in con.execute(
                         "SELECT settlement_day, merchant_id, payments, net_cents "
                         "FROM warehouse.settlement_handoff").fetchall()}
        held = {str(r[0]) for r in con.execute(
            "SELECT DISTINCT settlement_day FROM warehouse.settlement_handoff"
        ).fetchall()}
        closed = {str(r[0]) for r in con.execute(
            "SELECT DISTINCT settlement_day FROM warehouse.clearing_window "
            "WHERE status = 'SETTLED'").fetchall()}
        approvals = {str(r[0]): r[1] for r in con.execute(
            "SELECT settlement_day, ticket FROM warehouse.settlement_corrections"
        ).fetchall()}
        existing_ref = {str(r[0]): r[1] for r in con.execute(
            "SELECT DISTINCT settlement_day, correction_ref "
            "FROM warehouse.settlement_handoff WHERE correction_ref IS NOT NULL"
        ).fetchall()}

        publish, withheld = [], []
        for day in sorted({key[0] for key in desired}):
            want = {k: v for k, v in desired.items() if k[0] == day}
            have = {k: v for k, v in published.items() if k[0] == day}
            if want == have:
                continue
            if day not in held:
                publish.append((day, None, "late settlement"))
            elif day not in closed:
                publish.append((day, existing_ref.get(day), "day still open"))
            elif day in approvals:
                publish.append((day, approvals[day], "approved correction"))
            else:
                withheld.append(day)

        for day, ref, why in publish:
            con.execute("DELETE FROM warehouse.settlement_handoff "
                        "WHERE settlement_day = DATE '" + day + "'")
            con.execute(
                "INSERT INTO warehouse.settlement_handoff "
                "SELECT settlement_day, merchant_id, payments, net_cents, "
                + ("NULL" if ref is None else "'" + str(ref).replace("'", "''") + "'")
                + " FROM (" + DESIRED + ") WHERE settlement_day = DATE '" + day + "'")
            print("settlement: " + day + " -> the bank (" + why + ")")
        for day in withheld:
            print("settlement: " + day + " MOVED and is closed with no approval; "
                  "NOT republished - raise a correction ticket")
        con.execute("CHECKPOINT")
        rows = con.execute(
            "SELECT COUNT(*) FROM warehouse.settlement_handoff").fetchone()[0]
        print("settlement: handoff holds " + str(rows) + " row(s)")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
