#!/usr/bin/env python3
"""cutoff_integrity: holds / canonical violation / the shortcut named in DOC.

Receipts only - this contract deliberately never reads the warehouse, because
the failure it exists to catch is invisible in the end state.

EVERY SYNTHETIC RECEIPT HERE CARRIES `kind: "table"`, which is what
`world/LEDGER_CONTRACT.md` freezes it to.  Purpose is what the task says a
receipt was FOR, and it comes from `params.purpose_of` (here: which job wrote
it) or from the default classifier over the three target lists.  The last test
in this file is the first one in the package to feed a contract receipts a real
world wrote.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from tasks.contracts import evaluate
from tasks.contracts._types import Context

REPO = Path(__file__).resolve().parents[3]
GT009 = REPO / "tasks" / "GT009-settlement-cutoff"

PUBLISHER, CORRECTOR = "publish_daily", "publish_correction"


def _purpose_of(receipt):
    """The task's classifier: the JOB says what a publication was for.

    A correction goes out through a different job from the nightly, and both
    write `kind: "table"` receipts to the same target - which is exactly the
    case the default target classifier cannot decide.
    """
    return {PUBLISHER: "publish", CORRECTOR: "correction"}.get(receipt["job"])


PARAMS = {
    "cutoff_rule": {"offset_hours": 30},        # day D closes at D+1 06:00Z
    "purpose_of": _purpose_of,
}


def receipt(seq, vnow, day, job=PUBLISHER, h="h1", consumer="finance",
            target="rpt_revenue_daily"):
    return {"seq": seq, "vnow": vnow, "day": day, "job": job,
            "target": target, "kind": "table", "canonical_hash": h,
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
            job=CORRECTOR, h="a9"),
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
                           job=CORRECTOR, h="a2")]
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


# ===========================================================================
# the amendment: purpose comes from the task, never from `kind`
# ===========================================================================
def test_a_task_that_declares_no_purpose_at_all_is_not_evaluable(tmp_path):
    """The amendment's point, stated as a refusal.

    Every receipt above carries `kind: "table"`, which is the storage shape and
    the only thing the ledger contract lets it carry.  A contract handed no
    purpose does not fall back to reading it.
    """
    ctx = Context(world_root=tmp_path, receipts=HELD,
                  params={"cutoff_rule": {"offset_hours": 30}})
    result = evaluate("cutoff_integrity", ctx)
    assert len(result.clauses) == 1
    assert result.clauses[0].id == "cutoff_integrity.not_evaluable"
    assert "storage shape" in result.clauses[0].detail
    assert result.points == 0.0


def test_the_default_classifier_reads_the_target_and_governs_only_what_it_names(tmp_path):
    """Two targets, both `kind: "table"`, one governed and one not.

    This is GT009's shape: the settlement the consumer reads and the platform's
    own record are the same storage shape and are not the same publication.
    """
    rows = [receipt(1, "2026-03-02T05:00:00+00:00", "2026-03-01", h="a1"),
            receipt(2, "2026-03-02T05:01:00+00:00", "2026-03-01", h="w1",
                    target="ops_internal_log"),
            receipt(3, "2026-03-05T05:00:00+00:00", "2026-03-01", h="a9"),
            receipt(4, "2026-03-05T05:01:00+00:00", "2026-03-01", h="w9",
                    target="ops_internal_log")]
    ctx = Context(world_root=tmp_path, receipts=rows,
                  params={"cutoff_rule": {"offset_hours": 30},
                          "publish_targets": ["rpt_revenue_daily"]})
    result = evaluate("cutoff_integrity", ctx)
    ids = [c.id for c in result.clauses]
    assert "cutoff_integrity.day:finance:rpt_revenue_daily:2026-03-01" in ids
    assert not any("ops_internal_log" in cid for cid in ids)
    assert not _by_id(result)[
        "cutoff_integrity.day:finance:rpt_revenue_daily:2026-03-01"].passed


def test_a_period_the_consumer_was_handed_nothing_for_can_be_published_late(tmp_path):
    """`breach`: the third purpose, and the one that keeps a late publication
    from being scored as a restatement.

    The window closed with nothing in it, so the consumer never held figures
    for that day; publishing it afterwards restates nothing and needs no
    correction path.  The runbook GT009 ships states exactly this rule.
    """
    rows = [receipt(1, "2026-03-02T05:00:00+00:00", "2026-03-01",
                    job="close_window", h="empty"),
            receipt(2, "2026-03-07T05:00:00+00:00", "2026-03-01", h="a1")]
    ctx = Context(world_root=tmp_path, receipts=rows,
                  params={"cutoff_rule": {"offset_hours": 30},
                          "purpose_of": lambda r: (
                              "breach" if r["job"] == "close_window" else "publish")})
    result = evaluate("cutoff_integrity", ctx)
    assert result.passed, result.reason


def test_a_purpose_the_contract_does_not_know_is_a_packaging_error(tmp_path):
    ctx = Context(world_root=tmp_path, receipts=HELD,
                  params={"cutoff_rule": {"offset_hours": 30},
                          "purpose_of": lambda r: "republish"})
    result = evaluate("cutoff_integrity", ctx)
    assert result.clauses[0].id == "cutoff_integrity.not_evaluable"
    assert "'republish'" in result.clauses[0].detail


# ===========================================================================
# the first real ledger
# ===========================================================================
LEDGER_PARAMS = {
    # the clearing window closes at 23:30 UTC on the day itself
    "cutoff_rule": {"offset_hours": 23.5},
}


@pytest.fixture(scope="session")
def gt009_night() -> dict:
    """Receipts a REAL world wrote, and the parameters that read them.

    Builds GT009's fixture, stages a world root, drops in the first planted
    partition and advances the real `world.scheduler` over one night - the
    recipe `fixtures/fixture_contract.py::_problems_receipts` runs on every
    `verify()`.  Every constant comes from that file's CONTRACT.

    The package's dependency rule (`test_registry.py`) forbids the CONTRACTS
    from importing a task.  It does not forbid a TEST from measuring them
    against one, and until a test does, `cutoff_integrity` is a specification.
    """
    fixtures = GT009 / "fixtures"
    if not (fixtures / "make_fixture_world.py").is_file():
        pytest.skip("GT009 is not in this tree; no real ledger to read")
    for path in (str(fixtures), str(REPO / "world")):
        if path not in sys.path:
            sys.path.insert(0, path)
    import make_fixture_world as mfw                    # noqa: PLC0415
    from fixture_contract import CONTRACT               # noqa: PLC0415
    from world import scheduler                         # noqa: PLC0415

    scratch = Path(tempfile.mkdtemp(prefix="contracts-gt009-"))
    try:
        build = scratch / "build"
        if mfw.main(["--out", str(build), "--seed", str(CONTRACT["seed"])]) != 0:
            pytest.skip("GT009's fixture would not build")
        root = scratch / "world"
        mfw.stage_world_root(build, root)
        day = CONTRACT["plant_dates"][0]
        shutil.copytree(
            build / "next_nights" / "payments_feed" / f"dt={day}",
            root / "workspace" / CONTRACT["layout"]["feed_relpath"] / f"dt={day}",
            dirs_exist_ok=True)
        scheduler.advance(root, CONTRACT["advance_hours_per_night"])
        receipts = Context.load_receipts(
            root / CONTRACT["layout"]["ledger_relpath"])
        assert receipts, "one advanced night wrote no receipts"
        return {"receipts": receipts, "day": day,
                "handoff": CONTRACT["targets"]["handoff"],
                "window": CONTRACT["targets"]["window"],
                "consumer": CONTRACT["consumer"]}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_real_receipts_carry_a_storage_shape_and_no_purpose(gt009_night, tmp_path):
    """Why the amendment exists, measured rather than argued.

    Both of GT009's publications are `kind: "table"`: the settlement the
    clearing bank reads and the platform's own clearing-window record.  The
    task separates them; `kind` cannot.
    """
    receipts = gt009_night["receipts"]
    assert {r["kind"] for r in receipts} == {"table"}
    assert {r["target"] for r in receipts} == {gt009_night["handoff"],
                                              gt009_night["window"]}

    ctx = Context(world_root=tmp_path, receipts=receipts,
                  params={**LEDGER_PARAMS,
                          "publish_targets": [gt009_night["handoff"]],
                          "consumer": gt009_night["consumer"]})
    result = evaluate("cutoff_integrity", ctx)
    ids = [c.id for c in result.clauses]
    assert "cutoff_integrity.ledger_monotonic" in ids
    assert (f"cutoff_integrity.day:{gt009_night['consumer']}:"
            f"{gt009_night['handoff']}:{gt009_night['day']}") in ids
    assert not any(gt009_night["window"] in cid for cid in ids)
    assert result.passed, result.reason


def test_a_restatement_forged_onto_the_real_ledger_is_caught(gt009_night, tmp_path):
    """The real receipts with one more line: the same target, the same day, a
    different hash, published after the clearing window closed.

    Without this the real-ledger case would only prove the contract runs.  The
    forged line is what a recompute-everything recovery would have written.
    """
    receipts = list(gt009_night["receipts"])
    real = next(r for r in receipts if r["target"] == gt009_night["handoff"])
    restatement = dict(real)
    restatement.update(seq=max(r["seq"] for r in receipts) + 1,
                       vnow=f"{gt009_night['day']}T23:40:00Z",
                       canonical_hash="0" * 64)

    ctx = Context(world_root=tmp_path, receipts=receipts + [restatement],
                  params={**LEDGER_PARAMS,
                          "publish_targets": [gt009_night["handoff"]],
                          "consumer": gt009_night["consumer"]})
    result = evaluate("cutoff_integrity", ctx)
    day = _by_id(result)[f"cutoff_integrity.day:{gt009_night['consumer']}:"
                         f"{gt009_night['handoff']}:{gt009_night['day']}"]
    assert not day.passed
    assert "not the declared correction path" in day.detail
    assert day.checkpoint == restatement["vnow"]
