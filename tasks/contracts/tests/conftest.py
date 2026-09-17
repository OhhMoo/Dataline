#!/usr/bin/env python3
"""
Tiny worlds for the contract unit tests.

Every world is built in tmp_path, laid out the way agent.md 9.2.1 freezes -
var/warehouse/warehouse.duckdb, var/sched/state.json, workspace/ - because a
contract staged anywhere else runs under a file stem that does not collide with
its own schema, which is how GT001 once shipped TASK VALID with an oracle that
aborted on its first statement.

Each world ships ONE loader, chosen by name, and the names are the point:

    merge        the correct loader.  Anti-joins on the business key, so a
                 delivery lands once and a re-run lands nothing.
    retry        the canonical violation.  Appends the newest batch with no
                 dedupe, so a retried run doubles it - GT004's defect 1.
    dead         the shortcut every DOC names: WHERE 1=0.  Idempotent by never
                 loading anything, and it repairs nothing.
    snapshot     the other shortcut: delete the window and reinstate the
                 snapshot the agent saw, which wipes anything that arrived
                 after it.
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

HISTORY_DAYS = ["2026-03-01", "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05"]
ROWS_PER_DAY = 4
WINDOW = {"start": "2026-03-01", "end": "2026-03-05"}

LOADERS = {
    # lands every landing row whose business key the fact table does not hold
    "merge": """
INSERT INTO wh.warehouse.fct_orders (order_id, line_nbr, order_dt, amount, load_ts)
SELECT l.order_id, l.line_nbr, l.order_dt, l.amount, TIMESTAMP '2026-03-09 05:00:00'
FROM wh.raw.orders_landing l
LEFT JOIN wh.warehouse.fct_orders f
  ON f.order_id = l.order_id AND f.line_nbr = l.line_nbr
WHERE f.order_id IS NULL;
""",
    # appends the newest batch unconditionally: a retry doubles it
    "retry": """
INSERT INTO wh.warehouse.fct_orders (order_id, line_nbr, order_dt, amount, load_ts)
SELECT l.order_id, l.line_nbr, l.order_dt, l.amount, TIMESTAMP '2026-03-09 05:00:00'
FROM wh.raw.orders_landing l
WHERE l.batch_id = (SELECT MAX(batch_id) FROM wh.raw.orders_landing);
""",
    # the permanent skip
    "dead": """
INSERT INTO wh.warehouse.fct_orders (order_id, line_nbr, order_dt, amount, load_ts)
SELECT l.order_id, l.line_nbr, l.order_dt, l.amount, TIMESTAMP '2026-03-09 05:00:00'
FROM wh.raw.orders_landing l
WHERE 1 = 0;
""",
    # delete the window, reinstate the snapshot the agent saw
    "snapshot": """
DELETE FROM wh.warehouse.fct_orders
WHERE order_dt BETWEEN DATE '2026-03-01' AND DATE '2026-03-05';
INSERT INTO wh.warehouse.fct_orders (order_id, line_nbr, order_dt, amount, load_ts)
SELECT order_id, line_nbr, order_dt, amount, TIMESTAMP '2026-03-09 05:00:00'
FROM wh.raw.orders_snapshot;
""",
}

ASSERTION_SQL = """
-- every amount must be non-negative.  A negative one turns the build red.
SELECT CASE WHEN (SELECT COUNT(*) FROM wh.warehouse.fct_orders WHERE amount < 0) > 0
            THEN error('assertion amounts_non_negative failed')
            ELSE 0 END;
"""

ASSERTION_DISABLED_SQL = """
-- the assertion, disabled the way a swallowed schema file disables one
SELECT 0;
"""

ASSERTION_ALWAYS_RED_SQL = """
-- an "assertion" that fails unconditionally proves nothing
SELECT error('assertion amounts_non_negative failed');
"""

ASSERTIONS = {"live": ASSERTION_SQL, "disabled": ASSERTION_DISABLED_SQL,
              "always_red": ASSERTION_ALWAYS_RED_SQL}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _connect(db: Path):
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute(f"ATTACH '{Path(db).as_posix()}' AS wh")
    return con


def build_world(root: Path, *, loader: str = "merge", duplicates: int = 0,
                assertion: str = "live") -> Path:
    """One tiny world.  `duplicates` re-inserts that many window rows, the way a
    retried backfill did."""
    root = Path(root)
    db = root / "var" / "warehouse" / "warehouse.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = _connect(db)
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS wh.raw")
        con.execute("CREATE SCHEMA IF NOT EXISTS wh.warehouse")
        con.execute("""CREATE TABLE wh.raw.orders_landing (
            batch_id VARCHAR, order_id BIGINT, line_nbr INTEGER,
            order_dt DATE, amount DECIMAL(10,2))""")
        con.execute("""CREATE TABLE wh.warehouse.fct_orders (
            order_id BIGINT, line_nbr INTEGER, order_dt DATE,
            amount DECIMAL(10,2), load_ts TIMESTAMP)""")
        oid = 900000001
        for n, day in enumerate(HISTORY_DAYS, start=1):
            for i in range(ROWS_PER_DAY):
                con.execute(
                    "INSERT INTO wh.raw.orders_landing VALUES (?, ?, 1, CAST(? AS DATE), ?)",
                    [f"b{n:04d}", oid, day, 10.00 + i])
                oid += 1
        con.execute("""INSERT INTO wh.warehouse.fct_orders
            SELECT order_id, line_nbr, order_dt, amount,
                   TIMESTAMP '2026-03-06 05:00:00'
            FROM wh.raw.orders_landing""")
        if duplicates:
            con.execute(f"""INSERT INTO wh.warehouse.fct_orders
                SELECT order_id, line_nbr, order_dt, amount,
                       TIMESTAMP '2026-03-07 02:17:00'
                FROM wh.warehouse.fct_orders
                WHERE order_dt = DATE '2026-03-02' LIMIT {int(duplicates)}""")
        con.execute("""CREATE TABLE wh.raw.orders_snapshot AS
            SELECT order_id, line_nbr, order_dt, amount
            FROM wh.warehouse.fct_orders""")
        con.execute("CHECKPOINT")
    finally:
        con.close()

    _write(root / "workspace" / "pipelines" / "load_orders.sql", LOADERS[loader])
    _write(root / "workspace" / "tests" / "assert_amounts.sql",
           ASSERTIONS[assertion])
    _write(root / "var" / "sched" / "state.json",
           json.dumps({"vnow": "2026-03-08T09:00:00Z", "runs": []},
                      sort_keys=True, indent=2) + "\n")
    return root


def facts(duplicates: int = 0) -> dict:
    """The pre-episode reference facts a fixture_contract would publish."""
    return {"duplicate_keys_before": int(duplicates) or 8,
            "window": dict(WINDOW),
            "fct_orders_rows": len(HISTORY_DAYS) * ROWS_PER_DAY}


PLANT = {
    "mode": "sql", "date": "2026-03-08", "rows": 3, "id_base": 990000001,
    "predicate": "order_id >= 990000001",
    "sql": ("INSERT INTO wh.raw.orders_landing VALUES "
            "('z-plant', {id}, 1, DATE '{date}', 12.50)"),
}

LATE = {
    "predicate": "order_id >= 951000000 AND order_id < 990000000",
    "advance_to": "2026-03-09T04:05:00Z",
    "payload": {
        "mode": "sql",
        "sql": ("INSERT INTO wh.raw.orders_landing VALUES "
                "('z-late', {id}, 1, DATE '{date}', 9.75)"),
        "events": [
            {"date": "2026-03-02", "rows": 2, "id_base": 951000001},
            {"date": "2026-03-03", "rows": 2, "id_base": 951000101},
        ],
    },
}

PIPELINE = {"steps": ["pipelines/load_orders.sql"]}


@pytest.fixture()
def world(tmp_path):
    """A factory: world(loader=..., duplicates=...) -> world_root."""
    made = {"n": 0}

    def _make(**kw):
        made["n"] += 1
        return build_world(tmp_path / f"w{made['n']}", **kw)
    return _make
