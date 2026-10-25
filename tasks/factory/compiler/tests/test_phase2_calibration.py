"""Live calibration cannot start before a valid Phase 2 pilot exists."""
from __future__ import annotations

from pathlib import Path

from tasks.factory.calibrate_queue import classify_stage1_arm
from tasks.factory.compiler import phase2_calibration


def test_calibration_blocks_before_any_rollout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(phase2_calibration, "inspect_phase2_readiness",
                        lambda: {"ready": False, "blockers": ["missing upstream Phase 2"]})
    monkeypatch.setattr(phase2_calibration, "_provenance_matches", lambda *args: False)

    def forbidden(*args, **kwargs):
        raise AssertionError("live rollout was launched")

    monkeypatch.setattr(phase2_calibration, "run_two_stage_calibration", forbidden)
    monkeypatch.setattr(phase2_calibration, "evaluate_hardness", forbidden)
    report = phase2_calibration.run_pilot_calibration(
        report_path=tmp_path / "report.json", run_index_path=tmp_path / "runs.json",
        stage0_path=tmp_path / "stage0.json")
    assert report["status"] == "blocked_preflight"
    assert "missing upstream Phase 2" in report["deficits"]
    assert not (tmp_path / "runs.json").exists()


def test_quarantined_mixed_screen_cannot_advance_to_k5() -> None:
    report = {"task": "example", "summary": {
        "scored": 3, "quarantined": 1, "measurement_valid": True,
        "classification_counts": {"pass": 1}, "pass_at_1": 1 / 3,
    }, "rollouts": [
        {"passed": True, "score": {"reward": 1.0}},
        {"passed": False, "score": {"reward": 0.3}},
        {"passed": False, "score": {"reward": 0.1}},
    ]}
    result = classify_stage1_arm(report)
    assert result["classification"] == "invalid"
    assert result["admit_to_stage2"] is False
