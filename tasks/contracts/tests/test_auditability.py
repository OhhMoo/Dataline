#!/usr/bin/env python3
"""auditability: holds / canonical violation / the shortcuts named in DOC.

Nothing here READS the project.  It plants rows and fires the build, because the
world as shipped already HAS the assertion in the file - and it has never
executed once.
"""
from __future__ import annotations

import json

from tasks.contracts import evaluate
from tasks.contracts._types import Context

STEPS = ["pipelines/load_orders.sql", "tests/assert_amounts.sql"]

PHASES = [
    # the agent's project on its own world, untouched: MUST be green.
    # Kills any "fix" that makes the suite fail unconditionally.
    {"name": "clean", "expect": "green", "steps": STEPS},
    # tomorrow's ordinary extract: MUST stay green.
    {"name": "recur", "expect": "green", "steps": STEPS, "plant_sql": [
        "INSERT INTO wh.raw.orders_landing VALUES "
        "('z-recur', 970000001, 1, DATE '2026-03-08', 11.00)"]},
    # the input the assertion exists to catch: MUST turn the build red.
    # Kills the assertion that never ran, severity: warn, and enabled: false.
    {"name": "novel", "expect": "red", "steps": STEPS, "plant_sql": [
        "INSERT INTO wh.raw.orders_landing VALUES "
        "('z-novel', 980000001, 1, DATE '2026-03-08', -5.00)"]},
]

PARAMS = {"declared": ["amounts_non_negative"], "phases": PHASES}


def _results(root, rows):
    path = root / "workspace" / "target" / "run_results.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps({"results": rows}, sort_keys=True) + "\n")
    return "workspace/target/run_results.json"


def _run(root, **over):
    ctx = Context(world_root=root, params={**PARAMS, **over})
    try:
        return evaluate("auditability", ctx)
    finally:
        ctx.close()


def _by_id(result):
    return {c.id: c for c in result.clauses}


def test_invariant_holding_passes_every_clause(world):
    root = world(loader="merge", assertion="live")
    results = _results(root, [{"name": "amounts_non_negative", "status": "pass"}])
    result = _run(root, results=results)
    assert result.passed, result.reason
    assert all(c.passed for c in result.clauses), result.reason


def test_canonical_violation_the_assertion_does_not_discriminate(world):
    """The declared test is in the project and has never fired.  It goes green
    on the exact input it exists to catch."""
    root = world(loader="merge", assertion="disabled")
    results = _results(root, [{"name": "amounts_non_negative", "status": "pass"}])
    result = _run(root, results=results)
    clauses = _by_id(result)
    assert not clauses["auditability.discriminates"].passed
    assert clauses["auditability.discriminates"].fatal
    assert "novel: expected red, got green" in clauses["auditability.discriminates"].detail
    assert result.points == 0.0


def test_shortcut_an_assertion_that_fails_unconditionally_proves_nothing(world):
    root = world(loader="merge", assertion="always_red")
    results = _results(root, [{"name": "amounts_non_negative", "status": "fail"}])
    clauses = _by_id(_run(root, results=results))
    assert not clauses["auditability.discriminates"].passed
    assert "clean: expected green, got red" in clauses["auditability.discriminates"].detail


def test_shortcut_a_test_that_is_only_declared_never_ran(world):
    root = world(loader="merge", assertion="live")
    results = _results(root, [{"name": "some_other_test", "status": "pass"}])
    clauses = _by_id(_run(root, results=results))
    assert not clauses["auditability.declared_ran"].passed
    assert "amounts_non_negative" in clauses["auditability.declared_ran"].detail
    assert clauses["auditability.discriminates"].passed


def test_an_indefinite_verdict_is_not_a_verdict(world):
    root = world(loader="merge", assertion="live")
    results = _results(root, [{"name": "amounts_non_negative", "status": "skipped"}])
    clauses = _by_id(_run(root, results=results))
    assert not clauses["auditability.result_definite"].passed


def test_no_declared_phase_fails_the_gate_rather_than_passing_quietly(world):
    root = world(loader="merge", assertion="live")
    results = _results(root, [{"name": "amounts_non_negative", "status": "pass"}])
    result = _run(root, results=results, phases=[])
    clauses = _by_id(result)
    assert not clauses["auditability.discriminates"].passed
    assert "nothing here fired the assertion" in clauses["auditability.discriminates"].detail
    assert result.points == 0.0


def test_results_may_come_from_the_receipt_ledger(world):
    root = world(loader="merge", assertion="live")
    ctx = Context(world_root=root, params=PARAMS, receipts=[
        {"seq": 1, "vnow": "2026-03-08T05:00:00+00:00", "kind": "assertion",
         "name": "amounts_non_negative", "status": "pass"}])
    try:
        result = evaluate("auditability", ctx)
    finally:
        ctx.close()
    assert result.passed, result.reason


def test_no_record_at_all_is_not_evaluable(world):
    root = world(loader="merge", assertion="live")
    result = _run(root)
    assert result.points == 0.0
    assert "no record that any assertion ever ran" in result.reason
