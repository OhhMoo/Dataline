"""Selective Phase 3 delta materialization leaves certified pilot tasks untouched."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tasks.factory.compiler.peer_materializer import (
    PHASE3_CERTIFIED_TASK_IDS,
    PHASE3_CERTIFIED_TOPOLOGIES,
    PHASE3_MUTATED_TOPOLOGIES,
    REPO,
    main,
    materialize_phase3_delta,
    select_phase3_delta_blueprints,
)


def _blueprint(task_id: str, topology: str) -> dict:
    return {"task_id": task_id, "fault_topology_id": topology}


def _pilot_rows() -> list[dict]:
    rows = [
        _blueprint("FT0001-settlement-clearing-ft01-auth-a", "FT01"),
        _blueprint("FT0091-commerce-analytics-ft01-auth-a", "FT01"),
        _blueprint("FT0100-commerce-analytics-ft02-auth-b", "FT02"),
        _blueprint("FT0193-revenue-daily-ft03-auth-a", "FT03"),
        _blueprint("FT0485-excel-close-ft06-hold-b", "FT06"),
        _blueprint("FT0332-sftp-vendor-ft11-hold-a", "FT11"),
        _blueprint("FT0677-ad-attribution-events-ft08-hold-b", "FT08"),
        _blueprint("FT0963-fintech-kyc-risk-limits-ft11-esc-a", "FT11"),
        _blueprint("FT1077-iot-telemetry-billing-ft15-esc-a", "FT15"),
        _blueprint("FT9999-theme-ft03-auth-a", "FT03"),
    ]
    return rows


def _scratch(tmp_path: Path) -> Path:
    scratch = tmp_path / "scratch"
    resolved = scratch.resolve()
    if resolved == REPO or REPO in resolved.parents:
        scratch = Path("/home/ubuntu/alkera_factory_1017/phase3_delta_scratch/unit") / tmp_path.name
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def _package(task: Path) -> None:
    (task / "tests").mkdir(parents=True, exist_ok=True)
    (task / "solution").mkdir(parents=True, exist_ok=True)
    (task / "tests/oracle_receipts.json").write_text(
        json.dumps({"nights": [{"receipts": [1]}]}) + "\n", encoding="utf-8")
    (task / "tests/governance.json").write_text(
        json.dumps({"mode": "act-authorized"}) + "\n", encoding="utf-8")
    (task / "solution/solve.py").write_text("print('ok')\n", encoding="utf-8")


def test_select_skips_certified_topologies_and_ids() -> None:
    rebuild, skipped = select_phase3_delta_blueprints(_pilot_rows())
    rebuilt_ids = {row["task_id"] for row in rebuild}
    assert rebuilt_ids == {
        "FT0001-settlement-clearing-ft01-auth-a",
        "FT0100-commerce-analytics-ft02-auth-b",
    }
    reasons = {row["task_id"]: row["reason"] for row in skipped}
    assert reasons["FT0193-revenue-daily-ft03-auth-a"] == "certified_topology"
    assert reasons["FT9999-theme-ft03-auth-a"] == "certified_topology"
    assert reasons["FT1077-iot-telemetry-billing-ft15-esc-a"] == "certified_topology"
    for stem in PHASE3_CERTIFIED_TASK_IDS:
        match = next(row for row in skipped if row["task_id"].startswith(stem))
        assert match["reason"] == "certified_task_id"
    assert PHASE3_CERTIFIED_TOPOLOGIES.isdisjoint(PHASE3_MUTATED_TOPOLOGIES)


def test_only_topologies_still_skips_certified_ids() -> None:
    rebuild, skipped = select_phase3_delta_blueprints(
        _pilot_rows(), only_topologies={"FT01", "FT11"})
    assert [row["task_id"] for row in rebuild] == ["FT0001-settlement-clearing-ft01-auth-a"]
    assert {row["task_id"] for row in skipped if row["reason"] == "topology_not_selected"} == {
        "FT0100-commerce-analytics-ft02-auth-b",
    }
    with pytest.raises(ValueError, match="unknown fault topologies"):
        select_phase3_delta_blueprints(_pilot_rows(), only_topologies={"FT99"})


def test_delta_does_not_rebuild_certified_trees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pilot = tmp_path / "pilot"
    rows = _pilot_rows()
    for row in rows:
        task = pilot / row["task_id"]
        task.mkdir(parents=True)
        (task / "marker.txt").write_text("keep\n", encoding="utf-8")
    calls: list[str] = []

    def fake(payload: tuple) -> dict:
        blueprint, out_root, _scratch_root = payload
        calls.append(blueprint["task_id"])
        task = Path(out_root) / blueprint["task_id"]
        if task.exists():
            import shutil
            shutil.rmtree(task)
        task.mkdir()
        _package(task)
        return {"task_id": blueprint["task_id"], "status": "ok", "path": str(task), "seconds": 0.1}

    monkeypatch.setattr(
        "tasks.factory.compiler.peer_materializer._materialize_stage0_one", fake)
    report = materialize_phase3_delta(
        pilot, _scratch(tmp_path), blueprints=rows, workers=1)
    assert report["ok"] is True
    assert calls == [
        "FT0001-settlement-clearing-ft01-auth-a",
        "FT0100-commerce-analytics-ft02-auth-b",
    ]
    for row in rows:
        if row["task_id"] in calls:
            continue
        assert (pilot / row["task_id"] / "marker.txt").read_text(encoding="utf-8") == "keep\n"
    assert {row["task_id"] for row in report["skipped"] if row["reason"].startswith("certified")} == {
        row["task_id"] for row in rows if row["task_id"] not in calls
    }


def test_missing_receipts_are_not_a_successful_rebuild(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pilot = tmp_path / "pilot"
    row = _blueprint("FT0100-commerce-analytics-ft02-auth-b", "FT02")

    def fake(payload: tuple) -> dict:
        blueprint, out_root, _scratch_root = payload
        task = Path(out_root) / blueprint["task_id"]
        task.mkdir(parents=True)
        (task / "solution").mkdir()
        (task / "solution/solve.py").write_text("print(1)\n", encoding="utf-8")
        (task / "tests").mkdir()
        (task / "tests/governance.json").write_text("{}\n", encoding="utf-8")
        return {"task_id": blueprint["task_id"], "status": "ok", "path": str(task)}

    monkeypatch.setattr(
        "tasks.factory.compiler.peer_materializer._materialize_stage0_one", fake)
    report = materialize_phase3_delta(
        pilot, _scratch(tmp_path), blueprints=[row], workers=1)
    assert report["ok"] is False
    assert report["rebuilt_task_ids"] == []
    assert "oracle_receipts.json" in report["failed"][0]["error"]


def test_parallel_workers_and_in_repo_scratch_are_refused(tmp_path: Path) -> None:
    row = _blueprint("FT0100-commerce-analytics-ft02-auth-b", "FT02")
    with pytest.raises(ValueError, match="one worker"):
        materialize_phase3_delta(tmp_path, _scratch(tmp_path), blueprints=[row], workers=4)
    with pytest.raises(ValueError, match="outside the git repository"):
        materialize_phase3_delta(tmp_path, REPO / "experiments", blueprints=[row], workers=1)


def test_cli_delta_only_is_selective(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake(pilot: Path, scratch: Path, **kwargs):
        seen["pilot"] = Path(pilot)
        seen["scratch"] = Path(scratch)
        seen["kwargs"] = kwargs
        return {"ok": True, "rebuilt_task_ids": ["FT0100"], "skipped": [{}], "failed": [],
                "scratch": str(scratch)}

    monkeypatch.setattr(
        "tasks.factory.compiler.peer_materializer.materialize_phase3_delta", fake)
    scratch = _scratch(tmp_path)
    rc = main([
        "--delta-only", "--skip-certified",
        "--only-topologies", "FT02,FT04",
        "--out", str(tmp_path),
        "--scratch", str(scratch),
        "--workers", "1",
        "--limit", "1",
    ])
    assert rc == 0
    assert seen["kwargs"]["only_topologies"] == {"FT02", "FT04"}
    assert seen["kwargs"]["workers"] == 1
    assert main(["--delta-only", "--out", str(tmp_path), "--workers", "8"]) == 2
    assert main(["--delta-only", "--skip-certified", "--out", str(tmp_path), "--workers", "8"]) == 2
