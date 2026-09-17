#!/usr/bin/env python3
"""version_precedence: holds / canonical violation / the shortcut named in DOC.

The ledger below is GT008's hazard in miniature: a v3 that ARRIVES before its
v2, a byte-identical replay of a superseded version, and a delete at the highest
version.  Arrival order is the row order; version order is not.
"""
from __future__ import annotations

import duckdb
import pytest

from tasks.contracts import evaluate
from tasks.contracts._types import Context

LEDGER = [
    {"order_id": "O-1", "version": "1", "event_type": "create",
     "order_day": "2026-03-01", "amount": "10.00"},
    {"order_id": "O-2", "version": "1", "event_type": "create",
     "order_day": "2026-03-01", "amount": "20.00"},
    {"order_id": "O-3", "version": "1", "event_type": "create",
     "order_day": "2026-03-02", "amount": "30.00"},
    # the v3 arrives BEFORE its v2 - arrival order is not version order
    {"order_id": "O-1", "version": "3", "event_type": "update",
     "order_day": "2026-03-01", "amount": "13.00"},
    {"order_id": "O-1", "version": "2", "event_type": "update",
     "order_day": "2026-03-01", "amount": "12.00"},
    # a byte-identical replay of a version already delivered: changes nothing
    {"order_id": "O-2", "version": "1", "event_type": "create",
     "order_day": "2026-03-01", "amount": "20.00"},
    # a delete at the highest version removes the key
    {"order_id": "O-3", "version": "2", "event_type": "delete",
     "order_day": "2026-03-02", "amount": "30.00"},
]

PARAMS = {
    "target": "warehouse.fct_orders_v",
    "key_column": "order_id", "version_column": "version",
    "type_field": "event_type", "delete_value": "delete",
    "identity_fields": ["order_id", "version", "event_type", "order_day", "amount"],
    "events": LEDGER,
    "compare": {"amount": "amount", "order_day": "order_day"},
}

# what the fold says: O-1 at v3 amount 13.00, O-2 at v1 amount 20.00, O-3 gone
CORRECT = [("O-1", 3, "2026-03-01", "13.00"), ("O-2", 1, "2026-03-01", "20.00")]
# the conventional merge on ARRIVAL order: the v2 replay overwrote the v3
NAIVE_MERGE = [("O-1", 2, "2026-03-01", "12.00"), ("O-2", 1, "2026-03-01", "20.00")]
# the full reload that resurrects the deleted key
FULL_RELOAD = CORRECT + [("O-3", 2, "2026-03-02", "30.00")]


def _target(tmp_path, rows, name="w"):
    root = tmp_path / name
    db = root / "var" / "warehouse" / "warehouse.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute(f"ATTACH '{db.as_posix()}' AS wh")
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS wh.warehouse")
        con.execute("""CREATE TABLE wh.warehouse.fct_orders_v (
            order_id VARCHAR, version INTEGER, order_day DATE,
            amount DECIMAL(10,2))""")
        for oid, ver, day, amount in rows:
            con.execute("INSERT INTO wh.warehouse.fct_orders_v VALUES "
                        "(?, ?, CAST(? AS DATE), ?)", [oid, ver, day, amount])
        con.execute("CHECKPOINT")
    finally:
        con.close()
    return root


def _run(root, **over):
    ctx = Context(world_root=root, params={**PARAMS, **over})
    try:
        return evaluate("version_precedence", ctx)
    finally:
        ctx.close()


def _by_id(result):
    return {c.id: c for c in result.clauses}


def test_invariant_holding_passes_every_clause(tmp_path):
    result = _run(_target(tmp_path, CORRECT, "ok"))
    assert result.passed, result.reason
    assert all(c.passed for c in result.clauses), result.reason


def test_canonical_violation_an_older_replay_wins(tmp_path):
    """The shortcut DOC names: the conventional merge on arrival order."""
    result = _run(_target(tmp_path, NAIVE_MERGE, "naive"))
    clauses = _by_id(result)
    assert not clauses["version_precedence.highest_version"].passed
    assert clauses["version_precedence.highest_version"].fatal
    assert "target v2, ledger v3" in clauses["version_precedence.highest_version"].detail
    assert result.points == 0.0, result.reason


def test_shortcut_full_reload_resurrects_a_deleted_key(tmp_path):
    result = _run(_target(tmp_path, FULL_RELOAD, "reload"))
    clauses = _by_id(result)
    assert clauses["version_precedence.highest_version"].passed
    assert not clauses["version_precedence.no_resurrection"].passed
    assert "O-3" in clauses["version_precedence.no_resurrection"].detail
    assert 0 < result.points < result.max


def test_a_duplicated_key_fails_one_row_per_key(tmp_path):
    result = _run(_target(tmp_path, CORRECT + [CORRECT[0]], "dup"))
    clauses = _by_id(result)
    assert not clauses["version_precedence.one_row_per_key"].passed


def test_a_ledger_that_contradicts_itself_is_not_evaluable(tmp_path):
    bad = LEDGER + [{"order_id": "O-2", "version": "1", "event_type": "create",
                     "order_day": "2026-03-01", "amount": "99.00"}]
    result = _run(_target(tmp_path, CORRECT, "bad"), events=bad)
    assert result.points == 0.0
    assert "violates its own source contract" in result.reason


@pytest.mark.parametrize("missing", ["target", "key_column", "version_column"])
def test_a_missing_parameter_is_named_not_guessed(tmp_path, missing):
    params = {k: v for k, v in PARAMS.items() if k != missing}
    ctx = Context(world_root=_target(tmp_path, CORRECT, "p" + missing), params=params)
    try:
        result = evaluate("version_precedence", ctx)
    finally:
        ctx.close()
    assert f"params.{missing}" in result.reason
