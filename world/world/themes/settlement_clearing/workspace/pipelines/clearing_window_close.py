"""Append the clearing bank's cutoff record for newly observed settlement days.

Usage: python pipelines/clearing_window_close.py <warehouse.duckdb> <workspace>
"""
import sys
from datetime import timedelta
from pathlib import Path

import duckdb


def _connect(db_path: Path):
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + db_path.as_posix().replace("'", "''") + "' AS wh")
    con.execute("USE wh")
    return con


def close(db_path: Path) -> dict:
    con = _connect(db_path)
    try:
        closed_on = con.execute(
            "SELECT max(through_day) FROM warehouse.load_checkpoint").fetchone()[0]
        if closed_on is None:
            print("clearing: no loaded partition; no window to close")
            return {"closed_on": None, "settled": 0, "breaches": 0}
        window_days = int(con.execute(
            "SELECT window_days FROM raw.settlement_config LIMIT 1").fetchone()[0])
        first_day = closed_on - timedelta(days=window_days - 1)
        delivered_days = [row[0] for row in con.execute("""
            SELECT DISTINCT payment_day FROM raw.payments_events
            WHERE CAST(delivered_at AS DATE) <= ? AND payment_day <= ?
              AND payment_day >= ?
            ORDER BY payment_day
        """, [closed_on, closed_on, first_day]).fetchall()]
        settled = {row[0] for row in con.execute(
            "SELECT DISTINCT settlement_day FROM warehouse.clearing_window "
            "WHERE status = 'SETTLED'").fetchall()}
        given = {row[0]: (int(row[1]), int(row[2])) for row in con.execute("""
            SELECT settlement_day, sum(payments), sum(net_cents)
            FROM warehouse.settlement_handoff GROUP BY settlement_day
        """).fetchall()}

        appended = {"SETTLED": 0, "SLA_BREACH": 0}
        con.execute("BEGIN TRANSACTION")
        try:
            for day in delivered_days:
                if day in settled:
                    continue
                if day in given:
                    status = "SETTLED"
                    payments, net_cents = given[day]
                else:
                    status = "SLA_BREACH"
                    payments, net_cents = 0, 0
                con.execute(
                    "INSERT INTO warehouse.clearing_window "
                    "(closed_on, settlement_day, status, payments, net_cents) "
                    "VALUES (?, ?, ?, ?, ?)", [closed_on, day, status, payments, net_cents])
                appended[status] += 1
        except Exception:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")
        return {"closed_on": closed_on.isoformat(), "settled": appended["SETTLED"],
                "breaches": appended["SLA_BREACH"]}
    finally:
        con.close()


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: clearing_window_close.py <warehouse.duckdb> <workspace>", file=sys.stderr)
        return 2
    summary = close(Path(args[0]).resolve())
    print("clearing: " + ", ".join(f"{key}={value}" for key, value in summary.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
