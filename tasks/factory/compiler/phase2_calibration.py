"""Run the Phase 2 51-task mechanical and live calibration gates."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from tasks.factory.calibrate_queue import (classify_stage1_arm, classify_stage2_arm,
                                           run_two_stage_calibration)
from tasks.factory.compiler.phase2_hardness import audit_live_run, evaluate_hardness
from tasks.factory.compiler.phase2_readiness import inspect_phase2_readiness
from tasks.factory.compiler.phase2_scale import (
    DEFAULT_CATALOG,
    DEFAULT_PILOT,
    _hardness_deficits,
    _pilot_rows,
    _pilot_package_provenance,
    _pilot_stage0,
    _provenance_matches,
    _read_catalog,
    _write,
)

DEFAULT_STAGE0 = Path("/home/ubuntu/alkera_factory_1017/phase2_stage0_51.json")
DEFAULT_RUNS = Path("/home/ubuntu/alkera_factory_1017/runs_phase2")
DEFAULT_INDEX = DEFAULT_PILOT / "phase2_run_index.json"
DEFAULT_REPORT = DEFAULT_PILOT / "phase2_calibration_report.json"
DIFFICULTY_DEFICITS = frozenset({"stage1_ceiling", "outside_core_band",
                                 "zero_scalar_variance"})


def _structural_deficits(task_id: str, path: str | None, k: int) -> list[dict]:
    run = audit_live_run(Path(path) if path else None, task_id, k)
    return [item for item in run["deficits"]
            if item["code"] not in DIFFICULTY_DEFICITS]


def _read_index(path: Path) -> dict:
    if not path.is_file():
        return {"runs": {}}
    document = json.loads(path.read_text(encoding="utf-8"))
    runs = document.get("runs") if isinstance(document, dict) else None
    if not isinstance(runs, dict):
        raise ValueError(f"invalid run index: {path}")
    return document


def _run_is_present(path: str | None) -> bool:
    if not isinstance(path, str):
        return False
    run = Path(path)
    return (run / "report.json").is_file() and any(run.glob("r*/rollout.json"))


def run_pilot_calibration(*, catalog: Path = DEFAULT_CATALOG,
                          selection: Path = DEFAULT_PILOT / "phase2_selection.json",
                          pilot_dir: Path = DEFAULT_PILOT,
                          stage0_path: Path = DEFAULT_STAGE0,
                          runs_dir: Path = DEFAULT_RUNS,
                          run_index_path: Path = DEFAULT_INDEX,
                          report_path: Path = DEFAULT_REPORT,
                          verify_workers: int = 16,
                          concurrency: int = 3,
                          stage0_only: bool = False) -> dict:
    if verify_workers < 1 or concurrency < 1:
        raise ValueError("worker counts must be positive")
    readiness = inspect_phase2_readiness()
    entries, catalog_report = _read_catalog(catalog)
    rows, selection_deficits = _pilot_rows(selection, catalog, entries)
    package_failures = [row["task_id"] for row in rows
                        if not _provenance_matches(pilot_dir / row["task_id"], row)]
    deficits = [*readiness["blockers"], *selection_deficits]
    if not catalog_report["valid"]:
        deficits.extend(catalog_report["errors"])
    if package_failures:
        deficits.append(f"{len(package_failures)} pilot tasks missing or provenance-invalid")
    report = {
        "updated_utc": datetime.now(timezone.utc).isoformat(),
        "status": "blocked_preflight" if deficits else "ready_for_stage0",
        "selection": str(selection),
        "pilot_dir": str(pilot_dir),
        "pilot_selected": len(rows),
        "package_failures": package_failures,
        "readiness": readiness,
        "deficits": deficits,
        "stage0": str(stage0_path),
        "run_index": str(run_index_path),
        "hardness_certified": False,
        "hardness_deficits": [],
    }
    _write(report_path, report)
    if deficits:
        return report

    stage0_deficits = _pilot_stage0(stage0_path, rows, pilot_dir)
    if stage0_deficits:
        from tasks.factory.verify_all import verify_all
        stage0 = verify_all(pilot_dir, workers=verify_workers,
                            phase2=True, strict_phase2=True)
        stage0["phase2_package_provenance"] = _pilot_package_provenance(pilot_dir, rows)
        _write(stage0_path, stage0)
        stage0_deficits = _pilot_stage0(stage0_path, rows, pilot_dir)
    if stage0_deficits:
        report["status"] = "stage0_failed"
        report["deficits"] = stage0_deficits
        _write(report_path, report)
        return report
    report["status"] = "stage0_passed"
    _write(report_path, report)
    if stage0_only:
        return report

    index = _read_index(run_index_path)
    indexed = index["runs"]
    pending = []
    for row in rows:
        task_id = row["task_id"]
        seal = json.loads((pilot_dir / task_id / "tests/phase2_provenance.json")
                          .read_text(encoding="utf-8"))
        runs = indexed.get(task_id) or {}
        if (runs.get("fingerprint") != seal["fingerprint"] or
                runs.get("task_integrity") != seal["task_integrity"]):
            runs = {}
            indexed[task_id] = runs
        # Saturated K=3 arms are retained for diagnosis. A source or blueprint
        # change invalidates their package provenance and forces rematerialization.
        k3_present = _run_is_present(runs.get("k3"))
        k5_present = _run_is_present(runs.get("k5"))
        k3_structural = _structural_deficits(task_id, runs.get("k3"), 3)
        k5_structural = (_structural_deficits(task_id, runs.get("k5"), 5)
                         if runs.get("k5") else [])
        try:
            stage1 = classify_stage1_arm(Path(runs["k3"])) if k3_present else {}
        except (OSError, ValueError, KeyError):
            stage1 = {"classification": "invalid", "admit_to_stage2": False}
        try:
            stage2 = classify_stage2_arm(Path(runs["k5"])) if k5_present else {}
        except (OSError, ValueError, KeyError):
            stage2 = {"measurement_valid": False}
        k5_measurement_invalid = bool(k5_present and (
            stage2.get("measurement_valid") is False
            or stage2.get("quarantined_count", 0) > 0
            or stage2.get("scored", 0) < 5))
        if (not k3_present or k3_structural or stage1.get("classification") == "invalid"
                or (runs.get("k5") and not k5_present)
                or (stage1.get("admit_to_stage2") and not k5_present)
                or k5_measurement_invalid or k5_structural):
            pending.append(f"{task_id}:default")

    summary = None
    if pending:
        summary = run_two_stage_calibration(
            pending, tasks_dir=pilot_dir, out_dir=runs_dir,
            driver="cursor", model="claude-opus-5-5-high", topology="container",
            concurrency=concurrency)
        for entry, arm in summary["stage1"]["arms"].items():
            task_id = entry.split(":", 1)[0]
            if arm.get("run_dir"):
                indexed.setdefault(task_id, {})["k3"] = arm["run_dir"]
        for entry, arm in summary["stage2"]["arms"].items():
            task_id = entry.split(":", 1)[0]
            if arm.get("run_dir"):
                indexed.setdefault(task_id, {})["k5"] = arm["run_dir"]
        for row in rows:
            task_id = row["task_id"]
            if task_id not in indexed:
                continue
            seal = json.loads((pilot_dir / task_id / "tests/phase2_provenance.json")
                              .read_text(encoding="utf-8"))
            indexed[task_id]["fingerprint"] = seal["fingerprint"]
            indexed[task_id]["task_integrity"] = seal["task_integrity"]
        index["model"] = "claude-opus-5-5-high"
        index["driver"] = "cursor"
        index["topology"] = "container"
        index["updated_utc"] = datetime.now(timezone.utc).isoformat()
        _write(run_index_path, index)

    proof = evaluate_hardness(selection, run_index_path)
    proof_path = report_path.with_name("phase2_hardness_proof.json")
    _write(proof_path, proof)
    adjustment_queue = []
    for row in rows:
        task_id = row["task_id"]
        k3 = (indexed.get(task_id) or {}).get("k3")
        if not _run_is_present(k3):
            continue
        if _structural_deficits(task_id, k3, 3):
            continue
        diagnosis = classify_stage1_arm(Path(k3))
        if diagnosis["classification"] in {"saturated_floor", "saturated_ceiling", "invalid"}:
            adjustment_queue.append({
                "task_id": task_id,
                "theme": row["theme"],
                "governance_mode": row["governance_mode"],
                "fault_topology_id": row["fault_topology_id"],
                "hazards": row.get("hazards"),
                "classification": diagnosis["classification"],
                "prescription": diagnosis["prescription"],
            })
    report["hardness_certified"] = bool(proof["certified"])
    report["hardness_proof"] = str(proof_path)
    report["hardness_deficits"] = _hardness_deficits(proof)
    report["hardness_deficit_counts"] = proof.get("deficit_counts", {})
    report["hardness_certified_tasks"] = proof.get("certified_tasks", 0)
    report["adjustment_queue"] = adjustment_queue
    report["calibration_summary"] = summary.get("summary_path") if summary else None
    report["pending_run_count"] = len(pending)
    report["status"] = "certified" if proof["certified"] else "needs_adjustment"
    _write(report_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--selection", type=Path,
                        default=DEFAULT_PILOT / "phase2_selection.json")
    parser.add_argument("--pilot-dir", type=Path, default=DEFAULT_PILOT)
    parser.add_argument("--stage0", type=Path, default=DEFAULT_STAGE0)
    parser.add_argument("--runs", type=Path, default=DEFAULT_RUNS)
    parser.add_argument("--run-index", type=Path, default=DEFAULT_INDEX)
    parser.add_argument("--json", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--verify-workers", type=int, default=16)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--stage0-only", action="store_true")
    args = parser.parse_args()
    result = run_pilot_calibration(catalog=args.catalog, selection=args.selection,
                                   pilot_dir=args.pilot_dir, stage0_path=args.stage0,
                                   runs_dir=args.runs, run_index_path=args.run_index,
                                   report_path=args.json, verify_workers=args.verify_workers,
                                   concurrency=args.concurrency,
                                   stage0_only=args.stage0_only)
    print(json.dumps({key: result[key] for key in
                      ("status", "pilot_selected", "hardness_certified", "deficits")}, indent=2))
    return 0 if result["status"] in {"certified", "stage0_passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
