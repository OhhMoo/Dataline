"""Select and resume the 51-task Phase 2 stratified hardness pilot.

Selection is deterministic and available before Phase 2 implementation work
finishes. Materialization is a separate, readiness-gated action. Neither a
selection record nor a materialized task is labeled Stage-0 or calibration
ready by this module.

    python -m tasks.factory.compiler.phase2_pilot
    python -m tasks.factory.compiler.phase2_pilot --materialize --workers 3
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import shutil
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tasks.factory.compiler.blueprint_matrix import (
    FAULT_TOPOLOGIES, GOVERNANCE_MODES, SCHEDULE_SEEDS, THEMES,
)
from tasks.factory.paths import FACTORY_ROOT


REPO = Path(__file__).resolve().parents[3]
DEFAULT_CATALOG = REPO / "experiments/scale_1000/catalog_1080.jsonl"
DEFAULT_OUT = FACTORY_ROOT / "phase2_pilot_51"
DEFAULT_SCRATCH = FACTORY_ROOT / "phase2_pilot_51_scratch"
SELECTION_FILE = "phase2_selection.json"
PROGRESS_FILE = "phase2_materialization_report.json"
ATTESTATION_FILE = "tests/phase2_pilot_provenance.json"
PHASE3_DELTA_FILE = "phase3_delta_manifest.json"
PHASE3_DELTA_REPORT = "phase3_delta_materialization_report.json"
DEFAULT_CALIBRATION_SUMMARY = FACTORY_ROOT / "runs_phase2/calibration_summary.json"
PHASE3_STATUS_DIR = REPO / "experiments/scale_1000/status"
PHASE3_MUTATED_TOPOLOGIES = frozenset(FAULT_TOPOLOGIES) - {"FT03", "FT15"}


class SelectionError(ValueError):
    """The source catalog cannot yield an unambiguous 51-task selection."""


class Phase2NotReady(RuntimeError):
    """Upstream Phase 2 implementation is incomplete; no task was built."""


class Phase3NotReady(RuntimeError):
    """Phase 3 upstream changes have not all passed their completion gates."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True,
                                   ensure_ascii=False) + "\n", encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def load_catalog(catalog_path: Path = DEFAULT_CATALOG) -> tuple[dict, list[dict]]:
    """Read the frozen JSONL once and attach exact line/byte provenance."""
    path = Path(catalog_path).resolve()
    raw = path.read_bytes()
    records: list[dict] = []
    seen: set[str] = set()
    for line_number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            blueprint = json.loads(line)
        except (UnicodeDecodeError, ValueError) as exc:
            raise SelectionError(f"catalog line {line_number}: {exc}") from exc
        if not isinstance(blueprint, dict):
            raise SelectionError(f"catalog line {line_number} is not an object")
        task_id = blueprint.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise SelectionError(f"catalog line {line_number} has no task_id")
        if task_id in seen:
            raise SelectionError(f"duplicate source task_id: {task_id}")
        seen.add(task_id)
        records.append({"blueprint": blueprint, "source_line": line_number,
                        "source_row_sha256": _sha(line)})
    expected = len(THEMES) * len(FAULT_TOPOLOGIES) * len(GOVERNANCE_MODES) * len(SCHEDULE_SEEDS)
    if len(records) != expected:
        raise SelectionError(f"expected {expected} source rows, got {len(records)}")
    provenance = {"path": str(path), "sha256": _sha(raw),
                  "row_count": len(records)}
    return provenance, records


def select_pilot(records: list[dict]) -> list[dict]:
    """Choose 36 paired twins plus one additional row for each FT01..FT15.

    The 36 rows use one complete governance triplet per theme. Their topology
    rotates across FT01..FT12 and their schedule alternates. The additional
    15 rows cover every topology, distribute modes 5/5/5, and use each theme's
    opposite schedule so every selected task ID is unique.
    """
    themes = list(THEMES)
    modes = list(GOVERNANCE_MODES)
    topologies = list(FAULT_TOPOLOGIES)
    schedules = list(SCHEDULE_SEEDS)
    if (len(themes), len(modes), len(topologies), len(schedules)) != (12, 3, 15, 2):
        raise SelectionError("Section 7.2E requires exactly 12 themes, 3 modes, 15 topologies, 2 schedules")
    index: dict[tuple[str, str, str, str], dict] = {}
    for record in records:
        row = record["blueprint"]
        key = (row.get("theme"), row.get("fault_topology_id"),
               row.get("schedule_seed_id"), row.get("governance_mode"))
        if key in index:
            raise SelectionError(f"duplicate catalog axes: {key}")
        index[key] = record

    selected: list[dict] = []
    def add(key: tuple[str, str, str, str], role: str) -> None:
        if key not in index:
            raise SelectionError(f"catalog missing required cell: {key}")
        source = index[key]
        selected.append({**source, "selection_role": role})

    for theme_index, theme in enumerate(themes):
        topology = topologies[theme_index]
        schedule = schedules[theme_index % 2]
        for mode in modes:
            add((theme, topology, schedule, mode), "theme_governance")

    for ft_index, topology in enumerate(topologies):
        theme_index = (ft_index * 5 + 1) % len(themes)
        theme = themes[theme_index]
        schedule = schedules[(theme_index + 1) % 2]
        mode = modes[ft_index % len(modes)]
        add((theme, topology, schedule, mode), "topology")

    ids = [record["blueprint"]["task_id"] for record in selected]
    if len(selected) != 51 or len(set(ids)) != 51:
        raise SelectionError("selection must contain 51 distinct source task IDs")
    theme_modes = {(r["blueprint"]["theme"], r["blueprint"]["governance_mode"])
                   for r in selected if r["selection_role"] == "theme_governance"}
    topology_coverage = {r["blueprint"]["fault_topology_id"] for r in selected
                         if r["selection_role"] == "topology"}
    if len(theme_modes) != 36 or topology_coverage != set(topologies):
        raise SelectionError("selection does not cover all theme/mode pairs and topologies")
    return selected


def _coverage(selected: list[dict]) -> dict:
    rows = [record["blueprint"] for record in selected]
    roles = Counter(record["selection_role"] for record in selected)
    return {
        "task_count": len(rows),
        "roles": dict(sorted(roles.items())),
        "theme_counts": dict(sorted(Counter(r["theme"] for r in rows).items())),
        "governance_counts": dict(sorted(Counter(r["governance_mode"] for r in rows).items())),
        "topology_counts": dict(sorted(Counter(r["fault_topology_id"] for r in rows).items())),
        "theme_governance_pairs": len({(r["theme"], r["governance_mode"]) for r in rows}),
    }


def prepare_selection(catalog_path: Path = DEFAULT_CATALOG,
                      out_root: Path = DEFAULT_OUT, *, replace: bool = False) -> dict:
    """Write or validate immutable selection metadata; build no tasks."""
    catalog, records = load_catalog(catalog_path)
    selected = select_pilot(records)
    digest = _sha(_canonical([(r["blueprint"]["task_id"], r["source_row_sha256"],
                               r["selection_role"]) for r in selected]))
    payload = {"schema_version": 1, "purpose": "phase2_stratified_hardness_pilot",
               "phase2_status": "selection_only", "stage0_verified": False,
               "calibration_certified": False, "catalog": catalog,
               "selection_sha256": digest, "coverage": _coverage(selected),
               "selected": selected}
    out = Path(out_root).resolve()
    if out.is_relative_to(REPO):
        raise ValueError("pilot output must be outside the repository")
    destination = out / SELECTION_FILE
    if destination.is_file() and not replace:
        previous = json.loads(destination.read_text(encoding="utf-8"))
        if previous != payload:
            raise SelectionError("existing Phase 2 selection has different source provenance; "
                                 "use --replace-selection to regenerate")
        return previous
    _atomic_json(destination, payload)
    return payload


def _valid_existing(task: Path, record: dict, catalog_sha: str) -> bool:
    from tasks.factory.compiler.phase2_readiness import (phase2_fingerprint,
                                                         phase2_task_integrity)
    attestation = task / ATTESTATION_FILE
    seal = task / "tests/phase2_provenance.json"
    report = task / "tests/materializer_report.json"
    blueprint = task / "fixtures/blueprint.json"
    if not (attestation.is_file() and seal.is_file() and report.is_file()
            and blueprint.is_file()):
        return False
    try:
        proof = json.loads(attestation.read_text(encoding="utf-8"))
        materialized = json.loads(report.read_text(encoding="utf-8"))
        staged = json.loads(blueprint.read_text(encoding="utf-8"))
        package_seal = json.loads(seal.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    source = record["blueprint"]
    return all((
        proof.get("task_id") == source["task_id"],
        proof.get("catalog_sha256") == catalog_sha,
        proof.get("source_line") == record["source_line"],
        proof.get("source_row_sha256") == record["source_row_sha256"],
        proof.get("selection_role") == record["selection_role"],
        proof.get("phase2_materializer_invoked") is True,
        proof.get("materializer_report_sha256") == _sha(report.read_bytes()),
        materialized.get("composed") is True,
        staged.get("task_id") == source["task_id"],
        staged.get("theme") == source["theme"],
        staged.get("governance_mode") == source["governance_mode"],
        staged.get("fault_topology_id") == source["fault_topology_id"],
        package_seal.get("task_id") == source["task_id"],
        package_seal.get("fingerprint") == phase2_fingerprint(source),
        package_seal.get("task_integrity") == phase2_task_integrity(task),
    ))


def refresh_task_provenance_seal(task_dir: Path, record: dict, catalog_sha: str) -> None:
    """Refresh Phase 2 provenance and pilot attestation seals in place."""
    from tasks.factory.compiler.phase2_readiness import (phase2_fingerprint,
                                                         phase2_task_integrity)
    task = Path(task_dir).resolve()
    blueprint = record["blueprint"] if isinstance(record.get("blueprint"), dict) else record
    task_id = blueprint["task_id"]
    seal_path = task / "tests/phase2_provenance.json"
    existing_seal: dict[str, Any] = {}
    if seal_path.is_file():
        try:
            loaded = json.loads(seal_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing_seal = loaded
        except (OSError, ValueError):
            existing_seal = {}
    seal = {
        **existing_seal,
        "task_id": task_id,
        "theme": blueprint.get("theme", existing_seal.get("theme", "")),
        "fingerprint": phase2_fingerprint(blueprint),
        "task_integrity": phase2_task_integrity(task),
    }
    _atomic_json(seal_path, seal)

    attestation_path = task / ATTESTATION_FILE
    existing_proof: dict[str, Any] = {}
    if attestation_path.is_file():
        try:
            loaded_proof = json.loads(attestation_path.read_text(encoding="utf-8"))
            if isinstance(loaded_proof, dict):
                existing_proof = loaded_proof
        except (OSError, ValueError):
            existing_proof = {}
    report_path = task / "tests/materializer_report.json"
    report_sha = (_sha(report_path.read_bytes()) if report_path.is_file()
                  else existing_proof.get("materializer_report_sha256", ""))
    source_line = record.get("source_line", existing_proof.get("source_line", 0))
    source_row_sha = record.get(
        "source_row_sha256",
        existing_proof.get("source_row_sha256", _sha(_canonical(blueprint))),
    )
    selection_role = record.get("selection_role", existing_proof.get("selection_role", "scale"))
    proof = {
        **existing_proof,
        "schema_version": existing_proof.get("schema_version", 1),
        "task_id": task_id,
        "catalog_sha256": catalog_sha,
        "source_line": source_line,
        "source_row_sha256": source_row_sha,
        "selection_role": selection_role,
        "materializer_report_sha256": report_sha,
        "phase2_materializer_invoked": True,
        "stage0_verified": bool(existing_proof.get("stage0_verified", False)),
        "calibration_certified": bool(existing_proof.get("calibration_certified", False)),
    }
    _atomic_json(attestation_path, proof)


def _load_certified_task_ids(summary_path: Path | None) -> set[str]:
    if summary_path is None:
        return set()
    path = Path(summary_path).resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SelectionError(f"unable to load calibration summary {path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("certified"), list):
        raise SelectionError(f"calibration summary {path} missing 'certified' list")
    certified_ids: set[str] = set()
    for entry in payload["certified"]:
        if isinstance(entry, str) and entry.strip():
            certified_ids.add(entry.split(":", 1)[0].strip())
    return certified_ids


def _normalize_topologies(
    only_topologies: set[str] | list[str] | tuple[str, ...] | str | None,
) -> set[str] | None:
    if only_topologies is None:
        return None
    if isinstance(only_topologies, str):
        tokens = [only_topologies]
    else:
        tokens = [str(item) for item in only_topologies]
    parsed = {
        code.strip().upper()
        for token in tokens
        for code in token.split(",")
        if code.strip()
    }
    unknown = parsed - set(FAULT_TOPOLOGIES)
    if unknown or not parsed:
        raise SelectionError(f"invalid fault topology selection: {sorted(unknown)}")
    return parsed


def _materialize_one(record: dict, out: Path, scratch: Path,
                     catalog_sha: str,
                     materialize_fn: Callable | None = None) -> dict:
    row = record["blueprint"]
    task_id = row["task_id"]
    try:
        if materialize_fn is None:
            from tasks.factory.compiler.peer_materializer import materialize as _peer_materialize
            task = Path(_peer_materialize(row, out, scratch=scratch,
                                          overwrite=True)).resolve()
        else:
            task = Path(materialize_fn(row, out, scratch=scratch,
                                       overwrite=True, phase2=True)).resolve()
        expected = (out / task_id).resolve()
        if task != expected:
            raise ValueError(f"materializer returned {task}, expected {expected}")
        report = task / "tests/materializer_report.json"
        blueprint = task / "fixtures/blueprint.json"
        if not (report.is_file() and blueprint.is_file()):
            raise ValueError("materializer omitted report or compiled blueprint")
        detail = json.loads(report.read_text(encoding="utf-8"))
        staged = json.loads(blueprint.read_text(encoding="utf-8"))
        if detail.get("composed") is not True:
            raise ValueError("materializer did not compose an executable oracle")
        if staged.get("task_id") != task_id:
            raise ValueError("compiled blueprint task_id mismatch")
        from tasks.factory.compiler.phase2_readiness import (phase2_fingerprint,
                                                             phase2_task_integrity)
        seal = json.loads((task / "tests/phase2_provenance.json").read_text(encoding="utf-8"))
        if (seal.get("task_id") != task_id
                or seal.get("fingerprint") != phase2_fingerprint(row)
                or seal.get("task_integrity") != phase2_task_integrity(task)):
            raise ValueError("materialized Phase 2 package has invalid source/integrity seal")
        proof = {"schema_version": 1, "task_id": task_id,
                 "catalog_sha256": catalog_sha,
                 "source_line": record["source_line"],
                 "source_row_sha256": record["source_row_sha256"],
                 "selection_role": record["selection_role"],
                 "materializer_report_sha256": _sha(report.read_bytes()),
                 "phase2_materializer_invoked": True,
                 "stage0_verified": False,
                 "calibration_certified": False}
        _atomic_json(task / ATTESTATION_FILE, proof)
        return {"task_id": task_id, "status": "materialized_unverified",
                "path": str(task), "selection_role": record["selection_role"]}
    except (Exception, SystemExit) as exc:
        return {"task_id": task_id, "status": "failed",
                "selection_role": record["selection_role"],
                "error": f"{type(exc).__name__}: {exc}"[-1600:]}


def _progress(out: Path, selection: dict, statuses: dict[str, dict],
              readiness: dict, *, state: str) -> dict:
    ordered = []
    for record in selection["selected"]:
        task_id = record["blueprint"]["task_id"]
        ordered.append(statuses.get(task_id, {"task_id": task_id,
                                              "status": "selected_unmaterialized",
                                              "selection_role": record["selection_role"]}))
    counts = dict(sorted(Counter(item["status"] for item in ordered).items()))
    report = {"schema_version": 1,
              "updated_utc": datetime.now(timezone.utc).isoformat(),
              "state": state, "phase2_ready": False,
              "stage0_verified": False, "calibration_certified": False,
              "catalog_sha256": selection["catalog"]["sha256"],
              "selection_sha256": selection["selection_sha256"],
              "readiness": readiness, "counts": counts, "tasks": ordered}
    _atomic_json(out / PROGRESS_FILE, report)
    return report


def run_materialization(selection: dict, out_root: Path = DEFAULT_OUT, *,
                        scratch: Path = DEFAULT_SCRATCH, workers: int = 1,
                        limit: int | None = None, overwrite: bool = False,
                        skip_certified_summary: Path | None = None,
                        only_topologies: set[str] | None = None,
                        materialize_fn: Callable | None = None,
                        readiness_fn: Callable | None = None) -> dict:
    """Resume selected builds only when the Phase 2 preflight is fully ready."""
    if workers < 1 or (limit is not None and limit < 0):
        raise ValueError("workers must be positive and limit nonnegative")
    if materialize_fn is not None and workers != 1:
        raise ValueError("injected materialize_fn requires workers=1")
    out = Path(out_root).resolve()
    out.mkdir(parents=True, exist_ok=True)
    stored_path = out / SELECTION_FILE
    if not stored_path.is_file() or json.loads(stored_path.read_text(encoding="utf-8")) != selection:
        raise SelectionError("materialization requires the matching on-disk selection manifest")
    source_path = Path(selection["catalog"]["path"])
    if not source_path.is_file() and DEFAULT_CATALOG.is_file():
        source_path = DEFAULT_CATALOG
    if not source_path.is_file() or _sha(source_path.read_bytes()) != selection["catalog"]["sha256"]:
        raise SelectionError("source catalog changed after pilot selection")
    if prepare_selection(source_path, out) != selection:
        raise SelectionError("pilot selection differs from the source catalog")
    scratch = Path(scratch).resolve()
    if scratch.is_relative_to(REPO):
        raise ValueError("materialization scratch must be outside the repository")
    if scratch.is_relative_to(out):
        raise ValueError("materialization scratch must not be inside the pilot task directory")
    if readiness_fn is None:
        from tasks.factory.compiler.phase2_readiness import inspect_phase2_readiness
        readiness_fn = inspect_phase2_readiness
    topologies = _normalize_topologies(only_topologies)
    certified_ids = _load_certified_task_ids(skip_certified_summary)
    catalog_sha = selection["catalog"]["sha256"]
    statuses: dict[str, dict] = {}
    pending = []
    for record in selection["selected"]:
        task_id = record["blueprint"]["task_id"]
        topology_id = record["blueprint"]["fault_topology_id"]
        task = out / task_id
        has_existing = task.is_dir() and (
            (task / "tests/phase2_provenance.json").is_file()
            or (task / "fixtures/blueprint.json").is_file()
        )
        is_certified = task_id in certified_ids
        forced_by_topology = topologies is not None and topology_id in topologies
        excluded_by_topology = topologies is not None and topology_id not in topologies
        if is_certified and has_existing and not forced_by_topology:
            refresh_task_provenance_seal(task, record, catalog_sha)
            statuses[task_id] = {"task_id": task_id, "status": "preserved_certified",
                                 "path": str(task),
                                 "selection_role": record["selection_role"]}
        elif excluded_by_topology and has_existing:
            refresh_task_provenance_seal(task, record, catalog_sha)
            statuses[task_id] = {"task_id": task_id, "status": "existing_unverified",
                                 "path": str(task),
                                 "selection_role": record["selection_role"]}
        elif not overwrite and not forced_by_topology and _valid_existing(task, record, catalog_sha):
            statuses[task_id] = {"task_id": task_id, "status": "existing_unverified",
                                 "path": str(task),
                                 "selection_role": record["selection_role"]}
        else:
            pending.append(record)
    readiness = readiness_fn(repo=REPO)
    if not readiness.get("ready"):
        _progress(out, selection, statuses, readiness, state="blocked_upstream")
        raise Phase2NotReady("Phase 2 upstream blockers: " +
                             "; ".join(readiness.get("blockers", ["unknown blocker"])))
    if limit is not None:
        pending = pending[:limit]
    _progress(out, selection, statuses, readiness, state="materializing")

    if workers == 1:
        for record in pending:
            result = _materialize_one(record, out, scratch, catalog_sha,
                                      materialize_fn=materialize_fn)
            statuses[result["task_id"]] = result
            _progress(out, selection, statuses, readiness, state="materializing")
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_materialize_one, record, out, scratch,
                                   catalog_sha) for record in pending]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                statuses[result["task_id"]] = result
                _progress(out, selection, statuses, readiness, state="materializing")
    failures = sum(item["status"] == "failed" for item in statuses.values())
    state = "materialized_unverified" if len(statuses) == 51 and not failures else (
        "failed" if failures else "partial_unverified")
    return _progress(out, selection, statuses, readiness, state=state)


def _phase3_marker_gate(status_dir: Path) -> dict[str, str]:
    """Require all four upstream Phase 3 owners before replacing pilot tasks."""
    markers: dict[str, str] = {}
    for agent in ("codex", "opus", "kimi", "grok"):
        path = Path(status_dir) / f"DONE_{agent}_phase3.json"
        try:
            raw = path.read_bytes()
            payload = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise Phase3NotReady(f"{agent} Phase 3 marker unavailable: {exc}") from exc
        if not isinstance(payload, dict) or str(payload.get("status", "")).lower() not in {
                "complete", "completed", "passed", "success", "done", "ready"}:
            raise Phase3NotReady(f"{agent} Phase 3 marker has no completed status: {path}")
        markers[agent] = _sha(raw)
    return markers


def _phase3_certified(summary_path: Path, out: Path,
                      selected_ids: set[str],
                      audit_live_run_fn: Callable | None = None) -> tuple[dict, set[str], dict]:
    """Read the historical K=5 certifications before any delta build."""
    from tasks.factory.calibrate_queue import classify_stage2_arm
    if audit_live_run_fn is None:
        from tasks.factory.compiler.phase2_hardness import audit_live_run
        audit_live_run_fn = audit_live_run
    path = Path(summary_path).resolve()
    try:
        raw = path.read_bytes()
        summary = json.loads(raw)
        certified_list = summary["certified"]
        stage2 = summary["stage2"]["arms"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SelectionError(f"valid calibration summary required for delta mode: {exc}") from exc
    if not isinstance(certified_list, list) or not isinstance(stage2, dict):
        raise SelectionError("calibration summary has no K=5 certification records")
    certified = set(certified_list)
    if len(certified) != len(certified_list) or not certified <= selected_ids:
        raise SelectionError("certified IDs must be unique members of the 51-task selection")
    if Path(summary.get("tasks_dir", "")).resolve() != out:
        raise SelectionError("calibration summary does not refer to this pilot directory")
    runs: dict[str, dict] = {}
    for task_id in sorted(certified):
        arm = stage2.get(task_id)
        if (not isinstance(arm, dict) or arm.get("certified") is not True
                or int(arm.get("k") or 0) < 5 or not arm.get("run_id")
                or not Path(str(arm.get("run_dir") or "")).is_dir()):
            raise SelectionError(f"{task_id} has no completed certified K=5 arm")
        run_dir = Path(arm["run_dir"]).resolve()
        audit = audit_live_run_fn(run_dir, task_id, 5)
        metrics = audit.get("metrics") if isinstance(audit, dict) else None
        if not isinstance(audit, dict) or audit.get("deficits") or not isinstance(metrics, dict):
            reasons = [item.get("code", "invalid") for item in (audit or {}).get("deficits", [])]
            raise SelectionError(f"{task_id} K=5 live evidence failed audit: {reasons}")
        classified = classify_stage2_arm(metrics)
        if (classified.get("certified") is not True
                or classified.get("band") != "core"
                or int(classified.get("scored") or 0) != 5
                or int(classified.get("quarantined_count") or 0) != 0
                or float(classified.get("scalar_variance") or 0) <= 0
                or audit.get("run_id") != arm["run_id"]
                or classified.get("passed") != arm.get("passed")
                or classified.get("pass_at_1") != arm.get("pass_at_1")):
            raise SelectionError(f"{task_id} summary certification differs from live K=5 evidence")
        evidence_files = [run_dir / "report.json"] + [
            run_dir / f"r{i}/rollout.json" for i in range(5)]
        try:
            evidence = [(str(file.relative_to(run_dir)), _sha(file.read_bytes()))
                        for file in evidence_files]
        except OSError as exc:
            raise SelectionError(f"{task_id} K=5 evidence is incomplete: {exc}") from exc
        runs[task_id] = {
            "stage2_run_id": arm["run_id"],
            "stage2_run_dir": str(run_dir),
            "stage2_arm_sha256": _sha(_canonical(arm)),
            "stage2_live_evidence_sha256": _sha(_canonical(evidence)),
        }
    source = {"path": str(path), "sha256": _sha(raw),
              "certified_task_ids": sorted(certified)}
    return source, certified, runs


def _phase3_current_blueprints(selection: dict) -> dict[str, dict]:
    """Bind frozen selected IDs to the newly generated topology definitions."""
    from tasks.factory.compiler.blueprint_matrix import generate_catalog

    current = {row["task_id"]: row for row in generate_catalog()}
    if len(current) != 1080:
        raise SelectionError("current blueprint matrix must contain 1,080 unique IDs")
    selected: dict[str, dict] = {}
    for record in selection["selected"]:
        source = record["blueprint"]
        task_id = source["task_id"]
        updated = current.get(task_id)
        if updated is None or any(updated.get(key) != source.get(key) for key in (
                "task_id", "theme", "fault_topology_id", "schedule_seed_id",
                "governance_mode", "seed", "twin_group_id")):
            raise SelectionError(f"Phase 3 matrix changed immutable selection axes: {task_id}")
        selected[task_id] = updated
    return selected


def _phase3_package_lineage(task: Path, record: dict, compiled: dict,
                            catalog_sha: str,
                            *, require_current: bool = False) -> dict:
    """Validate package bytes against its own seal and frozen catalog ancestry.

    A historical package deliberately retains its original source fingerprint.
    That hash cannot be recomputed from current Phase 3 sources.
    """
    from tasks.factory.compiler.phase2_readiness import (phase2_fingerprint,
                                                         phase2_task_integrity)

    source = record["blueprint"]
    task_id = source["task_id"]
    try:
        attestation_path = task / ATTESTATION_FILE
        seal_path = task / "tests/phase2_provenance.json"
        report_path = task / "tests/materializer_report.json"
        blueprint_path = task / "fixtures/blueprint.json"
        proof_raw, seal_raw, report_raw, blueprint_raw = (
            path.read_bytes() for path in
            (attestation_path, seal_path, report_path, blueprint_path))
        proof, seal, report, staged = (
            json.loads(body) for body in
            (proof_raw, seal_raw, report_raw, blueprint_raw))
    except (OSError, ValueError, TypeError) as exc:
        raise SelectionError(f"{task_id} has no valid package provenance: {exc}") from exc
    if not all((
            proof.get("task_id") == task_id,
            proof.get("catalog_sha256") == catalog_sha,
            proof.get("source_line") == record["source_line"],
            proof.get("source_row_sha256") == record["source_row_sha256"],
            proof.get("selection_role") == record["selection_role"],
            proof.get("phase2_materializer_invoked") is True,
            proof.get("materializer_report_sha256") == _sha(report_raw),
            seal.get("task_id") == task_id,
            seal.get("theme") == source["theme"],
            isinstance(seal.get("fingerprint"), str),
            seal.get("task_integrity") == phase2_task_integrity(task),
            report.get("composed") is True,
            staged.get("task_id") == task_id,
            staged.get("theme") == source["theme"],
            staged.get("fault_topology_id") == source["fault_topology_id"],
            staged.get("governance_mode") == source["governance_mode"],
            staged.get("t0_stressors") == compiled["t0_stressor_codes"],
            staged.get("hazards") == compiled["hazards"],
    )):
        raise SelectionError(f"{task_id} package provenance or integrity mismatch")
    current_sha = _sha(_canonical(compiled))
    if require_current and (proof.get("phase3_blueprint_sha256") != current_sha
                            or seal["fingerprint"] != phase2_fingerprint(compiled)):
        raise SelectionError(f"{task_id} is not bound to the current Phase 3 blueprint")
    return {
        "source_line": record["source_line"],
        "source_row_sha256": record["source_row_sha256"],
        "frozen_blueprint_sha256": _sha(_canonical(source)),
        "current_blueprint_sha256": current_sha,
        "compiled_blueprint_sha256": _sha(blueprint_raw),
        "package_fingerprint": seal["fingerprint"],
        "package_integrity": seal["task_integrity"],
        "package_seal_sha256": _sha(seal_raw),
        "pilot_attestation_sha256": _sha(proof_raw),
    }


def _phase3_adopt_current(task: Path, record: dict, compiled: dict,
                          catalog_sha: str) -> dict | None:
    """Bind an already rebuilt package to the frozen pilot selection.

    A concurrent plain materialization may have used the revised catalog. Its
    package can be retained if its sealed bytes and source fingerprint match
    the Phase 3 blueprint. Only the selection attestation is rewritten; that
    file is excluded from the package integrity seal.
    """
    from tasks.factory.compiler.phase2_readiness import (phase2_fingerprint,
                                                         phase2_task_integrity)

    proof_path = task / ATTESTATION_FILE
    try:
        old_proof = proof_path.read_bytes()
        prior = json.loads(old_proof)
        seal = json.loads((task / "tests/phase2_provenance.json").read_text())
        report_path = task / "tests/materializer_report.json"
        report = json.loads(report_path.read_text())
        staged = json.loads((task / "fixtures/blueprint.json").read_text())
    except (OSError, ValueError, TypeError):
        return None
    if (not isinstance(prior, dict) or prior.get("phase2_materializer_invoked") is not True
            or report.get("composed") is not True
            or seal.get("fingerprint") != phase2_fingerprint(compiled)
            or seal.get("task_integrity") != phase2_task_integrity(task)
            or staged.get("task_id") != compiled["task_id"]
            or staged.get("t0_stressors") != compiled["t0_stressor_codes"]
            or staged.get("hazards") != compiled["hazards"]):
        return None
    proof = {"schema_version": 2, "task_id": compiled["task_id"],
             "catalog_sha256": catalog_sha,
             "source_line": record["source_line"],
             "source_row_sha256": record["source_row_sha256"],
             "selection_role": record["selection_role"],
             "phase3_blueprint_sha256": _sha(_canonical(compiled)),
             "materializer_report_sha256": _sha(report_path.read_bytes()),
             "phase2_materializer_invoked": True,
             "stage0_verified": False,
             "calibration_certified": False}
    _atomic_json(proof_path, proof)
    try:
        return _phase3_package_lineage(task, record, compiled, catalog_sha,
                                       require_current=True)
    except SelectionError:
        restore = proof_path.with_name(proof_path.name + f".{uuid.uuid4().hex}.tmp")
        restore.write_bytes(old_proof)
        restore.replace(proof_path)
        return None


def _phase3_stage_and_swap(record: dict, compiled: dict, out: Path,
                           scratch: Path, catalog_sha: str,
                           materialize_fn: Callable | None) -> dict:
    """Build away from the pilot, validate, then replace one uncertified task."""
    from tasks.factory.compiler.phase2_readiness import (phase2_fingerprint,
                                                         phase2_task_integrity)

    task_id = record["blueprint"]["task_id"]
    token = uuid.uuid4().hex
    staging_root = out.parent / f".{out.name}.phase3-stage-{task_id}-{token}"
    backup = out.parent / f".{out.name}.phase3-backup-{task_id}-{token}"
    final = out / task_id
    moved_old = False
    moved_new = False
    try:
        staging_root.mkdir()
        if materialize_fn is None:
            from tasks.factory.compiler.peer_materializer import materialize
            staged = Path(materialize(compiled, staging_root, scratch=scratch,
                                      overwrite=True)).resolve()
        else:
            staged = Path(materialize_fn(compiled, staging_root, scratch=scratch,
                                         overwrite=True, phase2=True)).resolve()
        if staged != (staging_root / task_id).resolve():
            raise SelectionError(f"{task_id} materializer returned an unexpected task path")
        report_path = staged / "tests/materializer_report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["task_dir"] = str(final)
        _atomic_json(report_path, report)
        seal_path = staged / "tests/phase2_provenance.json"
        seal = json.loads(seal_path.read_text(encoding="utf-8"))
        if seal.get("fingerprint") != phase2_fingerprint(compiled):
            raise SelectionError(f"{task_id} materializer used a different source fingerprint")
        seal["task_integrity"] = phase2_task_integrity(staged)
        _atomic_json(seal_path, seal)
        proof = {
            "schema_version": 2, "task_id": task_id,
            "catalog_sha256": catalog_sha,
            "source_line": record["source_line"],
            "source_row_sha256": record["source_row_sha256"],
            "selection_role": record["selection_role"],
            "phase3_blueprint_sha256": _sha(_canonical(compiled)),
            "materializer_report_sha256": _sha(report_path.read_bytes()),
            "phase2_materializer_invoked": True,
            "stage0_verified": False,
            "calibration_certified": False,
        }
        _atomic_json(staged / ATTESTATION_FILE, proof)
        _phase3_package_lineage(staged, record, compiled, catalog_sha,
                                require_current=True)
        if final.exists():
            final.replace(backup)
            moved_old = True
        staged.replace(final)
        moved_new = True
        lineage = _phase3_package_lineage(final, record, compiled, catalog_sha,
                                          require_current=True)
        if moved_old:
            shutil.rmtree(backup)
            moved_old = False
        return {"task_id": task_id, "status": "rematerialized", **lineage}
    except (Exception, SystemExit) as exc:
        if moved_new and final.exists():
            shutil.rmtree(final)
        if moved_old and backup.exists():
            backup.replace(final)
        return {"task_id": task_id, "status": "failed_restored",
                "error": f"{type(exc).__name__}: {exc}"[-1600:]}
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def run_delta_materialization(selection: dict, out_root: Path = DEFAULT_OUT, *,
                              scratch: Path = DEFAULT_SCRATCH,
                              workers: int = 1, limit: int | None = None,
                              only_topologies: set[str] | None = None,
                              skip_certified: bool = True,
                              calibration_summary: Path = DEFAULT_CALIBRATION_SUMMARY,
                              status_dir: Path = PHASE3_STATUS_DIR,
                              materialize_fn: Callable | None = None,
                              readiness_fn: Callable | None = None,
                              audit_live_run_fn: Callable | None = None) -> dict:
    """Rebuild Phase 3 changed pilot cells while retaining certified K=5 cells."""
    if not skip_certified:
        raise SelectionError("Phase 3 delta mode must preserve certified tasks")
    if workers < 1 or (limit is not None and limit < 0):
        raise ValueError("workers must be positive and limit nonnegative")
    if materialize_fn is not None and workers != 1:
        raise ValueError("injected materialize_fn requires workers=1")
    out = Path(out_root).resolve()
    scratch = Path(scratch).resolve()
    if out.is_relative_to(REPO) or scratch.is_relative_to(REPO) or scratch.is_relative_to(out):
        raise ValueError("pilot and scratch must be separate paths outside the repository")
    stored_path = out / SELECTION_FILE
    if not stored_path.is_file() or json.loads(stored_path.read_text(encoding="utf-8")) != selection:
        raise SelectionError("delta mode requires the unchanged on-disk pilot selection")
    source_path = Path(selection["catalog"]["path"])
    if not source_path.is_file() and DEFAULT_CATALOG.is_file():
        source_path = DEFAULT_CATALOG
    if not source_path.is_file() or _sha(source_path.read_bytes()) != selection["catalog"]["sha256"]:
        raise SelectionError("frozen catalog changed after pilot selection")
    topologies = set(PHASE3_MUTATED_TOPOLOGIES if only_topologies is None
                     else only_topologies)
    unknown = topologies - set(FAULT_TOPOLOGIES)
    if unknown or not topologies:
        raise SelectionError(f"invalid Phase 3 topology selection: {sorted(unknown)}")
    selected = {r["blueprint"]["task_id"]: r for r in selection["selected"]}
    current = _phase3_current_blueprints(selection)
    summary_source, certified, stage2_runs = _phase3_certified(
        calibration_summary, out, set(selected), audit_live_run_fn)
    markers = _phase3_marker_gate(status_dir)
    if readiness_fn is None:
        from tasks.factory.compiler.phase2_readiness import inspect_phase2_readiness
        readiness_fn = inspect_phase2_readiness
    readiness = readiness_fn(repo=REPO)
    if not readiness.get("ready"):
        raise Phase3NotReady("Phase 2 preflight still blocked: " +
                             "; ".join(readiness.get("blockers", [])))

    entries: dict[str, dict] = {}
    pending: list[dict] = []
    for record in selection["selected"]:
        source = record["blueprint"]
        task_id = source["task_id"]
        task = out / task_id
        topology = source["fault_topology_id"]
        active = topology in topologies and task_id not in certified
        # Every retained package must still match its historical byte seal.
        try:
            lineage = _phase3_package_lineage(task, record, source,
                                              selection["catalog"]["sha256"])
        except SelectionError:
            try:
                lineage = _phase3_package_lineage(
                    task, record, current[task_id],
                    selection["catalog"]["sha256"],
                    require_current=False if task_id in certified else True)
            except SelectionError:
                if task_id in certified or not active:
                    raise
                lineage = {}
        entry = {"task_id": task_id, "topology": topology, **lineage,
                 "current_blueprint_sha256": _sha(_canonical(current[task_id]))}
        if task_id in certified:
            entry.update({"status": "preserved_certified",
                          "runtime_compatibility": "unverified",
                          "cached_run_reuse": "requires_runtime_equivalence",
                          "full1080_treatment": "copy_historical_sealed_package",
                          "mixed_version_catalog_exception": (
                              _sha(_canonical(source)) !=
                              _sha(_canonical(current[task_id]))),
                          **stage2_runs[task_id]})
        elif not active:
            entry["status"] = "preserved_unselected"
        else:
            try:
                _phase3_package_lineage(task, record, current[task_id],
                                        selection["catalog"]["sha256"],
                                        require_current=True)
            except SelectionError:
                adopted = _phase3_adopt_current(
                    task, record, current[task_id], selection["catalog"]["sha256"])
                if adopted is None:
                    entry["status"] = "pending_rebuild"
                    pending.append(record)
                else:
                    entry.update({"status": "existing_current", **adopted})
            else:
                entry["status"] = "existing_current"
        entries[task_id] = entry

    if limit is not None:
        pending = pending[:limit]
    manifest = {
        "schema_version": 1,
        "purpose": "phase3_delta_pilot_lineage",
        "source_catalog_sha256": selection["catalog"]["sha256"],
        "selection_sha256": selection["selection_sha256"],
        "calibration_summary": summary_source,
        "phase3_marker_sha256": markers,
        "requested_topologies": sorted(topologies),
        "preserved_certified_task_ids": sorted(certified),
        "mixed_version_catalog_exceptions": sorted(
            task_id for task_id in certified
            if entries[task_id]["mixed_version_catalog_exception"]),
        "tasks": entries,
    }

    def save() -> dict:
        counts = dict(sorted(Counter(e["status"] for e in entries.values()).items()))
        unfinished = counts.get("pending_rebuild", 0)
        failures = counts.get("failed_restored", 0) + counts.get("stale_after_source_change", 0)
        state = "failed" if failures else ("partial" if unfinished else "complete")
        manifest["state"] = state
        manifest["counts"] = counts
        manifest["updated_utc"] = datetime.now(timezone.utc).isoformat()
        _atomic_json(out / PHASE3_DELTA_FILE, manifest)
        report = {"schema_version": 1, "state": state,
                  "selection_sha256": selection["selection_sha256"],
                  "requested_topologies": sorted(topologies),
                  "certified_preserved": len(certified),
                  "counts": counts,
                  "failed": [e for e in entries.values()
                             if e["status"] == "failed_restored"]}
        _atomic_json(out / PHASE3_DELTA_REPORT, report)
        return report

    report = save()
    if workers == 1:
        for record in pending:
            result = _phase3_stage_and_swap(record, current[record["blueprint"]["task_id"]],
                                            out, scratch, selection["catalog"]["sha256"],
                                            materialize_fn)
            entries[result["task_id"]].update(result)
            report = save()
    else:
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_phase3_stage_and_swap, record,
                                   current[record["blueprint"]["task_id"]], out,
                                   scratch, selection["catalog"]["sha256"], None)
                       for record in pending]
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                entries[result["task_id"]].update(result)
                report = save()
    if _sha(Path(calibration_summary).read_bytes()) != summary_source["sha256"]:
        raise SelectionError("historical calibration summary changed during delta build")
    for task_id in certified:
        try:
            _phase3_package_lineage(out / task_id, selected[task_id],
                                    selected[task_id]["blueprint"],
                                    selection["catalog"]["sha256"])
        except SelectionError:
            _phase3_package_lineage(out / task_id, selected[task_id],
                                    current[task_id],
                                    selection["catalog"]["sha256"],
                                    require_current=False)
    for record in selection["selected"]:
        task_id = record["blueprint"]["task_id"]
        if entries[task_id]["status"] not in {"rematerialized", "existing_current"}:
            continue
        try:
            lineage = _phase3_package_lineage(
                out / task_id, record, current[task_id],
                selection["catalog"]["sha256"], require_current=True)
        except SelectionError as exc:
            entries[task_id].update({"status": "stale_after_source_change",
                                     "error": str(exc)})
        else:
            entries[task_id].update(lineage)
    return save()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--scratch", type=Path, default=DEFAULT_SCRATCH)
    parser.add_argument("--replace-selection", action="store_true")
    parser.add_argument("--materialize", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--delta-only", action="store_true",
                        help="rebuild changed Phase 3 pilot cells while preserving certified tasks")
    parser.add_argument("--only-topologies", nargs="+", metavar="FT##",
                        help="restrict pilot or delta builds to named fault topologies (e.g. FT01,FT02)")
    parser.add_argument("--skip-certified", nargs="?", const=DEFAULT_CALIBRATION_SUMMARY,
                        type=Path, metavar="SUMMARY_JSON",
                        help="retain certified task packages from calibration_summary.json")
    parser.add_argument("--calibration-summary", type=Path,
                        default=DEFAULT_CALIBRATION_SUMMARY)
    args = parser.parse_args(argv)
    try:
        requested = _normalize_topologies(args.only_topologies) if args.only_topologies else None
        if args.delta_only:
            if args.replace_selection or args.overwrite:
                raise SelectionError("delta mode preserves selection and manages replacement itself")
            selection_path = Path(args.out) / SELECTION_FILE
            if not selection_path.is_file():
                raise SelectionError(f"existing pilot selection required: {selection_path}")
            selection = json.loads(selection_path.read_text(encoding="utf-8"))
            summary_path = (args.skip_certified
                            if isinstance(args.skip_certified, Path)
                            and args.skip_certified != DEFAULT_CALIBRATION_SUMMARY
                            else args.calibration_summary)
            report = run_delta_materialization(
                selection, args.out, scratch=args.scratch, workers=args.workers,
                limit=args.limit, only_topologies=requested, skip_certified=True,
                calibration_summary=summary_path)
            print(json.dumps({"report": str(Path(args.out).resolve() / PHASE3_DELTA_REPORT),
                              "manifest": str(Path(args.out).resolve() / PHASE3_DELTA_FILE),
                              "state": report["state"], "counts": report["counts"]}))
            return 0 if report["state"] == "complete" else 1
        selection = prepare_selection(args.catalog, args.out,
                                      replace=args.replace_selection)
        if not args.materialize:
            if args.only_topologies or args.skip_certified:
                raise SelectionError("--only-topologies and --skip-certified require --materialize or --delta-only")
            print(json.dumps({"selection": str(Path(args.out).resolve() / SELECTION_FILE),
                              "count": selection["coverage"]["task_count"],
                              "phase2_status": "selection_only"}))
            return 0
        progress = run_materialization(selection, args.out, scratch=args.scratch,
                                       workers=args.workers, limit=args.limit,
                                       overwrite=args.overwrite,
                                       skip_certified_summary=args.skip_certified,
                                       only_topologies=requested)
        print(json.dumps({"report": str(Path(args.out).resolve() / PROGRESS_FILE),
                          "state": progress["state"], "counts": progress["counts"]}))
        return 1 if progress["state"] == "failed" else 0
    except (SelectionError, Phase2NotReady, Phase3NotReady, ValueError) as exc:
        print(f"phase2_pilot: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
