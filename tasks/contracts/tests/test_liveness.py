#!/usr/bin/env python3
"""liveness: holds / canonical violation / the shortcut named in DOC."""
from __future__ import annotations

from conftest import PIPELINE, PLANT, WINDOW, facts

from tasks.contracts import evaluate
from tasks.contracts._types import Context

PARAMS = {
    "table": "warehouse.fct_orders",
    "date_column": "order_dt",
    "audit_columns": ["load_ts"],
    "window": WINDOW,
    "pipeline": PIPELINE,
    "plant": PLANT,
}


def _run(root, **over):
    ctx = Context(world_root=root, before=facts(), params={**PARAMS, **over})
    try:
        return evaluate("liveness", ctx)
    finally:
        ctx.close()


def _by_id(result):
    return {c.id: c for c in result.clauses}


def test_invariant_holding_passes_every_clause(world):
    result = _run(world(loader="merge"))
    assert result.passed, result.reason
    assert all(c.passed for c in result.clauses), result.reason


def test_canonical_violation_a_retry_doubles_the_delivery(world):
    """The load is still append-only: run 1 lands 3, run 2 lands 3 more."""
    result = _run(world(loader="retry"))
    clauses = _by_id(result)
    assert clauses["liveness.ingests"].passed
    assert clauses["liveness.no_collateral"].passed
    assert not clauses["liveness.idempotent"].passed
    assert "3 to 6" in clauses["liveness.idempotent"].detail
    assert result.points == 0.0 or result.points < result.max


def test_shortcut_permanent_skip_zeroes_the_contract(world):
    """The shortcut DOC names: WHERE 1=0.  Idempotent by never loading
    anything, and it repairs nothing - so it must score ZERO, not collect the
    idempotence points."""
    result = _run(world(loader="dead"))
    clauses = _by_id(result)
    assert not clauses["liveness.ingests"].passed
    assert clauses["liveness.ingests"].fatal
    assert "repairs nothing" in clauses["liveness.ingests"].detail
    assert clauses["liveness.idempotent"].passed is False
    assert result.points == 0.0, result.reason


def test_shortcut_snapshot_restore_is_also_dead_to_new_data(world):
    result = _run(world(loader="snapshot"))
    clauses = _by_id(result)
    assert not clauses["liveness.ingests"].passed
    assert result.points == 0.0


def test_an_unimplemented_runner_is_not_evaluable(world):
    result = _run(world(loader="merge"), runner="advance")
    assert result.points == 0.0
    assert "only 'pipeline' is implemented" in result.reason


def test_a_plant_the_pipeline_has_already_seen_is_not_evaluable(world):
    seen = dict(PLANT)
    seen["date"] = "2026-03-02"          # a partition the fact table holds
    result = _run(world(loader="merge"), plant=seen)
    assert result.points == 0.0
    assert "ALREADY exists" in result.reason
