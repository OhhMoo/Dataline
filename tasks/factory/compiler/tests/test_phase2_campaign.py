"""The Phase 2 campaign must preserve the live proof as the bulk-build gate."""
from __future__ import annotations

import fcntl
import json
from pathlib import Path

import pytest

from tasks.factory.compiler import phase2_campaign


def _ready(*args, **kwargs) -> dict:
    return {"ready": True, "blockers": []}


def test_campaign_stops_before_pilot_when_upstream_is_incomplete(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(phase2_campaign, "inspect_phase2_readiness",
                        lambda: {"ready": False, "blockers": ["native composer incomplete"]})

    def forbidden(*args, **kwargs):
        raise AssertionError("pilot or bulk work started")

    monkeypatch.setattr(phase2_campaign, "run_materialization", forbidden)
    monkeypatch.setattr(phase2_campaign, "run_scale", forbidden)
    report = phase2_campaign.run_campaign(workspace=tmp_path / "campaign")
    assert report["status"] == "blocked_upstream"
    assert report["rounds"] == []
    assert report["deficits"] == ["native composer incomplete"]
    assert report["hardness_certified"] is False


def test_campaign_scales_only_the_certified_candidate(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(phase2_campaign, "inspect_phase2_readiness", _ready)
    monkeypatch.setattr(phase2_campaign, "run_materialization",
                        lambda *args, **kwargs: {"state": "materialized_unverified"})
    monkeypatch.setattr(phase2_campaign, "run_pilot_calibration",
                        lambda **kwargs: {"status": "certified",
                                          "hardness_certified": True,
                                          "hardness_certified_tasks": 51})
    calls = []

    def scale(**kwargs):
        calls.append(kwargs)
        return {"status": "complete", "deficits": []}

    monkeypatch.setattr(phase2_campaign, "run_scale", scale)
    report = phase2_campaign.run_campaign(workspace=tmp_path / "campaign")
    assert report["status"] == "complete"
    assert report["hardness_certified"] is True
    assert len(calls) == 1
    assert calls[0]["catalog"] == phase2_campaign.DEFAULT_CATALOG
    assert calls[0]["selection"] == tmp_path / "campaign/pilot_51/phase2_selection.json"


def test_campaign_cannot_scale_without_mutation_evidence(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(phase2_campaign, "inspect_phase2_readiness", _ready)
    monkeypatch.setattr(phase2_campaign, "run_materialization",
                        lambda *args, **kwargs: {"state": "materialized_unverified"})
    monkeypatch.setattr(phase2_campaign, "run_pilot_calibration",
                        lambda **kwargs: {"status": "needs_adjustment",
                                          "hardness_certified": False,
                                          "hardness_deficits": [{"code": "zero_scalar_variance"}]})
    monkeypatch.setattr(phase2_campaign, "_adjustments",
                        lambda *args: ([], ["K=5 measurement invalid"]))

    def forbidden(*args, **kwargs):
        raise AssertionError("bulk scale started without a certificate")

    monkeypatch.setattr(phase2_campaign, "run_scale", forbidden)
    report = phase2_campaign.run_campaign(workspace=tmp_path / "campaign")
    assert report["status"] == "measurement_or_mutation_blocked"
    assert report["hardness_certified"] is False
    assert report["deficits"] == ["K=5 measurement invalid"]


def test_failed_native_canary_stops_the_51_task_build(
        tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(phase2_campaign, "inspect_phase2_readiness", _ready)
    calls = []

    def materialize(*args, **kwargs):
        calls.append(kwargs)
        return {"state": "failed", "tasks": [{"task_id": "FT0001",
                 "status": "failed", "error": "raw feed is still a sidecar"}]}

    monkeypatch.setattr(phase2_campaign, "run_materialization", materialize)

    def forbidden(*args, **kwargs):
        raise AssertionError("Stage 0 or live work started after canary failure")

    monkeypatch.setattr(phase2_campaign, "run_pilot_calibration", forbidden)
    report = phase2_campaign.run_campaign(workspace=tmp_path / "campaign")
    assert report["status"] == "integration_canary_failed"
    assert len(calls) == 1 and calls[0]["limit"] == 1
    assert "raw feed is still a sidecar" in report["deficits"][0]


def test_incomplete_live_run_cannot_prescribe_a_catalog_mutation(
        tmp_path: Path, monkeypatch) -> None:
    task_id = "FT0001"
    pilot = tmp_path / "pilot"
    seal = pilot / task_id / "tests/phase2_provenance.json"
    seal.parent.mkdir(parents=True)
    seal.write_text(json.dumps({"fingerprint": "fp", "task_integrity": "pkg"}))
    index = tmp_path / "index.json"
    index.write_text(json.dumps({"runs": {task_id: {
        "fingerprint": "fp", "task_integrity": "pkg",
        "k3": str(tmp_path / "partial-k3")}}}))
    monkeypatch.setattr(phase2_campaign, "_provenance_matches", lambda *args: True)
    monkeypatch.setattr(phase2_campaign, "audit_live_run",
                        lambda *args: {"deficits": [{"code": "run_not_fully_scored"}]})

    def forbidden(*args, **kwargs):
        raise AssertionError("unaudited run was classified for mutation")

    monkeypatch.setattr(phase2_campaign, "classify_stage1_arm", forbidden)
    actions, blockers = phase2_campaign._adjustments(
        {}, index, [{"blueprint": {"task_id": task_id}}], pilot)
    assert actions == []
    assert "run_not_fully_scored" in blockers[0]


def test_campaign_workspace_lock_prevents_overlapping_runs(
        tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    root.mkdir()
    with (root / "campaign.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="already running"):
            phase2_campaign.run_campaign(workspace=root)
