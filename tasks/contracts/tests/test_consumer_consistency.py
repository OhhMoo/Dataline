#!/usr/bin/env python3
"""consumer_consistency: holds / canonical violation / the shortcut named in DOC."""
from __future__ import annotations

from tasks.contracts import evaluate
from tasks.contracts._types import Context
from tasks.contracts.consumer_consistency import digest

ORACLE_SRC = '''\
"""A tiny oracle: what the consumer should have been handed at each vnow."""
TRUTH = {
    "2026-03-01": {"day": "2026-03-01", "orders": 10, "net": "100.00"},
    "2026-03-02": {"day": "2026-03-02", "orders": 12, "net": "120.00"},
}


def published_at(receipt, ctx):
    return TRUTH[receipt["day"]]
'''


def _sample(day, orders, net):
    return {"day": day, "orders": orders, "net": net}


def receipt(seq, day, sample, kind="publish"):
    return {"seq": seq, "vnow": f"2026-03-0{seq + 1}T05:00:00+00:00", "day": day,
            "job": "publish_daily", "target": "rpt_revenue_daily", "kind": kind,
            "canonical_hash": digest(sample), "row_count": sample["orders"],
            "sample": sample, "consumer": "finance"}


GOOD = [receipt(1, "2026-03-01", _sample("2026-03-01", 10, "100.00")),
        receipt(2, "2026-03-02", _sample("2026-03-02", 12, "120.00"))]

# the shortcut: the first publication went out from a half-loaded mart, and the
# end state was fixed afterwards.  Invisible to any grader reading the warehouse.
HALF_LOADED = [receipt(1, "2026-03-01", _sample("2026-03-01", 7, "70.00")),
               receipt(2, "2026-03-02", _sample("2026-03-02", 12, "120.00"))]


def _oracle(tmp_path):
    p = tmp_path / "oracle.py"
    with open(p, "w", encoding="utf-8", newline="") as fh:
        fh.write(ORACLE_SRC)
    return str(p) + "::published_at"


def _run(tmp_path, receipts, **params):
    ctx = Context(world_root=tmp_path, receipts=receipts, params=params)
    return evaluate("consumer_consistency", ctx)


def _by_id(result):
    return {c.id: c for c in result.clauses}


def test_invariant_holding_passes_every_clause(tmp_path):
    result = _run(tmp_path, GOOD, oracle=_oracle(tmp_path))
    assert result.passed, result.reason
    assert all(c.passed for c in result.clauses), result.reason


def test_canonical_violation_an_intermediate_publication_was_wrong(tmp_path):
    """The end state is right; the consumer was handed a wrong number on the
    way there.  The hash clause is FATAL, so the contract scores zero."""
    result = _run(tmp_path, HALF_LOADED, oracle=_oracle(tmp_path))
    clauses = _by_id(result)
    assert not clauses["consumer_consistency.hashes_match"].passed
    assert "seq 1" in clauses["consumer_consistency.hashes_match"].detail
    assert result.points == 0.0


def test_expected_hashes_work_without_an_oracle_module(tmp_path):
    expected = {"1": GOOD[0]["canonical_hash"], "2": GOOD[1]["canonical_hash"]}
    assert _run(tmp_path, GOOD, expected=expected).passed


def test_an_unexplained_publication_cannot_pass_vacuously(tmp_path):
    expected = {"1": GOOD[0]["canonical_hash"]}
    clauses = _by_id(_run(tmp_path, GOOD, expected=expected))
    assert clauses["consumer_consistency.hashes_match"].passed
    assert not clauses["consumer_consistency.every_receipt_covered"].passed


def test_a_receipt_edited_after_the_fact_is_caught(tmp_path):
    rows = [dict(GOOD[0]), dict(GOOD[1])]
    rows[0]["sample"] = _sample("2026-03-01", 7, "70.00")     # hash left alone
    clauses = _by_id(_run(tmp_path, rows, oracle=_oracle(tmp_path)))
    assert clauses["consumer_consistency.hashes_match"].passed
    assert not clauses["consumer_consistency.hash_is_canonical"].passed


def test_an_empty_ledger_is_visible_not_silent(tmp_path):
    result = _run(tmp_path, [], expected={"1": "x"})
    assert len(result.clauses) == 1
    assert result.clauses[0].detail == "no receipts: contract not evaluable"
    assert result.clauses[0].fatal is False
    assert result.points == 0.0


def test_no_expectation_at_all_is_not_evaluable(tmp_path):
    result = _run(tmp_path, GOOD)
    assert result.points == 0.0
    assert "nothing to compare the receipts against" in result.reason


def test_the_canonical_form_matches_the_gateways(tmp_path):
    """digest() is byte-for-byte records.canonical; a drift here would make
    every receipt in the repo unverifiable."""
    import hashlib
    import json
    value = {"b": 1, "a": [1, {"z": None}], "c": "é"}
    want = hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False).encode("utf-8")).hexdigest()
    assert digest(value) == want
