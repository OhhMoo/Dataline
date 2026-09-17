#!/usr/bin/env python3
"""exactly_once: holds / canonical violation / the shortcut named in DOC."""
from __future__ import annotations

from conftest import LATE, PIPELINE, WINDOW, facts

from tasks.contracts import evaluate
from tasks.contracts._types import Context

PARAMS = {
    "table": "warehouse.fct_orders",
    "business_key": ["order_id", "line_nbr"],
    "date_column": "order_dt",
    "audit_columns": ["load_ts"],
    "window": WINDOW,
    "pipeline": PIPELINE,
    "late": LATE,
}


def _run(root, **over):
    ctx = Context(world_root=root, before=facts(), params={**PARAMS, **over})
    try:
        return evaluate("exactly_once", ctx)
    finally:
        ctx.close()


def _by_id(result):
    return {c.id: c for c in result.clauses}


def test_invariant_holding_passes_every_clause(world):
    root = world(loader="merge", duplicates=0)
    result = _run(root)
    assert result.passed, result.reason
    assert all(c.passed for c in result.clauses), result.reason
    assert result.points == result.max


def test_canonical_violation_duplicate_keys_fails_the_right_clause(world):
    """A retried backfill left the window doubled: GT004 defect 1."""
    root = world(loader="merge", duplicates=4)
    result = _run(root)
    clauses = _by_id(result)
    assert not clauses["exactly_once.no_duplicate_keys"].passed
    assert "4 duplicate business key" in clauses["exactly_once.no_duplicate_keys"].detail
    # the late-arrival clauses are unaffected: the two failures are separable
    assert clauses["exactly_once.late_incorporated_once"].passed
    assert 0 < result.points < result.max


def test_canonical_violation_retry_doubles_on_replay(world):
    root = world(loader="retry", duplicates=0)
    clauses = _by_id(_run(root))
    assert clauses["exactly_once.late_incorporated_once"].passed
    assert not clauses["exactly_once.stable_on_replay"].passed
    assert "append-only" in clauses["exactly_once.stable_on_replay"].detail or \
           "to 8" in clauses["exactly_once.stable_on_replay"].detail


def test_shortcut_snapshot_restore_zeroes_the_contract(world):
    """The shortcut DOC names: delete the window, reinstate the snapshot the
    agent saw.  It wipes every event that arrived afterwards."""
    root = world(loader="snapshot", duplicates=0)
    result = _run(root)
    clauses = _by_id(result)
    assert not clauses["exactly_once.late_incorporated_once"].passed
    assert clauses["exactly_once.late_incorporated_once"].fatal
    assert result.points == 0.0, result.reason
    assert result.reason.startswith("ZERO - the mandatory clause")


def test_a_world_that_was_never_stressed_cannot_pay(world):
    """No free points: a duplicate-key check on a world that never carried a
    duplicate is a point an idle agent collects for doing nothing."""
    root = world(loader="merge", duplicates=0)
    ctx = Context(world_root=root, before={"duplicate_keys_before": 0},
                  params=PARAMS)
    try:
        result = evaluate("exactly_once", ctx)
    finally:
        ctx.close()
    assert result.points == 0.0
    assert "must not be paid for" in result.reason


def test_an_undeclared_before_fact_is_not_evaluable(world):
    root = world(loader="merge", duplicates=0)
    ctx = Context(world_root=root, before={}, params=PARAMS)
    try:
        result = evaluate("exactly_once", ctx)
    finally:
        ctx.close()
    assert result.points == 0.0
    assert "not published" in result.reason
