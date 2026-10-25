"""Phase 3 delta builds retain historical K=5 packages and cached runs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tasks.factory.compiler.phase2_pilot import (
    ATTESTATION_FILE, DEFAULT_CATALOG, PHASE3_DELTA_FILE,
    PHASE3_DELTA_REPORT, Phase3NotReady, SelectionError, _phase3_adopt_current,
    _sha, prepare_selection,
    run_delta_materialization,
)
from tasks.factory.compiler.phase2_readiness import (phase2_fingerprint,
                                                     phase2_task_integrity)


def _ready(*, repo: Path) -> dict:
    return {"ready": True, "blockers": []}


def _package(task: Path, row: dict, record: dict, catalog_sha: str,
             *, current: bool = False) -> None:
    (task / "fixtures").mkdir(parents=True)
    (task / "tests").mkdir(parents=True)
    blueprint = {**row, "t0_stressors": row["t0_stressor_codes"],
                 "hazards": row["hazards"]}
    (task / "fixtures/blueprint.json").write_text(json.dumps(blueprint))
    report = {"composed": True, "task_dir": str(task)}
    report_path = task / "tests/materializer_report.json"
    report_path.write_text(json.dumps(report))
    seal_path = task / "tests/phase2_provenance.json"
    seal_path.write_text(json.dumps({
        "task_id": row["task_id"], "theme": row["theme"],
        "fingerprint": phase2_fingerprint(row) if current else "historical-source-fingerprint",
        "task_integrity": phase2_task_integrity(task),
    }))
    proof = {
        "task_id": row["task_id"], "catalog_sha256": catalog_sha,
        "source_line": record["source_line"],
        "source_row_sha256": record["source_row_sha256"],
        "selection_role": record["selection_role"],
        "phase2_materializer_invoked": True,
        "materializer_report_sha256": _sha(report_path.read_bytes()),
    }
    (task / ATTESTATION_FILE).write_text(json.dumps(proof))


def _fixture(tmp_path: Path) -> tuple[dict, Path, Path, Path, str]:
    out = tmp_path / "pilot"
    selection = prepare_selection(DEFAULT_CATALOG, out)
    catalog_sha = selection["catalog"]["sha256"]
    for record in selection["selected"]:
        row = record["blueprint"]
        _package(out / row["task_id"], row, record, catalog_sha)
    certified = next(r["blueprint"]["task_id"] for r in selection["selected"]
                     if r["blueprint"]["fault_topology_id"] == "FT01")
    run_dir = tmp_path / "runs" / certified / "k5"
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text('{"cached":true}\n')
    for i in range(5):
        child = run_dir / f"r{i}"
        child.mkdir()
        (child / "rollout.json").write_text('{"cached":true}\n')
    summary = tmp_path / "calibration_summary.json"
    summary.write_text(json.dumps({
        "tasks_dir": str(out), "certified": [certified],
        "stage2": {"arms": {certified: {
            "certified": True, "k": 5, "run_id": "k5",
            "run_dir": str(run_dir), "passed": 2, "pass_at_1": 0.4}}},
    }))
    status_dir = tmp_path / "status"
    status_dir.mkdir()
    for agent in ("codex", "opus", "kimi", "grok"):
        (status_dir / f"DONE_{agent}_phase3.json").write_text(
            json.dumps({"status": "complete"}))
    return selection, out, summary, status_dir, certified


def _fake_materialize(row: dict, root: Path, *, scratch: Path,
                      overwrite: bool, phase2: bool) -> Path:
    assert phase2 and overwrite
    task = root / row["task_id"]
    _package(task, row, {"source_line": 0, "source_row_sha256": "",
                         "selection_role": ""}, "", current=True)
    return task


def _bytes(task: Path) -> dict[str, bytes]:
    return {p.relative_to(task).as_posix(): p.read_bytes()
            for p in task.rglob("*") if p.is_file()}


def _audit_fake(run_dir: Path, task_id: str, k: int) -> dict:
    assert k == 5 and run_dir.is_dir() and task_id
    return {"run_id": "k5", "deficits": [], "metrics": {
        "task": task_id, "k": 5, "scored": 5, "passed": 2,
        "pass_at_1": 0.4, "scalar_variance": 0.24,
        "quarantined_count": 0, "measurement_valid": True,
        "outcome_counts": {"resolved": 2, "nothing_done": 3},
    }}


def test_delta_rebuilds_only_selected_noncertified_tasks_and_resumes(tmp_path: Path) -> None:
    selection, out, summary, status_dir, certified = _fixture(tmp_path)
    certified_bytes = _bytes(out / certified)
    run_file = next((tmp_path / "runs" / certified).rglob("rollout.json"))
    run_bytes = run_file.read_bytes()
    scratch = tmp_path / "scratch"
    first = run_delta_materialization(
        selection, out, scratch=scratch, only_topologies={"FT01"},
        calibration_summary=summary, status_dir=status_dir,
        materialize_fn=_fake_materialize, readiness_fn=_ready,
        audit_live_run_fn=_audit_fake, limit=2)
    assert first["state"] == "partial"
    assert first["certified_preserved"] == 1
    assert first["counts"]["rematerialized"] == 2
    assert _bytes(out / certified) == certified_bytes
    assert run_file.read_bytes() == run_bytes

    second = run_delta_materialization(
        selection, out, scratch=scratch, only_topologies={"FT01"},
        calibration_summary=summary, status_dir=status_dir,
        materialize_fn=_fake_materialize, readiness_fn=_ready,
        audit_live_run_fn=_audit_fake)
    assert second["state"] == "complete"
    assert second["counts"]["preserved_certified"] == 1
    assert second["counts"]["existing_current"] == 2
    assert second["counts"]["rematerialized"] == 1
    assert second["counts"]["preserved_unselected"] == 47
    manifest = json.loads((out / PHASE3_DELTA_FILE).read_text())
    assert len(manifest["tasks"]) == 51
    assert manifest["tasks"][certified]["runtime_compatibility"] == "unverified"
    assert manifest["tasks"][certified]["cached_run_reuse"] == "requires_runtime_equivalence"
    assert (out / PHASE3_DELTA_REPORT).is_file()
    assert _bytes(out / certified) == certified_bytes
    assert run_file.read_bytes() == run_bytes


def test_delta_adopts_current_package_from_revised_catalog_without_rebuilding(
        tmp_path: Path) -> None:
    selection, _, _, _, _ = _fixture(tmp_path)
    record = selection["selected"][0]
    row = record["blueprint"]
    task = tmp_path / "concurrent" / row["task_id"]
    _package(task, row, record, "revised-catalog", current=True)
    integrity = phase2_task_integrity(task)
    lineage = _phase3_adopt_current(task, record, row,
                                   selection["catalog"]["sha256"])
    assert lineage is not None
    assert lineage["package_integrity"] == integrity
    assert phase2_task_integrity(task) == integrity
    proof = json.loads((task / ATTESTATION_FILE).read_text())
    assert proof["catalog_sha256"] == selection["catalog"]["sha256"]
    assert proof["phase3_blueprint_sha256"] == _sha(
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode())

    proof_before = (task / ATTESTATION_FILE).read_bytes()
    seal_path = task / "tests/phase2_provenance.json"
    seal = json.loads(seal_path.read_text())
    seal["fingerprint"] = "wrong-source"
    seal_path.write_text(json.dumps(seal))
    assert _phase3_adopt_current(task, record, row,
                                 selection["catalog"]["sha256"]) is None
    assert (task / ATTESTATION_FILE).read_bytes() == proof_before


def test_delta_requires_every_phase3_marker_before_writing(tmp_path: Path) -> None:
    selection, out, summary, status_dir, certified = _fixture(tmp_path)
    (status_dir / "DONE_kimi_phase3.json").unlink()
    original = _bytes(out / certified)
    with pytest.raises(Phase3NotReady, match="kimi Phase 3 marker"):
        run_delta_materialization(
            selection, out, scratch=tmp_path / "scratch",
            only_topologies={"FT01"}, calibration_summary=summary,
            status_dir=status_dir, materialize_fn=_fake_materialize,
            readiness_fn=_ready, audit_live_run_fn=_audit_fake)
    assert not (out / PHASE3_DELTA_FILE).exists()
    assert _bytes(out / certified) == original


def test_failed_delta_restores_previous_package(tmp_path: Path) -> None:
    selection, out, summary, status_dir, certified = _fixture(tmp_path)
    target = next(r["blueprint"]["task_id"] for r in selection["selected"]
                  if r["blueprint"]["fault_topology_id"] == "FT02")
    original = _bytes(out / target)

    def fail(row: dict, root: Path, *, scratch: Path,
             overwrite: bool, phase2: bool) -> Path:
        (root / row["task_id"]).mkdir()
        raise RuntimeError("deliberate compile failure")

    report = run_delta_materialization(
        selection, out, scratch=tmp_path / "scratch",
        only_topologies={"FT02"}, calibration_summary=summary,
        status_dir=status_dir, materialize_fn=fail, readiness_fn=_ready,
        audit_live_run_fn=_audit_fake,
        limit=1)
    assert report["state"] == "failed"
    assert report["counts"]["failed_restored"] == 1
    assert _bytes(out / target) == original


def test_delta_rejects_summary_only_certificate(tmp_path: Path) -> None:
    selection, out, summary, status_dir, certified = _fixture(tmp_path)
    original = _bytes(out / certified)
    with pytest.raises(SelectionError, match="K=5 live evidence failed audit"):
        run_delta_materialization(
            selection, out, scratch=tmp_path / "scratch",
            only_topologies={"FT01"}, calibration_summary=summary,
            status_dir=status_dir, materialize_fn=_fake_materialize,
            readiness_fn=_ready)
    assert not (out / PHASE3_DELTA_FILE).exists()
    assert _bytes(out / certified) == original
