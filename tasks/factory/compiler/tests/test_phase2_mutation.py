"""Provenance and matrix invariants for the Phase 2 candidate catalog overlay."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from tasks.factory.compiler.phase2_mutation import (
    MutationError, derive_catalog, validate_derived_catalog,
)
from tasks.factory.compiler.phase2_pilot import DEFAULT_CATALOG, load_catalog


def _write_journal(path: Path, source_sha: str, operations: list[dict]) -> Path:
    path.write_text(json.dumps({
        "schema_version": 1,
        "source_catalog_sha256": source_sha,
        "operations": operations,
    }), encoding="utf-8")
    return path


def _operation(record: dict, *, round_number: int = 1,
               action: str = "increase_difficulty") -> dict:
    return {
        "round": round_number,
        "source_task_id": record["blueprint"]["task_id"],
        "source_row_sha256": record["source_row_sha256"],
        "scope": "fault_topology",
        "prescription": {"action": action},
        "evidence": {"k3_run_dir": "/tmp/example-k3"},
    }


def test_ceiling_mutation_propagates_with_source_lineage_and_matching_selection(tmp_path: Path) -> None:
    source_path = tmp_path / "frozen_catalog.jsonl"
    original_bytes = DEFAULT_CATALOG.read_bytes()
    source_path.write_bytes(original_bytes)
    provenance, records = load_catalog(source_path)
    anchor = next(row for row in records if row["blueprint"]["fault_topology_id"] == "FT01")
    journal = _write_journal(tmp_path / "journal.json", provenance["sha256"],
                             [_operation(anchor)])

    result = derive_catalog(source_path, journal, tmp_path / "candidate")
    assert result["status"] == "candidate_unverified"
    assert result["changed_task_count"] == 72
    assert len(result["changed_proof_task_ids"]) == 4
    assert source_path.read_bytes() == original_bytes
    assert result["source_catalog_sha256"] == hashlib.sha256(original_bytes).hexdigest()
    assert result["operations"][0]["anchor_helper_mutation"]["to_task_id"].endswith("-m1")

    derived_provenance, derived_records = load_catalog(Path(result["derived_catalog"]))
    derived = [record["blueprint"] for record in derived_records]
    original = [record["blueprint"] for record in records]
    assert derived_provenance["sha256"] == result["derived_catalog_sha256"]
    assert not validate_derived_catalog(original, derived)
    assert [row["task_id"] for row in derived] == [row["task_id"] for row in original]
    assert all(len(row["r_hazards"]) == 3 for row in derived
               if row["fault_topology_id"] == "FT01")
    assert all(row == old for old, row in zip(original, derived)
               if old["fault_topology_id"] != "FT01")
    fixed_width = next(row for row in derived if row["theme"] == "mainframe_batch"
                       and row["fault_topology_id"] == "FT01")
    assert "H02" not in fixed_width["hazards"]
    assert len(fixed_width["hazards"]) == 3

    crosswalk = json.loads(Path(result["crosswalk"]).read_text(encoding="utf-8"))
    assert len(crosswalk["rows"]) == 1080
    assert sum(bool(row["changed_fields"]) for row in crosswalk["rows"]) == 72
    anchor_lineage = next(row for row in crosswalk["rows"]
                          if row["source_task_id"] == anchor["blueprint"]["task_id"])
    assert anchor_lineage["source_row_sha256"] == anchor["source_row_sha256"]
    assert anchor_lineage["applied_sequences"] == [1]
    assert "r_hazards" in anchor_lineage["changed_fields"]

    selection = json.loads(Path(result["selection"]).read_text(encoding="utf-8"))
    by_id = {row["task_id"]: row for row in derived}
    assert selection["catalog"]["sha256"] == result["derived_catalog_sha256"]
    assert len(selection["selected"]) == 51
    assert all(item["blueprint"] == by_id[item["blueprint"]["task_id"]]
               for item in selection["selected"])
    assert result == derive_catalog(source_path, journal, tmp_path / "candidate")


def test_empty_journal_preserves_all_source_rows_and_selection(tmp_path: Path) -> None:
    source_sha, _ = load_catalog(DEFAULT_CATALOG)
    journal = _write_journal(tmp_path / "journal.json", source_sha["sha256"], [])
    result = derive_catalog(DEFAULT_CATALOG, journal, tmp_path / "candidate")
    assert Path(result["derived_catalog"]).read_bytes() == DEFAULT_CATALOG.read_bytes()
    assert result["changed_task_count"] == 0
    assert result["changed_proof_task_ids"] == []


def test_later_round_replays_and_same_round_conflict_is_rejected(tmp_path: Path) -> None:
    provenance, records = load_catalog(DEFAULT_CATALOG)
    ft01 = [row for row in records if row["blueprint"]["fault_topology_id"] == "FT01"]
    journal = _write_journal(tmp_path / "journal.json", provenance["sha256"], [
        _operation(ft01[0]),
        _operation(ft01[1], round_number=2, action="reduce_hazard_count"),
    ])
    result = derive_catalog(DEFAULT_CATALOG, journal, tmp_path / "candidate")
    _, derived_records = load_catalog(Path(result["derived_catalog"]))
    affected = [record["blueprint"] for record in derived_records
                if record["blueprint"]["fault_topology_id"] == "FT01"]
    assert result["changed_task_count"] == 72
    assert all(len(row["r_hazards"]) == 2 for row in affected)
    assert all(row["breadcrumb_density"] == "explicit_ops_log" for row in affected)
    assert len(result["operations"]) == 2

    _write_journal(journal, provenance["sha256"], [
        _operation(ft01[0]), _operation(ft01[1]),
    ])
    with pytest.raises(MutationError, match="conflicting prescriptions"):
        derive_catalog(DEFAULT_CATALOG, journal, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


def test_second_ceiling_round_adds_third_compatible_stressor_from_frozen_source(tmp_path: Path) -> None:
    provenance, records = load_catalog(DEFAULT_CATALOG)
    ft01 = [row for row in records if row["blueprint"]["fault_topology_id"] == "FT01"]
    journal = _write_journal(tmp_path / "journal.json", provenance["sha256"], [
        _operation(ft01[0]),
    ])
    first = derive_catalog(DEFAULT_CATALOG, journal, tmp_path / "candidate")
    _write_journal(journal, provenance["sha256"], [
        _operation(ft01[0]),
        _operation(ft01[1], round_number=2),
    ])
    second = derive_catalog(DEFAULT_CATALOG, journal, tmp_path / "candidate")
    assert first["version"] != second["version"]
    assert first["source_catalog_sha256"] == second["source_catalog_sha256"]
    _, first_records = load_catalog(Path(first["derived_catalog"]))
    _, second_records = load_catalog(Path(second["derived_catalog"]))
    first_rows = [r["blueprint"] for r in first_records if r["blueprint"]["fault_topology_id"] == "FT01"]
    second_rows = [r["blueprint"] for r in second_records if r["blueprint"]["fault_topology_id"] == "FT01"]
    assert all(len(row["t0_stressors"]) == 2 for row in first_rows)
    assert all(len(row["t0_stressors"]) == 3 for row in second_rows)
    assert all(len(row["r_hazards"]) == 3 for row in second_rows)
    assert len(second["operations"]) == 2
    source_rows = [r["blueprint"] for r in records]
    bad_rows = [copy.deepcopy(r["blueprint"]) for r in second_records]
    bad = next(row for row in bad_rows if row["fault_topology_id"] == "FT01")
    bad["t0_stressors"][2] = "S99_unknown"
    bad["t0_stressor_codes"][2] = "S99"
    assert any("unknown third t0 stressor" in error
               for error in validate_derived_catalog(source_rows, bad_rows))


def test_source_drift_and_bad_prescription_fail_before_writing(tmp_path: Path) -> None:
    provenance, records = load_catalog(DEFAULT_CATALOG)
    anchor = records[0]
    operation = _operation(anchor)
    operation["source_row_sha256"] = "0" * 64
    journal = _write_journal(tmp_path / "journal.json", provenance["sha256"], [operation])
    with pytest.raises(MutationError, match="source row SHA-256 differs"):
        derive_catalog(DEFAULT_CATALOG, journal, tmp_path / "candidate")
    assert not (tmp_path / "candidate").exists()

    operation = _operation(anchor, action="unsupported")
    _write_journal(journal, provenance["sha256"], [operation])
    with pytest.raises(MutationError, match="unsupported prescription action"):
        derive_catalog(DEFAULT_CATALOG, journal, tmp_path / "candidate")
    assert not (tmp_path / "candidate").exists()

    operation = _operation(anchor)
    operation["expected_before_blueprint_sha256"] = "0" * 64
    _write_journal(journal, provenance["sha256"], [operation])
    with pytest.raises(MutationError, match="journal precondition"):
        derive_catalog(DEFAULT_CATALOG, journal, tmp_path / "candidate")
    assert not (tmp_path / "candidate").exists()

    _write_journal(journal, "0" * 64, [_operation(anchor)])
    with pytest.raises(MutationError, match="source catalog SHA-256 differs"):
        derive_catalog(DEFAULT_CATALOG, journal, tmp_path / "candidate")
