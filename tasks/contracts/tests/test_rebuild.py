#!/usr/bin/env python3
"""A-T1..A-T4 for the shared rebuild verifier.

Worlds follow tasks/contracts/tests/conftest.py: var/warehouse/warehouse.duckdb,
schema warehouse, table fct_orders, and the merge / dead loaders. The dead
loader is WHERE 1=0. Nothing here starts docker or opens a socket.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

from tasks.contracts.rebuild import (
    rematerialise_matches,
    run_twice,
    second_seed_hardcoded,
)

# Copied from conftest.LOADERS. merge anti-joins on the business key; dead
# inserts nothing.
LOADERS = {
    "merge": """
INSERT INTO wh.warehouse.fct_orders (order_id, line_nbr, order_dt, amount, load_ts)
SELECT l.order_id, l.line_nbr, l.order_dt, l.amount, TIMESTAMP '2026-03-09 05:00:00'
FROM wh.raw.orders_landing l
LEFT JOIN wh.warehouse.fct_orders f
  ON f.order_id = l.order_id AND f.line_nbr = l.line_nbr
WHERE f.order_id IS NULL;
""",
    "dead": """
INSERT INTO wh.warehouse.fct_orders (order_id, line_nbr, order_dt, amount, load_ts)
SELECT l.order_id, l.line_nbr, l.order_dt, l.amount, TIMESTAMP '2026-03-09 05:00:00'
FROM wh.raw.orders_landing l
WHERE 1 = 0;
""",
}

PLANT = {
    "mode": "sql",
    "date": "2026-03-08",
    "rows": 3,
    "id_base": 990000001,
    "predicate": "order_id >= 990000001",
    "sql": ("INSERT INTO wh.raw.orders_landing VALUES "
            "('z-plant', {id}, 1, DATE '{date}', 12.50)"),
}

TABLE = "warehouse.fct_orders"
STEPS = ["pipelines/load_orders.sql"]
AUDIT = ["load_ts"]


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _build_world(root: Path, loader: str) -> Path:
    root = Path(root)
    db = root / "var" / "warehouse" / "warehouse.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:", config={"threads": 1})
    try:
        con.execute(f"ATTACH '{db.as_posix()}' AS wh")
        con.execute("CREATE SCHEMA IF NOT EXISTS wh.raw")
        con.execute("CREATE SCHEMA IF NOT EXISTS wh.warehouse")
        con.execute("""CREATE TABLE wh.raw.orders_landing (
            batch_id VARCHAR, order_id BIGINT, line_nbr INTEGER,
            order_dt DATE, amount DECIMAL(10,2))""")
        con.execute("""CREATE TABLE wh.warehouse.fct_orders (
            order_id BIGINT, line_nbr INTEGER, order_dt DATE,
            amount DECIMAL(10,2), load_ts TIMESTAMP)""")
        oid = 900000001
        for n, day in enumerate(("2026-03-01", "2026-03-02"), start=1):
            for i in range(2):
                con.execute(
                    "INSERT INTO wh.raw.orders_landing VALUES (?, ?, 1, CAST(? AS DATE), ?)",
                    [f"b{n:04d}", oid, day, 10.00 + i])
                oid += 1
        con.execute("""INSERT INTO wh.warehouse.fct_orders
            SELECT order_id, line_nbr, order_dt, amount,
                   TIMESTAMP '2026-03-06 05:00:00'
            FROM wh.raw.orders_landing""")
        con.execute("CHECKPOINT")
    finally:
        con.close()
    _write(root / "workspace" / "pipelines" / "load_orders.sql", LOADERS[loader])
    return root


def _fact_rows(root: Path) -> int:
    db = root / "var" / "warehouse" / "warehouse.duckdb"
    con = duckdb.connect(":memory:", config={"threads": 1})
    try:
        con.execute(f"ATTACH '{db.as_posix()}' AS wh (READ_ONLY)")
        return int(con.execute(
            "SELECT COUNT(*) FROM wh.warehouse.fct_orders").fetchone()[0])
    finally:
        con.close()


def test_a_t1_merge_loader_is_idempotent_and_live(tmp_path):
    root = _build_world(tmp_path / "merge", "merge")
    before = _fact_rows(root)
    result = run_twice(root, STEPS, table=TABLE, plant_spec=PLANT,
                       audit_columns=AUDIT)
    assert result.business_identical is True, result.detail
    assert result.planted_once is True, result.detail
    assert result.liveness_failed is False, result.detail
    assert _fact_rows(root) == before


def test_plant_into_counted_table_is_not_a_load(tmp_path):
    """R-A1: plant.sql that inserts the fact rows must not look ingested."""
    root = _build_world(tmp_path / "into-fact", "dead")
    spec = dict(PLANT)
    spec["sql"] = (
        "INSERT INTO wh.warehouse.fct_orders "
        "(order_id, line_nbr, order_dt, amount, load_ts) VALUES "
        "({id}, 1, DATE '{date}', {amount}, TIMESTAMP '2026-03-09 05:00:00')"
    )
    result = run_twice(root, STEPS, table=TABLE, plant_spec=spec,
                       audit_columns=AUDIT)
    assert result.liveness_failed is True, result.detail
    assert result.planted_once is False, result.detail


def test_a_t2_dead_loader_ingests_nothing(tmp_path):
    root = _build_world(tmp_path / "dead", "dead")
    result = run_twice(root, STEPS, table=TABLE, plant_spec=PLANT,
                       audit_columns=AUDIT)
    assert result.liveness_failed is True, result.detail
    assert result.planted_once is False, result.detail


def test_a_t3_rematerialise_matches():
    match = rematerialise_matches("abc", "abc")
    assert match.matches is True
    assert match.detail.startswith("match")

    mismatch = rematerialise_matches("abc", "def")
    assert mismatch.matches is False
    assert mismatch.detail.startswith("mismatch")

    empty = rematerialise_matches("", "abc")
    assert empty.matches is False
    assert empty.detail.startswith("empty")


def test_a_t4_second_seed_hardcoded():
    copied = second_seed_hardcoded(rows_seed_b=[1], expected_b=[2], rows_seed_a=[1])
    assert copied.hardcoded is True

    honest = second_seed_hardcoded(rows_seed_b=[2], expected_b=[2], rows_seed_a=[1])
    assert honest.hardcoded is False

    skipped = second_seed_hardcoded(rows_seed_b=[1], expected_b=[2])
    assert skipped.hardcoded is False
    assert skipped.detail == "not evaluated"
