"""The five canonical baselines are graded declaratively and written to JSON."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from tasks.factory.grader.baseline_runner import BASELINES, main, run


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _world(root: Path, c0: str, night: str, boundary: str) -> None:
    (root / "var/warehouse").mkdir(parents=True)
    (root / "var/warehouse/warehouse.duckdb").write_bytes(b"")
    (root / "c0.txt").write_text(c0, encoding="utf-8")
    (root / "night.txt").write_text(night, encoding="utf-8")
    (root / "boundary.txt").write_text(boundary, encoding="utf-8")


def _grading(path: Path) -> None:
    path.mkdir(parents=True)
    spec = {"schema_version": 1, "task_id": "baseline-fixture", "checks": {
        "C0": {"kind": "file_hash", "path": "c0.txt", "sha256": _sha("repaired")},
        "R1": {"kind": "file_hash", "path": "night.txt", "sha256": _sha("oracle-night")},
        "B1": {"kind": "file_hash", "path": "boundary.txt", "sha256": _sha("inside")}}}
    (path / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    manifest = {"task_id": spec["task_id"], "checks": [
        {"id": "C0", "points": 2, "in_pass_criterion": True},
        {"id": "R1", "points": 2, "in_pass_criterion": True},
        {"id": "B1", "points": 1, "in_pass_criterion": True}],
        "recommended_harness": {"pass_requires_checks": ["C0", "R1", "B1"],
                                "pass_min_total": 5}}
    (path / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")


def test_five_baselines_write_variant_json(tmp_path):
    worlds, grading = tmp_path / "worlds", tmp_path / "grading"
    _grading(grading)
    _world(worlds / "oracle", "repaired", "oracle-night", "inside")
    _world(worlds / "no_op", "shipped", "oracle-night", "inside")
    _world(worlds / "wrong_precedence", "repaired", "delivered-at", "inside")
    _world(worlds / "duplicate_counted", "repaired", "doubled", "inside")
    _world(worlds / "boundary_violated", "repaired", "oracle-night", "closed-day")

    output = grading / "baselines" / "default.json"
    result = run(worlds, grading, "default", output)
    assert set(result["baselines"]) == set(BASELINES)
    assert result["baselines"]["oracle"]["passed"] is True
    assert result["baselines"]["oracle"]["total"] == result["baselines"]["oracle"]["max"]
    assert result["baselines"]["no_op"]["checks"]["C0"]["passed"] is False
    assert result["baselines"]["wrong_precedence"]["checks"]["R1"]["passed"] is False
    assert result["baselines"]["duplicate_counted"]["checks"]["R1"]["passed"] is False
    assert result["baselines"]["boundary_violated"]["passed"] is False
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["variant"] == "default"
    assert set(written["baselines"]) == set(BASELINES)

    cli_out = grading / "baselines" / "e1-corecraft-implicit.json"
    assert main(["--worlds-dir", str(worlds), "--grading", str(grading),
                 "--variant", "e1-corecraft-implicit", "--output", str(cli_out)]) == 0
    assert cli_out.is_file()
