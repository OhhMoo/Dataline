"""Unit tests for the 51-task Stratified Hardness Proof Selector & Materializer CLI."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tasks.factory.compiler import select_stratified_51 as mod
from tasks.factory.compiler.blueprint_matrix import (
    DEFAULT_CATALOG_PATH,
    FAULT_TOPOLOGIES,
    GOVERNANCE_MODES,
    R_HAZARDS,
    T0_STRESSORS,
    THEMES,
    WITHHOLD_SUBMODES,
)
from tasks.factory.compiler.select_stratified_51 import (
    EXPECTED_HAZARD_CODES,
    EXPECTED_STRESSOR_CODES,
    main,
    select_stratified_51,
    summarize_selection,
)


def test_select_stratified_51_determinism_and_uniqueness() -> None:
    rows_first = select_stratified_51(DEFAULT_CATALOG_PATH)
    rows_second = select_stratified_51(DEFAULT_CATALOG_PATH)
    assert rows_first == rows_second
    assert len(rows_first) == 51
    task_ids = [r["task_id"] for r in rows_first]
    assert len(set(task_ids)) == 51


def test_part_a_36_theme_governance_twins() -> None:
    rows = select_stratified_51(DEFAULT_CATALOG_PATH)
    part_a = rows[:36]
    assert len(part_a) == 36
    assert all(r["selection_reason"] == "theme_governance_grid" for r in part_a)
    assert all(r["topology_id"] == "FT01" for r in part_a)
    assert all(r["fault_topology_id"] == "FT01" for r in part_a)
    assert all(r["schedule_seed"] == "a" for r in part_a)
    assert all(r["schedule_seed_id"] == "H4_seedA" for r in part_a)

    pairs = [(r["theme"], r["governance_mode"]) for r in part_a]
    assert len(set(pairs)) == 36
    assert Counter(r["theme"] for r in part_a) == {t: 3 for t in THEMES}
    assert Counter(r["governance_mode"] for r in part_a) == {
        "act-authorized": 12,
        "withhold-blocked": 12,
        "escalate-ambiguous": 12,
    }

    withhold_rows = [r for r in part_a if r["governance_mode"] == "withhold-blocked"]
    assert len(withhold_rows) == 12
    expected_cycle = [
        WITHHOLD_SUBMODES[idx % len(WITHHOLD_SUBMODES)]
        for idx in range(len(THEMES))
    ]
    assert [r["governance_submode"] for r in withhold_rows] == expected_cycle
    submode_counts = Counter(r["governance_submode"] for r in withhold_rows)
    assert submode_counts == {
        "wrong_approver_role": 4,
        "scope_date_mismatch": 4,
        "conditional_prerequisite_unmet": 4,
    }


def test_part_b_15_topology_representatives() -> None:
    rows = select_stratified_51(DEFAULT_CATALOG_PATH)
    part_a_ids = {r["task_id"] for r in rows[:36]}
    part_b = rows[36:]
    assert len(part_b) == 15

    for ft_id, row in zip(FAULT_TOPOLOGIES.keys(), part_b):
        assert row["topology_id"] == ft_id
        assert row["selection_reason"] == f"topology_coverage_{ft_id}"
        assert row["task_id"] not in part_a_ids

    ft01_row = next(r for r in part_b if r["topology_id"] == "FT01")
    assert ft01_row["schedule_seed"] == "b"
    assert ft01_row["schedule_seed_id"] == "H4_seedB"

    mode_counts = Counter(r["governance_mode"] for r in part_b)
    assert mode_counts == {
        "act-authorized": 5,
        "withhold-blocked": 5,
        "escalate-ambiguous": 5,
    }

    theme_counts = Counter(r["theme"] for r in part_b)
    assert set(theme_counts.keys()) == set(THEMES.keys())
    assert set(theme_counts.values()).issubset({1, 2})


def test_full_51_axis_and_stressor_hazard_coverage() -> None:
    rows = select_stratified_51(DEFAULT_CATALOG_PATH)
    assert len({r["theme"] for r in rows}) == 12
    assert len({r["governance_mode"] for r in rows}) == 3
    assert len({r["topology_id"] for r in rows}) == 15

    stressor_codes = {
        code
        for r in rows
        for code in r["t0_stressor_codes"]
    }
    assert stressor_codes == set(EXPECTED_STRESSOR_CODES)
    assert len(stressor_codes) == 10

    hazard_codes = {
        code
        for r in rows
        for code in r["hazards"]
    }
    assert hazard_codes == set(EXPECTED_HAZARD_CODES)
    assert len(hazard_codes) == 14


def test_summarize_selection() -> None:
    rows = select_stratified_51(DEFAULT_CATALOG_PATH)
    summary = summarize_selection(rows)

    assert summary["total"] == 51
    assert summary["unique_task_ids"] == 51
    assert summary["part_a_count"] == 36
    assert summary["part_b_count"] == 15

    assert set(summary["by_theme"].keys()) == set(THEMES.keys())
    assert sum(summary["by_theme"].values()) == 51

    assert summary["by_governance_mode"] == {
        "act-authorized": 17,
        "withhold-blocked": 17,
        "escalate-ambiguous": 17,
    }
    assert summary["part_a_withhold_submodes"] == {
        "wrong_approver_role": 4,
        "scope_date_mismatch": 4,
        "conditional_prerequisite_unmet": 4,
    }
    assert summary["part_b_governance_modes"] == {
        "act-authorized": 5,
        "withhold-blocked": 5,
        "escalate-ambiguous": 5,
    }

    assert set(summary["by_topology_id"].keys()) == set(FAULT_TOPOLOGIES.keys())
    assert summary["by_topology_id"]["FT01"] == 37
    for ft_id in list(FAULT_TOPOLOGIES.keys())[1:]:
        assert summary["by_topology_id"][ft_id] == 1

    assert set(summary["by_stressor"].keys()) == {f"S{i:02d}" for i in range(1, 11)}
    assert all(v >= 1 for v in summary["by_stressor"].values())

    assert set(summary["by_hazard"].keys()) == {f"H{i:02d}" for i in range(1, 15)}
    assert all(v >= 1 for v in summary["by_hazard"].values())


def test_cli_dry_run_and_manifest_out(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest_path = tmp_path / "manifest_51.json"
    rc = main(["--dry-run", "--manifest-out", str(manifest_path)])
    assert rc == 0
    assert manifest_path.is_file()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["total_selected"] == 51
    assert manifest["active_count"] == 51
    assert manifest["only_topologies"] is False
    assert len(manifest["blueprints"]) == 51
    assert len(manifest["task_ids"]) == 51
    assert manifest["summary"]["total"] == 51

    stdout_payload = json.loads(capsys.readouterr().out)
    assert stdout_payload["count"] == 51
    assert stdout_payload["manifest_out"] == str(manifest_path)


def test_cli_only_topologies_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifest_path = tmp_path / "manifest_15.json"
    rc = main(["--dry-run", "--only-topologies", "--manifest-out", str(manifest_path)])
    assert rc == 0

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["total_selected"] == 51
    assert manifest["active_count"] == 15
    assert manifest["only_topologies"] is True
    assert len(manifest["blueprints"]) == 15
    assert {b["topology_id"] for b in manifest["blueprints"]} == set(FAULT_TOPOLOGIES.keys())

    stdout_payload = json.loads(capsys.readouterr().out)
    assert stdout_payload["count"] == 15
    assert stdout_payload["only_topologies"] is True


def test_cli_require_phase2_ready_blocks_when_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mod,
        "inspect_phase2_readiness",
        lambda repo=None: {"ready": False, "blockers": ["codex.completion: missing"]},
    )
    out_dir = tmp_path / "out"
    scratch_dir = tmp_path / "scratch"
    report_path = tmp_path / "report.json"

    rc = main(
        [
            "--out",
            str(out_dir),
            "--scratch",
            str(scratch_dir),
            "--report",
            str(report_path),
            "--require-phase2-ready",
            "--workers",
            "1",
        ]
    )
    assert rc == 1
    assert not report_path.exists()


def test_cli_materialization_and_existing_skip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        mod,
        "inspect_phase2_readiness",
        lambda repo=None: {"ready": True, "blockers": []},
    )
    calls: list[str] = []

    def fake_materialize(row: dict, out: Path, *, scratch: Path, overwrite: bool = True) -> Path:
        calls.append(row["task_id"])
        task_dir = out / row["task_id"]
        (task_dir / "tests").mkdir(parents=True, exist_ok=True)
        (task_dir / "tests/materializer_report.json").write_text(
            json.dumps({"task_id": row["task_id"], "composed": True}),
            encoding="utf-8",
        )
        return task_dir

    monkeypatch.setattr(mod, "materialize", fake_materialize)

    out_dir = tmp_path / "out"
    scratch_dir = tmp_path / "scratch"
    report_path = tmp_path / "report.json"

    rc1 = main(
        [
            "--out",
            str(out_dir),
            "--scratch",
            str(scratch_dir),
            "--report",
            str(report_path),
            "--only-topologies",
            "--require-phase2-ready",
            "--workers",
            "1",
        ]
    )
    assert rc1 == 0
    assert len(calls) == 15
    report1 = json.loads(report_path.read_text(encoding="utf-8"))
    assert report1["materialized"] == 15
    assert report1["existing"] == 0
    assert report1["failed"] == 0

    # Second run without --overwrite should mark all 15 as existing
    calls.clear()
    rc2 = main(
        [
            "--out",
            str(out_dir),
            "--scratch",
            str(scratch_dir),
            "--report",
            str(report_path),
            "--only-topologies",
            "--workers",
            "1",
        ]
    )
    assert rc2 == 0
    assert len(calls) == 0
    report2 = json.loads(report_path.read_text(encoding="utf-8"))
    assert report2["materialized"] == 0
    assert report2["existing"] == 15
    assert report2["failed"] == 0
