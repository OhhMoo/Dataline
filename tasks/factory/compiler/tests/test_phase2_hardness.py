"""The Phase 2 scale gate must need complete, disjoint live evidence."""

from __future__ import annotations

import json
from pathlib import Path

from tasks.factory.compiler.blueprint_matrix import FAULT_TOPOLOGIES, GOVERNANCE_MODES, THEMES
from tasks.factory.compiler.phase2_hardness import audit_live_run, evaluate_hardness, main


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _live_run(root: Path, task_id: str, stage: str, passed: list[bool], rewards: list[float]) -> Path:
    run_dir = root / task_id / stage
    run_id = f"{task_id}-{stage}"
    per_rollout = {}
    for i, (is_pass, reward) in enumerate(zip(passed, rewards)):
        rollout_id = f"r{i}"
        episode_dir = run_dir / rollout_id
        episode = {
            "task": task_id, "run_id": run_id, "rollout_id": rollout_id,
            "driver": "cursor", "model": "claude-opus-5-5-high", "topology": "container",
            "redteam_detector_test": False, "scored": True,
            "blindness": {"observed": True, "quarantined": False},
            "episode_id": f"episode-{task_id}-{stage}-{i}",
            "container": {"container_id": f"container-{task_id}-{stage}-{i}",
                          "image_digest": "sha256:" + "a" * 64},
            "surface_audit": {"gate": "pass", "probed": True, "clean": True},
            "started_utc": "2026-10-18T00:00:00Z", "ended_utc": "2026-10-18T00:01:00Z",
            "score": {"total": 20.0 if is_pass else 10.0, "max": 20.0},
            "passed": is_pass,
            "rollout_dir": str(episode_dir),
        }
        _write_json(episode_dir / "rollout.json", episode)
        per_rollout[rollout_id] = {
            "pass": is_pass, "reward": {"reward": reward},
            "outcome": {"outcome": "resolved" if is_pass else "acted_unresolved"},
        }
    k = len(passed)
    report = {
        "task": task_id, "run_id": run_id,
        "summary": {
            "rollouts": k, "scored": k, "quarantined": 0,
            "measurement_valid": True, "invalidated_because": [],
            "run_conditions": {
                "measurement_valid": True, "invalidated_because": [],
                "surface_gate": ["pass"], "surfaces_dead_undeclared": [],
                "rollouts_without_surface_audit": 0,
            },
            "matrix": {
                "pass": {"n": k, "passed": sum(passed), "rate": sum(passed) / k},
                "per_rollout": per_rollout,
            },
        },
    }
    _write_json(run_dir / "report.json", report)
    return run_dir


def _manifest(tmp_path: Path, *, live: bool) -> Path:
    themes = sorted(THEMES)
    modes = sorted(GOVERNANCE_MODES)
    topologies = sorted(FAULT_TOPOLOGIES)
    rows = []
    for theme in themes:
        for mode in modes:
            rows.append({"theme": theme, "governance_mode": mode,
                         "fault_topology_id": topologies[0]})
    for topology in topologies:
        rows.append({"theme": themes[0], "governance_mode": modes[0],
                     "fault_topology_id": topology})
    assert len(rows) == 51
    for i, row in enumerate(rows):
        task_id = f"phase2-{i:02d}"
        row["task_id"] = task_id
        if live:
            row["k3_run_dir"] = str(_live_run(tmp_path / "runs", task_id, "k3",
                                               [False, True, False], [0.2, 1.0, 0.4]))
            row["k5_run_dir"] = str(_live_run(tmp_path / "runs", task_id, "k5",
                                               [False, True, False, True, False],
                                               [0.2, 1.0, 0.4, 0.9, 0.3]))
    path = tmp_path / "manifest.json"
    _write_json(path, {"tasks": rows})
    return path


def _codes(result: dict) -> set[str]:
    return set(result["deficit_counts"])


def test_complete_live_proof_certifies_and_cli_exits_zero(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, live=True)
    result = evaluate_hardness(manifest)
    assert result["certified"] is True
    assert result["certified_tasks"] == 51
    assert result["coverage"]["distinct_task_assignment"]["matched"] == 51
    assert result["deficit_counts"] == {}
    assert main(["--manifest", str(manifest)]) == 0


def test_report_only_or_synthetic_baseline_cannot_certify(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, live=False)
    rows = json.loads(manifest.read_text())["tasks"]
    rows[0]["k3_run_dir"] = str(_live_run(tmp_path / "runs", rows[0]["task_id"], "k3",
                                          [False, True, False], [0.2, 1.0, 0.4]))
    for rollout in (tmp_path / "runs" / rows[0]["task_id"] / "k3").glob("r*/rollout.json"):
        rollout.unlink()
    _write_json(manifest, {"tasks": rows})
    result = evaluate_hardness(manifest)
    assert result["certified"] is False
    assert {"missing_live_rollouts", "missing_k5_run"} <= _codes(result)
    assert main(["--manifest", str(manifest)]) == 1


def test_selection_manifest_and_run_index_certify_same_live_proof(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, live=True)
    rows = json.loads(manifest.read_text())["tasks"]
    index = {row["task_id"]: {"k3": row.pop("k3_run_dir"),
                               "k5": row.pop("k5_run_dir")} for row in rows}
    _write_json(manifest, {"selected": [{"blueprint": row,
                                          "selection_role": "theme_governance"} for row in rows]})
    index_path = tmp_path / "run-index.json"
    _write_json(index_path, {"runs": index})
    result = evaluate_hardness(manifest, index_path)
    assert result["certified"] is True
    assert result["certified_tasks"] == 51
    assert main(["--manifest", str(manifest), "--run-index", str(index_path)]) == 0


def test_invalid_measurement_quarantine_ceiling_and_zero_variance_fail(tmp_path: Path) -> None:
    task_id = "phase2-probe"
    k3 = _live_run(tmp_path, task_id, "k3", [True, True, True], [0.5, 0.5, 0.5])
    report_path = k3 / "report.json"
    report = json.loads(report_path.read_text())
    report["summary"]["measurement_valid"] = False
    report["summary"]["quarantined"] = 1
    _write_json(report_path, report)
    deficits = {item["code"] for item in audit_live_run(k3, task_id, 3)["deficits"]}
    assert {"measurement_invalid", "quarantined_rollouts", "stage1_ceiling",
            "zero_scalar_variance"} <= deficits


def test_outside_band_and_missing_coverage_report_exact_deficits(tmp_path: Path) -> None:
    task_id = "phase2-probe"
    k5 = _live_run(tmp_path, task_id, "k5", [False] * 5, [0.1, 0.2, 0.3, 0.4, 0.5])
    assert "outside_core_band" in {d["code"] for d in audit_live_run(k5, task_id, 5)["deficits"]}
    manifest = _manifest(tmp_path / "manifest", live=False)
    rows = json.loads(manifest.read_text())["tasks"]
    rows[3]["task_id"] = rows[4]["task_id"]
    rows[3]["theme"] = ["malformed"]
    _write_json(manifest, {"tasks": rows})
    result = evaluate_hardness(manifest)
    assert result["certified"] is False
    assert {"duplicate_task_id", "invalid_manifest_axis", "missing_theme_mode_pairs",
            "stratification_not_disjoint"} <= _codes(result)


def test_complete_axis_presence_without_distinct_proof_tasks_fails(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, live=False)
    rows = json.loads(manifest.read_text())["tasks"]
    topologies = sorted(FAULT_TOPOLOGIES)
    for i, topology in enumerate(topologies[1:], start=1):
        rows[i]["fault_topology_id"] = topology
    for row in rows[36:]:
        row["fault_topology_id"] = topologies[0]
    _write_json(manifest, {"tasks": rows})
    result = evaluate_hardness(manifest)
    assert result["coverage"]["fault_topologies"]["missing"] == []
    assert result["coverage"]["theme_mode_pairs"]["missing"] == []
    assert result["coverage"]["distinct_task_assignment"]["matched"] < 51
    assert "stratification_not_disjoint" in _codes(result)


def test_run_index_paths_are_supported_and_malformed_report_fails_closed(tmp_path: Path) -> None:
    task_id = "phase2-probe"
    k3 = _live_run(tmp_path, task_id, "k3", [False, True, False], [0.2, 1.0, 0.4])
    report_path = k3 / "report.json"
    report = json.loads(report_path.read_text())
    report["summary"]["matrix"]["pass"]["rate"] = "invalid"
    _write_json(report_path, report)
    assert "invalid_k3_report" in {d["code"] for d in audit_live_run(k3, task_id, 3)["deficits"]}
    manifest = _manifest(tmp_path / "manifest", live=False)
    rows = json.loads(manifest.read_text())["tasks"]
    run_index = tmp_path / "run-index.json"
    _write_json(run_index, {"runs": {rows[0]["task_id"]: {"k3": str(k3)}}})
    result = evaluate_hardness(manifest, run_index)
    assert result["tasks"][0]["k3"]["path"] == str(k3)
    assert "invalid_k3_report" in _codes(result)


def test_live_pass_rate_accepts_harness_three_decimal_rounding(tmp_path: Path) -> None:
    task_id = "phase2-rounded-rate"
    k3 = _live_run(tmp_path, task_id, "k3", [False, True, False], [0.2, 1.0, 0.4])
    report_path = k3 / "report.json"
    report = json.loads(report_path.read_text())
    report["summary"]["matrix"]["pass"]["rate"] = 0.333
    _write_json(report_path, report)
    deficits = {item["code"] for item in audit_live_run(k3, task_id, 3)["deficits"]}
    assert "pass_rate_mismatch" not in deficits
    assert "parsed_pass_rate_mismatch" not in deficits
    report["summary"]["matrix"]["pass"]["rate"] = 0.334
    _write_json(report_path, report)
    deficits = {item["code"] for item in audit_live_run(k3, task_id, 3)["deficits"]}
    assert "pass_rate_mismatch" in deficits
    assert "parsed_pass_rate_mismatch" in deficits
