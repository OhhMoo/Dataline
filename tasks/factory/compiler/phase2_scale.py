"""Materialize 1,080 Phase 2 tasks only after the 51-task proof is certified."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import multiprocessing
import os
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from tasks.factory.calibrate_queue import assert_outside_git_repo
from tasks.factory.compiler.blueprint_matrix import validate_catalog
from tasks.factory.compiler.phase2_hardness import evaluate_hardness
from tasks.factory.compiler.phase2_mutation import _compatible_phase2, validate_derived_catalog
from tasks.factory.compiler.phase2_readiness import (
    inspect_phase2_readiness,
    phase2_fingerprint,
    phase2_task_integrity,
)

REPO = Path(__file__).resolve().parents[3]
DEFAULT_CATALOG = REPO / "experiments/scale_1000/catalog_1080.jsonl"
DEFAULT_PILOT = Path("/home/ubuntu/alkera_factory_1017/phase2_pilot_51")
DEFAULT_OUT = Path("/home/ubuntu/alkera_factory_1017/tasks_1080")
DEFAULT_SCRATCH = Path("/home/ubuntu/alkera_factory_1017/materialize_phase2_1080")
FINAL_REPORT = REPO / "experiments/scale_1000/implementation_report.md"
DONE_MARKER = REPO / "experiments/scale_1000/status/DONE_gemini_phase2.json"
INTEGRATION_DIAGNOSTIC = Path("/home/ubuntu/alkera_factory_1017/phase2_diagnostic.json")
CAMPAIGN_CANARY = Path("/home/ubuntu/alkera_factory_1017/phase2_campaign/campaign_report.json")
PHASE3_PILOT_STAGE0 = Path("/home/ubuntu/alkera_factory_1017/phase3_stage0_51.json")
PHASE3_SCALE_REPORT = Path("/home/ubuntu/alkera_factory_1017/phase3_scale_report.json")
PHASE3_STAGE0_1080 = Path("/home/ubuntu/alkera_factory_1017/phase3_stage0_1080.json")
PHASE3_CATALOG = Path("/home/ubuntu/alkera_factory_1017/phase3_catalog_1080.jsonl")
PHASE3_CANDIDATE_CATALOG = Path("/home/ubuntu/alkera_factory_1017/phase3_catalog_candidate_1080.jsonl")
PHASE3_DELTA_MANIFEST = DEFAULT_PILOT / "phase3_delta_manifest.json"
PHASE3_RUNTIME_AUDIT = DEFAULT_PILOT / "phase3_runtime_compatibility.json"
PHASE3_CALIBRATION_SUMMARY = Path("/home/ubuntu/alkera_factory_1017/runs_phase2/calibration_summary.json")
PHASE3_STALE_CACHE = PHASE3_CALIBRATION_SUMMARY.with_name("phase3_stale_certification.json")
PHASE3_RUNTIME_DIAGNOSTIC = PHASE3_CALIBRATION_SUMMARY.with_name(
    "phase3_runtime_diagnostic_post_marker.json")
PHASE3_OPUS_MARKER = REPO / "experiments/scale_1000/status/DONE_opus_phase3.json"
PHASE3_REPORT = REPO / "experiments/scale_1000/implementation_report.md"
PHASE3_DONE = REPO / "experiments/scale_1000/status/DONE_gemini_phase3.json"
PHASE3_UPSTREAM = ("codex", "opus", "kimi", "grok")
PHASE3_MUTATED_TOPOLOGIES = frozenset({
    "FT01", "FT02", "FT04", "FT05", "FT06", "FT07", "FT08", "FT09",
    "FT10", "FT11", "FT12", "FT13", "FT14",
})


def _current_integration_diagnostic() -> dict | None:
    """Show the last isolated composer failure only while its sources match."""
    if not INTEGRATION_DIAGNOSTIC.is_file():
        return None
    sources = [REPO / relative for relative in
               ("tasks/factory/stressors/oracle_composer.py",
                "tasks/factory/stressors/t0_pipeline_stressors.py",
                "tasks/factory/stressors/domain_schema_adapter.py")]
    if any(path.is_file() and path.stat().st_mtime > INTEGRATION_DIAGNOSTIC.stat().st_mtime
           for path in sources):
        return None
    try:
        document = json.loads(INTEGRATION_DIAGNOSTIC.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) and document.get("status") == "diagnostic_failure" else None


def _current_campaign_canary() -> dict | None:
    if not CAMPAIGN_CANARY.is_file():
        return None
    try:
        report = json.loads(CAMPAIGN_CANARY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) and report.get("status") == "integration_canary_failed" else None


def _hardness_deficits(proof: dict) -> list[dict]:
    """Include per-task live failures; the proof's top-level list covers axes only."""
    deficits = list(proof.get("deficits") or [])
    for task in proof.get("tasks") or []:
        for item in task.get("deficits") or []:
            deficits.append({"task_id": task.get("task_id"), **item})
    return deficits


def _validate_phase2_catalog(entries: list[dict]) -> dict:
    """Keep the original 1080-axis audit while admitting Phase 2's third fault."""
    result = validate_catalog(entries)
    phase2_count_exceptions = set()
    extra_errors = []
    for row in entries:
        task_id = row.get("task_id")
        stressors = row.get("t0_stressors") or []
        hazards = row.get("r_hazards") or []
        if len(stressors) == 3:
            phase2_count_exceptions.add(
                f"{task_id}: Expected 1 or 2 t0_stressors, got 3")
        if len(hazards) == 3:
            phase2_count_exceptions.add(
                f"{task_id}: Expected exactly 2 r_hazards, got 3")
        codes = [str(item.get("hazard_id", "")).split("_", 1)[0]
                 for item in hazards if isinstance(item, dict)]
        if row.get("hazards") != codes:
            extra_errors.append(f"{task_id}: hazards codes differ from r_hazards")
        s_codes = [str(item).split("_", 1)[0] for item in stressors]
        if row.get("t0_stressor_codes") != s_codes:
            extra_errors.append(f"{task_id}: t0_stressor_codes differ from t0_stressors")
        extra_errors.extend(f"{task_id}: {issue}" for issue in _compatible_phase2(row))
    result["errors"] = [error for error in result["errors"]
                        if error not in phase2_count_exceptions] + extra_errors
    result["valid"] = not result["errors"]
    return result


def _read_catalog(path: Path) -> tuple[list[dict], dict]:
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
               if line.strip()]
    report = _validate_phase2_catalog(entries)
    if Path(path).resolve() != DEFAULT_CATALOG.resolve():
        source = [json.loads(line) for line in DEFAULT_CATALOG.read_text(encoding="utf-8").splitlines()
                  if line.strip()]
        report["errors"].extend(validate_derived_catalog(source, entries))
        report["valid"] = not report["errors"]
    return entries, report


def _pilot_rows(path: Path, catalog: Path, entries: list[dict]) -> tuple[list[dict], list[str]]:
    deficits: list[str] = []
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        selected = document["selected"]
        if not isinstance(selected, list):
            raise ValueError("selected is not a list")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [], [f"pilot selection unreadable: {exc}"]
    expected_hash = hashlib.sha256(catalog.read_bytes()).hexdigest()
    if document.get("catalog", {}).get("sha256") != expected_hash:
        deficits.append("pilot selection catalog hash differs from the current catalog")
    by_id = {entry["task_id"]: entry for entry in entries}
    rows: list[dict] = []
    for item in selected:
        bp = item.get("blueprint") if isinstance(item, dict) else None
        if not isinstance(bp, dict) or bp.get("task_id") not in by_id:
            deficits.append(f"pilot selection contains unknown blueprint: {item!r}"[:300])
            continue
        if bp != by_id[bp["task_id"]]:
            deficits.append(f"pilot blueprint differs from catalog: {bp['task_id']}")
        rows.append(bp)
    if len(rows) != 51 or len({row["task_id"] for row in rows}) != 51:
        deficits.append(f"pilot selection has {len(rows)} rows and "
                        f"{len({row['task_id'] for row in rows})} distinct IDs, expected 51")
    return rows, deficits


def _pilot_package_provenance(pilot_dir: Path, rows: list[dict]) -> dict[str, dict] | None:
    provenance: dict[str, dict] = {}
    for row in rows:
        path = pilot_dir / row["task_id"] / "tests/phase2_provenance.json"
        try:
            seal = json.loads(path.read_text(encoding="utf-8"))
            provenance[row["task_id"]] = {
                "fingerprint": seal["fingerprint"],
                "task_integrity": seal["task_integrity"],
            }
        except (OSError, ValueError, KeyError, TypeError):
            return None
    return provenance


def _pilot_stage0(path: Path, rows: list[dict],
                  pilot_dir: Path = DEFAULT_PILOT) -> list[str]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"51-task Phase 2 Stage 0 report unavailable: {exc}"]
    expected = {row["task_id"] for row in rows}
    actual = {row.get("task") for row in report.get("results", [])}
    deficits = []
    if report.get("phase2") is not True:
        deficits.append("51-task Stage 0 report was not run with --phase2")
    if report.get("strict_phase2") is not True:
        deficits.append("51-task Stage 0 report was not run with --strict-phase2")
    if not report.get("ok") or report.get("total") != 51 or report.get("passed") != 51:
        deficits.append(f"51-task Stage 0 has {report.get('passed')}/{report.get('total')} PASS")
    if actual != expected:
        deficits.append(f"51-task Stage 0 task IDs differ: "
                        f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}")
    current = _pilot_package_provenance(pilot_dir, rows)
    if current is None or report.get("phase2_package_provenance") != current:
        deficits.append("51-task Stage 0 is not bound to current Phase 2 pilot packages")
    return deficits


def _provenance_matches(task: Path, blueprint: dict) -> bool:
    path = task / "tests/phase2_provenance.json"
    if not path.is_file():
        return False
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        return (record.get("task_id") == blueprint["task_id"]
                and record.get("theme") == blueprint["theme"]
                and record.get("fingerprint") == phase2_fingerprint(blueprint, REPO)
                and record.get("task_integrity") == phase2_task_integrity(task))
    except (OSError, ValueError, KeyError):
        return False


def _run_provenance_failures(path: Path | None, pilot_dir: Path,
                             rows: list[dict]) -> list[str]:
    if path is None or not path.is_file():
        return [row["task_id"] for row in rows]
    try:
        runs = json.loads(path.read_text(encoding="utf-8"))["runs"]
        if not isinstance(runs, dict):
            raise ValueError("runs is not a mapping")
    except (OSError, ValueError, KeyError, TypeError):
        return [row["task_id"] for row in rows]
    failures = []
    for row in rows:
        task_id = row["task_id"]
        provenance = pilot_dir / task_id / "tests/phase2_provenance.json"
        try:
            seal = json.loads(provenance.read_text(encoding="utf-8"))
            entry = runs[task_id]
            if (entry.get("fingerprint") != seal["fingerprint"] or
                    entry.get("task_integrity") != seal["task_integrity"]):
                failures.append(task_id)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            failures.append(task_id)
    return failures


def _materialize_one(blueprint: dict, out_dir: str, scratch: str) -> dict:
    from tasks.factory.compiler.peer_materializer import materialize
    try:
        task = materialize(blueprint, Path(out_dir), scratch=Path(scratch),
                           overwrite=True)
        return {"task_id": blueprint["task_id"], "status": "materialized", "path": str(task)}
    except Exception as exc:
        return {"task_id": blueprint["task_id"], "status": "failed",
                "error": f"{type(exc).__name__}: {exc}"}


def _write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _persist_scale_report(path: Path, report: dict) -> None:
    _write(path, report)
    lines = ["# Phase 2 hardness and scale verification", "",
             f"Status: **{report['status']}**", "",
             f"Catalog: {report['catalog_rows']} validated blueprints; "
             f"pilot selection: {report['pilot_selected']} tasks.", "",
             f"Hardness certified: **{report['hardness_certified']}**. "
             f"Full materialization: {report['materialized']} built, "
             f"{report['reused']} reused, {report['failed']} failed.", "",
             f"51-task Stage 0: `{report['pilot_stage0']}`. "
             f"1,080-task Stage 0: `{report['stage0_1080']}`.", "",
             "## Evidence", "",
             f"- Pilot selection: `{report['selection']}`",
             f"- Catalog SHA-256: `{report['catalog_sha256']}`",
             f"- Hardness proof: `{report['hardness_proof']}`",
             f"- Detailed scale report: `{path}`",
             f"- Scale output: `{report['out_dir']}`", ""]
    if report["deficits"]:
        lines += ["## Blocking findings", ""]
        lines += [f"- {item}" for item in report["deficits"]]
        lines.append("")
    if report["hardness_deficits"]:
        lines += ["## Live hardness evidence", "",
                  f"{len(report['hardness_deficits'])} live-proof deficits remain. "
                  "The detailed per-task list is in the JSON scale report.", ""]
    diagnostic = report.get("integration_diagnostic")
    if diagnostic:
        lines += ["## Latest isolated integration diagnostic", "",
                  f"- Task: `{diagnostic.get('task_id')}`",
                  f"- Failure: {diagnostic.get('failure')}",
                  f"- Evidence: `{INTEGRATION_DIAGNOSTIC}`", ""]
    canary = report.get("integration_canary")
    if canary:
        lines += ["## Current pilot canary", "",
                  *(f"- {item}" for item in canary.get("deficits", [])),
                  f"- Evidence: `{CAMPAIGN_CANARY}`", ""]
        lines += ["## Required integration repair", "",
                  "The composer must execute its three modules on each built theme's "
                  "native raw, fact, and publication tables and columns. Its "
                  "`execution_tables` and `future_tables` must name the same "
                  "physical relations declared in `spec.tables`. A fallback to "
                  "`raw.factory_events` or `warehouse.factory_handoff` cannot "
                  "certify the Phase 2 pilot. Re-run the one-task canary after "
                  "updating the upstream composer and completion marker.", ""]
    FINAL_REPORT.parent.mkdir(parents=True, exist_ok=True)
    FINAL_REPORT.write_text("\n".join(lines), encoding="utf-8")
    if report["status"] == "complete":
        _write(DONE_MARKER, {"agent": "gemini", "status": "complete",
                             "catalog_blueprints": report["catalog_rows"],
                             "catalog": report["catalog"],
                             "catalog_sha256": report["catalog_sha256"],
                             "pilot_tasks": report["pilot_selected"],
                             "selection": report["selection"],
                             "pilot_stage0": report["pilot_stage0"],
                             "run_index": report["run_index"],
                             "hardness_proof": report["hardness_proof"],
                             "hardness_certified": True,
                             "materialized_1080": report["materialized"] + report["reused"],
                             "stage0_1080": report["stage0_1080"],
                             "report": str(FINAL_REPORT)})
    elif DONE_MARKER.is_file():
        DONE_MARKER.unlink()


def run_scale(*, catalog: Path = DEFAULT_CATALOG,
              selection: Path = DEFAULT_PILOT / "phase2_selection.json",
              pilot_stage0: Path = Path("/home/ubuntu/alkera_factory_1017/phase2_stage0_51.json"),
              run_index: Path | None = DEFAULT_PILOT / "phase2_run_index.json",
              out_dir: Path = DEFAULT_OUT, scratch: Path = DEFAULT_SCRATCH,
              report_path: Path = Path("/home/ubuntu/alkera_factory_1017/phase2_scale_report.json"),
              materialize_workers: int = 4, verify_workers: int = 16,
              preflight_only: bool = False,
              only_topologies: set[str] | None = None,
              refresh_preserved_seals: bool = False) -> dict:
    """Run the complete scale gate; blocked preflight leaves ``out_dir`` untouched."""
    if materialize_workers < 1 or verify_workers < 1:
        raise ValueError("worker counts must be positive")
    from tasks.factory.compiler.phase2_pilot import (
        _normalize_topologies,
        refresh_task_provenance_seal,
    )
    topologies = _normalize_topologies(only_topologies)
    safe_out = assert_outside_git_repo(out_dir, repo_root=REPO)
    safe_scratch = assert_outside_git_repo(scratch, repo_root=REPO)
    readiness = inspect_phase2_readiness(REPO)
    entries, catalog_report = _read_catalog(Path(catalog))
    rows, selection_deficits = _pilot_rows(Path(selection), Path(catalog), entries)
    stage0_deficits = _pilot_stage0(Path(pilot_stage0), rows, Path(selection).parent)
    hardness = evaluate_hardness(selection, run_index)
    hardness_proof_path = Path(report_path).with_name("phase2_hardness_proof.json")
    _write(hardness_proof_path, hardness)
    pilot_integrity = [row["task_id"] for row in rows
                       if not _provenance_matches(Path(selection).parent / row["task_id"], row)]
    run_provenance_failures = _run_provenance_failures(
        run_index, Path(selection).parent, rows)
    deficits = [*readiness["blockers"], *selection_deficits, *stage0_deficits]
    if not catalog_report["valid"]:
        deficits.extend(catalog_report["errors"])
    if pilot_integrity:
        deficits.append(f"pilot source/package provenance invalid for {len(pilot_integrity)} tasks")
    if run_provenance_failures:
        deficits.append(f"live runs are not bound to current pilot packages for "
                        f"{len(run_provenance_failures)} tasks")
    hardness_deficits = _hardness_deficits(hardness)
    if not hardness["certified"]:
        deficits.append(f"hardness proof uncertified: {len(hardness_deficits)} deficits")
    report = {
        "updated_utc": datetime.now(timezone.utc).isoformat(),
        "status": "blocked_preflight" if deficits else "ready_to_scale",
        "catalog": str(catalog),
        "catalog_sha256": hashlib.sha256(Path(catalog).read_bytes()).hexdigest(),
        "catalog_valid": catalog_report["valid"],
        "catalog_rows": len(entries),
        "selection": str(selection),
        "pilot_selected": len(rows),
        "pilot_stage0": str(pilot_stage0),
        "run_index": str(run_index) if run_index else None,
        "pilot_integrity_failures": pilot_integrity,
        "run_provenance_failures": run_provenance_failures,
        "hardness_certified": hardness["certified"],
        "hardness_proof": str(hardness_proof_path),
        "hardness_deficits": hardness_deficits,
        "hardness_deficit_counts": hardness.get("deficit_counts", {}),
        "hardness_certified_tasks": hardness.get("certified_tasks", 0),
        "readiness": readiness,
        "integration_diagnostic": _current_integration_diagnostic(),
        "integration_canary": _current_campaign_canary(),
        "deficits": deficits,
        "out_dir": str(safe_out),
        "only_topologies": sorted(topologies) if topologies is not None else None,
        "refresh_preserved_seals": refresh_preserved_seals,
        "materialized": 0,
        "reused": 0,
        "refreshed_seals": 0,
        "failed": 0,
        "stage0_1080": None,
    }
    _persist_scale_report(report_path, report)
    if deficits or preflight_only:
        return report

    safe_out.mkdir(parents=True, exist_ok=True)
    safe_scratch.mkdir(parents=True, exist_ok=True)
    expected_ids = {entry["task_id"] for entry in entries}
    extra = sorted(path.name for path in safe_out.iterdir()
                   if path.is_dir() and path.name not in expected_ids)
    if extra:
        report["status"] = "blocked_unexpected_tasks"
        report["deficits"].append(f"unexpected task directories in scale output: {extra}")
        _persist_scale_report(report_path, report)
        return report

    pending = []
    for bp in entries:
        task_dir = safe_out / bp["task_id"]
        in_selected_topologies = (topologies is None
                                  or bp["fault_topology_id"] in topologies)
        if not in_selected_topologies:
            if refresh_preserved_seals and task_dir.is_dir():
                refresh_task_provenance_seal(task_dir, bp, report["catalog_sha256"])
                report["refreshed_seals"] += 1
                report["reused"] += 1
            elif _provenance_matches(task_dir, bp) or task_dir.is_dir():
                report["reused"] += 1
            else:
                pending.append(bp)
        elif _provenance_matches(task_dir, bp):
            report["reused"] += 1
        elif refresh_preserved_seals and topologies is None and task_dir.is_dir():
            refresh_task_provenance_seal(task_dir, bp, report["catalog_sha256"])
            report["refreshed_seals"] += 1
            report["reused"] += 1
        else:
            pending.append(bp)
    _persist_scale_report(report_path, report)
    context = multiprocessing.get_context("spawn")
    with concurrent.futures.ProcessPoolExecutor(max_workers=materialize_workers,
                                                mp_context=context) as pool:
        futures = [pool.submit(_materialize_one, bp, str(safe_out), str(safe_scratch))
                   for bp in pending]
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            item = future.result()
            if item["status"] == "materialized":
                report["materialized"] += 1
            else:
                report["failed"] += 1
                report.setdefault("materialization_errors", []).append(item)
            if index % 10 == 0 or item["status"] == "failed":
                _persist_scale_report(report_path, report)
    if report["failed"] or report["materialized"] + report["reused"] != 1080:
        report["status"] = "materialization_incomplete"
        _persist_scale_report(report_path, report)
        return report

    from tasks.factory.verify_all import verify_all
    verified = verify_all(safe_out, workers=verify_workers,
                          phase2=True, strict_phase2=True)
    stage0_path = safe_out.parent / "phase2_stage0_1080.json"
    _write(stage0_path, verified)
    report["stage0_1080"] = str(stage0_path)
    report["status"] = ("complete" if verified.get("ok") and
                        verified.get("total") == 1080 and verified.get("passed") == 1080
                        else "stage0_failed")
    _persist_scale_report(report_path, report)
    return report


def _phase3_source_rows(task: Path, blueprint: dict) -> bool:
    """Compare the authored blueprint fields in a compiled package."""
    try:
        packaged = json.loads((task / "fixtures/blueprint.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    for key, expected in blueprint.items():
        actual = packaged.get(key)
        if key == "t0_stressors":
            expected = [str(item).split("_", 1)[0] for item in expected]
        if actual != expected:
            return False
    return True


def _phase3_package_valid(task: Path, blueprint: dict, *, current_source: bool) -> bool:
    """Validate package bytes; permit historical source hashes only explicitly."""
    try:
        seal = json.loads((task / "tests/phase2_provenance.json").read_text(encoding="utf-8"))
        if (seal.get("task_id") != blueprint["task_id"] or
                seal.get("theme") != blueprint["theme"] or
                not isinstance(seal.get("fingerprint"), str) or
                seal.get("task_integrity") != phase2_task_integrity(task) or
                not _phase3_source_rows(task, blueprint)):
            return False
        return not current_source or seal["fingerprint"] == phase2_fingerprint(blueprint, REPO)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _phase3_upstream_markers() -> tuple[dict, list[str], float]:
    markers, deficits, latest = {}, [], 0.0
    for agent in PHASE3_UPSTREAM:
        path = REPO / f"experiments/scale_1000/status/DONE_{agent}_phase3.json"
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
            state = str(body.get("status", "")).lower()
            if state not in {"complete", "completed", "passed", "success", "done", "ready"}:
                raise ValueError(f"status {state or 'missing'}")
            markers[agent] = {"path": str(path), "status": state}
            latest = max(latest, path.stat().st_mtime)
        except (OSError, ValueError, TypeError) as exc:
            deficits.append(f"Phase 3 upstream marker {agent} unavailable: {exc}")
    return markers, deficits, latest


def _phase3_read_document(path: Path) -> tuple[dict, str | None]:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(body, dict):
            raise ValueError("expected a JSON object")
        return body, None
    except (OSError, ValueError, TypeError) as exc:
        return {}, f"{path}: {exc}"


def _phase3_stale_cache_evidence() -> dict:
    """Surface the pre-live cache finding only while its inputs still match."""
    diagnostic, error = _phase3_read_document(PHASE3_STALE_CACHE)
    if error or not PHASE3_CALIBRATION_SUMMARY.is_file():
        return {}
    opus, opus_error = _phase3_read_document(PHASE3_OPUS_MARKER)
    if opus_error:
        return {}
    regrade_value = (opus.get("offline_regrade") or {}).get("summary")
    if not isinstance(regrade_value, str) or not regrade_value:
        return {}
    regrade_path = Path(regrade_value)
    if not regrade_path.is_absolute():
        regrade_path = REPO / regrade_path
    if not regrade_path.is_file():
        return {}
    if (diagnostic.get("status") != "blocked_stale_certification"
            or diagnostic.get("source_summary_sha256") != hashlib.sha256(
                PHASE3_CALIBRATION_SUMMARY.read_bytes()).hexdigest()
            or diagnostic.get("offline_regrade_sha256") != hashlib.sha256(
                regrade_path.read_bytes()).hexdigest()):
        return {}
    ids = diagnostic.get("stale_certified_task_ids")
    if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
        return {}
    return {"path": str(PHASE3_STALE_CACHE),
            "sha256": hashlib.sha256(PHASE3_STALE_CACHE.read_bytes()).hexdigest(),
            "stale_task_ids": sorted(ids),
            "runtime_audit_performed": diagnostic.get("runtime_audit_performed"),
            "live_queues_started": diagnostic.get("live_queues_started")}


def _phase3_runtime_diagnostic_evidence() -> dict:
    diagnostic, error = _phase3_read_document(PHASE3_RUNTIME_DIAGNOSTIC)
    marker = REPO / "experiments/scale_1000/status/DONE_codex_phase3.json"
    if error or not marker.is_file() or not PHASE3_CALIBRATION_SUMMARY.is_file():
        return {}
    if (diagnostic.get("purpose") != "read_only_post_marker_runtime_diagnostic"
            or diagnostic.get("source_stable") is not True
            or diagnostic.get("codex_marker_sha256") != hashlib.sha256(marker.read_bytes()).hexdigest()
            or diagnostic.get("source_summary_sha256") != hashlib.sha256(
                PHASE3_CALIBRATION_SUMMARY.read_bytes()).hexdigest()):
        return {}
    compatible = diagnostic.get("compatible_task_ids")
    incompatible = diagnostic.get("incompatible_task_ids")
    if not isinstance(compatible, list) or not isinstance(incompatible, list):
        return {}
    return {"path": str(PHASE3_RUNTIME_DIAGNOSTIC),
            "sha256": hashlib.sha256(PHASE3_RUNTIME_DIAGNOSTIC.read_bytes()).hexdigest(),
            "compatible_task_ids": compatible,
            "incompatible_task_ids": incompatible,
            "live_queues_started": diagnostic.get("live_queues_started")}


def _phase3_regrade_invalidations(proof: dict, regrade: dict) -> list[dict]:
    """Find reused live arms whose corrected offline pass counts violate hardness."""
    proof_tasks = {row.get("task_id"): row for row in proof.get("tasks", [])
                   if isinstance(row, dict)}
    invalid = []
    for row in regrade.get("tasks", []):
        if not isinstance(row, dict) or not isinstance(row.get("runs"), list):
            continue
        task_id = row.get("task")
        task_proof = proof_tasks.get(task_id) or {}
        for arm in row["runs"]:
            if not isinstance(arm, dict):
                continue
            old, new = arm.get("old"), arm.get("new")
            if not isinstance(old, dict) or not isinstance(new, dict):
                continue
            stage = {3: "k3", 5: "k5"}.get(new.get("rollouts"))
            if not stage or (task_proof.get(stage) or {}).get("run_id") != arm.get("run_id"):
                continue
            changed = any(old.get(key) != new.get(key) for key in
                          ("passed", "scored", "quarantined"))
            if not changed:
                continue
            scored, passed = new.get("scored"), new.get("passed")
            if not isinstance(scored, int) or not isinstance(passed, int) or scored <= 0:
                reason = "corrected scored/pass counts are invalid"
            elif new.get("quarantined"):
                reason = "corrected arm has quarantined rollouts"
            elif stage == "k3" and (scored != 3 or passed == 3):
                reason = f"corrected K3 screen is {passed}/{scored}"
            elif stage == "k5" and (scored != 5 or not 0.15 <= passed / scored <= 0.80):
                reason = f"corrected K5 pass rate is {passed}/{scored}"
            else:
                continue
            invalid.append({"task_id": task_id, "stage": stage,
                            "run_id": arm.get("run_id"),
                            "old": {"passed": old.get("passed"), "scored": old.get("scored")},
                            "new": {"passed": passed, "scored": scored}, "reason": reason})
    return invalid


def _phase3_opus_regrade(proof: dict) -> tuple[dict, list[str]]:
    marker, marker_error = _phase3_read_document(PHASE3_OPUS_MARKER)
    if marker_error:
        return {}, [f"Opus Phase 3 offline regrade marker unavailable: {marker_error}"]
    path_value = (marker.get("offline_regrade") or {}).get("summary")
    if not isinstance(path_value, str) or not path_value:
        return {}, ["Opus Phase 3 marker omits offline regrade report"]
    path = Path(path_value)
    if not path.is_absolute():
        path = REPO / path
    regrade, regrade_error = _phase3_read_document(path)
    if regrade_error:
        return {}, [f"Opus Phase 3 offline regrade report unavailable: {regrade_error}"]
    marker_counts = marker.get("offline_regrade") or {}
    tasks = regrade.get("tasks")
    if (not isinstance(tasks, list) or len(tasks) != 51 or
            len({row.get("task") for row in tasks if isinstance(row, dict)}) != 51 or
            regrade.get("errors") or regrade.get("skipped") != 0 or
            regrade.get("rescored") != 208 or regrade.get("rollout_dirs") != 208 or
            regrade.get("rescored") != marker_counts.get("rescored") or
            regrade.get("rollout_dirs") != marker_counts.get("rollout_dirs")):
        return {}, ["Opus Phase 3 offline regrade report is incomplete or disagrees with marker"]
    invalid = _phase3_regrade_invalidations(proof, regrade)
    evidence = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "rescored": regrade["rescored"], "invalidated_reused_arms": invalid}
    deficits = [f"corrected offline regrade invalidates reused {item['stage'].upper()} "
                f"for {item['task_id']} ({item['run_id']}): "
                f"{item['old']['passed']}/{item['old']['scored']} -> "
                f"{item['new']['passed']}/{item['new']['scored']}"
                for item in invalid]
    return evidence, deficits


def _phase3_copy_certified(pilot_task: Path, full_task: Path, expected_integrity: str) -> None:
    """Promote the exact live-certified package with a recoverable directory swap."""
    if phase2_task_integrity(pilot_task) != expected_integrity:
        raise ValueError(f"pilot package changed while copying: {pilot_task.name}")
    temp = full_task.parent / f".phase3-copy-{full_task.name}"
    backup = full_task.parent / f".phase3-backup-{full_task.name}"
    if temp.exists() or backup.exists():
        raise FileExistsError(f"unfinished Phase 3 package swap for {full_task.name}")
    shutil.copytree(pilot_task, temp, symlinks=True)
    try:
        if phase2_task_integrity(temp) != expected_integrity:
            raise ValueError(f"certified copy integrity mismatch: {full_task.name}")
        if full_task.exists():
            os.replace(full_task, backup)
        try:
            os.replace(temp, full_task)
        except Exception:
            if backup.exists():
                os.replace(backup, full_task)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def _phase3_certified_bound(provenance: dict, pilot_dir: Path,
                            full_dir: Path, task_ids: set[str]) -> bool:
    for task_id in task_ids:
        pilot_seal, pilot_error = _phase3_read_document(
            pilot_dir / task_id / "tests/phase2_provenance.json")
        full_seal, full_error = _phase3_read_document(
            full_dir / task_id / "tests/phase2_provenance.json")
        if (pilot_error or full_error or pilot_seal != full_seal or
                provenance.get(task_id) != {
                    "fingerprint": pilot_seal.get("fingerprint"),
                    "task_integrity": pilot_seal.get("task_integrity"),
                } or phase2_task_integrity(full_dir / task_id) != pilot_seal.get("task_integrity")):
            return False
    return True


def _phase3_source_snapshot(entries: list[dict]) -> dict[str, str]:
    representatives = {}
    for row in entries:
        representatives.setdefault(row["theme"], row)
    snapshot = {theme: phase2_fingerprint(row, REPO)
                for theme, row in sorted(representatives.items())}
    for relative in ("tasks/factory/compiler/blueprint_matrix.py",
                     "tasks/factory/compiler/phase2_scale.py"):
        path = REPO / relative
        snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    for agent in PHASE3_UPSTREAM:
        path = REPO / f"experiments/scale_1000/status/DONE_{agent}_phase3.json"
        snapshot[f"phase3_marker:{agent}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    opus, _ = _phase3_read_document(PHASE3_OPUS_MARKER)
    regrade_value = (opus.get("offline_regrade") or {}).get("summary")
    if isinstance(regrade_value, str) and regrade_value:
        regrade_path = Path(regrade_value)
        if not regrade_path.is_absolute():
            regrade_path = REPO / regrade_path
        snapshot["opus_offline_regrade"] = (hashlib.sha256(regrade_path.read_bytes()).hexdigest()
                                                if regrade_path.is_file() else "MISSING")
    return snapshot


def _persist_phase3_report(path: Path, report: dict) -> None:
    _write(path, report)
    pilot_passed = report.get("pilot_stage0_passed")
    pilot_total = report.get("pilot_stage0_total")
    pilot_result = (f"{pilot_passed}/{pilot_total} PASS" if pilot_total is not None
                    else "pending")
    lines = ["# Phase 3 certification", "", f"Status: **{report['status']}**", "",
             f"Catalog: {report['catalog_rows']} blueprints; pilot: {report['pilot_selected']} tasks.",
             f"Pilot Stage 0: {pilot_result}. Current package-bound K3/K5 proof: "
             f"{report['hardness_certified_tasks']}/51 certified. "
             f"Full Stage 0: {report['stage0_1080_passed']}/1080 verified.", "",
             f"Historical certified packages copied into full scale: {report.get('copied_certified', 0)}; "
             f"mixed-version exceptions: {len(report.get('mixed_version_catalog_exceptions', []))}.", "",
             f"Certified cache schedule mirrors accepted: "
             f"{len(report.get('runtime_surface_exceptions', []))}.", "",
             "## Evidence", "",
             f"- Detailed report: `{path}`",
             f"- Pilot Stage 0: `{report['pilot_stage0']}`",
             f"- Live proof: `{report['hardness_proof']}`",
             f"- Opus offline regrade: `{report.get('opus_offline_regrade', {}).get('path')}`",
             f"- Pre-live stale-cache diagnostic: "
             f"`{report.get('prelive_stale_cache', {}).get('path')}` "
             f"({len(report.get('prelive_stale_cache', {}).get('stale_task_ids', []))} "
             "historical certificates)",
             f"- Post-marker runtime diagnostic: "
             f"`{report.get('post_marker_runtime_diagnostic', {}).get('path')}` "
             f"({len(report.get('post_marker_runtime_diagnostic', {}).get('compatible_task_ids', []))}/10 "
             "historical worlds compatible)",
             f"- Full Stage 0: `{report['stage0_1080']}`",
             f"- Full catalog: `{report['catalog_artifact']}`",
             f"- Revised catalog candidate: `{report.get('candidate_catalog')}`",
             f"- Full task directory: `{report['out_dir']}`", ""]
    stale = report.get("prelive_stale_cache", {})
    runtime = report.get("post_marker_runtime_diagnostic", {})
    if stale.get("stale_task_ids") or runtime.get("incompatible_task_ids"):
        lines += ["## Constraint requiring a decision", "",
                  f"The pre-live gate started {stale.get('live_queues_started', 0)} live queues. "
                  f"The corrected grader invalidates {len(stale.get('stale_task_ids', []))} "
                  "historical certificates, and current worlds differ for "
                  f"{len(runtime.get('incompatible_task_ids', []))} preserved tasks. "
                  "Completing package-bound live certification requires rebuilding and "
                  "recalibrating the affected historical tasks, which requires changing "
                  "the plan's no-rerun constraint.", ""]
    if report["deficits"]:
        lines += ["## Blocking findings", "", *(f"- {item}" for item in report["deficits"]), ""]
    PHASE3_REPORT.parent.mkdir(parents=True, exist_ok=True)
    PHASE3_REPORT.write_text("\n".join(lines), encoding="utf-8")
    if report["status"] == "complete":
        _write(PHASE3_DONE, {"agent": "gemini", "status": "complete",
                             "catalog_blueprints": 1080,
                             "catalog": report["catalog_artifact"],
                             "catalog_sha256": report["catalog_sha256"],
                             "pilot_stage0": report["pilot_stage0"],
                             "hardness_proof": report["hardness_proof"],
                             "hardness_certified_tasks": 51,
                             "stage0_1080": report["stage0_1080"],
                             "stage0_1080_passed": 1080,
                             "report": str(PHASE3_REPORT)})
    elif PHASE3_DONE.is_file():
        PHASE3_DONE.unlink()


def run_phase3_scale(*, selection: Path = DEFAULT_PILOT / "phase2_selection.json",
                     pilot_stage0: Path = PHASE3_PILOT_STAGE0,
                     run_index: Path = DEFAULT_PILOT / "phase2_run_index.json",
                     out_dir: Path = DEFAULT_OUT, scratch: Path = DEFAULT_SCRATCH,
                     report_path: Path = PHASE3_SCALE_REPORT,
                     catalog_artifact: Path = PHASE3_CATALOG,
                     materialize_workers: int = 4, verify_workers: int = 16,
                     preflight_only: bool = False) -> dict:
    """Rebuild only Phase 3 topologies after exact pilot and live proof gates.

    Frozen FT03/FT15 packages retain their historical source fingerprint only
    when their blueprint and sealed package bytes still match. The Phase 2
    scale gate continues to require a current source fingerprint everywhere.
    """
    if materialize_workers < 1 or verify_workers < 1:
        raise ValueError("worker counts must be positive")
    safe_out = assert_outside_git_repo(out_dir, repo_root=REPO)
    safe_scratch = assert_outside_git_repo(scratch, repo_root=REPO)
    safe_catalog = assert_outside_git_repo(catalog_artifact, repo_root=REPO)
    from tasks.factory.compiler.blueprint_matrix import generate_catalog
    entries = generate_catalog()
    catalog_report = _validate_phase2_catalog(entries)
    original_entries, original_report = _read_catalog(DEFAULT_CATALOG)
    original_by_id = {row["task_id"]: row for row in original_entries}
    current_by_id = {row["task_id"]: row for row in entries}
    rows, selection_deficits = _pilot_rows(Path(selection), DEFAULT_CATALOG, original_entries)
    markers, marker_deficits, marker_mtime = _phase3_upstream_markers()
    deficits = [*marker_deficits, *selection_deficits]
    if not catalog_report["valid"]:
        deficits.extend(catalog_report["errors"])
    if not original_report["valid"]:
        deficits.extend(original_report["errors"])
    if set(current_by_id) != set(original_by_id):
        deficits.append("Phase 3 catalog task IDs differ from frozen 1,080-task catalog")
    changed = {task_id for task_id in set(current_by_id) & set(original_by_id)
               if current_by_id[task_id] != original_by_id[task_id]}
    unexpected = sorted(task_id for task_id in changed
                        if current_by_id[task_id]["fault_topology_id"] not in PHASE3_MUTATED_TOPOLOGIES)
    if unexpected:
        deficits.append(f"blueprint mutations outside Phase 3 topologies: {unexpected[:10]}")
    pilot_dir = Path(selection).parent
    delta_path = pilot_dir / PHASE3_DELTA_MANIFEST.name
    audit_path = pilot_dir / PHASE3_RUNTIME_AUDIT.name
    delta, delta_error = _phase3_read_document(delta_path)
    audit, audit_error = _phase3_read_document(audit_path)
    if delta_error:
        deficits.append(f"Phase 3 delta manifest unavailable: {delta_error}")
    if audit_error:
        deficits.append(f"preserved-run runtime compatibility audit unavailable: {audit_error}")
    certified_ids = set(delta.get("preserved_certified_task_ids") or [])
    selected_ids = {row["task_id"] for row in rows}
    if delta and (delta.get("schema_version") != 1 or delta.get("state") != "complete" or
                  set(delta.get("tasks") or {}) != selected_ids or
                  len(certified_ids) < 10 or not certified_ids <= selected_ids):
        deficits.append("Phase 3 delta manifest is incomplete or has invalid certified-task coverage")
    if delta and delta.get("source_catalog_sha256") != hashlib.sha256(DEFAULT_CATALOG.read_bytes()).hexdigest():
        deficits.append("Phase 3 delta manifest is not bound to the frozen catalog")
    selection_doc, selection_error = _phase3_read_document(Path(selection))
    if selection_error or (delta and delta.get("selection_sha256") != selection_doc.get("selection_sha256")):
        deficits.append("Phase 3 delta manifest is not bound to the pilot selection")
    if delta and set((delta.get("calibration_summary") or {}).get("certified_task_ids") or []) != certified_ids:
        deficits.append("Phase 3 delta manifest certified IDs differ from source calibration summary")
    for agent, marker in markers.items():
        if delta and (delta.get("phase3_marker_sha256") or {}).get(agent) != hashlib.sha256(
                Path(marker["path"]).read_bytes()).hexdigest():
            deficits.append(f"Phase 3 delta manifest marker hash differs: {agent}")
    delta_mtime = delta_path.stat().st_mtime if delta_path.is_file() else 0.0
    if delta and delta_mtime < marker_mtime:
        deficits.append("Phase 3 delta manifest predates an upstream completion marker")
    audit_checks = audit.get("checks") if isinstance(audit.get("checks"), dict) else {}
    runtime_surface_exceptions = [
        {"task_id": task_id, "accepted_additions": check["accepted_additions"]}
        for task_id, check in sorted(audit_checks.items())
        if isinstance(check, dict) and check.get("accepted_additions")
    ]
    stale_cache = _phase3_stale_cache_evidence()
    runtime_diagnostic = _phase3_runtime_diagnostic_evidence()
    if stale_cache.get("stale_task_ids"):
        deficits.append(
            f"corrected grader invalidates {len(stale_cache['stale_task_ids'])} "
            "preserved historical K=5 certificates")
    if runtime_diagnostic.get("incompatible_task_ids"):
        deficits.append(
            f"fresh worlds differ from historical K3/K5 for "
            f"{len(runtime_diagnostic['incompatible_task_ids'])} preserved tasks")
    if audit and (audit.get("schema_version") != 1 or audit.get("compatible") is not True or
                  set(audit.get("preserved_certified") or []) != certified_ids or
                  set(audit_checks) != certified_ids or
                  any(not check.get("compatible") for check in audit_checks.values())):
        deficits.append("preserved certified runs failed runtime compatibility audit")
    if audit and audit_path.stat().st_mtime < max(marker_mtime, delta_mtime):
        deficits.append("runtime compatibility audit predates Phase 3 delta materialization")
    summary, summary_error = _phase3_read_document(PHASE3_CALIBRATION_SUMMARY)
    if summary_error:
        deficits.append(f"post-delta calibration summary unavailable: {summary_error}")
    else:
        resume = summary.get("phase3_resume")
        resume = resume if isinstance(resume, dict) else {}
        audit_sha = hashlib.sha256(audit_path.read_bytes()).hexdigest() if audit_path.is_file() else None
        if (resume.get("runtime_audit_sha256") != audit_sha or
                resume.get("source_summary_sha256") != audit.get("source_summary_sha256")):
            deficits.append("post-delta calibration summary does not bind runtime audit/source summary")
    if not Path(pilot_stage0).is_file() or Path(pilot_stage0).stat().st_mtime < max(marker_mtime, delta_mtime):
        deficits.append("51-task strict Stage 0 must run after Phase 3 delta materialization")
    else:
        deficits.extend(_pilot_stage0(Path(pilot_stage0), rows, pilot_dir))
    pilot_stage0_report, pilot_stage0_error = _phase3_read_document(Path(pilot_stage0))
    pilot_bad = []
    for row in rows:
        task_id = row["task_id"]
        preserved = task_id in certified_ids
        expected = row if preserved else current_by_id.get(task_id, row)
        task = pilot_dir / task_id
        entry = (delta.get("tasks") or {}).get(task_id, {})
        try:
            seal = json.loads((task / "tests/phase2_provenance.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            seal = {}
        expected_status = "preserved_certified" if preserved else {"rematerialized", "existing_current"}
        status_ok = (entry.get("status") == expected_status if preserved
                     else entry.get("status") in expected_status)
        if (not status_ok or entry.get("package_integrity") != seal.get("task_integrity") or
                not _phase3_package_valid(task, expected, current_source=not preserved)):
            pilot_bad.append(task_id)
    if pilot_bad:
        deficits.append(f"pilot package integrity, delta status, or blueprint mismatch: {len(pilot_bad)} tasks")
    run_bad = _run_provenance_failures(Path(run_index), pilot_dir, rows)
    if run_bad:
        deficits.append(f"live K3/K5 run index is not bound to current pilot packages: {len(run_bad)} tasks")
    hardness = evaluate_hardness(selection, run_index)
    proof_path = Path(report_path).with_name("phase3_hardness_proof.json")
    _write(proof_path, hardness)
    opus_regrade, opus_deficits = _phase3_opus_regrade(hardness)
    deficits.extend(opus_deficits)
    if not hardness["certified"] or hardness.get("certified_tasks") != 51:
        deficits.append(f"live K3/K5 hardness proof uncertified: {hardness.get('certified_tasks')}/51 tasks")
    effective_entries = [original_by_id[row["task_id"]] if row["task_id"] in certified_ids else row
                         for row in entries]
    effective_report = _validate_phase2_catalog(effective_entries)
    if not effective_report["valid"]:
        deficits.extend(effective_report["errors"])
    catalog_bytes = "".join(json.dumps(row, sort_keys=True) + "\n" for row in effective_entries).encode("utf-8")
    current_catalog_bytes = "".join(json.dumps(row, sort_keys=True) + "\n" for row in entries).encode("utf-8")
    exceptions = [
        {"task_id": task_id,
         "historical_blueprint_sha256": hashlib.sha256(json.dumps(original_by_id[task_id], sort_keys=True).encode()).hexdigest(),
         "current_blueprint_sha256": hashlib.sha256(json.dumps(current_by_id[task_id], sort_keys=True).encode()).hexdigest(),
         "pilot_package_integrity": (delta.get("tasks") or {}).get(task_id, {}).get("package_integrity")}
        for task_id in sorted(certified_ids)
        if task_id in current_by_id and task_id in original_by_id
        and current_by_id[task_id] != original_by_id[task_id]
    ]
    if delta and set(delta.get("mixed_version_catalog_exceptions") or []) != {
            item["task_id"] for item in exceptions}:
        deficits.append("Phase 3 mixed-version exception list differs from delta manifest")
    source_snapshot = _phase3_source_snapshot(entries) if not deficits else {}
    report = {
        "updated_utc": datetime.now(timezone.utc).isoformat(),
        "status": "blocked_preflight" if deficits else "ready_to_scale",
        "catalog_rows": len(entries), "catalog_valid": catalog_report["valid"] and effective_report["valid"],
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "generated_current_catalog_sha256": hashlib.sha256(current_catalog_bytes).hexdigest(),
        "catalog_artifact": str(safe_catalog), "changed_blueprints": len(changed),
        "candidate_catalog": (str(PHASE3_CANDIDATE_CATALOG)
                              if PHASE3_CANDIDATE_CATALOG.is_file() else None),
        "mixed_version_catalog_exceptions": exceptions,
        "preserved_certified_task_ids": sorted(certified_ids),
        "changed_topologies": dict(sorted(Counter(current_by_id[i]["fault_topology_id"]
                                                   for i in changed).items())),
        "mutated_topologies": sorted(PHASE3_MUTATED_TOPOLOGIES),
        "source_fingerprints_by_theme": source_snapshot,
        "upstream_markers": markers, "pilot_selected": len(rows),
        "pilot_stage0": str(pilot_stage0), "run_index": str(run_index),
        "pilot_stage0_passed": (pilot_stage0_report.get("passed")
                                if not pilot_stage0_error else None),
        "pilot_stage0_total": (pilot_stage0_report.get("total")
                               if not pilot_stage0_error else None),
        "delta_manifest": str(delta_path), "runtime_compatibility_audit": str(audit_path),
        "runtime_surface_exceptions": runtime_surface_exceptions,
        "prelive_stale_cache": stale_cache,
        "post_marker_runtime_diagnostic": runtime_diagnostic,
        "pilot_integrity_failures": pilot_bad, "run_provenance_failures": run_bad,
        "hardness_certified_tasks": hardness.get("certified_tasks", 0),
        "hardness_proof": str(proof_path), "hardness_deficit_counts": hardness.get("deficit_counts", {}),
        "opus_offline_regrade": opus_regrade,
        "out_dir": str(safe_out), "materialized": 0, "reused": 0,
        "copied_certified": 0,
        "failed": 0, "stage0_1080": None, "stage0_1080_passed": 0,
        "deficits": deficits,
    }
    if deficits:
        _persist_phase3_report(report_path, report)
        return report

    existing = {path.name for path in safe_out.iterdir() if path.is_dir()} if safe_out.is_dir() else set()
    extra = sorted(existing - set(current_by_id))
    if extra:
        report["status"] = "blocked_unexpected_tasks"
        report["deficits"].append(f"unexpected full-catalog task directories: {extra[:10]}")
        _persist_phase3_report(report_path, report)
        return report
    pending = []
    certified_copies = []
    for blueprint in effective_entries:
        task = safe_out / blueprint["task_id"]
        if blueprint["task_id"] in certified_ids:
            pilot_task = pilot_dir / blueprint["task_id"]
            pilot_seal = json.loads((pilot_task / "tests/phase2_provenance.json").read_text(encoding="utf-8"))
            full_seal, _ = _phase3_read_document(task / "tests/phase2_provenance.json")
            if (_phase3_package_valid(task, blueprint, current_source=False) and
                    full_seal.get("task_integrity") == pilot_seal["task_integrity"] and
                    full_seal.get("fingerprint") == pilot_seal["fingerprint"]):
                report["reused"] += 1
            else:
                certified_copies.append((pilot_task, task, pilot_seal["task_integrity"]))
            continue
        current_source = blueprint["fault_topology_id"] in PHASE3_MUTATED_TOPOLOGIES
        if _phase3_package_valid(task, blueprint, current_source=current_source):
            report["reused"] += 1
        elif current_source:
            pending.append(blueprint)
        else:
            report["deficits"].append(f"frozen topology package missing or altered: {blueprint['task_id']}")
    report["pending"] = len(pending)
    report["pending_certified_copies"] = len(certified_copies)
    if report["deficits"]:
        report["status"] = "blocked_frozen_packages"
        _persist_phase3_report(report_path, report)
        return report
    _persist_phase3_report(report_path, report)
    if preflight_only:
        return report

    safe_catalog.parent.mkdir(parents=True, exist_ok=True)
    safe_catalog.write_bytes(catalog_bytes)
    safe_out.mkdir(parents=True, exist_ok=True)
    safe_scratch.mkdir(parents=True, exist_ok=True)
    for pilot_task, task, integrity in certified_copies:
        try:
            _phase3_copy_certified(pilot_task, task, integrity)
            report["copied_certified"] += 1
        except Exception as exc:
            report["failed"] += 1
            report.setdefault("materialization_errors", []).append(
                {"task_id": task.name, "status": "certified_copy_failed",
                 "error": f"{type(exc).__name__}: {exc}"})
            report["status"] = "certified_copy_failed"
            report["deficits"].append(f"failed to copy certified pilot package: {task.name}")
            _persist_phase3_report(report_path, report)
            return report
    if pending:
        context = multiprocessing.get_context("spawn")
        with concurrent.futures.ProcessPoolExecutor(max_workers=materialize_workers,
                                                    mp_context=context) as pool:
            futures = [pool.submit(_materialize_one, bp, str(safe_out), str(safe_scratch))
                       for bp in pending]
            for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
                item = future.result()
                if item["status"] == "materialized":
                    report["materialized"] += 1
                else:
                    report["failed"] += 1
                    report.setdefault("materialization_errors", []).append(item)
                if index % 10 == 0 or item["status"] == "failed":
                    _persist_phase3_report(report_path, report)
    if report["failed"] or report["materialized"] + report["reused"] + report["copied_certified"] != 1080:
        report["status"] = "materialization_incomplete"
        report["deficits"].append("full-catalog package count or materialization failed")
        _persist_phase3_report(report_path, report)
        return report
    if _phase3_source_snapshot(entries) != source_snapshot:
        report["status"] = "source_changed_during_scale"
        report["deficits"].append("Phase 3 materializer sources changed during full-catalog build")
        _persist_phase3_report(report_path, report)
        return report

    from tasks.factory.verify_all import verify_all
    verified = verify_all(safe_out, workers=verify_workers, phase2=True, strict_phase2=True)
    _write(PHASE3_STAGE0_1080, verified)
    report["stage0_1080"] = str(PHASE3_STAGE0_1080)
    report["stage0_1080_passed"] = verified.get("passed", 0)
    certified_bound = _phase3_certified_bound(
        verified.get("phase2_package_provenance") or {}, pilot_dir, safe_out, certified_ids)
    source_unchanged = _phase3_source_snapshot(entries) == source_snapshot
    if (verified.get("ok") and verified.get("total") == 1080 and
            verified.get("passed") == 1080 and
            len(verified.get("phase2_package_provenance", {})) == 1080 and
            certified_bound and source_unchanged):
        report["status"] = "complete"
    else:
        report["status"] = "stage0_failed"
        report["deficits"].append(
            f"full strict Stage 0 passed {verified.get('passed')}/{verified.get('total')}; "
            f"certified package binding: {certified_bound}; "
            f"source unchanged: {source_unchanged}")
    _persist_phase3_report(report_path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--phase3", action="store_true",
                        help="run the selective Phase 3 scale gate with frozen FT03/FT15 lineage")
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--selection", type=Path,
                        default=DEFAULT_PILOT / "phase2_selection.json")
    parser.add_argument("--pilot-stage0", type=Path,
                        default=Path("/home/ubuntu/alkera_factory_1017/phase2_stage0_51.json"))
    parser.add_argument("--run-index", type=Path,
                        default=DEFAULT_PILOT / "phase2_run_index.json")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--scratch", type=Path, default=DEFAULT_SCRATCH)
    parser.add_argument("--json", type=Path,
                        default=Path("/home/ubuntu/alkera_factory_1017/phase2_scale_report.json"))
    parser.add_argument("--materialize-workers", type=int, default=4)
    parser.add_argument("--verify-workers", type=int, default=16)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--only-topologies", nargs="+", metavar="FT##",
                        help="selectively re-materialize only named fault topologies (e.g. FT01,FT02)")
    parser.add_argument("--refresh-preserved-seals", action="store_true",
                        help="refresh provenance seals in place on preserved topologies")
    parser.add_argument("--phase3-pilot-stage0", type=Path, default=PHASE3_PILOT_STAGE0)
    parser.add_argument("--phase3-catalog", type=Path, default=PHASE3_CATALOG)
    args = parser.parse_args(argv)
    from tasks.factory.compiler.phase2_pilot import _normalize_topologies
    requested = _normalize_topologies(args.only_topologies) if args.only_topologies else None
    if args.phase3:
        result = run_phase3_scale(selection=args.selection,
                                  pilot_stage0=args.phase3_pilot_stage0,
                                  run_index=args.run_index, out_dir=args.out,
                                  scratch=args.scratch, report_path=args.json
                                  if args.json != Path("/home/ubuntu/alkera_factory_1017/phase2_scale_report.json")
                                  else PHASE3_SCALE_REPORT,
                                  catalog_artifact=args.phase3_catalog,
                                  materialize_workers=args.materialize_workers,
                                  verify_workers=args.verify_workers,
                                  preflight_only=args.preflight_only)
        print(json.dumps({key: result[key] for key in
                          ("status", "catalog_rows", "pilot_selected",
                           "hardness_certified_tasks", "materialized", "reused",
                           "failed", "stage0_1080")}, indent=2))
        return 0 if result["status"] == "complete" else 1
    result = run_scale(catalog=args.catalog, selection=args.selection,
                       pilot_stage0=args.pilot_stage0, run_index=args.run_index,
                       out_dir=args.out, scratch=args.scratch, report_path=args.json,
                       materialize_workers=args.materialize_workers,
                       verify_workers=args.verify_workers,
                       preflight_only=args.preflight_only,
                       only_topologies=requested,
                       refresh_preserved_seals=args.refresh_preserved_seals)
    print(json.dumps({key: result[key] for key in
                      ("status", "catalog_rows", "pilot_selected", "hardness_certified",
                       "materialized", "reused", "failed", "stage0_1080")}, indent=2))
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
