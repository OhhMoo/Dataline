"""Deterministic 51-cell selection and gated, resumable materialization."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from tasks.factory.compiler.phase2_pilot import (
    DEFAULT_CATALOG, PROGRESS_FILE, SELECTION_FILE, Phase2NotReady,
    SelectionError, load_catalog, prepare_selection, run_materialization,
    select_pilot,
)
from tasks.factory.compiler.phase2_readiness import (phase2_fingerprint,
                                                     phase2_task_integrity)


def _ready(*, repo: Path) -> dict:
    return {"ready": True, "checks": {}, "blockers": []}


def _blocked(*, repo: Path) -> dict:
    return {"ready": False, "checks": {},
            "blockers": ["codex.three_modules: missing Phase 2 pipeline"]}


def test_stratified_selection_covers_every_required_axis() -> None:
    provenance, rows = load_catalog(DEFAULT_CATALOG)
    chosen = select_pilot(rows)
    assert provenance["row_count"] == 1080
    assert len(chosen) == 51
    assert len({r["blueprint"]["task_id"] for r in chosen}) == 51
    assert Counter(r["selection_role"] for r in chosen) == {
        "theme_governance": 36, "topology": 15}
    pairs = {(r["blueprint"]["theme"], r["blueprint"]["governance_mode"])
             for r in chosen if r["selection_role"] == "theme_governance"}
    assert len(pairs) == 36
    assert {r["blueprint"]["fault_topology_id"] for r in chosen
            if r["selection_role"] == "topology"} == {
                f"FT{i:02d}" for i in range(1, 16)}
    assert Counter(r["blueprint"]["governance_mode"] for r in chosen) == {
        "act-authorized": 17, "withhold-blocked": 17,
        "escalate-ambiguous": 17}
    assert sorted(Counter(r["blueprint"]["theme"] for r in chosen).values()) == [4] * 9 + [5] * 3
    assert select_pilot(rows) == chosen


def test_selection_manifest_is_idempotent_and_detects_source_drift(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.jsonl"
    catalog.write_bytes(DEFAULT_CATALOG.read_bytes())
    out = tmp_path / "pilot"
    first = prepare_selection(catalog, out)
    body = (out / SELECTION_FILE).read_bytes()
    second = prepare_selection(catalog, out)
    assert second == first
    assert (out / SELECTION_FILE).read_bytes() == body
    assert first["stage0_verified"] is False
    assert first["phase2_status"] == "selection_only"
    assert first["selected"][0]["source_line"] >= 1
    assert len(first["selected"][0]["source_row_sha256"]) == 64
    catalog.write_bytes(catalog.read_bytes() + b"\n")
    with pytest.raises(SelectionError, match="different source provenance"):
        prepare_selection(catalog, out)
    with pytest.raises(SelectionError, match="source catalog changed"):
        run_materialization(first, out, scratch=tmp_path / "scratch",
                            readiness_fn=_ready)


def test_readiness_blocker_prevents_any_materializer_call(tmp_path: Path) -> None:
    selection = prepare_selection(DEFAULT_CATALOG, tmp_path / "pilot")
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(1)
        raise AssertionError("must not materialize while blocked")

    with pytest.raises(Phase2NotReady, match="codex.three_modules"):
        run_materialization(selection, tmp_path / "pilot",
                            scratch=tmp_path / "scratch",
                            materialize_fn=forbidden, readiness_fn=_blocked)
    assert not calls
    progress = json.loads((tmp_path / "pilot" / PROGRESS_FILE).read_text())
    assert progress["state"] == "blocked_upstream"
    assert progress["counts"] == {"selected_unmaterialized": 51}
    assert progress["phase2_ready"] is False


def test_materialization_resumes_and_passes_phase2_flag(tmp_path: Path) -> None:
    out = tmp_path / "pilot"
    selection = prepare_selection(DEFAULT_CATALOG, out)
    calls = []

    def fake_materialize(row, root, *, scratch, overwrite, phase2):
        assert phase2 is True
        assert overwrite is True
        task = root / row["task_id"]
        (task / "fixtures").mkdir(parents=True, exist_ok=True)
        (task / "tests").mkdir(parents=True, exist_ok=True)
        (task / "fixtures/blueprint.json").write_text(json.dumps(row))
        (task / "tests/materializer_report.json").write_text(
            json.dumps({"task_id": row["task_id"], "composed": True}))
        (task / "tests/phase2_provenance.json").write_text(json.dumps({
            "task_id": row["task_id"], "fingerprint": phase2_fingerprint(row),
            "task_integrity": phase2_task_integrity(task)}))
        calls.append(row["task_id"])
        return task

    first = run_materialization(selection, out, scratch=tmp_path / "scratch",
                                limit=2, materialize_fn=fake_materialize,
                                readiness_fn=_ready)
    assert len(calls) == 2
    assert first["counts"] == {"materialized_unverified": 2,
                                "selected_unmaterialized": 49}
    second = run_materialization(selection, out, scratch=tmp_path / "scratch",
                                 limit=2, materialize_fn=fake_materialize,
                                 readiness_fn=_ready)
    assert len(calls) == 4
    assert len(set(calls)) == 4
    assert second["counts"] == {"existing_unverified": 2,
                                 "materialized_unverified": 2,
                                 "selected_unmaterialized": 47}
    assert second["stage0_verified"] is False
    assert second["calibration_certified"] is False
    proof = json.loads((out / calls[0] / "tests/phase2_pilot_provenance.json").read_text())
    assert proof["catalog_sha256"] == selection["catalog"]["sha256"]
    assert proof["stage0_verified"] is False
