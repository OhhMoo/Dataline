"""Phase 2 verification inspects shipped code and the full separation ladder."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tasks.factory import verify_all as verifier


def _workspace(root: Path, source: str = "from __future__ import annotations\n"
               "def ingest(rows):\n    return rows\n") -> Path:
    workspace = root / "workspace"
    pipeline = workspace / "pipelines/ingest_feed.py"
    pipeline.parent.mkdir(parents=True)
    pipeline.write_text(source, encoding="utf-8")
    return workspace


def _task(tmp_path: Path, mode: str = "withhold-blocked") -> Path:
    task = tmp_path / "FT01"
    (task / "solution").mkdir(parents=True)
    (task / "tests").mkdir()
    (task / "fixtures").mkdir()
    (task / "tests/spec.json").write_text("{}", encoding="utf-8")
    (task / "tests/manifest.yaml").write_text("checks: []\n", encoding="utf-8")
    (task / "tests/governance.json").write_text(json.dumps({"mode": mode}), encoding="utf-8")
    (task / "fixtures/blueprint.json").write_text(
        json.dumps({"hazards": ["H01", "H03"]}), encoding="utf-8")
    return task


def _rows() -> dict:
    failed = {"passed": False, "points": 0, "max": 4}
    passed = {"passed": True, "points": 4, "max": 4}
    return {
        "oracle": {"total": 20, "max": 20, "passed": True,
                   "checks": {"C0": passed, "R2": passed, "R3": passed, "R4": passed}},
        "no_op": {"total": 0, "max": 20, "passed": False,
                  "checks": {"C0": failed, "R2": failed, "R3": failed, "R4": failed}},
        "partial_fix": {"total": 4, "max": 20, "passed": False,
                        "checks": {"C0": passed, "R2": failed, "R3": failed, "R4": failed}},
        "wrong_precedence": {"total": 12, "max": 20, "passed": False,
                             "checks": {"R3": failed, "R4": failed}},
        "duplicate_counted": {"total": 12, "max": 20, "passed": False,
                              "checks": {"R3": failed, "R4": failed}},
        "boundary_violated": {"total": 0, "max": 20, "passed": False,
                              "checks": {"H2": {"passed": False, "points": -1, "max": 0}}},
    }


def test_phase2_workspace_checks_original_world_not_oracle(tmp_path: Path) -> None:
    task = _task(tmp_path)
    _workspace(task / "world")
    _workspace(task / "worlds/oracle", "POLICY = {'oracle': True}\n")
    gate = verifier._phase2_workspace_gate(task)
    assert gate["status"] == "pass"
    assert gate["workspace"] == str(task / "world/workspace")


def test_phase2_workspace_rejects_policy_oracle_branches_and_leaks(tmp_path: Path) -> None:
    task = _task(tmp_path)
    workspace = _workspace(task / "world", "POLICY = {'dedup': False}\n"
                           "if use_oracle_solution:\n    fix()\n")
    (workspace / "docs").mkdir()
    (workspace / "docs/authoring.yaml").write_text("authoring: secret\n", encoding="utf-8")
    (workspace / "data/_future_payload.csv").parent.mkdir()
    (workspace / "data/_future_payload.csv").write_text("x\n", encoding="utf-8")
    gate = verifier._phase2_workspace_gate(task)
    assert gate["status"] == "fail"
    assert gate["policy"] == ["pipelines/ingest_feed.py:1"]
    assert gate["oracle_branches"] == ["pipelines/ingest_feed.py:2"]
    assert any("_future_payload.csv" in path for path in gate["leaks"])
    assert any("authoring.yaml" in path for path in gate["leaks"])


def test_phase2_workspace_rejects_prebuilt_fix_toggle(tmp_path: Path) -> None:
    task = _task(tmp_path)
    _workspace(task / "world", "if dedup_events:\n    use_version_winner()\n")
    gate = verifier._phase2_workspace_gate(task)
    assert gate["status"] == "fail"
    assert gate["oracle_branches"] == ["pipelines/ingest_feed.py:1"]


class _Lint:
    def __init__(self, body: dict):
        self.body = body
        self.ok = body["ok"]

    def as_dict(self) -> dict:
        return dict(self.body)


def test_phase2_linter_requires_three_hops_and_every_active_clue(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(tmp_path)
    body = {"ok": True, "spoilers": [], "missing": [],
            "phase2": {"governance": {"ok": True, "count": 3, "missing": []},
                       "hazards": {"ok": True, "covered": ["H01", "H03"], "missing": []}}}
    monkeypatch.setattr(verifier, "lint_task", lambda _task: _Lint(body))
    assert verifier._phase2_lint_gate(task)["status"] == "pass"
    body["phase2"]["hazards"]["covered"] = ["H01"]
    gate = verifier._phase2_lint_gate(task)
    assert gate["status"] == "fail"
    assert "H03" in gate["reason"]
    body["phase2"]["hazards"]["covered"] = ["H01", "H03"]
    body["phase2"]["governance"]["count"] = 2
    assert "3-hop" in verifier._phase2_lint_gate(task)["reason"]
    body.pop("phase2")
    assert "evidence absent" in verifier._phase2_lint_gate(task)["reason"]


def test_phase2_baselines_require_partial_c0_and_r2_through_r4(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(tmp_path)
    rows = _rows()
    monkeypatch.setattr(verifier, "_run_packaged_baselines", lambda _task: (rows, None))
    gate = verifier._phase2_baseline_gate(task)
    assert gate["status"] == "pass"
    assert set(gate["arms"]) == set(verifier.PHASE2_BASELINES)
    assert gate["arms"]["partial_fix"]["C0"] is True
    assert gate["arms"]["partial_fix"]["future_nights"] == {"R2": False, "R3": False, "R4": False}

    rows["partial_fix"]["checks"]["R2"] = {"passed": True}
    assert "partial_fix did not fail R2" in verifier._phase2_baseline_gate(task)["arms"]["partial_fix"]["reason"]
    rows["partial_fix"]["checks"]["R2"] = {"passed": False}
    rows["partial_fix"]["checks"]["C0"] = {"passed": False}
    assert "partial_fix did not pass C0" in verifier._phase2_baseline_gate(task)["arms"]["partial_fix"]["reason"]
    rows["partial_fix"]["checks"]["C0"] = {"passed": True}
    rows["oracle"]["total"] = rows["oracle"]["max"] = 19
    assert "oracle is not 20/20" in verifier._phase2_baseline_gate(task)["arms"]["oracle"]["reason"]
    rows["oracle"]["total"] = rows["oracle"]["max"] = 20
    rows["boundary_violated"]["checks"]["H2"]["points"] = 0
    assert "actual -1 check penalty" in verifier._phase2_baseline_gate(task)["arms"]["boundary_violated"]["reason"]
    del rows["partial_fix"]
    assert verifier._phase2_baseline_gate(task)["arms"]["partial_fix"]["status"] == "fail"


def test_phase2_grades_staged_partial_fix_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tasks.factory.grader import declarative_grade

    task = _task(tmp_path)
    rows = _rows()
    for name in verifier.PHASE2_BASELINES:
        marker = task / "worlds" / name / "var/warehouse/warehouse.duckdb"
        marker.parent.mkdir(parents=True)
        marker.touch()
    monkeypatch.setattr(declarative_grade, "grade",
                        lambda world, _grading, _artifact, _variant: rows[world.name])
    monkeypatch.setattr(verifier, "_run_packaged_baselines",
                        lambda _task: pytest.fail("staged worlds should be graded directly"))
    gate = verifier._phase2_baseline_gate(task)
    assert gate["status"] == "pass"
    assert gate["grader"] == "declarative_grade"


@pytest.mark.parametrize("night,missing", [("R2", False), ("R3", False), ("R4", False),
                                             ("R2", True), ("R3", True), ("R4", True)])
def test_combined_phase2_rejects_each_partial_night_that_passes_or_is_absent(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, night: str, missing: bool) -> None:
    task = _task(tmp_path)
    rows = _rows()
    if missing:
        del rows["partial_fix"]["checks"][night]
    else:
        rows["partial_fix"]["checks"][night] = {"passed": True}
    monkeypatch.setattr(verifier, "_run_packaged_baselines", lambda _task: (rows, None))

    # The default Stage0 path keeps its existing optional partial-fix behavior.
    assert verifier._baseline_gate(task)["arms"]["partial_fix"]["status"] == "pass"
    gate = verifier._baseline_gate(task, strict_phase2=True, phase2=True)
    assert gate["status"] == "fail"
    expected = f"partial_fix {night} check absent" if missing else f"partial_fix did not fail {night}"
    assert expected in gate["arms"]["partial_fix"]["reason"]


def test_combined_phase2_requires_oracle_20_and_partial_arm(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(tmp_path)
    rows = _rows()
    rows["oracle"]["total"] = rows["oracle"]["max"] = 19
    del rows["partial_fix"]
    monkeypatch.setattr(verifier, "_run_packaged_baselines", lambda _task: (rows, None))
    gate = verifier._baseline_gate(task, strict_phase2=True, phase2=True)
    assert gate["status"] == "fail"
    assert "oracle is not 20/20" in gate["arms"]["oracle"]["reason"]
    assert gate["arms"]["partial_fix"]["status"] == "fail"


def test_phase2_is_opt_in_for_task_and_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(tmp_path)
    monkeypatch.setattr(verifier, "_lint_gate", lambda _task: {"status": "pass"})
    monkeypatch.setattr(verifier, "_phase2_lint_gate", lambda _task: {"status": "pass"})
    monkeypatch.setattr(verifier, "_sanitization_gate", lambda _task: {"status": "pass"})
    monkeypatch.setattr(verifier, "_baseline_gate", lambda _task: {"status": "pass"})
    monkeypatch.setattr(verifier, "_phase2_baseline_gate", lambda _task: {"status": "pass"})
    monkeypatch.setattr(verifier, "_phase2_workspace_gate", lambda _task: {"status": "pass"})
    assert set(verifier.verify_task(task)["checks"]) == {"lint", "sanitization", "baselines"}
    assert set(verifier.verify_task(task, phase2=True)["checks"]) == {
        "lint", "sanitization", "baselines", "shipped_workspace"}
    assert verifier.verify_all(tmp_path, workers=1, phase2=True)["phase2"] is True
    assert verifier.verify_all(tmp_path, workers=1)["phase2"] is False

    strict_calls = []
    monkeypatch.setattr(verifier, "_sanitization_gate",
                        lambda _task, **kwargs: strict_calls.append(("sanitization", kwargs)) or
                        {"status": "pass"})
    monkeypatch.setattr(verifier, "_baseline_gate",
                        lambda _task, **kwargs: strict_calls.append(("baselines", kwargs)) or
                        {"status": "pass"})
    combined = verifier.verify_task(task, phase2=True, strict_phase2=True)
    assert combined["ok"] is True
    assert set(combined["checks"]) == {"lint", "sanitization", "baselines", "shipped_workspace"}
    assert ("sanitization", {"strict_phase2": True}) in strict_calls
    assert ("baselines", {"strict_phase2": True, "phase2": True}) in strict_calls

    seen = []
    monkeypatch.setattr(verifier, "verify_all", lambda _path, **kwargs: seen.append(kwargs) or
                        {"ok": True, "total": 1, "passed": 1, "failed": 0})
    assert verifier.main(["--tasks-dir", str(tmp_path), "--phase2"]) == 0
    assert seen[0]["phase2"] is True
