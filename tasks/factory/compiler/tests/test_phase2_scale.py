"""The scale gate must preserve pilot provenance and fail before bulk writes."""
from __future__ import annotations

import json
import copy
from pathlib import Path

from tasks.factory.compiler import phase2_scale
from tasks.factory.compiler.phase2_mutation import derive_catalog
from tasks.factory.compiler.phase2_pilot import load_catalog
from tasks.factory.compiler.phase2_readiness import phase2_task_integrity


def test_selection_ties_to_exact_catalog_bytes(tmp_path: Path) -> None:
    catalog = phase2_scale.DEFAULT_CATALOG
    entries, validation = phase2_scale._read_catalog(catalog)
    assert validation["valid"]
    selection = phase2_scale.DEFAULT_PILOT / "phase2_selection.json"
    rows, deficits = phase2_scale._pilot_rows(selection, catalog, entries)
    assert len(rows) == 51
    assert not deficits

    rewritten = tmp_path / "catalog.jsonl"
    rewritten.write_text(catalog.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    _, deficits = phase2_scale._pilot_rows(selection, rewritten, entries)
    assert "pilot selection catalog hash differs from the current catalog" in deficits


def test_phase2_catalog_accepts_compatible_third_hazard() -> None:
    entries, _ = phase2_scale._read_catalog(phase2_scale.DEFAULT_CATALOG)
    mutated = copy.deepcopy(entries)
    row = mutated[0]
    row["r_hazards"].append({"hazard_id": "H02_permuted_column_order", "night": 4})
    row["hazards"].append("H02")
    row["schedule"]["hazard_nights"].append(4)
    row["difficulty_knobs"]["r_hazard_count"] = 3
    assert phase2_scale._validate_phase2_catalog(mutated)["valid"] is True
    row["hazards"][-1] = "H03"
    result = phase2_scale._validate_phase2_catalog(mutated)
    assert result["valid"] is False
    assert any("hazards codes differ" in error for error in result["errors"])


def test_scale_validator_accepts_a_versioned_mutation_catalog(tmp_path: Path) -> None:
    source = phase2_scale.DEFAULT_CATALOG
    provenance, records = load_catalog(source)
    anchor = next(record for record in records
                  if record["blueprint"]["fault_topology_id"] == "FT01")
    journal = tmp_path / "journal.json"
    journal.write_text(json.dumps({
        "schema_version": 1, "source_catalog_sha256": provenance["sha256"],
        "operations": [{"round": 1,
                        "source_task_id": anchor["blueprint"]["task_id"],
                        "source_row_sha256": anchor["source_row_sha256"],
                        "scope": "fault_topology",
                        "prescription": {"action": "increase_difficulty"},
                        "evidence": {"k3_run_dir": "/tmp/live-k3"}}]}))
    candidate = derive_catalog(source, journal, tmp_path / "versions")
    rows, validation = phase2_scale._read_catalog(Path(candidate["derived_catalog"]))
    assert len(rows) == 1080
    assert validation["valid"] is True


def test_hardness_report_includes_live_cell_deficits() -> None:
    proof = {"deficits": [], "tasks": [{"task_id": "FT0001",
             "deficits": [{"code": "missing_k3_run", "detail": "missing"}]}]}
    assert phase2_scale._hardness_deficits(proof) == [
        {"task_id": "FT0001", "code": "missing_k3_run", "detail": "missing"}]


def test_stage0_report_is_bound_to_current_pilot_package(tmp_path: Path) -> None:
    rows = [{"task_id": "FT0001"}]
    task_tests = tmp_path / "pilot/FT0001/tests"
    task_tests.mkdir(parents=True)
    seal = {"fingerprint": "source-a", "task_integrity": "package-a"}
    (task_tests / "phase2_provenance.json").write_text(json.dumps(seal))
    report_path = tmp_path / "stage0.json"
    report = {"ok": True, "phase2": True, "strict_phase2": True,
              "total": 51, "passed": 51, "results": [{"task": "FT0001"}],
              "phase2_package_provenance": {"FT0001": seal}}
    report_path.write_text(json.dumps(report))
    assert phase2_scale._pilot_stage0(report_path, rows, tmp_path / "pilot") == []
    seal["task_integrity"] = "package-b"
    (task_tests / "phase2_provenance.json").write_text(json.dumps(seal))
    assert "51-task Stage 0 is not bound to current Phase 2 pilot packages" in (
        phase2_scale._pilot_stage0(report_path, rows, tmp_path / "pilot"))


def test_unready_scale_never_creates_bulk_tasks(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(phase2_scale, "FINAL_REPORT", tmp_path / "final.md")
    monkeypatch.setattr(phase2_scale, "DONE_MARKER", tmp_path / "done.json")
    monkeypatch.setattr(phase2_scale, "assert_outside_git_repo",
                        lambda path, **_kwargs: Path(path))
    monkeypatch.setattr(phase2_scale, "inspect_phase2_readiness",
                        lambda *args, **kwargs: {"ready": False,
                                                  "blockers": ["upstream incomplete"]})
    monkeypatch.setattr(phase2_scale, "_pilot_stage0", lambda *args: [])
    monkeypatch.setattr(phase2_scale, "_provenance_matches", lambda *args: True)
    monkeypatch.setattr(phase2_scale, "evaluate_hardness",
                        lambda *args: {"certified": True, "deficits": []})
    out = tmp_path / "tasks_1080"
    report = phase2_scale.run_scale(out_dir=out, scratch=tmp_path / "scratch",
                                    report_path=tmp_path / "report.json")
    assert report["status"] == "blocked_preflight"
    assert report["hardness_certified"] is True
    assert "upstream incomplete" in report["deficits"]
    assert not out.exists()


def test_task_integrity_ignores_generated_baseline_results(tmp_path: Path) -> None:
    task = tmp_path / "task"
    (task / "workspace/pipelines").mkdir(parents=True)
    (task / "workspace/pipelines/ingest_feed.py").write_text("print('broken')\n")
    first = phase2_task_integrity(task)
    baseline = task / "tests/baselines/default.json"
    baseline.parent.mkdir(parents=True)
    baseline.write_text(json.dumps({"baselines": {"oracle": "pass"}}))
    assert phase2_task_integrity(task) == first
    (task / "tests/phase2_pilot_provenance.json").write_text(
        json.dumps({"task_id": "pilot-task"}))
    assert phase2_task_integrity(task) == first
    (task / "workspace/pipelines/ingest_feed.py").write_text("print('altered')\n")
    assert phase2_task_integrity(task) != first


def test_phase3_historical_source_reuse_requires_unchanged_package_and_blueprint(
        tmp_path: Path, monkeypatch) -> None:
    task = tmp_path / "FT0001"
    (task / "tests").mkdir(parents=True)
    (task / "fixtures").mkdir()
    (task / "workspace").mkdir()
    (task / "workspace/file.txt").write_text("original\n", encoding="utf-8")
    blueprint = {"task_id": "FT0001", "theme": "test_theme",
                 "t0_stressors": ["S01_checkpoint_gap"], "fault_topology_id": "FT01"}
    (task / "fixtures/blueprint.json").write_text(json.dumps({
        **blueprint, "t0_stressors": ["S01"], "controller": "ctrl"}))
    integrity = phase2_task_integrity(task)
    (task / "tests/phase2_provenance.json").write_text(json.dumps({
        "task_id": "FT0001", "theme": "test_theme", "fingerprint": "historical",
        "task_integrity": integrity}))
    monkeypatch.setattr(phase2_scale, "phase2_fingerprint", lambda *_: "current")
    assert phase2_scale._phase3_package_valid(task, blueprint, current_source=False)
    assert not phase2_scale._phase3_package_valid(task, blueprint, current_source=True)
    assert not phase2_scale._phase3_package_valid(
        task, {**blueprint, "fault_topology_id": "FT02"}, current_source=False)
    (task / "workspace/file.txt").write_text("mutated\n", encoding="utf-8")
    assert not phase2_scale._phase3_package_valid(task, blueprint, current_source=False)


def test_phase3_certified_copy_binds_full_package_to_pilot(tmp_path: Path) -> None:
    pilot = tmp_path / "pilot/FT0001"
    full = tmp_path / "full/FT0001"
    (pilot / "tests").mkdir(parents=True)
    (pilot / "workspace").mkdir()
    (pilot / "workspace/file.txt").write_text("certified\n", encoding="utf-8")
    integrity = phase2_task_integrity(pilot)
    seal = {"task_id": "FT0001", "theme": "test_theme",
            "fingerprint": "historical", "task_integrity": integrity}
    (pilot / "tests/phase2_provenance.json").write_text(json.dumps(seal))
    full.mkdir(parents=True)
    (full / "old.txt").write_text("stale\n", encoding="utf-8")
    phase2_scale._phase3_copy_certified(pilot, full, integrity)
    assert not (full / "old.txt").exists()
    assert (full / "workspace/file.txt").read_text() == "certified\n"
    assert phase2_scale._phase3_certified_bound(
        {"FT0001": {"fingerprint": "historical", "task_integrity": integrity}},
        pilot.parent, full.parent, {"FT0001"})
    (full / "workspace/file.txt").write_text("changed\n", encoding="utf-8")
    assert not phase2_scale._phase3_certified_bound(
        {"FT0001": {"fingerprint": "historical", "task_integrity": integrity}},
        pilot.parent, full.parent, {"FT0001"})


def test_phase3_regrade_rejects_stale_cached_ceiling_by_run_id() -> None:
    invalidated = ("FT0091", "FT0332", "FT0963", "FT1077")
    proof = {"tasks": [
        {"task_id": task_id, "k3": {"run_id": f"{task_id}-old-k3"},
         "k5": {"run_id": f"{task_id}-old-k5"}}
        for task_id in invalidated
    ] + [{"task_id": "FT0001", "k5": {"run_id": "FT0001-new-k5"}}]}
    regrade = {"tasks": [
        {"task": task_id, "runs": [
            {"run_id": f"{task_id}-old-k3", "old": {"rollouts": 3, "scored": 3,
             "passed": 2, "quarantined": 0},
             "new": {"rollouts": 3, "scored": 3, "passed": 3, "quarantined": 0}},
            {"run_id": f"{task_id}-old-k5", "old": {"rollouts": 5, "scored": 5,
             "passed": 4, "quarantined": 0},
             "new": {"rollouts": 5, "scored": 5, "passed": 5, "quarantined": 0}},
        ]}
        for task_id in invalidated
    ] + [{"task": "FT0001", "runs": [{
        "run_id": "FT0001-old-k5", "old": {"rollouts": 5, "scored": 5,
                                      "passed": 4, "quarantined": 0},
        "new": {"rollouts": 5, "scored": 5, "passed": 5, "quarantined": 0},
    }]}]}
    invalid = phase2_scale._phase3_regrade_invalidations(proof, regrade)
    assert {(row["task_id"], row["stage"]) for row in invalid} == {
        (task_id, stage) for task_id in invalidated for stage in ("k3", "k5")}
    assert all(row["new"]["passed"] == row["new"]["scored"] for row in invalid)
