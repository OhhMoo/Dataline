"""Derive a versioned Phase 2 catalog from a frozen catalog and mutation journal.

This module prepares candidate blueprints only. It does not materialize tasks,
run Stage 0, or claim that a candidate has passed live hardness calibration.

Journal schema::

    {
      "schema_version": 1,
      "source_catalog_sha256": "<sha256 of exact source JSONL bytes>",
      "operations": [{
        "round": 1,
        "source_task_id": "<task ID in the frozen catalog>",
        "source_row_sha256": "<sha256 of its exact JSONL line, sans newline>",
        "scope": "fault_topology",
        "expected_before_blueprint_sha256": "<optional anchor blueprint hash>",
        "prescription": {"action": "increase_difficulty",
                         "classification": "saturated_ceiling"},
        "evidence": {"k3_run_dir": "..."}
      }]
    }

Each operation applies to all 72 rows of its fault topology. At most one
operation per topology is allowed in a round. For a later round, append to
the same journal and derive again from the same frozen original catalog;
every operation retains its original source-row hash. The helper's ``-mN``
task ID and bookkeeping fields are kept in the journal; derived blueprints
retain source task IDs and matrix axes.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping

from tasks.factory.calibrate_queue import mutate_blueprint_for_prescription
from tasks.factory.compiler.blueprint_matrix import (
    GOVERNANCE_MODES, R_HAZARDS,
    T0_STRESSORS, THEMES, VALID_CONTRACTS, is_blueprint_compatible,
    validate_catalog,
)
from tasks.factory.compiler.phase2_pilot import (
    DEFAULT_CATALOG, REPO, load_catalog, prepare_selection,
)

SCHEMA_VERSION = 1
ACTION_NAMES = frozenset({
    "increase_difficulty", "add_hazard_or_obfuscate",
    "reduce_hazard_count", "step_down_difficulty", "reduce_obfuscation",
})
IMMUTABLE_FIELDS = (
    "task_id", "blueprint_index", "twin_group_id", "theme",
    "fault_topology_id", "topology_id", "governance_mode",
    "schedule_seed_id", "seed", "lexicon_seed", "lexicon_shift",
)
HELPER_BOOKKEEPING = ("base_task_id", "mutation_step", "mutation_history")


class MutationError(ValueError):
    """The candidate cannot be traced to or validated against its source."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _read_journal(path: Path, source_sha: str,
                  source_by_id: Mapping[str, dict]) -> tuple[dict, list[dict]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MutationError(f"mutation journal unreadable: {exc}") from exc
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise MutationError("mutation journal must have schema_version=1")
    if document.get("source_catalog_sha256") != source_sha:
        raise MutationError("mutation journal source catalog SHA-256 differs from frozen source")
    raw_ops = document.get("operations")
    if not isinstance(raw_ops, list):
        raise MutationError("mutation journal operations must be a list")

    seen: set[tuple[int, str]] = set()
    operations: list[dict] = []
    for index, raw in enumerate(raw_ops, 1):
        if not isinstance(raw, dict):
            raise MutationError(f"operation {index} must be an object")
        task_id = raw.get("source_task_id")
        if not isinstance(task_id, str) or task_id not in source_by_id:
            raise MutationError(f"operation {index} has unknown source_task_id {task_id!r}")
        source = source_by_id[task_id]
        if raw.get("source_row_sha256") != source["source_row_sha256"]:
            raise MutationError(f"operation {index} source row SHA-256 differs: {task_id}")
        if raw.get("scope") != "fault_topology":
            raise MutationError(f"operation {index} scope must be fault_topology")
        round_number = raw.get("round")
        if isinstance(round_number, bool) or not isinstance(round_number, int) or round_number < 1:
            raise MutationError(f"operation {index} round must be a positive integer")
        prescription = raw.get("prescription")
        if not isinstance(prescription, dict):
            raise MutationError(f"operation {index} prescription must be an object")
        inner = prescription.get("prescription")
        action = prescription.get("action") or (inner.get("action") if isinstance(inner, dict) else None)
        if action not in ACTION_NAMES:
            raise MutationError(f"operation {index} has unsupported prescription action {action!r}")
        evidence = raw.get("evidence", {})
        if not isinstance(evidence, dict):
            raise MutationError(f"operation {index} evidence must be an object")
        ft = source["blueprint"]["fault_topology_id"]
        key = (round_number, ft)
        if key in seen:
            raise MutationError(f"conflicting prescriptions for {ft} in round {round_number}")
        seen.add(key)
        operations.append({**raw, "_fault_topology_id": ft})
    operations.sort(key=lambda item: (item["round"], item["_fault_topology_id"]))
    return document, operations


def _compatible_phase2(entry: dict) -> list[str]:
    """Check 2–3 hazards/stressors while the base validator permits fewer."""
    hazards = entry.get("r_hazards")
    if not isinstance(hazards, list) or len(hazards) not in (2, 3):
        return [f"expected 2 or 3 future hazards, got {len(hazards) if isinstance(hazards, list) else 'invalid'}"]
    stressors = entry.get("t0_stressors")
    if not isinstance(stressors, list) or len(stressors) not in (1, 2, 3):
        return [f"expected 1 to 3 t0 stressors, got {len(stressors) if isinstance(stressors, list) else 'invalid'}"]
    check = copy.deepcopy(entry)
    check["r_hazards"] = hazards[:2]
    check["t0_stressors"] = stressors[:2]
    valid, errors = is_blueprint_compatible(check)
    result = list(errors)
    if len(hazards) == 3:
        third = hazards[2]
        hazard_id = third.get("hazard_id") if isinstance(third, dict) else None
        night = third.get("night") if isinstance(third, dict) else None
        spec = R_HAZARDS.get(hazard_id)
        if spec is None:
            result.append(f"unknown third hazard {hazard_id!r}")
        elif THEMES[entry["theme"]]["landing_format"] not in spec["supported_landing_formats"]:
            result.append(f"third hazard {hazard_id} incompatible with {entry['theme']}")
        if night not in (1, 2, 3, 4):
            result.append(f"third hazard night must be 1..4, got {night!r}")
        if hazard_id in {h.get("hazard_id") for h in hazards[:2] if isinstance(h, dict)}:
            result.append(f"duplicate future hazard {hazard_id}")
    if len(stressors) == 3:
        third_stressor = stressors[2]
        spec = T0_STRESSORS.get(third_stressor)
        if spec is None:
            result.append(f"unknown third t0 stressor {third_stressor!r}")
        else:
            theme_spec = THEMES[entry["theme"]]
            if theme_spec["pipeline_type"] not in spec["supported_pipeline_types"]:
                result.append(f"third t0 stressor {third_stressor} incompatible with pipeline on {entry['theme']}")
            if theme_spec["landing_format"] not in spec["supported_landing_formats"]:
                result.append(f"third t0 stressor {third_stressor} incompatible with landing format on {entry['theme']}")
        if third_stressor in stressors[:2]:
            result.append(f"duplicate t0 stressor {third_stressor}")
    codes = [h["hazard_id"].split("_", 1)[0] for h in hazards if isinstance(h, dict) and h.get("hazard_id")]
    if codes != entry.get("hazards"):
        result.append("hazards codes differ from r_hazards")
    schedule = entry.get("schedule")
    if not isinstance(schedule, dict) or schedule.get("hazard_nights") != [h.get("night") for h in hazards]:
        result.append("schedule hazard_nights differ from r_hazards")
    if not isinstance(stressors, list) or [s.split("_", 1)[0] for s in stressors] != entry.get("t0_stressor_codes"):
        result.append("t0_stressor_codes differ from t0_stressors")
    knobs = entry.get("difficulty_knobs")
    if not isinstance(knobs, dict) or knobs.get("r_hazard_count") != len(hazards):
        result.append("difficulty_knobs r_hazard_count differs from r_hazards")
    if not isinstance(knobs, dict) or knobs.get("t0_stressor_count") != len(stressors or []):
        result.append("difficulty_knobs t0_stressor_count differs from t0_stressors")
    if not valid and not result:
        result.append("blueprint compatibility failed")
    return result


def validate_derived_catalog(source: list[dict], derived: list[dict]) -> list[str]:
    """Validate matrix axes, identity, twin symmetry, and Phase 2 compatibility."""
    errors: list[str] = []
    if len(source) != 1080 or len(derived) != len(source):
        return [f"derived catalog has {len(derived)} rows; expected 1080 source-aligned rows"]
    for old, new in zip(source, derived):
        task_id = old["task_id"]
        for field in IMMUTABLE_FIELDS:
            if new.get(field) != old.get(field):
                errors.append(f"{task_id}: immutable axis {field} changed")
        errors.extend(f"{task_id}: {issue}" for issue in _compatible_phase2(new))
        if len(errors) > 50:
            return errors
    if len({row["task_id"] for row in derived}) != 1080:
        errors.append("derived catalog task IDs are not unique")
    if (Counter(row["theme"] for row in derived) != Counter(row["theme"] for row in source)
            or Counter(row["fault_topology_id"] for row in derived) != Counter(row["fault_topology_id"] for row in source)
            or Counter(row["governance_mode"] for row in derived) != Counter(row["governance_mode"] for row in source)):
        errors.append("derived matrix axes differ from source")
    twins: dict[str, list[dict]] = defaultdict(list)
    for row in derived:
        twins[row["twin_group_id"]].append(row)
    if len(twins) != 360:
        errors.append(f"derived catalog has {len(twins)} twin groups; expected 360")
    for twin_id, members in twins.items():
        if len(members) != 3 or {m["governance_mode"] for m in members} != set(GOVERNANCE_MODES):
            errors.append(f"{twin_id}: incomplete governance triplet")
        if len({tuple((h["hazard_id"], h["night"]) for h in m["r_hazards"]) for m in members}) != 1:
            errors.append(f"{twin_id}: governance twins disagree on future hazards")
        if len({tuple(m["t0_stressors"]) for m in members}) != 1:
            errors.append(f"{twin_id}: governance twins disagree on t0 stressors")
    return errors


def _mutate_row(row: dict, prescription: Mapping[str, Any], step: int) -> tuple[dict, list[str], dict]:
    proposal = mutate_blueprint_for_prescription(row, prescription, step=step)
    helper_entry = copy.deepcopy(proposal.get("mutation_history", [{}])[-1])
    changes = helper_entry.get("changes", [])
    if not changes:
        return copy.deepcopy(row), [], helper_entry
    proposal["task_id"] = row["task_id"]
    if "task" in row:
        proposal["task"] = row["task"]
    for key in HELPER_BOOKKEEPING:
        if key in row:
            proposal[key] = copy.deepcopy(row[key])
        else:
            proposal.pop(key, None)
    # Keep contract metadata aligned with any newly added stressor or hazard.
    contracts = list(proposal.get("contracts") or [])
    for item in proposal["t0_stressors"]:
        for contract in T0_STRESSORS[item]["contracts"]:
            if contract not in contracts:
                contracts.append(contract)
    for item in proposal["r_hazards"]:
        for contract in R_HAZARDS[item["hazard_id"]]["contracts"]:
            if contract not in contracts:
                contracts.append(contract)
    if any(contract not in VALID_CONTRACTS for contract in contracts):
        raise MutationError(f"{row['task_id']}: mutation introduced unknown contract")
    proposal["contracts"] = contracts
    changed_fields = sorted(key for key in set(row) | set(proposal) if row.get(key) != proposal.get(key))
    return proposal, changed_fields, helper_entry


def _write_once(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise MutationError(f"versioned artifact already exists with different bytes: {path}")
        return
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temp.write_bytes(data)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def derive_catalog(source_catalog: str | Path, journal_path: str | Path,
                   out_root: str | Path) -> dict:
    """Write candidate catalog, immutable lineage, and matching pilot selection.

    Returns artifact paths and hashes. Repeating the same source/journal is
    idempotent. Artifacts are always under a versioned directory outside REPO.
    """
    source_path = Path(source_catalog).resolve()
    journal_path = Path(journal_path).resolve()
    out = Path(out_root).resolve()
    if out.is_relative_to(REPO):
        raise MutationError("versioned output must be outside the repository")
    source_bytes = source_path.read_bytes()
    provenance, records = load_catalog(source_path)
    if source_path.read_bytes() != source_bytes or provenance["sha256"] != _sha(source_bytes):
        raise MutationError("source catalog changed during derivation")
    source = [record["blueprint"] for record in records]
    source_validation = validate_catalog(source)
    if not source_validation["valid"]:
        raise MutationError("source catalog invalid: " + "; ".join(source_validation["errors"][:5]))
    source_by_id = {record["blueprint"]["task_id"]: record for record in records}
    journal, operations = _read_journal(journal_path, provenance["sha256"], source_by_id)
    journal_sha = _sha(_canonical(journal))
    by_id = {row["task_id"]: copy.deepcopy(row) for row in source}
    applied: dict[str, list[int]] = defaultdict(list)
    applied_ops: list[dict] = []
    for sequence, operation in enumerate(operations, 1):
        ft = operation["_fault_topology_id"]
        affected = [row["task_id"] for row in source if row["fault_topology_id"] == ft]
        if len(affected) != 72:
            raise MutationError(f"{ft}: expected 72 source rows, found {len(affected)}")
        before_anchor = _sha(_canonical(by_id[operation["source_task_id"]]))
        expected_before = operation.get("expected_before_blueprint_sha256")
        if expected_before is not None and expected_before != before_anchor:
            raise MutationError(
                f"{ft} round {operation['round']}: anchor blueprint SHA-256 differs from journal precondition")
        changed_ids: list[str] = []
        anchor_helper: dict = {}
        for task_id in affected:
            updated, fields, helper_entry = _mutate_row(
                by_id[task_id], operation["prescription"], sequence)
            by_id[task_id] = updated
            if task_id == operation["source_task_id"]:
                anchor_helper = helper_entry
            if fields:
                applied[task_id].append(sequence)
                changed_ids.append(task_id)
        if not changed_ids:
            raise MutationError(f"{ft} round {operation['round']}: prescription changed no blueprints")
        applied_ops.append({
            "sequence": sequence, "round": operation["round"],
            "fault_topology_id": ft, "source_task_id": operation["source_task_id"],
            "source_row_sha256": operation["source_row_sha256"],
            "prescription": operation["prescription"],
            "evidence": operation.get("evidence", {}),
            "anchor_helper_mutation": anchor_helper,
            "before_anchor_blueprint_sha256": before_anchor,
            "after_anchor_blueprint_sha256": _sha(_canonical(by_id[operation["source_task_id"]])),
            "changed_task_count": len(changed_ids),
            "changed_task_ids": changed_ids,
        })

    derived = [by_id[row["task_id"]] for row in source]
    errors = validate_derived_catalog(source, derived)
    if errors:
        raise MutationError("derived catalog invalid: " + "; ".join(errors[:8]))
    original_lines = source_bytes.splitlines()
    catalog_lines = []
    crosswalk_rows = []
    final_changed_ids: set[str] = set()
    for record, row in zip(records, derived):
        task_id = row["task_id"]
        original = record["blueprint"]
        final_fields = sorted(key for key in set(original) | set(row)
                              if original.get(key) != row.get(key))
        if final_fields:
            final_changed_ids.add(task_id)
        line = (_canonical(row) if row != original
                else original_lines[record["source_line"] - 1])
        catalog_lines.append(line)
        crosswalk_rows.append({
            "source_task_id": task_id, "derived_task_id": task_id,
            "source_line": record["source_line"],
            "source_row_sha256": record["source_row_sha256"],
            "source_blueprint_sha256": _sha(_canonical(original)),
            "derived_row_sha256": _sha(line),
            "derived_blueprint_sha256": _sha(_canonical(row)),
            "changed_fields": final_fields,
            "applied_sequences": applied.get(task_id, []),
        })
    catalog_bytes = b"\n".join(catalog_lines) + b"\n"
    if source_path.read_bytes() != source_bytes:
        raise MutationError("source catalog changed during derivation")

    version = _sha((provenance["sha256"] + journal_sha).encode("ascii"))[:20]
    version_dir = out / f"v-{version}"
    version_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = version_dir / "catalog_1080.jsonl"
    selection_dir = version_dir / "pilot_51"
    _write_once(catalog_path, catalog_bytes)
    _write_once(version_dir / "mutation_journal.json",
                json.dumps(journal, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8") + b"\n")
    selection = prepare_selection(catalog_path, selection_dir)
    proof_ids = [entry["blueprint"]["task_id"] for entry in selection["selected"]]
    changed_proof = [task_id for task_id in proof_ids if task_id in final_changed_ids]
    crosswalk = {
        "schema_version": SCHEMA_VERSION,
        "source_catalog_path": str(source_path),
        "source_catalog_sha256": provenance["sha256"],
        "derived_catalog_sha256": _sha(catalog_bytes),
        "journal_sha256": journal_sha,
        "rows": crosswalk_rows,
    }
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "status": "candidate_unverified",
        "version": version,
        "source_catalog": str(source_path),
        "source_catalog_sha256": provenance["sha256"],
        "derived_catalog": str(catalog_path),
        "derived_catalog_sha256": _sha(catalog_bytes),
        "journal": str(version_dir / "mutation_journal.json"),
        "journal_sha256": journal_sha,
        "crosswalk": str(version_dir / "source_crosswalk.json"),
        "pilot_dir": str(selection_dir),
        "selection": str(selection_dir / "phase2_selection.json"),
        "selection_sha256": selection["selection_sha256"],
        "source_rows": len(source), "derived_rows": len(derived),
        "operations": applied_ops,
        "changed_task_count": len(final_changed_ids),
        "changed_proof_task_ids": changed_proof,
    }
    _write_once(version_dir / "source_crosswalk.json",
                json.dumps(crosswalk, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8") + b"\n")
    _write_once(version_dir / "mutation_manifest.json",
                json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False).encode("utf-8") + b"\n")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--out-root", type=Path, required=True)
    args = parser.parse_args(argv)
    report = derive_catalog(args.catalog, args.journal, args.out_root)
    print(json.dumps({key: report[key] for key in (
        "status", "version", "derived_catalog", "selection",
        "changed_task_count", "changed_proof_task_ids",
    )}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
