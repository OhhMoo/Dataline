#!/usr/bin/env python3
"""cutoff_integrity: holds / canonical violation / the shortcut named in DOC.

Receipts only - this contract deliberately never reads the warehouse, because
the failure it exists to catch is invisible in the end state.
"""
from __future__ import annotations

from tasks.contracts import evaluate
from tasks.contracts._types import Context

PARAMS = {
    "cutoff_rule": {"offset_hours": 30},        # day D closes at D+1 06:00Z
    "publish_kinds": ["publish"],
    "correction_kinds": ["correction"],
}


def receipt(seq, vnow, day, kind="publish", h="h1", consumer="finance",
            target="rpt_revenue_daily"):
    return {"seq": seq, "vnow": vnow, "day": day, "job": "publish_daily",
            "target": target, "kind": kind, "canonical_hash": h,
            "row_count": 10, "sample": None, "consumer": consumer}


HELD = [
    receipt(1, "2026-03-02T05:00:00+00:00", "2026-03-01", h="a1"),
    # still OPEN (cutoff is 2026-03-02T06:00Z): a change here is legal
    receipt(2, "2026-03-02T05:30:00+00:00", "2026-03-01", h="a2"),
    receipt(3, "2026-03-03T05:00:00+00:00", "2026-03-02", h="b1"),
    # 2026-03-01 is closed now, and it is republished with the SAME hash
    receipt(4, "2026-03-04T05:00:00+00:00", "2026-03-01", h="a2"),
]

RESTATED = HELD + [
    # the shortcut: recompute everything so every consumer agrees with today
    receipt(5, "2026-03-05T05:00:00+00:00", "2026-03-01", h="a9"),
]

CORRECTED = HELD + [
    receipt(5, "2026-03-05T05:00:00+00:00", "2026-03-01",
            kind="correction", h="a9"),
]


def _run(tmp_path, receipts, **over):
    ctx = Context(world_root=tmp_path, receipts=receipts,
                  params={**PARAMS, **over})
    return evaluate("cutoff_integrity", ctx)


def _by_id(result):
    return {c.id: c for c in result.clauses}


def test_invariant_holding_passes_every_clause(tmp_path):
    result = _run(tmp_path, HELD)
    assert result.passed, result.reason
    assert all(c.passed for c in result.clauses), result.reason


def test_canonical_violation_a_closed_day_is_silently_restated(tmp_path):
    result = _run(tmp_path, RESTATED)
    clauses = _by_id(result)
    day = clauses["cutoff_integrity.day:finance:rpt_revenue_daily:2026-03-01"]
    assert not day.passed
    assert "after its" in day.detail and "cutoff" in day.detail
    assert day.checkpoint == "2026-03-05T05:00:00+00:00"
    assert 0 < result.points < result.max


def test_the_declared_correction_path_is_allowed(tmp_path):
    result = _run(tmp_path, CORRECTED)
    assert result.passed, result.reason


def test_a_task_that_forbids_corrections_still_fails_them(tmp_path):
    result = _run(tmp_path, CORRECTED, corrections_allowed=False)
    clauses = _by_id(result)
    assert not clauses["cutoff_integrity.day:finance:rpt_revenue_daily:2026-03-01"].passed


def test_a_correction_that_corrects_nothing_is_mislabelled(tmp_path):
    rows = HELD + [receipt(5, "2026-03-05T05:00:00+00:00", "2026-03-01",
                           kind="correction", h="a2")]
    clauses = _by_id(_run(tmp_path, rows))
    assert not clauses["cutoff_integrity.corrections_declared"].passed


def test_an_empty_ledger_is_visible_not_silent(tmp_path):
    result = _run(tmp_path, [])
    assert len(result.clauses) == 1
    c = result.clauses[0]
    assert c.passed is False and c.fatal is False
    assert c.detail == "no receipts: contract not evaluable"
    assert result.points == 0.0


def test_a_rewritten_ledger_zeroes_the_contract(tmp_path):
    """A ledger whose vnow goes backwards is not append-only, and an append-only
    ledger is the only kind that is evidence."""
    rows = HELD + [receipt(5, "2026-03-01T00:00:00+00:00", "2026-03-01", h="a2")]
    result = _run(tmp_path, rows)
    clauses = _by_id(result)
    assert not clauses["cutoff_integrity.ledger_monotonic"].passed
    assert result.points == 0.0


def test_a_day_with_no_declared_cutoff_is_reported_not_assumed(tmp_path):
    result = _run(tmp_path, HELD, cutoff_rule={}, cutoffs={})
    clauses = _by_id(result)
    day = clauses["cutoff_integrity.day:finance:rpt_revenue_daily:2026-03-01"]
    assert not day.passed
    assert "no cutoff is declared" in day.detail
