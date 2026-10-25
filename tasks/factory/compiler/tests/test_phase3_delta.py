"""Unit tests for Phase 3 readiness checks and selective delta materialization."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tasks.factory.compiler import phase2_pilot, phase2_readiness, phase2_scale
from tasks.factory.compiler.phase2_pilot import (
    ATTESTATION_FILE,
    DEFAULT_CATALOG,
    SelectionError,
    _valid_existing,
    prepare_selection,
    refresh_task_provenance_seal,
    run_materialization,
)
from tasks.factory.compiler.phase2_readiness import (
    inspect_phase2_readiness,
    inspect_phase3_readiness,
    phase2_fingerprint,
    phase2_task_integrity,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _ready_phase3_repo(root: Path) -> Path:
    for agent in ("codex", "opus", "kimi", "grok"):
        _write(
            root / f"experiments/scale_1000/status/DONE_{agent}_phase3.json",
            json.dumps({"agent": agent, "status": "complete"}),
        )
    _write(
        root / "tasks/factory/hazards/temporal_contract_hazards.py",
        "def inject_h13(seed: int, controller: str = 'ops_ctrl'):\n"
        "    return {'approved_by': controller}\n",
    )
    _write(
        root / "tasks/factory/stressors/oracle_composer.py",
        "SCHEDULE_PATH = 'workspace/orchestration/schedule.yml'\n",
    )
    _write(
        root / "tasks/factory/hazards/t0_breadcrumbs.py",
        "STRESSOR_BREADCRUMBS = {'S07': 'schema_evolution', 'S03': 'backfill_collision'}\n",
    )
    _write(
        root / "tasks/harness/blindness.py",
        "import re\n"
        "MARKERS = [('solution_dir_path', re.compile(r'(?:^|[\\s\"\\x27])(?:/workspace)?/solution(?=[/\\x27\"\\s]|$)', re.I))]\n",
    )
    _write(
        root / "world/world/scheduler.py",
        "SCHEDULE_YML = 'orchestration/schedule.yml'\n",
    )
    _write(
        root / "tasks/factory/narrative/corecraft_synthesizer.py",
        "CLUES = {'S07': 'contract v2', 'S09': 'quote escaping'}\n",
    )
    _write(
        root / "tasks/factory/narrative/discoverability_and_spoiler_linter.py",
        "REQUIRED = {'S07': ['v2'], 'S09': ['quote']}\n",
    )
    return root


def test_phase3_readiness_passes_when_markers_and_invariants_hold(tmp_path: Path) -> None:
    repo = _ready_phase3_repo(tmp_path)
    report = inspect_phase3_readiness(repo)
    assert report["ready"] is True
    assert report["blockers"] == []
    out_json = tmp_path / "phase3_readiness.json"
    rc = phase2_readiness.main(["--repo", str(repo), "--phase3", "--json", str(out_json)])
    assert rc == 0
    saved = json.loads(out_json.read_text(encoding="utf-8"))
    assert saved["ready"] is True


def test_phase3_readiness_detects_missing_markers_and_broken_invariants(tmp_path: Path) -> None:
    repo = _ready_phase3_repo(tmp_path)
    (repo / "experiments/scale_1000/status/DONE_codex_phase3.json").unlink()
    _write(
        repo / "tasks/factory/hazards/temporal_contract_hazards.py",
        "def inject_h13(seed: int):\n"
        '    return {"approved_by": "controller"}\n',
    )
    _write(
        repo / "tasks/factory/stressors/oracle_composer.py",
        "SCHEDULE_PATH = 'none'\n",
    )
    _write(
        repo / "tasks/factory/hazards/t0_breadcrumbs.py",
        "STRESSOR_BREADCRUMBS = {'S01': 'gap'}\n",
    )
    _write(
        repo / "tasks/harness/blindness.py",
        "import re\n"
        '_MARKERS = [\n'
        '    ("solution_dir_path", re.compile(r"[\\\\/]solution(?=[\\\\/\'\\"\\s]|$)", re.I)),\n'
        "]\n",
    )
    _write(
        repo / "world/world/scheduler.py",
        "SCHEDULE_YML = 'legacy.json'\n",
    )
    _write(
        repo / "tasks/factory/narrative/corecraft_synthesizer.py",
        "CLUES = {'S01': 'gap'}\n",
    )
    _write(
        repo / "tasks/factory/narrative/discoverability_and_spoiler_linter.py",
        "REQUIRED = {'S01': ['gap']}\n",
    )

    report = inspect_phase3_readiness(repo)
    assert report["ready"] is False
    blockers = "\n".join(report["blockers"])
    assert "codex.completion" in blockers
    assert "codex.h13_controller_parameter" in blockers
    assert "codex.oracle_composer_schedule_yml" in blockers
    assert "codex.stressor_breadcrumbs" in blockers
    assert "opus.blindness_updated" in blockers
    assert "kimi.scheduler_schedule_yml" in blockers
    assert "grok.synthesizer_stressor_clues" in blockers
    assert "grok.linter_stressor_clues" in blockers
    assert phase2_readiness.main(["--repo", str(repo), "--phase3"]) == 1


def test_phase2_readiness_backwards_compatible() -> None:
    report = inspect_phase2_readiness()
    assert isinstance(report, dict)
    assert "ready" in report and "checks" in report and "blockers" in report


def _create_fake_pilot_task(task: Path, record: dict, catalog_sha: str,
                            *, fingerprint: str | None = None) -> None:
    row = record["blueprint"]
    (task / "fixtures").mkdir(parents=True, exist_ok=True)
    (task / "tests").mkdir(parents=True, exist_ok=True)
    (task / "workspace").mkdir(parents=True, exist_ok=True)
    (task / "workspace/marker.txt").write_text("original\n", encoding="utf-8")
    (task / "fixtures/blueprint.json").write_text(json.dumps(row), encoding="utf-8")
    report_path = task / "tests/materializer_report.json"
    report_path.write_text(
        json.dumps({"task_id": row["task_id"], "composed": True}),
        encoding="utf-8",
    )
    (task / "tests/phase2_provenance.json").write_text(
        json.dumps({
            "task_id": row["task_id"],
            "theme": row["theme"],
            "fingerprint": fingerprint or phase2_readiness.phase2_fingerprint(row),
            "task_integrity": phase2_task_integrity(task),
        }),
        encoding="utf-8",
    )
    (task / ATTESTATION_FILE).write_text(
        json.dumps({
            "schema_version": 1,
            "task_id": row["task_id"],
            "catalog_sha256": catalog_sha,
            "source_line": record["source_line"],
            "source_row_sha256": record["source_row_sha256"],
            "selection_role": record["selection_role"],
            "materializer_report_sha256": phase2_pilot._sha(report_path.read_bytes()),
            "phase2_materializer_invoked": True,
            "stage0_verified": True,
            "calibration_certified": True,
        }),
        encoding="utf-8",
    )


def test_refresh_task_provenance_seal_restores_validity_after_fingerprint_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(phase2_readiness, "phase2_fingerprint", lambda bp, *_: f"fp-{bp.get('theme')}")
    out = tmp_path / "pilot"
    selection = prepare_selection(DEFAULT_CATALOG, out)
    record = selection["selected"][0]
    catalog_sha = selection["catalog"]["sha256"]
    task = out / record["blueprint"]["task_id"]
    _create_fake_pilot_task(task, record, catalog_sha, fingerprint="stale-phase2-fingerprint")

    assert not _valid_existing(task, record, catalog_sha)
    refresh_task_provenance_seal(task, record, catalog_sha)
    assert _valid_existing(task, record, catalog_sha)
    proof = json.loads((task / ATTESTATION_FILE).read_text(encoding="utf-8"))
    assert proof["calibration_certified"] is True
    assert proof["stage0_verified"] is True


def test_pilot_skip_certified_and_only_topologies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(phase2_readiness, "phase2_fingerprint", lambda bp, *_: f"fp-{bp.get('theme')}")
    out = tmp_path / "pilot"
    selection = prepare_selection(DEFAULT_CATALOG, out)
    catalog_sha = selection["catalog"]["sha256"]
    for record in selection["selected"]:
        _create_fake_pilot_task(
            out / record["blueprint"]["task_id"],
            record,
            catalog_sha,
            fingerprint="stale-phase2-fingerprint",
        )

    ft03_record = next(
        r for r in selection["selected"]
        if r["blueprint"]["fault_topology_id"] == "FT03"
    )
    ft01_records = [
        r for r in selection["selected"]
        if r["blueprint"]["fault_topology_id"] == "FT01"
    ]
    certified_ft03_id = ft03_record["blueprint"]["task_id"]
    certified_ft01_id = ft01_records[0]["blueprint"]["task_id"]

    summary_path = tmp_path / "calibration_summary.json"
    summary_path.write_text(
        json.dumps({
            "certified": [
                f"{certified_ft03_id}:default",
                f"{certified_ft01_id}:v1",
            ]
        }),
        encoding="utf-8",
    )

    rebuilt: list[str] = []

    def fake_materialize(row: dict, root: Path, *, scratch: Path, overwrite: bool, phase2: bool) -> Path:
        assert overwrite is True and phase2 is True
        task = root / row["task_id"]
        (task / "fixtures").mkdir(parents=True, exist_ok=True)
        (task / "tests").mkdir(parents=True, exist_ok=True)
        (task / "workspace").mkdir(parents=True, exist_ok=True)
        (task / "workspace/marker.txt").write_text("rebuilt\n", encoding="utf-8")
        (task / "fixtures/blueprint.json").write_text(json.dumps(row), encoding="utf-8")
        (task / "tests/materializer_report.json").write_text(
            json.dumps({"task_id": row["task_id"], "composed": True}),
            encoding="utf-8",
        )
        (task / "tests/phase2_provenance.json").write_text(
            json.dumps({
                "task_id": row["task_id"],
                "theme": row["theme"],
                "fingerprint": phase2_readiness.phase2_fingerprint(row),
                "task_integrity": phase2_task_integrity(task),
            }),
            encoding="utf-8",
        )
        rebuilt.append(row["task_id"])
        return task

    # 1. Run with skip_certified_summary only (limit=3 so it stays fast):
    # Both certified tasks are preserved_certified with refreshed seals; 3 uncertified stale tasks rebuild.
    progress = run_materialization(
        selection,
        out,
        scratch=tmp_path / "scratch",
        limit=3,
        skip_certified_summary=summary_path,
        materialize_fn=fake_materialize,
        readiness_fn=lambda *, repo: {"ready": True, "checks": {}, "blockers": []},
    )
    assert progress["counts"].get("preserved_certified") == 2
    assert certified_ft03_id not in rebuilt
    assert certified_ft01_id not in rebuilt
    assert _valid_existing(out / certified_ft03_id, ft03_record, catalog_sha)
    assert _valid_existing(out / certified_ft01_id, ft01_records[0], catalog_sha)
    assert (out / certified_ft03_id / "workspace/marker.txt").read_text(encoding="utf-8") == "original\n"

    # 2. Run with only_topologies={"FT02"} and skip_certified_summary:
    # Only FT02 tasks are rebuilt; certified tasks remain preserved_certified; all other existing tasks have seals refreshed!
    rebuilt.clear()
    progress2 = run_materialization(
        selection,
        out,
        scratch=tmp_path / "scratch",
        skip_certified_summary=summary_path,
        only_topologies={"FT02"},
        materialize_fn=fake_materialize,
        readiness_fn=lambda *, repo: {"ready": True, "checks": {}, "blockers": []},
    )
    ft02_ids = {
        r["blueprint"]["task_id"]
        for r in selection["selected"]
        if r["blueprint"]["fault_topology_id"] == "FT02"
    }
    assert set(rebuilt) == ft02_ids
    assert progress2["counts"].get("preserved_certified") == 2
    assert progress2["state"] == "materialized_unverified"
    for record in selection["selected"]:
        tid = record["blueprint"]["task_id"]
        assert _valid_existing(out / tid, record, catalog_sha)


def test_scale_only_topologies_and_refresh_preserved_seals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(phase2_readiness, "phase2_fingerprint", lambda bp, *_: f"fp-{bp.get('theme')}")
    monkeypatch.setattr(phase2_scale, "phase2_fingerprint", lambda bp, *_: f"fp-{bp.get('theme')}")
    entries = [
        {
            "task_id": "FT0001",
            "theme": "healthcare_clinical_intake",
            "fault_topology_id": "FT01",
            "governance_mode": "act-authorized",
            "schedule_seed_id": "S_A",
        },
        {
            "task_id": "FT0003",
            "theme": "healthcare_clinical_intake",
            "fault_topology_id": "FT03",
            "governance_mode": "act-authorized",
            "schedule_seed_id": "S_A",
        },
    ]
    catalog = tmp_path / "catalog.jsonl"
    catalog.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    pilot_dir = tmp_path / "pilot"
    pilot_dir.mkdir()
    selection = pilot_dir / "phase2_selection.json"
    selection.write_text("{}", encoding="utf-8")

    for row in entries:
        ptask = pilot_dir / row["task_id"]
        (ptask / "tests").mkdir(parents=True)
        (ptask / "workspace").mkdir()
        (ptask / "workspace/data.txt").write_text("pilot\n", encoding="utf-8")
        (ptask / "tests/phase2_provenance.json").write_text(
            json.dumps({
                "task_id": row["task_id"],
                "theme": row["theme"],
                "fingerprint": phase2_readiness.phase2_fingerprint(row),
                "task_integrity": phase2_task_integrity(ptask),
            }),
            encoding="utf-8",
        )

    out_dir = tmp_path / "tasks_1080"
    for row in entries:
        ftask = out_dir / row["task_id"]
        (ftask / "tests").mkdir(parents=True)
        (ftask / "workspace").mkdir()
        (ftask / "workspace/data.txt").write_text("stale_scale\n", encoding="utf-8")
        (ftask / "tests/phase2_provenance.json").write_text(
            json.dumps({
                "task_id": row["task_id"],
                "theme": row["theme"],
                "fingerprint": "stale-fingerprint",
                "task_integrity": phase2_task_integrity(ftask),
            }),
            encoding="utf-8",
        )

    rebuilt_scale: list[str] = []

    def fake_scale_materialize(bp: dict, out_str: str, scratch_str: str) -> dict:
        rebuilt_scale.append(bp["task_id"])
        ftask = Path(out_str) / bp["task_id"]
        (ftask / "workspace/data.txt").write_text("rebuilt_scale\n", encoding="utf-8")
        (ftask / "tests/phase2_provenance.json").write_text(
            json.dumps({
                "task_id": bp["task_id"],
                "theme": bp["theme"],
                "fingerprint": phase2_readiness.phase2_fingerprint(bp),
                "task_integrity": phase2_task_integrity(ftask),
            }),
            encoding="utf-8",
        )
        return {"task_id": bp["task_id"], "status": "materialized", "path": str(ftask)}

    class InlinePool:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, fn, *args):
            class DoneFuture:
                def __init__(self, val):
                    self._val = val

                def result(self):
                    return self._val

            return DoneFuture(fn(*args))

    monkeypatch.setattr(phase2_scale, "FINAL_REPORT", tmp_path / "final.md")
    monkeypatch.setattr(phase2_scale, "DONE_MARKER", tmp_path / "done.json")
    monkeypatch.setattr(phase2_scale, "assert_outside_git_repo", lambda p, **_kw: Path(p))
    monkeypatch.setattr(phase2_scale, "inspect_phase2_readiness", lambda *_: {"ready": True, "blockers": []})
    monkeypatch.setattr(phase2_scale, "_read_catalog", lambda *_: (entries, {"valid": True, "errors": []}))
    monkeypatch.setattr(phase2_scale, "_pilot_rows", lambda *_: (entries, []))
    monkeypatch.setattr(phase2_scale, "_pilot_stage0", lambda *_: [])
    monkeypatch.setattr(phase2_scale, "_run_provenance_failures", lambda *_: [])
    monkeypatch.setattr(phase2_scale, "evaluate_hardness", lambda *_: {"certified": True, "deficits": [], "certified_tasks": 51})
    monkeypatch.setattr(phase2_scale, "_materialize_one", fake_scale_materialize)
    monkeypatch.setattr(phase2_scale.concurrent.futures, "ProcessPoolExecutor", InlinePool)
    monkeypatch.setattr(phase2_scale.concurrent.futures, "as_completed", lambda fs: fs)

    report = phase2_scale.run_scale(
        catalog=catalog,
        selection=selection,
        out_dir=out_dir,
        scratch=tmp_path / "scratch",
        report_path=tmp_path / "scale_report.json",
        only_topologies={"FT01"},
        refresh_preserved_seals=True,
    )
    assert rebuilt_scale == ["FT0001"]
    assert report["materialized"] == 1
    assert report["reused"] == 1
    assert report["refreshed_seals"] == 1
    assert phase2_scale._provenance_matches(out_dir / "FT0003", entries[1])
    assert (out_dir / "FT0003/workspace/data.txt").read_text(encoding="utf-8") == "stale_scale\n"
