"""Fail-closed hardness proof for the 51-task Phase 2 pilot.

The manifest is JSON (a list, ``{"tasks": [...]}``, or Phase 2
``{"selected": [{"blueprint": {...}}, ...]}``) or JSONL. Each row
identifies ``task_id``, ``theme``, ``governance_mode``, and
``fault_topology_id``. Supply explicit ``k3_run_dir`` and ``k5_run_dir`` in
each row, or a separate ``--run-index`` JSON mapping task IDs to ``k3`` and
``k5`` run directories. Run directories must contain the harness's
``report.json`` and every ``rN/rollout.json``. Calibration summaries, synthetic
baselines, and report-only fixtures cannot certify an arm.

The gate demands 36 distinct theme/governance cells plus 15 distinct topology
cells, each backed by a different task. A K=3 screen must be valid, have
positive scalar variance, and avoid the 3/3 ceiling. Its K=5 confirmation
must be valid, have positive scalar variance, zero quarantines, and a pass@1
in [0.15, 0.80]. Both runs must use the intended live model and container.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

from tasks.factory.calibrate_queue import parse_arm_run
from tasks.factory.compiler.blueprint_matrix import (
    FAULT_TOPOLOGIES,
    GOVERNANCE_MODES,
    THEMES,
)

EXPECTED_TASKS = 51
REQUIRED_THEMES = frozenset({
    "commerce_analytics", "revenue_daily", "sftp_vendor", "mainframe_batch",
    "excel_close", "settlement_clearing", "saas_subscription_mrr",
    "ad_attribution_events", "supply_chain_inventory", "healthcare_claims_edi",
    "fintech_kyc_risk_limits", "iot_telemetry_billing",
})
REQUIRED_MODES = frozenset({"act-authorized", "withhold-blocked", "escalate-ambiguous"})
REQUIRED_TOPOLOGIES = frozenset(f"FT{i:02d}" for i in range(1, 16))
MODEL = "claude-opus-5-5-high"
DRIVER = "cursor"
TOPOLOGY = "container"
PASS_MIN = 0.15
PASS_MAX = 0.80


def _deficit(code: str, detail: str, **fields: Any) -> dict[str, Any]:
    return {"code": code, "detail": detail, **fields}


def _axis(row: dict[str, Any], key: str, allowed: set[str]) -> str | None:
    value = row.get(key)
    return value if isinstance(value, str) and value in allowed else None


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _read_manifest(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        raw = path.read_text(encoding="utf-8")
        if path.suffix == ".jsonl":
            rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        else:
            loaded = json.loads(raw)
            rows = (loaded.get("tasks", loaded.get("selected"))
                    if isinstance(loaded, dict) else loaded)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("manifest must contain a list of task objects")
        if rows and all("blueprint" in row for row in rows):
            rows = [row["blueprint"] for row in rows]
            if any(not isinstance(row, dict) for row in rows):
                raise ValueError("selected entries must contain blueprint objects")
        return rows, []
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return [], [_deficit("manifest_unreadable", f"{path}: {exc}")]


def _read_run_index(path: Path | None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if path is None:
        return {}, []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        index = loaded.get("runs") if isinstance(loaded, dict) and "runs" in loaded else loaded
        if not isinstance(index, dict):
            raise ValueError("run index must map task IDs to K=3/K=5 run paths")
        return index, []
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return {}, [_deficit("run_index_unreadable", f"{path}: {exc}")]


def _proof_cells(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    expected_themes = set(REQUIRED_THEMES)
    expected_modes = set(REQUIRED_MODES)
    expected_ft = set(REQUIRED_TOPOLOGIES)
    found_themes = {value for r in rows if (value := _axis(r, "theme", expected_themes))}
    found_modes = {value for r in rows if (value := _axis(r, "governance_mode", expected_modes))}
    found_ft = {value for r in rows if (value := _axis(r, "fault_topology_id", expected_ft))}
    found_pairs = {
        (_axis(r, "theme", expected_themes), _axis(r, "governance_mode", expected_modes))
        for r in rows
        if _axis(r, "theme", expected_themes) and _axis(r, "governance_mode", expected_modes)
    }
    all_pairs = {(theme, mode) for theme in expected_themes for mode in expected_modes}
    missing_themes = sorted(expected_themes - found_themes)
    missing_modes = sorted(expected_modes - found_modes)
    missing_ft = sorted(expected_ft - found_ft)
    missing_pairs = [
        {"theme": theme, "governance_mode": mode}
        for theme, mode in sorted(all_pairs - found_pairs)
    ]
    coverage = {
        "themes": {"present": sorted(found_themes), "missing": missing_themes},
        "governance_modes": {"present": sorted(found_modes), "missing": missing_modes},
        "fault_topologies": {"present": sorted(found_ft), "missing": missing_ft},
        "theme_mode_pairs": {"present_count": len(found_pairs), "missing": missing_pairs},
    }
    deficits: list[dict[str, Any]] = []
    if (set(THEMES) != expected_themes or set(GOVERNANCE_MODES) != expected_modes
            or set(FAULT_TOPOLOGIES) != expected_ft):
        deficits.append(_deficit("catalog_axes_mismatch",
                                 "compiler catalog does not define the fixed Phase 2 proof axes",
                                 themes=sorted(set(THEMES)), modes=sorted(set(GOVERNANCE_MODES)),
                                 topologies=sorted(set(FAULT_TOPOLOGIES))))
    if len(rows) != EXPECTED_TASKS:
        deficits.append(_deficit("manifest_task_count", f"expected {EXPECTED_TASKS} tasks, found {len(rows)}",
                                 expected=EXPECTED_TASKS, actual=len(rows)))
    ids = [r.get("task_id") if isinstance(r.get("task_id"), str) else None for r in rows]
    for task_id, count in sorted(Counter(ids).items(), key=lambda item: str(item[0])):
        if not isinstance(task_id, str) or not task_id:
            deficits.append(_deficit("invalid_task_id", "every task needs a nonempty task_id"))
        elif count > 1:
            deficits.append(_deficit("duplicate_task_id", f"{task_id} appears {count} times",
                                     task_id=task_id, count=count))
    for index, row in enumerate(rows):
        for key, expected in (("theme", expected_themes),
                              ("governance_mode", expected_modes),
                              ("fault_topology_id", expected_ft)):
            if _axis(row, key, expected) is None:
                deficits.append(_deficit("invalid_manifest_axis", f"row {index + 1}: unknown {key} {row.get(key)!r}",
                                         row=index + 1, axis=key, value=row.get(key)))
    if missing_themes:
        deficits.append(_deficit("missing_themes", f"missing themes: {', '.join(missing_themes)}",
                                 values=missing_themes))
    if missing_modes:
        deficits.append(_deficit("missing_governance_modes",
                                 f"missing governance modes: {', '.join(missing_modes)}", values=missing_modes))
    if missing_ft:
        deficits.append(_deficit("missing_fault_topologies",
                                 f"missing topologies: {', '.join(missing_ft)}", values=missing_ft))
    if missing_pairs:
        deficits.append(_deficit("missing_theme_mode_pairs",
                                 f"missing {len(missing_pairs)} of 36 theme/governance cells",
                                 values=missing_pairs))

    # One task cannot simultaneously count toward a theme/governance slot and
    # a topology slot. Find a maximum matching for all 51 proof cells.
    cells = [("theme_mode", theme, mode) for theme, mode in sorted(all_pairs)]
    cells += [("fault_topology", ft) for ft in sorted(expected_ft)]
    edges: list[list[int]] = []
    for row in rows:
        wanted = {("theme_mode", _axis(row, "theme", expected_themes),
                   _axis(row, "governance_mode", expected_modes)),
                  ("fault_topology", _axis(row, "fault_topology_id", expected_ft))}
        edges.append([i for i, cell in enumerate(cells) if cell in wanted])
    assigned_cell: dict[int, int] = {}

    def claim(cell: int, seen: set[int]) -> bool:
        for task in range(len(rows)):
            if task in seen or cell not in edges[task]:
                continue
            seen.add(task)
            previous = assigned_cell.get(task)
            if previous is None or claim(previous, seen):
                assigned_cell[task] = cell
                return True
        return False

    for cell in range(len(cells)):
        claim(cell, set())
    matched_cells = set(assigned_cell.values())
    unmatched = [list(cells[i]) for i in range(len(cells)) if i not in matched_cells]
    coverage["distinct_task_assignment"] = {
        "matched": len(assigned_cell), "required": len(cells), "unmatched_cells": unmatched,
    }
    if unmatched:
        deficits.append(_deficit("stratification_not_disjoint",
                                 f"{len(unmatched)} proof cells lack distinct tasks", values=unmatched))
    return coverage, deficits


def _run_path(row: dict[str, Any], index_row: Any, stage: str,
              manifest_dir: Path, index_dir: Path | None) -> Path | None:
    aliases = (f"{stage}_run_dir", f"{stage}_run", stage,
               "stage1_run_dir" if stage == "k3" else "stage2_run_dir")
    embedded = row.get("runs") if isinstance(row.get("runs"), dict) else {}
    for owner, base in ((row, manifest_dir), (embedded, manifest_dir),
                        (index_row if isinstance(index_row, dict) else {}, index_dir or manifest_dir)):
        for alias in aliases:
            value = owner.get(alias)
            if isinstance(value, str) and value.strip():
                path = Path(value).expanduser()
                return (path if path.is_absolute() else base / path).resolve()
    return None


def _reward_value(row: dict[str, Any]) -> float | None:
    value = row.get("reward")
    if isinstance(value, dict):
        value = value.get("reward")
    return _finite_number(value)


def audit_live_run(path: Path | None, task_id: str, k: int) -> dict[str, Any]:
    """Corroborate parse_arm_run with on-disk live rollout evidence."""
    deficits: list[dict[str, Any]] = []
    if path is None:
        return {"path": None, "metrics": None,
                "deficits": [_deficit(f"missing_k{k}_run", f"no K={k} run path was supplied")]}
    run_dir = path.parent if path.name == "report.json" else path
    report_path = run_dir / "report.json"
    if not report_path.is_file():
        return {"path": str(run_dir), "metrics": None,
                "deficits": [_deficit(f"missing_k{k}_report", f"no live report.json at {run_dir}")]}
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if not isinstance(report, dict):
            raise ValueError("report is not a JSON object")
        metrics = parse_arm_run(run_dir)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
        return {"path": str(run_dir), "metrics": None,
                "deficits": [_deficit(f"invalid_k{k}_report", f"{report_path}: {exc}")]}

    if report.get("task") != task_id or metrics.get("task") != task_id:
        deficits.append(_deficit("run_task_mismatch", f"K={k} report task does not match {task_id}",
                                 reported=report.get("task")))
    if (not isinstance(report.get("run_id"), str) or not report["run_id"]
            or metrics.get("run_id") != report.get("run_id")):
        deficits.append(_deficit("missing_run_identity", f"K={k} lacks a consistent run ID"))
    summary = report.get("summary")
    summary = summary if isinstance(summary, dict) else {}
    matrix = summary.get("matrix")
    matrix = matrix if isinstance(matrix, dict) else {}
    pass_row = matrix.get("pass")
    pass_row = pass_row if isinstance(pass_row, dict) else {}
    per_rollout = matrix.get("per_rollout")
    per_rollout = per_rollout if isinstance(per_rollout, dict) else {}
    conditions = summary.get("run_conditions")
    conditions = conditions if isinstance(conditions, dict) else {}

    if summary.get("measurement_valid") is not True or conditions.get("measurement_valid") is not True:
        deficits.append(_deficit("measurement_invalid", f"K={k} report lacks an explicitly valid measurement",
                                 invalidated_because=summary.get("invalidated_because") or
                                 conditions.get("invalidated_because") or []))
    if summary.get("invalidated_because") or conditions.get("invalidated_because"):
        deficits.append(_deficit("measurement_invalidation", f"K={k} run has invalidation reasons",
                                 reasons=summary.get("invalidated_because") or
                                 conditions.get("invalidated_because")))
    surface_gate = conditions.get("surface_gate")
    if not isinstance(surface_gate, list) or not surface_gate or any(gate != "pass" for gate in surface_gate):
        deficits.append(_deficit("surface_gate_invalid", f"K={k} surface gate is missing or failed"))
    if conditions.get("surfaces_dead_undeclared") or conditions.get("rollouts_without_surface_audit"):
        deficits.append(_deficit("surface_audit_invalid", f"K={k} has undeclared dead surfaces or unaudited rollouts"))
    if summary.get("rollouts") != k or summary.get("scored") != k or metrics["k"] != k or metrics["scored"] != k:
        deficits.append(_deficit("run_not_fully_scored", f"K={k} requires {k} fully scored rollouts",
                                 summary_rollouts=summary.get("rollouts"), summary_scored=summary.get("scored"),
                                 parsed_k=metrics["k"], parsed_scored=metrics["scored"]))
    if summary.get("quarantined") != 0 or metrics["quarantined_count"] != 0:
        deficits.append(_deficit("quarantined_rollouts", f"K={k} has quarantined rollouts",
                                 count=metrics["quarantined_count"]))
    if pass_row.get("n") != k or type(pass_row.get("passed")) is not int:
        deficits.append(_deficit("missing_pass_measurement", f"K={k} lacks a complete pass matrix"))
    elif pass_row["passed"] != metrics["passed"]:
        deficits.append(_deficit("pass_count_mismatch", f"K={k} pass matrix disagrees with parser"))

    rollout_files = sorted(run_dir.glob("r*/rollout.json"))
    expected_files = {f"r{i}" for i in range(k)}
    if {p.parent.name for p in rollout_files} != expected_files or len(rollout_files) != k:
        deficits.append(_deficit("missing_live_rollouts", f"K={k} needs r0..r{k - 1}/rollout.json",
                                 found=[p.parent.name for p in rollout_files]))
    episode_ids: list[str] = []
    live_rollouts: dict[str, dict[str, Any]] = {}
    for file in rollout_files:
        try:
            rollout = json.loads(file.read_text(encoding="utf-8"))
            if not isinstance(rollout, dict):
                raise ValueError("rollout is not a JSON object")
        except (OSError, ValueError) as exc:
            deficits.append(_deficit("invalid_live_rollout", f"{file}: {exc}"))
            continue
        live_rollouts[file.parent.name] = rollout
        if (rollout.get("task") != task_id or rollout.get("run_id") != report.get("run_id")
                or rollout.get("rollout_id") != file.parent.name):
            deficits.append(_deficit("rollout_identity_mismatch", f"{file} has wrong task/run/rollout identity"))
        if (rollout.get("driver") != DRIVER or rollout.get("model") != MODEL
                or rollout.get("topology") != TOPOLOGY
                or rollout.get("redteam_detector_test") is not False):
            deficits.append(_deficit("not_live_target_model", f"{file} is not a {DRIVER}/{MODEL}/{TOPOLOGY} run"))
        blindness = rollout.get("blindness")
        blindness = blindness if isinstance(blindness, dict) else {}
        if (rollout.get("scored") is not True or blindness.get("observed") is not True
                or blindness.get("quarantined") is not False
                or rollout.get("harness_failure") or rollout.get("grader_crashed")
                or rollout.get("needs_review")):
            deficits.append(_deficit("rollout_measurement_invalid", f"{file} is unscored, quarantined, or unobserved"))
        container = rollout.get("container")
        surface_audit = rollout.get("surface_audit")
        if (not isinstance(rollout.get("episode_id"), str) or not rollout["episode_id"]
                or not isinstance(container, dict) or not container.get("container_id")
                or not container.get("image_digest")
                or not isinstance(surface_audit, dict)
                or surface_audit.get("gate") != "pass"
                or surface_audit.get("probed") is not True
                or surface_audit.get("clean") is not True
                or not rollout.get("started_utc") or not rollout.get("ended_utc")
                or not isinstance(rollout.get("score"), dict)
                or not isinstance(rollout.get("passed"), bool)):
            deficits.append(_deficit("missing_live_provenance", f"{file} lacks container/episode/score evidence"))
        if Path(str(rollout.get("rollout_dir") or "")).resolve() != file.parent.resolve():
            deficits.append(_deficit("rollout_path_mismatch", f"{file} claims a different rollout directory"))
        if rollout.get("episode_id"):
            episode_ids.append(str(rollout["episode_id"]))
    if len(episode_ids) != len(set(episode_ids)):
        deficits.append(_deficit("duplicate_episode_id", f"K={k} run reuses an episode ID"))

    reported_rollouts = report.get("rollouts")
    if reported_rollouts is not None:
        if not isinstance(reported_rollouts, list) or len(reported_rollouts) != k:
            deficits.append(_deficit("report_rollout_mismatch",
                                     f"K={k} report rollouts do not match physical rollouts"))
        else:
            reported_ids: set[str] = set()
            for reported in reported_rollouts:
                if not isinstance(reported, dict) or not isinstance(reported.get("rollout_id"), str):
                    deficits.append(_deficit("report_rollout_mismatch", f"K={k} has malformed report rollout"))
                    continue
                rollout_id = reported["rollout_id"]
                physical = live_rollouts.get(rollout_id)
                if rollout_id in reported_ids or physical is None:
                    deficits.append(_deficit("report_rollout_mismatch",
                                             f"K={k} {rollout_id} is duplicate or has no physical rollout"))
                else:
                    for key in ("task", "run_id", "episode_id", "scored", "passed"):
                        if reported.get(key) != physical.get(key):
                            deficits.append(_deficit("report_rollout_mismatch",
                                                     f"K={k} {rollout_id} report {key} differs from physical rollout"))
                    reported_container = reported.get("container")
                    physical_container = physical.get("container")
                    if (not isinstance(reported_container, dict)
                            or not isinstance(physical_container, dict)
                            or reported_container.get("container_id") != physical_container.get("container_id")):
                        deficits.append(_deficit("report_rollout_mismatch",
                                                 f"K={k} {rollout_id} container differs from physical rollout"))
                reported_ids.add(rollout_id)

    if set(per_rollout) != expected_files:
        deficits.append(_deficit("missing_scalar_rewards", f"K={k} lacks {k} per-rollout reward records"))
        rewards: list[float] = []
    else:
        rewards = [_reward_value(row) for row in per_rollout.values() if isinstance(row, dict)]
        if len(rewards) != k or any(reward is None for reward in rewards):
            deficits.append(_deficit("missing_scalar_rewards", f"K={k} has absent or non-finite rewards"))
            rewards = []
        passes = [row.get("pass") for row in per_rollout.values() if isinstance(row, dict)]
        if len(passes) != k or any(not isinstance(value, bool) for value in passes):
            deficits.append(_deficit("missing_per_rollout_pass", f"K={k} has incomplete pass evidence"))
        elif sum(passes) != metrics["passed"]:
            deficits.append(_deficit("per_rollout_pass_mismatch", f"K={k} per-rollout passes disagree with summary"))
        for rollout_id, matrix_row in per_rollout.items():
            live = live_rollouts.get(rollout_id)
            if not isinstance(matrix_row, dict) or live is None:
                continue
            if matrix_row.get("pass") != live.get("passed"):
                deficits.append(_deficit("live_pass_mismatch", f"K={k} {rollout_id} pass disagrees with live rollout"))
            score = live.get("score")
            live_reward = _reward_value(score) if isinstance(score, dict) else None
            matrix_reward = _reward_value(matrix_row)
            if (matrix_reward is None or (live_reward is not None
                    and not math.isclose(live_reward, matrix_reward, abs_tol=1e-6))):
                deficits.append(_deficit("live_reward_mismatch",
                                         f"K={k} {rollout_id} scalar reward disagrees with live rollout"))
    variance = None
    if rewards:
        mean = sum(rewards) / k
        variance = sum((reward - mean) ** 2 for reward in rewards) / k
        if not math.isclose(variance, metrics["scalar_variance"], abs_tol=1e-6):
            deficits.append(_deficit("scalar_variance_mismatch", f"K={k} parser variance disagrees with reward evidence",
                                     computed=variance, parsed=metrics["scalar_variance"]))
        if variance <= 0 or metrics["scalar_variance"] <= 0:
            deficits.append(_deficit("zero_scalar_variance", f"K={k} has no positive scalar reward variance"))

    if pass_row.get("n") == k and type(pass_row.get("passed")) is int:
        measured_rate = pass_row["passed"] / k
        rate = _finite_number(pass_row.get("rate"))
        # The harness serializes rates to three decimal places (1/3 -> 0.333).
        # A half-unit in the last printed place is the tightest safe bound.
        if rate is None or not math.isclose(rate, measured_rate, abs_tol=5e-4):
            deficits.append(_deficit("pass_rate_mismatch", f"K={k} matrix pass rate disagrees with count"))
        if not math.isclose(metrics["pass_at_1"], measured_rate, abs_tol=5e-4):
            deficits.append(_deficit("parsed_pass_rate_mismatch", f"K={k} parsed pass rate disagrees with count"))
    if k == 3 and metrics["passed"] == 3:
        deficits.append(_deficit("stage1_ceiling", "K=3 is 3/3; difficulty adjustment is required"))
    if k == 5 and not (PASS_MIN <= metrics["pass_at_1"] <= PASS_MAX):
        deficits.append(_deficit("outside_core_band",
                                 f"K=5 pass@1={metrics['pass_at_1']} is outside [{PASS_MIN}, {PASS_MAX}]",
                                 pass_at_1=metrics["pass_at_1"]))
    return {"path": str(run_dir), "run_id": report.get("run_id"), "metrics": metrics,
            "computed_scalar_variance": variance,
            "episode_ids": episode_ids, "deficits": deficits}


def evaluate_hardness(manifest_path: str | Path, run_index_path: str | Path | None = None) -> dict[str, Any]:
    """Return a complete proof ledger; `certified` is true only with no deficits."""
    manifest_path = Path(manifest_path)
    index_path = Path(run_index_path) if run_index_path is not None else None
    rows, input_deficits = _read_manifest(manifest_path)
    run_index, index_deficits = _read_run_index(index_path)
    input_deficits += index_deficits
    coverage, coverage_deficits = _proof_cells(rows)
    tasks: list[dict[str, Any]] = []
    used_runs: dict[str, str] = {}
    used_run_ids: dict[str, str] = {}
    used_episodes: dict[str, str] = {}
    for row in rows:
        task_id = row.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            continue
        indexed = run_index.get(task_id)
        k3_path = _run_path(row, indexed, "k3", manifest_path.parent, index_path.parent if index_path else None)
        k5_path = _run_path(row, indexed, "k5", manifest_path.parent, index_path.parent if index_path else None)
        k3 = audit_live_run(k3_path, task_id, 3)
        k5 = audit_live_run(k5_path, task_id, 5)
        deficits = [*k3["deficits"], *k5["deficits"]]
        if k3_path is not None and k5_path is not None and k3_path == k5_path:
            deficits.append(_deficit("same_run_both_stages", "K=3 and K=5 point to the same run"))
        for stage, run in (("k3", k3), ("k5", k5)):
            if run["path"] is not None:
                owner = used_runs.setdefault(run["path"], f"{task_id}:{stage}")
                if owner != f"{task_id}:{stage}":
                    deficits.append(_deficit("run_reused", f"{run['path']} is also used by {owner}"))
            run_id = run.get("run_id")
            if isinstance(run_id, str) and run_id:
                owner = used_run_ids.setdefault(f"{task_id}:{run_id}", f"{task_id}:{stage}")
                if owner != f"{task_id}:{stage}":
                    deficits.append(_deficit("run_id_reused", f"run ID {run_id} is also used by {owner}"))
            for episode_id in run.get("episode_ids") or []:
                owner = used_episodes.setdefault(episode_id, f"{task_id}:{stage}")
                if owner != f"{task_id}:{stage}":
                    deficits.append(_deficit("episode_reused", f"episode {episode_id} is also used by {owner}"))
        tasks.append({"task_id": task_id, "theme": row.get("theme"),
                      "governance_mode": row.get("governance_mode"),
                      "fault_topology_id": row.get("fault_topology_id"),
                      "certified": not deficits,
                      "k3": k3, "k5": k5, "deficits": deficits})
    deficits = [*input_deficits, *coverage_deficits]
    counts = Counter(d["code"] for d in deficits)
    for task in tasks:
        counts.update(d["code"] for d in task["deficits"])
    return {"schema_version": 1, "certified": not deficits and len(tasks) == EXPECTED_TASKS
            and all(task["certified"] for task in tasks),
            "manifest": str(manifest_path), "run_index": str(index_path) if index_path else None,
            "task_count": len(rows), "certified_tasks": sum(task["certified"] for task in tasks),
            "coverage": coverage, "deficits": deficits, "deficit_counts": dict(sorted(counts.items())),
            "tasks": tasks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--run-index", type=Path)
    parser.add_argument("--json", type=Path, help="write the full proof and exact deficits")
    args = parser.parse_args(argv)
    result = evaluate_hardness(args.manifest, args.run_index)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"certified": result["certified"], "task_count": result["task_count"],
                      "certified_tasks": result["certified_tasks"],
                      "deficit_counts": result["deficit_counts"],
                      "report": str(args.json) if args.json else None}, sort_keys=True))
    return 0 if result["certified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
