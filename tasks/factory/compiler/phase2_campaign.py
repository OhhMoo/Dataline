"""Run the Phase 2 pilot, closed-loop hardness proof, then gated scale build.

The frozen catalog is never edited. Difficulty changes are recorded in an
append-only journal and replayed into a versioned 1,080-row catalog. Each
candidate gets its own 51-task pilot, Stage 0 report, and live run index.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from tasks.factory.calibrate_queue import (assert_outside_git_repo,
                                           classify_stage1_arm,
                                           classify_stage2_arm)
from tasks.factory.compiler.phase2_calibration import (DIFFICULTY_DEFICITS,
                                                        run_pilot_calibration)
from tasks.factory.compiler.phase2_hardness import audit_live_run
from tasks.factory.compiler.phase2_mutation import MutationError, derive_catalog
from tasks.factory.compiler.phase2_pilot import (
    DEFAULT_CATALOG, DEFAULT_OUT as DEFAULT_PILOT, Phase2NotReady,
    load_catalog, prepare_selection, run_materialization,
)
from tasks.factory.compiler.phase2_readiness import inspect_phase2_readiness
from tasks.factory.compiler.phase2_scale import _provenance_matches, run_scale

DEFAULT_WORKSPACE = Path("/home/ubuntu/alkera_factory_1017/phase2_campaign")
MUTATION_ACTIONS = frozenset({"increase_difficulty", "add_hazard_or_obfuscate",
                              "reduce_hazard_count", "step_down_difficulty",
                              "reduce_obfuscation"})


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temp.write_text(json.dumps(value, sort_keys=True, indent=2,
                                   ensure_ascii=False) + "\n", encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _read_journal(path: Path, source_sha: str) -> dict:
    if not path.exists():
        return {"schema_version": 1, "source_catalog_sha256": source_sha,
                "operations": []}
    document = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(document, dict) or document.get("schema_version") != 1
            or document.get("source_catalog_sha256") != source_sha
            or not isinstance(document.get("operations"), list)):
        raise MutationError("campaign journal does not match the frozen catalog")
    return document


def _candidate(source: Path, workspace: Path, journal: dict,
               journal_path: Path) -> dict:
    if not journal["operations"]:
        pilot = (DEFAULT_PILOT if workspace.resolve() == DEFAULT_WORKSPACE.resolve()
                 else workspace / "pilot_51")
        selection = prepare_selection(source, pilot)
        return {"version": "source", "catalog": source,
                "selection": pilot / "phase2_selection.json",
                "pilot_dir": pilot,
                "selection_sha256": selection["selection_sha256"],
                "changed_proof_task_ids": []}
    if not journal_path.is_file():
        raise MutationError("campaign journal vanished before candidate derivation")
    manifest = derive_catalog(source, journal_path, workspace / "versions")
    return {"version": manifest["version"],
            "catalog": Path(manifest["derived_catalog"]),
            "selection": Path(manifest["selection"]),
            "pilot_dir": Path(manifest["pilot_dir"]),
            "selection_sha256": manifest["selection_sha256"],
            "changed_proof_task_ids": manifest["changed_proof_task_ids"],
            "mutation_manifest": str(Path(manifest["derived_catalog"]).parent /
                                     "mutation_manifest.json")}


def _stage_paths(candidate: dict, workspace: Path) -> dict:
    if candidate["version"] == "source":
        default = workspace.resolve() == DEFAULT_WORKSPACE.resolve()
        return {"stage0": (Path("/home/ubuntu/alkera_factory_1017/phase2_stage0_51.json")
                           if default else workspace / "phase2_stage0_51.json"),
                "runs": (Path("/home/ubuntu/alkera_factory_1017/runs_phase2")
                         if default else workspace / "runs"),
                "run_index": candidate["pilot_dir"] / "phase2_run_index.json",
                "calibration_report": candidate["pilot_dir"] / "phase2_calibration_report.json"}
    root = workspace / "rounds" / candidate["version"]
    return {"stage0": root / "phase2_stage0_51.json",
            "runs": root / "runs", "run_index": root / "phase2_run_index.json",
            "calibration_report": root / "phase2_calibration_report.json"}


def _adjustments(report: dict, run_index_path: Path, selected: list[dict],
                 pilot_dir: Path) -> tuple[list[dict], list[str]]:
    """Derive mutations from strictly audited, package-bound live runs only."""
    by_id = {item["blueprint"]["task_id"]: item["blueprint"]
             for item in selected}
    actions: list[dict] = []
    blockers: list[str] = []
    try:
        run_index = json.loads(run_index_path.read_text(encoding="utf-8")).get("runs", {})
    except (OSError, ValueError, AttributeError):
        run_index = {}

    def checked_run(task_id: str, entry: dict, stage: str, k: int) -> tuple[Path | None, dict | None]:
        raw = entry.get(stage)
        path = Path(raw) if isinstance(raw, str) and raw else None
        audit = audit_live_run(path, task_id, k)
        structural = [item for item in audit["deficits"]
                      if item["code"] not in DIFFICULTY_DEFICITS]
        if structural:
            codes = ", ".join(sorted({item["code"] for item in structural}))
            blockers.append(f"{task_id}: K={k} live evidence invalid ({codes})")
            return path, None
        return path, audit

    for task_id, row in by_id.items():
        task_dir = pilot_dir / task_id
        if not _provenance_matches(task_dir, row):
            blockers.append(f"{task_id}: current pilot package provenance is invalid")
            continue
        try:
            seal = json.loads((task_dir / "tests/phase2_provenance.json")
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            blockers.append(f"{task_id}: pilot provenance seal unreadable")
            continue
        entry = run_index.get(task_id) or {}
        if (entry.get("fingerprint") != seal["fingerprint"] or
                entry.get("task_integrity") != seal["task_integrity"]):
            blockers.append(f"{task_id}: live run index is not bound to the current package")
            continue
        k3_path, k3_audit = checked_run(task_id, entry, "k3", 3)
        if k3_audit is None:
            continue
        try:
            stage1 = classify_stage1_arm(k3_path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            blockers.append(f"{task_id}: K=3 classification unreadable: {exc}")
            continue
        classification = stage1.get("classification")
        if classification in {"saturated_floor", "saturated_ceiling"}:
            prescription = stage1.get("prescription") or {}
            if prescription.get("action") not in MUTATION_ACTIONS:
                blockers.append(f"{task_id}: K=3 saturation has no supported mutation")
                continue
            actions.append({"task_id": task_id, "theme": row["theme"],
                            "governance_mode": row["governance_mode"],
                            "fault_topology_id": row["fault_topology_id"],
                            "classification": classification,
                            "prescription": prescription,
                            "k3_run_dir": str(k3_path)})
            continue
        if not stage1.get("admit_to_stage2"):
            blockers.append(f"{task_id}: K=3 is neither admissible nor measured saturation")
            continue
        if k3_audit["deficits"]:
            blockers.append(f"{task_id}: K=3 admitted but scalar reward is collapsed")
            continue
        k5_path, k5_audit = checked_run(task_id, entry, "k5", 5)
        if k5_audit is None:
            continue
        try:
            diagnosis = classify_stage2_arm(k5_path)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            blockers.append(f"{task_id}: K=5 classification unreadable: {exc}")
            continue
        if diagnosis.get("certified"):
            if k5_audit["deficits"]:
                blockers.append(f"{task_id}: K=5 pass band has collapsed scalar reward")
            continue
        prescription = dict(diagnosis.get("prescription") or {})
        action = prescription.get("action")
        if action not in MUTATION_ACTIONS and diagnosis.get("pass_at_1") == 0:
            action = "reduce_hazard_count"
            prescription = {"action": action, "type": action,
                            "reason": "Fully scored K=5 run was 0/5 outside the core band."}
        if action not in MUTATION_ACTIONS:
            blockers.append(f"{task_id}: K=5 rejected without a safe mutation action")
            continue
        actions.append({"task_id": task_id, "theme": row["theme"],
                        "governance_mode": row["governance_mode"],
                        "fault_topology_id": row["fault_topology_id"],
                        "classification": diagnosis.get("band"),
                        "prescription": prescription,
                        "k3_run_dir": str(k3_path), "k5_run_dir": str(k5_path)})
    return actions, blockers


def _append_mutations(journal: dict, actions: list[dict], selected: list[dict],
                      current_catalog: Path, source_records: list[dict],
                      journal_path: Path, source: Path, versions_root: Path) -> dict:
    """Append one evidence-backed operation per topology for the next round."""
    selected_by_id = {item["blueprint"]["task_id"]: item["blueprint"]
                      for item in selected}
    source_by_id = {record["blueprint"]["task_id"]: record
                    for record in source_records}
    current_by_id = {json.loads(line)["task_id"]: json.loads(line)
                     for line in current_catalog.read_text(encoding="utf-8").splitlines()
                     if line.strip()}
    grouped: dict[str, list[dict]] = defaultdict(list)
    for item in actions:
        grouped[selected_by_id[item["task_id"]]["fault_topology_id"]].append(item)
    conflicts = []
    for ft, group in grouped.items():
        actions_for_ft = {(item["prescription"]["action"].startswith("increase") or
                           item["prescription"]["action"] == "add_hazard_or_obfuscate")
                          for item in group}
        if len(actions_for_ft) > 1:
            conflicts.append(f"{ft}: pilot cells call for both increasing and decreasing difficulty")
    if conflicts:
        raise MutationError("; ".join(conflicts))
    round_number = max((item["round"] for item in journal["operations"]), default=0) + 1
    proposal = json.loads(json.dumps(journal))
    for ft, group in sorted(grouped.items()):
        # Prefer an action with structural effect. Ties are deterministic.
        priority = {"increase_difficulty": 0, "add_hazard_or_obfuscate": 1,
                    "reduce_hazard_count": 2, "step_down_difficulty": 3,
                    "reduce_obfuscation": 4}
        chosen = min(group, key=lambda item: (
            priority[item["prescription"]["action"]], item["task_id"]))
        task_id = chosen["task_id"]
        source_row = source_by_id[task_id]
        current_row = current_by_id[task_id]
        proposal["operations"].append({
            "round": round_number, "source_task_id": task_id,
            "source_row_sha256": source_row["source_row_sha256"],
            "expected_before_blueprint_sha256": _sha(_canonical(current_row)),
            "scope": "fault_topology",
            "prescription": {"classification": chosen.get("classification"),
                             **chosen["prescription"]},
            "evidence": {"pilot_task_id": task_id,
                         "k3_run_dir": chosen.get("k3_run_dir"),
                         "k5_run_dir": chosen.get("k5_run_dir"),
                         "diagnosed_cells": [item["task_id"] for item in group]},
        })
    if not grouped:
        raise MutationError("no evidence-backed difficulty mutation is available")
    prior_bytes = journal_path.read_bytes() if journal_path.is_file() else None
    if prior_bytes is None:
        if journal["operations"]:
            raise MutationError("campaign journal disappeared before mutation")
    else:
        try:
            if json.loads(prior_bytes) != journal:
                raise MutationError("campaign journal changed since the current candidate was read")
        except ValueError as exc:
            raise MutationError("campaign journal became unreadable") from exc
    draft = journal_path.with_name(
        f".{journal_path.name}.round-{round_number}.tmp-{os.getpid()}-{time.time_ns()}")
    try:
        _write(draft, proposal)
        manifest = derive_catalog(source, draft, versions_root)
        current_bytes = journal_path.read_bytes() if journal_path.is_file() else None
        if current_bytes != prior_bytes:
            raise MutationError("campaign journal changed during candidate derivation")
        draft.replace(journal_path)
    finally:
        draft.unlink(missing_ok=True)
    return {"round": round_number, "fault_topologies": sorted(grouped),
            "version": manifest["version"],
            "derived_catalog": manifest["derived_catalog"],
            "changed_proof_task_ids": manifest["changed_proof_task_ids"]}


def _run_campaign_locked(*, source_catalog: Path = DEFAULT_CATALOG,
                         workspace: Path = DEFAULT_WORKSPACE,
                         pilot_workers: int = 3, verify_workers: int = 16,
                         concurrency: int = 3, materialize_workers: int = 4,
                         max_mutation_rounds: int = 8) -> dict:
    """Resume work until certified, blocked by evidence, or the mutation limit."""
    if min(pilot_workers, verify_workers, concurrency, materialize_workers) < 1:
        raise ValueError("worker counts must be positive")
    if max_mutation_rounds < 0:
        raise ValueError("max_mutation_rounds must be nonnegative")
    source = Path(source_catalog).resolve()
    root = assert_outside_git_repo(workspace)
    root.mkdir(parents=True, exist_ok=True)
    source_meta, source_records = load_catalog(source)
    journal_path = root / "mutation_journal.json"
    journal = _read_journal(journal_path, source_meta["sha256"])
    report_path = root / "campaign_report.json"
    report = {"schema_version": 1, "updated_utc": None, "status": "starting",
              "source_catalog": str(source),
              "source_catalog_sha256": source_meta["sha256"],
              "journal": str(journal_path), "rounds": [],
              "deficits": [], "hardness_certified": False,
              "scale_report": None}

    def persist() -> dict:
        report["updated_utc"] = datetime.now(timezone.utc).isoformat()
        _write(report_path, report)
        return report

    while True:
        readiness = inspect_phase2_readiness()
        if not readiness["ready"]:
            report["status"] = "blocked_upstream"
            report["deficits"] = readiness["blockers"]
            report["readiness"] = readiness
            return persist()
        candidate = _candidate(source, root, journal, journal_path)
        paths = _stage_paths(candidate, root)
        selection = json.loads(candidate["selection"].read_text(encoding="utf-8"))
        round_report = {"version": candidate["version"],
                        "catalog": str(candidate["catalog"]),
                        "selection": str(candidate["selection"]),
                        "pilot_dir": str(candidate["pilot_dir"]),
                        "stage0": str(paths["stage0"]),
                        "run_index": str(paths["run_index"]),
                        "changed_proof_task_ids": candidate["changed_proof_task_ids"]}
        report["rounds"].append(round_report)
        persist()
        try:
            canary = run_materialization(
                selection, candidate["pilot_dir"],
                scratch=root / "scratch" / candidate["version"],
                workers=1, limit=1)
            round_report["integration_canary"] = canary
            failures = [item for item in canary.get("tasks", [])
                        if item.get("status") == "failed"]
            if failures:
                report["status"] = "integration_canary_failed"
                report["deficits"] = [f"{item['task_id']}: {item.get('error')}"
                                      for item in failures]
                return persist()
            build = run_materialization(
                selection, candidate["pilot_dir"],
                scratch=root / "scratch" / candidate["version"],
                workers=pilot_workers)
        except Phase2NotReady as exc:
            report["status"] = "blocked_upstream"
            report["deficits"] = [str(exc)]
            return persist()
        round_report["pilot_materialization"] = build
        if build["state"] != "materialized_unverified":
            report["status"] = "pilot_materialization_failed"
            report["deficits"] = [f"pilot materialization state: {build['state']}"]
            return persist()
        calibration = run_pilot_calibration(
            catalog=candidate["catalog"], selection=candidate["selection"],
            pilot_dir=candidate["pilot_dir"], stage0_path=paths["stage0"],
            runs_dir=paths["runs"], run_index_path=paths["run_index"],
            report_path=paths["calibration_report"],
            verify_workers=verify_workers, concurrency=concurrency)
        round_report["calibration_report"] = str(paths["calibration_report"])
        round_report["calibration_status"] = calibration["status"]
        round_report["hardness_certified_tasks"] = calibration.get("hardness_certified_tasks", 0)
        persist()
        if calibration["status"] == "certified" and calibration["hardness_certified"]:
            report["hardness_certified"] = True
            scale = run_scale(
                catalog=candidate["catalog"], selection=candidate["selection"],
                pilot_stage0=paths["stage0"], run_index=paths["run_index"],
                out_dir=Path("/home/ubuntu/alkera_factory_1017/tasks_1080"),
                scratch=root / "scale_scratch", report_path=root / "scale_report.json",
                materialize_workers=materialize_workers, verify_workers=verify_workers)
            report["scale_report"] = str(root / "scale_report.json")
            report["status"] = scale["status"]
            report["deficits"] = scale.get("deficits", [])
            return persist()
        if calibration["status"] != "needs_adjustment":
            report["status"] = calibration["status"]
            report["deficits"] = calibration.get("deficits", [])
            return persist()
        if max((item["round"] for item in journal["operations"]), default=0) >= max_mutation_rounds:
            report["status"] = "mutation_limit_reached"
            report["deficits"] = calibration.get("hardness_deficits", [])
            return persist()
        actions, blockers = _adjustments(calibration, paths["run_index"],
                                         selection["selected"], candidate["pilot_dir"])
        if blockers:
            report["status"] = "measurement_or_mutation_blocked"
            report["deficits"] = blockers
            return persist()
        if not actions:
            report["status"] = "hardness_uncertified_no_mutation"
            report["deficits"] = calibration.get("hardness_deficits", [])
            return persist()
        try:
            mutation = _append_mutations(
                journal, actions, selection["selected"], candidate["catalog"],
                source_records, journal_path, source, root / "versions")
        except MutationError as exc:
            report["status"] = "mutation_blocked"
            report["deficits"] = [str(exc)]
            return persist()
        round_report["mutation"] = mutation
        journal = _read_journal(journal_path, source_meta["sha256"])


def run_campaign(*, source_catalog: Path = DEFAULT_CATALOG,
                 workspace: Path = DEFAULT_WORKSPACE,
                 pilot_workers: int = 3, verify_workers: int = 16,
                 concurrency: int = 3, materialize_workers: int = 4,
                 max_mutation_rounds: int = 8) -> dict:
    """Hold a workspace lock for the entire pilot and scale campaign."""
    root = assert_outside_git_repo(workspace)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "campaign.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"Phase 2 campaign is already running in {root}") from exc
        try:
            return _run_campaign_locked(
                source_catalog=source_catalog, workspace=root,
                pilot_workers=pilot_workers, verify_workers=verify_workers,
                concurrency=concurrency, materialize_workers=materialize_workers,
                max_mutation_rounds=max_mutation_rounds)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--pilot-workers", type=int, default=3)
    parser.add_argument("--verify-workers", type=int, default=16)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--materialize-workers", type=int, default=4)
    parser.add_argument("--max-mutation-rounds", type=int, default=8)
    args = parser.parse_args(argv)
    result = run_campaign(source_catalog=args.catalog, workspace=args.workspace,
                          pilot_workers=args.pilot_workers,
                          verify_workers=args.verify_workers,
                          concurrency=args.concurrency,
                          materialize_workers=args.materialize_workers,
                          max_mutation_rounds=args.max_mutation_rounds)
    print(json.dumps({key: result[key] for key in
                      ("status", "hardness_certified", "deficits", "scale_report")},
                     indent=2))
    return 0 if result["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
