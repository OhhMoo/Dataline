"""Publish current settlements while protecting closed clearing windows.

Usage: python pipelines/publish_settlement.py <warehouse.duckdb> <workspace>
"""
import sys
from pathlib import Path

import duckdb


def _connect(db_path: Path):
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + db_path.as_posix().replace("'", "''") + "' AS wh")
    con.execute("USE wh")
    return con


def _grouped(rows, with_ref=False):
    out = {}
    for row in rows:
        day, merchant, payments, net = row[:4]
        value = (merchant, int(payments), int(net))
        if with_ref:
            value += (row[4],)
        out.setdefault(str(day), []).append(value)
    return {day: sorted(values) for day, values in out.items()}


def publish(db_path: Path) -> dict:
    con = _connect(db_path)
    try:
        current = _grouped(con.execute("""
            SELECT payment_day, merchant_id, count(*), sum(amount_cents)
            FROM warehouse.fct_payments
            GROUP BY payment_day, merchant_id
            ORDER BY payment_day, merchant_id
        """).fetchall())
        existing = _grouped(con.execute("""
            SELECT settlement_day, merchant_id, payments, net_cents, correction_ref
            FROM warehouse.settlement_handoff
            ORDER BY settlement_day, merchant_id
        """).fetchall(), with_ref=True)
        closed = {str(row[0]) for row in con.execute(
            "SELECT DISTINCT settlement_day FROM warehouse.clearing_window "
            "WHERE status = 'SETTLED'").fetchall()}
        approvals = {}
        for day, ticket, approved_by, approved_at in con.execute("""
            SELECT settlement_day, ticket, approved_by, approved_at
            FROM warehouse.settlement_corrections
            ORDER BY settlement_day, approved_at, ticket
        """).fetchall():
            approvals[str(day)] = ticket

        changed = 0
        withheld = 0
        con.execute("BEGIN TRANSACTION")
        try:
            for day in sorted(set(current) | set(existing)):
                desired = current.get(day, [])
                before = [value[:3] for value in existing.get(day, [])]
                if desired == before:
                    continue
                correction_ref = None
                if day in closed:
                    correction_ref = approvals.get(day)
                    if correction_ref is None:
                        withheld += 1
                        continue
                con.execute("DELETE FROM warehouse.settlement_handoff WHERE settlement_day = CAST(? AS DATE)", [day])
                con.executemany(
                    "INSERT INTO warehouse.settlement_handoff "
                    "(settlement_day, merchant_id, payments, net_cents, correction_ref) "
                    "VALUES (CAST(? AS DATE), ?, ?, ?, ?)",
                    [[day, merchant, payments, net, correction_ref]
                     for merchant, payments, net in desired])
                changed += 1
        except Exception:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")
        rows = con.execute("SELECT count(*) FROM warehouse.settlement_handoff").fetchone()[0]
        return {"days_published": changed, "closed_days_withheld": withheld,
                "handoff_rows": rows}
    finally:
        con.close()


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: publish_settlement.py <warehouse.duckdb> <workspace>", file=sys.stderr)
        return 2
    summary = publish(Path(args[0]).resolve())
    print("settlement: " + ", ".join(f"{key}={value}" for key, value in summary.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
