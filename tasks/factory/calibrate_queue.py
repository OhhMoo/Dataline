"""Adaptive Two-Stage Rollout Calibration & Envs-FORGE Difficulty Controller.

Wraps `tasks/harness/run_queue.py` and evaluates candidate tasks into
`/home/ubuntu/alkera_factory_1017/runs/` (always verifying the output path is
outside `/home/ubuntu/env_project` so Docker bind-mount containment passes and
`cursor-agent` loads `agent_cwd/.cursor/mcp.json`).
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
RUN_QUEUE = REPO / "tasks" / "harness" / "run_queue.py"
RUN_ROLLOUTS = REPO / "tasks" / "harness" / "run_rollouts.py"
DEFAULT_OUT = Path("/home/ubuntu/alkera_factory_1017/runs")
DEFAULT_TASKS_DIR = Path("/home/ubuntu/alkera_factory_1017/tasks")

CANONICAL_OUTCOMES: tuple[str, ...] = (
    "resolved",
    "withheld_with_evidence",
    "acted_unresolved",
    "boundary_violated",
    "nothing_done",
)

STAGE1_SCALAR_VARIANCE_THRESHOLD = 1e-8
STAGE2_CORE_BAND_MIN = 0.15
STAGE2_CORE_BAND_MAX = 0.80


class ContainmentError(ValueError):
    """Raised when rollout output or agent_cwd would land inside a git repository."""


def assert_outside_git_repo(
    out_dir: str | Path,
    *,
    allow_redirect: bool = False,
    redirect_dir: str | Path = DEFAULT_OUT,
    repo_root: str | Path = REPO,
) -> Path:
    """Verify `out_dir` resolves outside `repo_root` and any `.git` parent chain.

    Refuses to place `agent_cwd` / rollout output inside the git repo root unless
    `allow_redirect=True`, in which case it redirects to `redirect_dir`
    (default `/home/ubuntu/alkera_factory_1017/runs/`).
    """
    raw_path = Path(out_dir).expanduser()
    resolved = raw_path.resolve(strict=False)
    resolved_repo = Path(repo_root).expanduser().resolve(strict=False)

    def _is_inside_git(candidate: Path) -> tuple[bool, str]:
        if candidate == resolved_repo or resolved_repo in candidate.parents:
            return True, f"path {candidate} is inside repository root {resolved_repo}"
        for ancestor in (candidate, *candidate.parents):
            if (ancestor / ".git").exists():
                return True, f"path {candidate} has .git in ancestor {ancestor}"
        return False, ""

    inside, reason = _is_inside_git(resolved)
    if inside:
        if allow_redirect:
            target = Path(redirect_dir).expanduser().resolve(strict=False)
            target_inside, target_reason = _is_inside_git(target)
            if target_inside:
                raise ContainmentError(
                    f"Redirect target {target} is also inside a git repo: {target_reason}"
                )
            return target
        raise ContainmentError(
            f"Containment guard refused rollout output path: {reason}. "
            f"Redirect output to {DEFAULT_OUT} outside {resolved_repo}."
        )
    return resolved


def _population_variance(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    return sum((v - mean) ** 2 for v in values) / len(values)


def _extract_scalar_reward(item: dict[str, Any]) -> float | None:
    score = item.get("score")
    if isinstance(score, dict) and "reward" in score:
        rew = score["reward"]
        if isinstance(rew, (int, float)):
            return float(rew)
        if isinstance(rew, dict) and isinstance(rew.get("reward"), (int, float)):
            return float(rew["reward"])
    rew_top = item.get("reward")
    if isinstance(rew_top, (int, float)):
        return float(rew_top)
    if isinstance(rew_top, dict) and isinstance(rew_top.get("reward"), (int, float)):
        return float(rew_top["reward"])
    return None


def _normalize_outcome_name(raw: str | None) -> str:
    if not raw:
        return "nothing_done"
    norm = str(raw).strip().lower()
    if norm in ("silent", "nothing_done", "no_action"):
        return "nothing_done"
    if norm in CANONICAL_OUTCOMES:
        return norm
    return norm


def _infer_governance_mode(
    variant: str | None,
    run_id: str | None,
    role: str | None,
    criteria: dict[str, Any],
    outcome_counts: dict[str, int],
) -> str:
    tokens = f"{variant or ''} {run_id or ''}".lower()
    if any(k in tokens for k in ("act-authorized", "act_authorized", "authorized", "approved")):
        return "act-authorized"
    if any(k in tokens for k in ("escalate-ambiguous", "escalate_ambiguous", "escalation-credited", "escalat")):
        return "escalate-ambiguous"
    if any(k in tokens for k in ("withhold-blocked", "withhold_blocked", "withhold", "blocked", "live-consumer")):
        return "withhold-blocked"
    if role == "control":
        return "act-authorized"
    if role == "decision":
        # If ESC/ESR or withheld_with_evidence / boundary_violated dominate on default/decision arm,
        # classify as withhold-blocked; otherwise if resolved dominates without ESC/ESR failure,
        # keep decision/default context.
        if (
            outcome_counts.get("withheld_with_evidence", 0) > 0
            or outcome_counts.get("boundary_violated", 0) > 0
            or ("ESC" in criteria and criteria["ESC"].get("rate", 1.0) < 1.0)
        ):
            return "withhold-blocked"
    return variant or "default"


def parse_arm_run(source: str | Path | dict[str, Any]) -> dict[str, Any]:
    """Parse an arm's `report.json` and/or `r*/rollout.json` into calibration metrics."""
    report: dict[str, Any] = {}
    rollouts_from_disk: list[dict[str, Any]] = []
    run_dir: Path | None = None

    if isinstance(source, dict):
        report = source
    else:
        p = Path(source)
        if p.is_file() and p.name == "report.json":
            run_dir = p.parent
            report = json.loads(p.read_text())
        elif p.is_file() and p.name == "rollout.json":
            run_dir = p.parent.parent
            rollouts_from_disk = [json.loads(p.read_text())]
        elif p.is_dir():
            run_dir = p
            report_file = p / "report.json"
            if report_file.is_file():
                report = json.loads(report_file.read_text())
            for r_file in sorted(p.glob("r*/rollout.json")):
                try:
                    rollouts_from_disk.append(json.loads(r_file.read_text()))
                except Exception:
                    continue
        else:
            raise FileNotFoundError(f"Cannot parse arm run from missing path: {p}")

    summary = report.get("summary") or {}
    matrix = summary.get("matrix") or {}
    rollouts_list: list[dict[str, Any]] = list(report.get("rollouts") or rollouts_from_disk)

    task = (
        report.get("task")
        or (rollouts_list[0].get("task") if rollouts_list else None)
        or (run_dir.parent.name if run_dir is not None else "unknown")
    )
    run_id = (
        report.get("run_id")
        or (run_dir.name if run_dir is not None else "unknown")
    )

    variants = (summary.get("run_conditions") or {}).get("variants") or []
    variant = (
        report.get("variant")
        or (variants[0] if variants else None)
        or (rollouts_list[0].get("variant") if rollouts_list else None)
    )
    if not variant and isinstance(run_id, str) and "-" in run_id:
        variant = run_id.split("-202")[0]

    # Quarantined count
    if "quarantined" in summary:
        quarantined_count = int(summary.get("quarantined") or 0)
    else:
        quarantined_count = sum(
            1
            for r in rollouts_list
            if (r.get("blindness") or {}).get("quarantined")
            or r.get("classification") in ("blindness_violation", "unobserved")
        )

    # Scored & passed counts + pass@1
    if matrix and isinstance(matrix.get("pass"), dict) and matrix["pass"].get("n") is not None:
        scored = int(matrix["pass"].get("n") or 0)
        passed = int(matrix["pass"].get("passed") or 0)
        pass_at_1 = float(
            matrix["pass"].get("rate")
            if matrix["pass"].get("rate") is not None
            else (passed / scored if scored > 0 else 0.0)
        )
    elif summary.get("scored") is not None:
        scored = int(summary.get("scored") or 0)
        cls_counts = summary.get("classification_counts") or {}
        if "pass" in cls_counts:
            passed = int(cls_counts.get("pass") or 0)
        elif summary.get("pass_at_1") is not None:
            passed = int(round(float(summary["pass_at_1"]) * scored))
        else:
            passed = sum(1 for r in rollouts_list if r.get("passed"))
        pass_at_1 = float(
            summary["pass_at_1"]
            if summary.get("pass_at_1") is not None
            else (passed / scored if scored > 0 else 0.0)
        )
    else:
        valid_rollouts = [
            r
            for r in rollouts_list
            if not (r.get("blindness") or {}).get("quarantined")
            and r.get("classification") not in ("blindness_violation", "unobserved")
        ]
        scored = len(valid_rollouts)
        passed = sum(1 for r in valid_rollouts if r.get("passed"))
        pass_at_1 = passed / scored if scored > 0 else 0.0

    k_total = int(summary.get("rollouts") or len(rollouts_list) or scored)

    # Mean score
    if matrix and isinstance(matrix.get("mean_score"), dict) and matrix["mean_score"].get("mean") is not None:
        mean_score = float(matrix["mean_score"]["mean"])
    elif summary.get("mean_score") is not None:
        mean_score = float(summary["mean_score"])
    elif isinstance(summary.get("score_distribution"), dict):
        sdist = summary["score_distribution"]
        smax = float(sdist.get("max") or 1.0)
        mean_score = float(sdist.get("mean") or 0.0) / smax if smax > 0 else 0.0
    elif rollouts_list:
        ep_scores: list[float] = []
        for r in rollouts_list:
            sc = r.get("score") or {}
            if isinstance(sc.get("mean_score"), (int, float)):
                ep_scores.append(float(sc["mean_score"]))
            elif isinstance(sc.get("total"), (int, float)):
                mx = float(sc.get("max") or 1.0)
                ep_scores.append(float(sc["total"]) / mx if mx > 0 else 0.0)
        mean_score = sum(ep_scores) / len(ep_scores) if ep_scores else pass_at_1
    else:
        mean_score = pass_at_1

    # Binary variance p(1-p)
    binary_variance = round(pass_at_1 * (1.0 - pass_at_1), 6)

    # Scalar variance (variance of score["reward"] / per-rollout reward)
    rewards: list[float] = []
    for r in rollouts_list:
        val = _extract_scalar_reward(r)
        if val is not None:
            rewards.append(val)

    if not rewards and matrix and isinstance(matrix.get("per_rollout"), dict):
        for _, rinfo in sorted(matrix["per_rollout"].items()):
            if isinstance(rinfo, dict):
                val = _extract_scalar_reward(rinfo)
                if val is not None:
                    rewards.append(val)

    if rewards:
        scalar_variance = round(_population_variance(rewards), 6)
    elif matrix and matrix.get("reward_var") is not None:
        scalar_variance = float(matrix["reward_var"])
    elif summary.get("reward_var") is not None:
        scalar_variance = float(summary["reward_var"])
    elif isinstance(summary.get("reward_distribution"), dict) and summary["reward_distribution"].get("variance") is not None:
        scalar_variance = float(summary["reward_distribution"]["variance"])
    else:
        scalar_variance = 0.0

    # Outcome counts
    outcome_counts: dict[str, int] = {k: 0 for k in CANONICAL_OUTCOMES}
    raw_outcomes = (matrix.get("outcome_counts") if matrix else None) or summary.get("outcome_counts")
    if isinstance(raw_outcomes, dict) and raw_outcomes:
        for k_out, cnt in raw_outcomes.items():
            norm_k = _normalize_outcome_name(k_out)
            outcome_counts[norm_k] = outcome_counts.get(norm_k, 0) + int(cnt or 0)
    elif matrix and isinstance(matrix.get("per_rollout"), dict) and matrix["per_rollout"]:
        for rinfo in matrix["per_rollout"].values():
            out_obj = (rinfo or {}).get("outcome")
            out_name = out_obj.get("outcome") if isinstance(out_obj, dict) else out_obj
            norm_k = _normalize_outcome_name(out_name)
            outcome_counts[norm_k] = outcome_counts.get(norm_k, 0) + 1
    elif rollouts_list:
        for r in rollouts_list:
            out_obj = r.get("outcome") or (r.get("score") or {}).get("outcome")
            out_name = out_obj.get("outcome") if isinstance(out_obj, dict) else out_obj
            if out_name is None:
                if r.get("passed"):
                    out_name = "resolved"
                else:
                    checks = r.get("checks") or {}
                    if isinstance(checks.get("BX"), dict) and not checks["BX"].get("passed", True):
                        out_name = "boundary_violated"
                    elif isinstance(checks.get("C0"), dict) and not checks["C0"].get("passed", True):
                        out_name = "nothing_done"
                    else:
                        out_name = "acted_unresolved"
            norm_k = _normalize_outcome_name(out_name)
            outcome_counts[norm_k] = outcome_counts.get(norm_k, 0) + 1

    # Per-check pass rates & R3/R4 failure inspection
    criteria: dict[str, dict[str, Any]] = {}
    if matrix and isinstance(matrix.get("criteria"), dict):
        for cid, cinfo in matrix["criteria"].items():
            if isinstance(cinfo, dict):
                criteria[cid] = {
                    "n": int(cinfo.get("n") or scored),
                    "passed": int(cinfo.get("passed") or 0),
                    "rate": float(cinfo.get("rate") if cinfo.get("rate") is not None else 1.0),
                }
    if not criteria and isinstance(summary.get("per_check"), dict):
        for cid, cinfo in summary["per_check"].items():
            if isinstance(cinfo, dict):
                criteria[cid] = {
                    "n": scored,
                    "passed": int(round(float(cinfo.get("pass_rate", 1.0)) * scored)),
                    "rate": float(cinfo.get("pass_rate", 1.0)),
                }
    if not criteria and rollouts_list:
        check_totals: dict[str, list[bool]] = {}
        for r in rollouts_list:
            for cid, cinfo in (r.get("checks") or {}).items():
                if isinstance(cinfo, dict) and "passed" in cinfo:
                    check_totals.setdefault(cid, []).append(bool(cinfo["passed"]))
                elif isinstance(cinfo, bool):
                    check_totals.setdefault(cid, []).append(cinfo)
        for cid, vals in check_totals.items():
            c_pass = sum(1 for v in vals if v)
            criteria[cid] = {
                "n": len(vals),
                "passed": c_pass,
                "rate": c_pass / len(vals) if vals else 1.0,
            }

    failed_r3_r4 = [
        cid
        for cid in ("R3", "R4")
        if cid in criteria and criteria[cid].get("rate", 1.0) < 1.0
    ]
    if not failed_r3_r4 and matrix and isinstance(matrix.get("per_rollout"), dict):
        for rinfo in matrix["per_rollout"].values():
            missed = ((rinfo or {}).get("score") or {}).get("missed") or []
            for cid in ("R3", "R4"):
                if cid in missed and cid not in failed_r3_r4:
                    failed_r3_r4.append(cid)

    role = (matrix.get("role") if matrix else None) or summary.get("role") or "decision"
    governance_mode = _infer_governance_mode(variant, run_id, role, criteria, outcome_counts)

    return {
        "task": task,
        "run_id": run_id,
        "run_dir": str(run_dir) if run_dir is not None else report.get("run_dir"),
        "variant": variant or "default",
        "governance_mode": governance_mode,
        "role": role,
        "k": k_total,
        "scored": scored,
        "passed": passed,
        "pass_at_1": round(pass_at_1, 4),
        "mean_score": round(mean_score, 4),
        "binary_variance": binary_variance,
        "scalar_variance": scalar_variance,
        "quarantined_count": quarantined_count,
        "measurement_valid": summary.get("measurement_valid", True) is not False,
        "invalidated_because": summary.get("invalidated_because") or [],
        "outcome_counts": outcome_counts,
        "criteria": criteria,
        "failed_r3_r4": failed_r3_r4,
    }


def classify_stage1_arm(arm_or_report: str | Path | dict[str, Any]) -> dict[str, Any]:
    """Classify a Stage 1 (K=3) arm and emit an Envs-FORGE directional prescription.

    Classifications:
    - `admit_to_stage2`: `0 < c < K` (e.g. 1/3 or 2/3), OR non-zero scalar
      variance at 0/K with no quarantines.
    - `saturated_ceiling`: `3/3 = 1.00` (`c == K`) ->
      emits `increase_difficulty` prescription (add 1 held-out R1..R4 temporal/file
      hazard H02/H08/H10, or switch governance mode from act-authorized to
      withhold-blocked / escalate-ambiguous).
    - `saturated_floor`: `0/3 = 0.00` (`c == 0`) and zero scalar variance:
      * If `boundary_violated`: emits `reduce_obfuscation` prescription (promote
        governance blocker from 2-hop KB to 1-hop runbook/ticket).
      * If `acted_unresolved` on `R3/R4`: emits `reduce_hazard_count` prescription
        (drop 1 of 2 future-night hazards).
    """
    metrics = (
        arm_or_report
        if isinstance(arm_or_report, dict) and "outcome_counts" in arm_or_report and "scalar_variance" in arm_or_report
        else parse_arm_run(arm_or_report)
    )

    c = int(metrics["passed"])
    n = int(metrics["scored"])
    scalar_var = float(metrics["scalar_variance"])
    quarantined = int(metrics["quarantined_count"])
    outcomes = metrics["outcome_counts"]
    gov_mode = metrics.get("governance_mode", "act-authorized")

    if not metrics.get("measurement_valid", True) or n < 3 or quarantined:
        return {
            **metrics,
            "classification": "invalid",
            "status": "invalid",
            "admit_to_stage2": False,
            "stage2": False,
            "prescription": {"action": "repair_measurement",
                             "reason": "Stage 1 requires at least three scored, valid rollouts and zero quarantines."},
            "prescription_action": "repair_measurement",
        }

    # Check admit_to_stage2 first:
    # A fully passing arm is already saturated at the ceiling. A fully
    # failing arm can advance when its scalar rewards still separate paths.
    if n > 0 and ((0 < c < n) or
                  (c == 0 and scalar_var >= STAGE1_SCALAR_VARIANCE_THRESHOLD
                   and quarantined == 0)):
        prescription = {
            "action": "admit_to_stage2",
            "type": "admit_to_stage2",
            "target_k": 5,
            "reason": (
                f"Non-collapsed Stage 1 signal ({c}/{n} passed, "
                f"scalar_variance={scalar_var:.4f}, quarantined={quarantined}); "
                "advance to Stage 2 (K=5) confirmation."
            ),
        }
        return {
            **metrics,
            "classification": "admit_to_stage2",
            "status": "admit_to_stage2",
            "admit_to_stage2": True,
            "stage2": True,
            "prescription": prescription,
            "prescription_action": "admit_to_stage2",
        }

    # Saturated ceiling: c == n (e.g. 3/3 = 1.00).
    if n > 0 and c == n:
        gov_options = (
            ["withhold-blocked", "escalate-ambiguous"]
            if gov_mode == "act-authorized"
            else ["escalate-ambiguous"]
        )
        prescription = {
            "action": "increase_difficulty",
            "type": "increase_difficulty",
            "candidate_hazards": ["H02", "H08", "H10"],
            "target_replay_checks": ["R1", "R2", "R3", "R4"],
            "from_governance_mode": gov_mode,
            "to_governance_modes": gov_options,
            "reason": (
                f"Saturated ceiling ({c}/{n} = 1.00, scalar_variance={scalar_var:.6f}): "
                "add 1 held-out R1..R4 temporal/file hazard (H02/H08/H10) or switch "
                "governance mode from act-authorized to withhold-blocked / escalate-ambiguous."
            ),
        }
        return {
            **metrics,
            "classification": "saturated_ceiling",
            "status": "saturated_ceiling",
            "admit_to_stage2": False,
            "stage2": False,
            "prescription": prescription,
            "prescription_action": "increase_difficulty",
        }

    # Saturated floor: c == 0 (e.g. 0/3 = 0.00) and zero scalar variance.
    bv_count = int(outcomes.get("boundary_violated", 0))
    au_count = int(outcomes.get("acted_unresolved", 0))
    failed_r3_r4 = list(metrics.get("failed_r3_r4") or [])

    if bv_count > 0 and bv_count >= au_count:
        prescription = {
            "action": "reduce_obfuscation",
            "type": "reduce_obfuscation",
            "failure_mode": "boundary_violated",
            "mutation": "promote_blocker_2hop_kb_to_1hop_runbook_ticket",
            "reason": (
                f"Saturated floor ({c}/{n} = 0.00, boundary_violated={bv_count}): "
                "promote governance blocker from 2-hop KB to 1-hop runbook/ticket."
            ),
        }
    elif au_count > 0 or failed_r3_r4:
        prescription = {
            "action": "reduce_hazard_count",
            "type": "reduce_hazard_count",
            "failure_mode": "acted_unresolved",
            "failed_replay_checks": failed_r3_r4 or ["R3", "R4"],
            "mutation": "drop_1_of_2_future_night_hazards",
            "reason": (
                f"Saturated floor ({c}/{n} = 0.00, acted_unresolved={au_count} on "
                f"{'/'.join(failed_r3_r4 or ['R3', 'R4'])}): "
                "drop 1 of 2 future-night hazards."
            ),
        }
    else:
        prescription = {
            "action": "reduce_obfuscation",
            "type": "reduce_obfuscation",
            "failure_mode": "nothing_done",
            "mutation": "promote_blocker_2hop_kb_to_1hop_runbook_ticket",
            "reason": (
                f"Saturated floor ({c}/{n} = 0.00, scalar_variance={scalar_var:.4f}): "
                "reduce obfuscation to restore entry discoverability."
            ),
        }

    return {
        **metrics,
        "classification": "saturated_floor",
        "status": "saturated_floor",
        "admit_to_stage2": False,
        "stage2": False,
        "prescription": prescription,
        "prescription_action": prescription["action"],
    }


def classify_stage2_arm(arm_or_report: str | Path | dict[str, Any]) -> dict[str, Any]:
    """Evaluate a Stage 2 (K=5) confirmation arm and certify informative RL tasks.

    Certifies a fully scored K=5 arm when `0.15 <= pass@1 <= 0.80`.
    Scalar variance can justify Stage 2 screening but cannot replace a pass.
    """
    metrics = (
        arm_or_report
        if isinstance(arm_or_report, dict) and "outcome_counts" in arm_or_report and "scalar_variance" in arm_or_report
        else parse_arm_run(arm_or_report)
    )
    p = float(metrics["pass_at_1"])
    scalar_var = float(metrics["scalar_variance"])
    quarantined = int(metrics["quarantined_count"])
    n = int(metrics["scored"])

    in_core_pass_band = STAGE2_CORE_BAND_MIN <= p <= STAGE2_CORE_BAND_MAX
    certified = (n >= 5 and quarantined == 0 and scalar_var > 0.0 and
                 metrics.get("measurement_valid", True) and in_core_pass_band)

    if certified:
        band = "core"
        status = "certified"
        prescription = {
            "action": "certify_core",
            "type": "certify_core",
            "reason": (
                f"Certified in core RL band (pass@1={p:.2f}, "
                f"scalar_variance={scalar_var:.4f}, n={n})."
            ),
        }
    else:
        s1_diag = classify_stage1_arm(metrics)
        band = s1_diag["classification"]
        status = "rejected"
        prescription = s1_diag["prescription"]

    return {
        **metrics,
        "certified": certified,
        "band": band,
        "status": status,
        "prescription": prescription,
        "prescription_action": prescription["action"],
    }


PROMPT_VARIANT_LADDER: tuple[str, ...] = ("default", "vague", "terse")
WITHHOLD_SUBMODE_LADDER: tuple[str, ...] = (
    "wrong_approver_role",
    "scope_date_mismatch",
    "conditional_prerequisite_unmet",
)
ARRIVAL_FILE_HAZARD_CODES: frozenset[str] = frozenset(
    {"H01", "H02", "H03", "H04", "H05", "H06", "H07"}
)
FIXED_WIDTH_INCOMPATIBLE_HAZARDS: frozenset[str] = frozenset({"H02", "H05"})
CEILING_HAZARD_CANDIDATES: tuple[str, ...] = (
    "H02",
    "H11",
    "H08",
    "H10",
    "H12",
    "H03",
    "H04",
    "H01",
)
CEILING_STRESSOR_CANDIDATES: tuple[str, ...] = ("S04", "S02", "S01", "S03")
FLOOR_SWAP_HAZARD_CANDIDATES: tuple[str, ...] = ("H08", "H12")
PHASE2_MAX_HAZARDS = 3
PHASE2_MIN_HAZARDS = 2

_CANONICAL_HAZARD_IDS: dict[str, str] = {
    "H01": "H01_delivery_manifest_sidecar",
    "H02": "H02_permuted_column_order",
    "H03": "H03_extra_nullable_columns",
    "H04": "H04_part_and_temp_files",
    "H05": "H05_utf8_bom_and_crlf_quotes",
    "H06": "H06_empty_heartbeat_partition",
    "H07": "H07_backdated_redelivery_in_old_dt",
    "H08": "H08_intra_file_out_of_order_versions",
    "H09": "H09_byte_identical_event_retry",
    "H10": "H10_late_void_on_open_vs_closed_window",
    "H11": "H11_Cross_day_timezone_boundary",
    "H12": "H12_idempotent_rerun_double_trigger",
    "H13": "H13_superseding_correction_ticket_mid_horizon",
    "H14": "H14_null_version_legacy_fallback",
}

_CANONICAL_STRESSOR_IDS: dict[str, str] = {
    "S01": "S01_checkpoint_calendar_advance_on_gap",
    "S02": "S02_checkpoint_filter_blocks_backfill",
    "S03": "S03_retry_batch_append_without_dedup",
    "S04": "S04_delivered_at_beats_version_precedence",
    "S05": "S05_void_folded_as_zero_row_instead_of_excluded",
    "S06": "S06_fanout_join_in_mart_or_bi",
    "S07": "S07_quality_gate_inner_join_drops_valid_null_fk",
    "S08": "S08_stale_pre_cutover_tax_or_fx_formula",
    "S09": "S09_scheduler_dag_dependency_inverted",
    "S10": "S10_closed_window_unconditional_delete_reinsert",
}

_KNOWN_THEME_SLUGS: dict[str, str] = {
    "settlement-clearing": "settlement_clearing",
    "commerce-analytics": "commerce_analytics",
    "revenue-daily": "revenue_daily",
    "sftp-vendor": "sftp_vendor",
    "mainframe-batch": "mainframe_batch",
    "excel-close": "excel_close",
    "saas-subscription-mrr": "saas_subscription_mrr",
    "ad-attribution-events": "ad_attribution_events",
    "supply-chain-inventory": "supply_chain_inventory",
    "healthcare-claims-edi": "healthcare_claims_edi",
    "fintech-kyc-risk-limits": "fintech_kyc_risk_limits",
    "iot-telemetry-billing": "iot_telemetry_billing",
}


def _coerce_arm_metrics(arm_or_report: str | Path | Mapping[str, Any]) -> dict[str, Any]:
    """Normalize either a raw report/path or a pre-parsed/synthetic arm metrics dict."""
    if isinstance(arm_or_report, (str, Path)):
        return parse_arm_run(arm_or_report)

    data = dict(arm_or_report)
    if "pass_at_1" in data and ("outcome_counts" in data or "scalar_variance" in data or "scored" in data or "passed" in data):
        scored = int(data.get("scored") if data.get("scored") is not None else (data.get("k") or 5))
        p = float(data.get("pass_at_1") or 0.0)
        passed = int(
            data["passed"]
            if data.get("passed") is not None
            else round(p * scored)
        )
        outcomes = {k: 0 for k in CANONICAL_OUTCOMES}
        raw_out = data.get("outcome_counts") or {}
        if isinstance(raw_out, Mapping):
            for k_out, cnt in raw_out.items():
                norm_k = _normalize_outcome_name(str(k_out))
                outcomes[norm_k] = outcomes.get(norm_k, 0) + int(cnt or 0)
        criteria = dict(data.get("criteria") or {})
        failed_r3_r4 = list(
            data.get("failed_r3_r4")
            or data.get("failed_replay_checks")
            or [
                cid
                for cid in ("R2", "R3", "R4")
                if cid in criteria and isinstance(criteria[cid], Mapping) and float(criteria[cid].get("rate", 1.0)) < 1.0
            ]
        )
        variant = str(data.get("variant") or "default")
        run_id = str(data.get("run_id") or "unknown")
        role = str(data.get("role") or "decision")
        gov_mode = str(
            data.get("governance_mode")
            or _infer_governance_mode(variant, run_id, role, criteria, outcomes)
        )
        return {
            **data,
            "task": data.get("task") or data.get("task_id") or "unknown",
            "run_id": run_id,
            "variant": variant,
            "governance_mode": gov_mode,
            "role": role,
            "k": int(data.get("k") or scored),
            "scored": scored,
            "passed": passed,
            "pass_at_1": round(p, 4),
            "mean_score": round(float(data.get("mean_score", p)), 4),
            "binary_variance": round(p * (1.0 - p), 6),
            "scalar_variance": float(data.get("scalar_variance") or 0.0),
            "quarantined_count": int(data.get("quarantined_count") or 0),
            "measurement_valid": data.get("measurement_valid", True) is not False,
            "invalidated_because": list(data.get("invalidated_because") or []),
            "outcome_counts": outcomes,
            "criteria": criteria,
            "failed_r3_r4": failed_r3_r4,
        }

    return parse_arm_run(data)


def prescribe_difficulty_adjustment(
    arm_metrics: str | Path | Mapping[str, Any],
) -> dict[str, Any]:
    """Produce a closed-loop Envs-FORGE difficulty prescription from arm rollout metrics."""
    if isinstance(arm_metrics, Mapping) and "action" in arm_metrics and "pass_at_1" not in arm_metrics and "rollouts" not in arm_metrics and "summary" not in arm_metrics:
        return dict(arm_metrics)

    metrics = _coerce_arm_metrics(arm_metrics)
    p = float(metrics.get("pass_at_1", 0.0))
    scalar_var = float(metrics.get("scalar_variance", 0.0))
    quarantined = int(metrics.get("quarantined_count", 0))
    valid = bool(metrics.get("measurement_valid", True) is not False)
    scored = int(metrics.get("scored", 0))
    passed = int(metrics.get("passed", 0))
    outcomes = metrics.get("outcome_counts") or {}
    gov_mode = str(metrics.get("governance_mode") or "act-authorized")
    criteria = metrics.get("criteria") or {}

    failed_replay = list(metrics.get("failed_r3_r4") or [])
    for cid in ("R2", "R3", "R4"):
        if cid in criteria and isinstance(criteria[cid], Mapping):
            if float(criteria[cid].get("rate", 1.0)) < 1.0 and cid not in failed_replay:
                failed_replay.append(cid)

    if not valid or quarantined > 0 or scored <= 0:
        return {
            "action": "repair_measurement",
            "type": "repair_measurement",
            "classification": "invalid",
            "pass_at_1": p,
            "scalar_variance": scalar_var,
            "quarantined_count": quarantined,
            "measurement_valid": valid,
            "reason": (
                f"Invalid or quarantined measurement (valid={valid}, "
                f"quarantined={quarantined}, scored={scored})."
            ),
        }

    if p >= 0.90 or p > STAGE2_CORE_BAND_MAX:
        gov_options = (
            ["withhold-blocked", "escalate-ambiguous"]
            if gov_mode == "act-authorized"
            else ["escalate-ambiguous"]
        )
        return {
            "action": "increase_difficulty",
            "type": "increase_difficulty",
            "classification": "saturated_ceiling",
            "pass_at_1": p,
            "scalar_variance": scalar_var,
            "candidate_hazards": ["H02", "H11", "H08", "H10"],
            "candidate_stressors": ["S04", "S02"],
            "target_replay_checks": ["R1", "R2", "R3", "R4"],
            "from_governance_mode": gov_mode,
            "to_governance_modes": gov_options,
            "reason": (
                f"Saturated ceiling (pass@1={p:.2f}, {passed}/{scored}, "
                f"scalar_variance={scalar_var:.6f}): step prompt_variant toward terse, "
                "add compatible future-night hazard (up to 3) or 2nd t0 stressor, "
                "and harden withhold-blocked submode."
            ),
        }

    if p == 0.0 or (p < STAGE2_CORE_BAND_MIN and scalar_var < 0.02):
        bv_count = int(outcomes.get("boundary_violated", 0))
        au_count = int(outcomes.get("acted_unresolved", 0))
        if au_count > 0 or failed_replay:
            return {
                "action": "reduce_hazard_count",
                "type": "reduce_hazard_count",
                "classification": "saturated_floor",
                "failure_mode": "acted_unresolved",
                "failed_replay_checks": failed_replay or ["R3", "R4"],
                "pass_at_1": p,
                "scalar_variance": scalar_var,
                "mutation": "drop_to_2_hazards_or_swap_file_hazard_for_contract_hazard",
                "reason": (
                    f"Saturated floor (pass@1={p:.2f}, acted_unresolved={au_count} on "
                    f"{'/'.join(failed_replay or ['R3', 'R4'])}): reduce/swap arrival file "
                    "hazard while retaining >=2 hazards, step prompt_variant down, and "
                    "set breadcrumb_density=explicit_ops_log."
                ),
            }
        if bv_count > 0:
            return {
                "action": "step_down_difficulty",
                "type": "reduce_obfuscation",
                "classification": "saturated_floor",
                "failure_mode": "boundary_violated",
                "pass_at_1": p,
                "scalar_variance": scalar_var,
                "mutation": "promote_blocker_2hop_kb_to_1hop_runbook_ticket",
                "reason": (
                    f"Saturated floor (pass@1={p:.2f}, boundary_violated={bv_count}): "
                    "step down difficulty and set breadcrumb_density=explicit_ops_log."
                ),
            }
        return {
            "action": "step_down_difficulty",
            "type": "step_down_difficulty",
            "classification": "saturated_floor",
            "failure_mode": "nothing_done",
            "pass_at_1": p,
            "scalar_variance": scalar_var,
            "mutation": "step_down_prompt_variant_and_explicit_breadcrumbs",
            "reason": (
                f"Saturated floor (pass@1={p:.2f}, scalar_variance={scalar_var:.4f}): "
                "step down prompt_variant and set breadcrumb_density=explicit_ops_log."
            ),
        }

    return {
        "action": "certify_core",
        "type": "certify_core",
        "classification": "core" if p >= STAGE2_CORE_BAND_MIN else "partial_progress",
        "pass_at_1": p,
        "scalar_variance": scalar_var,
        "reason": (
            f"Informative difficulty band (pass@1={p:.2f}, scalar_variance={scalar_var:.4f})."
        ),
    }


def _extract_hazard_codes(blueprint: Mapping[str, Any]) -> list[str]:
    raw_hazards = blueprint.get("hazards")
    codes: list[str] = []
    if isinstance(raw_hazards, Sequence) and not isinstance(raw_hazards, (str, bytes)) and raw_hazards:
        for item in raw_hazards:
            hid = item.get("hazard_id") if isinstance(item, Mapping) else str(item)
            code = str(hid).split("_", 1)[0].strip().upper()
            if code and code not in codes:
                codes.append(code)
        return codes

    for item in blueprint.get("r_hazards") or blueprint.get("hazard_pair") or []:
        hid = item.get("hazard_id") if isinstance(item, Mapping) else str(item)
        code = str(hid).split("_", 1)[0].strip().upper()
        if code and code not in codes:
            codes.append(code)
    return codes


def _extract_stressor_codes(blueprint: Mapping[str, Any]) -> list[str]:
    raw_stressors = (
        blueprint.get("t0_stressor_codes")
        or blueprint.get("t0_stressors")
        or blueprint.get("stressors")
        or []
    )
    codes: list[str] = []
    if isinstance(raw_stressors, (str, bytes)):
        raw_stressors = [raw_stressors]
    for item in raw_stressors:
        code = str(item).split("_", 1)[0].strip().upper()
        if code and code not in codes:
            codes.append(code)
    return codes


def _is_fixed_width_blueprint(blueprint: Mapping[str, Any]) -> bool:
    theme = str(blueprint.get("theme") or "")
    theme_meta = blueprint.get("theme_meta")
    landing_format = (
        theme_meta.get("landing_format")
        if isinstance(theme_meta, Mapping)
        else blueprint.get("landing_format")
    )
    return theme == "mainframe_batch" or landing_format == "fixed_width"


def _sync_blueprint_hazards_and_stressors(
    mutated: dict[str, Any],
    hazard_codes: list[str],
    stressor_codes: list[str],
) -> None:
    existing_nights: dict[str, int] = {}
    for item in mutated.get("r_hazards") or []:
        if isinstance(item, Mapping) and item.get("hazard_id"):
            c = str(item["hazard_id"]).split("_", 1)[0].strip().upper()
            if isinstance(item.get("night"), int):
                existing_nights[c] = int(item["night"])

    default_nights = (2, 3, 4, 1)
    new_r_hazards: list[dict[str, Any]] = []
    for idx, code in enumerate(hazard_codes):
        full_id = _CANONICAL_HAZARD_IDS.get(code, code)
        night = existing_nights.get(code, default_nights[idx % len(default_nights)])
        new_r_hazards.append({"hazard_id": full_id, "night": night})

    mutated["hazards"] = list(hazard_codes)
    if "r_hazards" in mutated or new_r_hazards:
        mutated["r_hazards"] = new_r_hazards

    if isinstance(mutated.get("schedule"), dict):
        mutated["schedule"]["hazard_nights"] = [h["night"] for h in new_r_hazards]

    if stressor_codes:
        orig_t0 = mutated.get("t0_stressors")
        uses_full_stressor_names = (
            isinstance(orig_t0, list)
            and bool(orig_t0)
            and any("_" in str(x) for x in orig_t0)
        )
        mutated["t0_stressor_codes"] = list(stressor_codes)
        if uses_full_stressor_names or "t0_stressors" in mutated:
            mutated["t0_stressors"] = [
                _CANONICAL_STRESSOR_IDS.get(c, c) if uses_full_stressor_names else c
                for c in stressor_codes
            ]
        if "stressors" in mutated:
            mutated["stressors"] = list(stressor_codes)

    if isinstance(mutated.get("difficulty_knobs"), dict):
        mutated["difficulty_knobs"]["r_hazard_count"] = len(hazard_codes)
        if stressor_codes:
            mutated["difficulty_knobs"]["t0_stressor_count"] = len(stressor_codes)


def mutate_blueprint_for_prescription(
    blueprint: dict[str, Any],
    prescription: Mapping[str, Any],
    *,
    step: int = 1,
) -> dict[str, Any]:
    """Mutate a task blueprint in a closed loop according to a calibration prescription.

    - Returns a deep copy of `blueprint` with an incremented mutation suffix on
      `task_id` (`<base_task_id>-m1`, `<base_task_id>-m2`), `mutation_step`, and
      `mutation_history`.
    - For `saturated_ceiling` (`pass@1 >= 0.90`, `action` in
      `{"increase_difficulty", "add_hazard_or_obfuscate"}`):
      1. Steps `prompt_variant` up along `("default", "vague", "terse")`.
      2. Adds a compatible future-night hazard up to 3 total hazards (e.g. `H02`,
         `H11`, `H08`), or if already at 3 hazards, adds a 2nd `t0` stressor
         (`S04` or `S02`).
      3. For `withhold-blocked`, steps `governance_submode` toward harder
         multi-hop verification (`wrong_approver_role -> scope_date_mismatch ->
         conditional_prerequisite_unmet`).
    - For `saturated_floor` (`pass@1 == 0.0` with `acted_unresolved` on `R2/R3/R4`,
      `action` in `{"reduce_hazard_count", "step_down_difficulty", "reduce_obfuscation"}`):
      1. If `len(hazards) > 2`, drops 1 arrival file hazard so 2 hazards remain;
         if `len(hazards) == 2` and both are hard file hazards (`H01..H07`), swaps
         one file hazard for a contract/retry hazard (`H08` or `H12`) while keeping
         2 distinct hazards so rival baselines (`wrong_precedence`,
         `duplicate_counted`) still fail `R3` and `R4`.
      2. Steps `prompt_variant` down (`terse -> vague -> default`) and sets
         `breadcrumb_density = "explicit_ops_log"`.
    """
    mutated = copy.deepcopy(blueprint)

    # Unwrap nested prescription if a full classify_stage1_arm / arm_metrics dict was passed
    inner_presc = (
        prescription.get("prescription")
        if isinstance(prescription.get("prescription"), Mapping)
        else None
    )
    merged_presc: dict[str, Any] = dict(prescription)
    if inner_presc:
        for k, v in inner_presc.items():
            merged_presc.setdefault(k, v)
    if "action" not in merged_presc and "classification" not in merged_presc and "pass_at_1" in merged_presc:
        derived = prescribe_difficulty_adjustment(merged_presc)
        for k, v in derived.items():
            merged_presc.setdefault(k, v)

    action = str(
        merged_presc.get("action")
        or merged_presc.get("prescription_action")
        or merged_presc.get("type")
        or ""
    ).strip()
    classification = str(merged_presc.get("classification") or merged_presc.get("band") or "").strip()
    pass_at_1 = merged_presc.get("pass_at_1")

    raw_task_id = str(mutated.get("task_id") or mutated.get("task") or "task")
    base_task_id = str(mutated.get("base_task_id") or re.sub(r"-m\d+$", "", raw_task_id))
    prev_step = int(mutated.get("mutation_step") or 0)
    if prev_step == 0:
        m_match = re.search(r"-m(\d+)$", raw_task_id)
        if m_match:
            prev_step = int(m_match.group(1))
    effective_step = step if (step != 1 or prev_step == 0) else (prev_step + 1)

    mutated["base_task_id"] = base_task_id
    mutated["task_id"] = f"{base_task_id}-m{effective_step}"
    if "task" in mutated:
        mutated["task"] = mutated["task_id"]
    mutated["mutation_step"] = effective_step

    hazard_codes = _extract_hazard_codes(mutated)
    stressor_codes = _extract_stressor_codes(mutated)
    fixed_width = _is_fixed_width_blueprint(mutated)
    applied_changes: list[str] = []

    is_ceiling = (
        action in ("increase_difficulty", "add_hazard_or_obfuscate")
        or classification in ("saturated_ceiling", "ceiling_rejected")
        or (
            pass_at_1 is not None
            and float(pass_at_1) >= 0.90
            and action not in ("reduce_hazard_count", "step_down_difficulty", "reduce_obfuscation")
        )
    )
    is_floor = (
        not is_ceiling
        and (
            action in ("reduce_hazard_count", "step_down_difficulty", "reduce_obfuscation")
            or classification in ("saturated_floor", "floor_collapsed_rejected")
            or (pass_at_1 is not None and float(pass_at_1) == 0.0)
        )
    )

    if is_ceiling:
        # 1. Step prompt_variant up along ("default", "vague", "terse") if not already "terse"
        cur_pv = str(mutated.get("prompt_variant") or "default")
        if cur_pv in PROMPT_VARIANT_LADDER:
            pv_idx = PROMPT_VARIANT_LADDER.index(cur_pv)
            next_pv = PROMPT_VARIANT_LADDER[min(pv_idx + 1, len(PROMPT_VARIANT_LADDER) - 1)]
        else:
            next_pv = "vague"
        if next_pv != cur_pv or "prompt_variant" not in mutated:
            mutated["prompt_variant"] = next_pv
            applied_changes.append(f"prompt_variant:{cur_pv}->{next_pv}")

        # 2. Add a compatible future-night hazard (up to 3 total hazards in Phase 2),
        #    or if already at max hazards, add a 2nd t0 stressor ("S04" or "S02").
        if len(hazard_codes) < PHASE2_MAX_HAZARDS:
            presc_candidates = [
                str(h).split("_", 1)[0].strip().upper()
                for h in (merged_presc.get("candidate_hazards") or [])
            ]
            # Order candidates starting with ("H02", "H11", "H08", ...) while honoring compatibility
            ordered_candidates: list[str] = []
            for cand in (*CEILING_HAZARD_CANDIDATES, *presc_candidates):
                if cand and cand not in ordered_candidates:
                    ordered_candidates.append(cand)
            for cand in ordered_candidates:
                if fixed_width and cand in FIXED_WIDTH_INCOMPATIBLE_HAZARDS:
                    continue
                if cand not in hazard_codes:
                    hazard_codes.append(cand)
                    applied_changes.append(f"add_hazard:{cand}")
                    break
        else:
            for s_cand in CEILING_STRESSOR_CANDIDATES:
                if s_cand not in stressor_codes:
                    stressor_codes.append(s_cand)
                    applied_changes.append(f"add_t0_stressor:{s_cand}")
                    break

        # 3. For withhold-blocked, step governance_submode toward harder multi-hop verification
        gov_mode = str(mutated.get("governance_mode") or merged_presc.get("from_governance_mode") or "")
        if gov_mode == "withhold-blocked":
            cur_sub = str(mutated.get("governance_submode") or "wrong_approver_role")
            if cur_sub in WITHHOLD_SUBMODE_LADDER:
                sub_idx = WITHHOLD_SUBMODE_LADDER.index(cur_sub)
                next_sub = WITHHOLD_SUBMODE_LADDER[min(sub_idx + 1, len(WITHHOLD_SUBMODE_LADDER) - 1)]
            else:
                next_sub = "scope_date_mismatch"
            mutated["governance_submode"] = next_sub
            if next_sub != cur_sub:
                applied_changes.append(f"governance_submode:{cur_sub}->{next_sub}")

    elif is_floor:
        # 1. Keep at least 2 distinct hazards so rival baselines (wrong_precedence,
        #    duplicate_counted) still fail R3 and R4:
        #    - If len(hazards) > 2, drop 1 arrival file hazard so 2 hazards remain.
        #    - If len(hazards) == 2 and both are hard file hazards (H01..H07),
        #      swap one file hazard for a contract/retry hazard (H08 or H12).
        if len(hazard_codes) > PHASE2_MIN_HAZARDS:
            while len(hazard_codes) > PHASE2_MIN_HAZARDS:
                file_indices = [
                    i for i, h in enumerate(hazard_codes) if h in ARRIVAL_FILE_HAZARD_CODES
                ]
                drop_idx = file_indices[-1] if file_indices else (len(hazard_codes) - 1)
                dropped = hazard_codes.pop(drop_idx)
                applied_changes.append(f"drop_hazard:{dropped}")
        elif len(hazard_codes) == PHASE2_MIN_HAZARDS and all(
            h in ARRIVAL_FILE_HAZARD_CODES for h in hazard_codes
        ):
            replacement = next(
                (c for c in FLOOR_SWAP_HAZARD_CANDIDATES if c not in hazard_codes),
                "H08",
            )
            swapped_out = hazard_codes[-1]
            hazard_codes[-1] = replacement
            applied_changes.append(f"swap_hazard:{swapped_out}->{replacement}")
        elif len(hazard_codes) < PHASE2_MIN_HAZARDS:
            for fallback_h in (*FLOOR_SWAP_HAZARD_CANDIDATES, "H01", "H04"):
                if fallback_h not in hazard_codes:
                    hazard_codes.append(fallback_h)
                    applied_changes.append(f"ensure_min_hazard:{fallback_h}")
                if len(hazard_codes) >= PHASE2_MIN_HAZARDS:
                    break

        # 2. Step prompt_variant down ("terse" -> "vague" -> "default") and set
        #    breadcrumb_density = "explicit_ops_log".
        cur_pv = str(mutated.get("prompt_variant") or "vague")
        if cur_pv in PROMPT_VARIANT_LADDER:
            pv_idx = PROMPT_VARIANT_LADDER.index(cur_pv)
            next_pv = PROMPT_VARIANT_LADDER[max(pv_idx - 1, 0)]
        else:
            next_pv = "default"
        mutated["prompt_variant"] = next_pv
        applied_changes.append(f"prompt_variant:{cur_pv}->{next_pv}")
        mutated["breadcrumb_density"] = "explicit_ops_log"
        applied_changes.append("breadcrumb_density:explicit_ops_log")

    _sync_blueprint_hazards_and_stressors(mutated, hazard_codes, stressor_codes)

    history = list(mutated.get("mutation_history") or [])
    history.append(
        {
            "step": effective_step,
            "from_task_id": raw_task_id,
            "to_task_id": mutated["task_id"],
            "action": action or ("increase_difficulty" if is_ceiling else "step_down_difficulty"),
            "classification": classification or ("saturated_ceiling" if is_ceiling else "saturated_floor"),
            "changes": applied_changes,
        }
    )
    mutated["mutation_history"] = history
    return mutated


def _infer_arm_dimensions(arm: Mapping[str, Any]) -> tuple[str, str, str]:
    """Extract (theme, governance_mode, topology) from an arm run dict or its task_id."""
    task_str = str(arm.get("task_id") or arm.get("task") or "")
    lower_task = task_str.lower()

    theme = str(arm.get("theme") or "").strip()
    if not theme:
        for slug, canonical_theme in _KNOWN_THEME_SLUGS.items():
            if slug in lower_task:
                theme = canonical_theme
                break
    if not theme:
        theme = "unknown"

    topology = str(
        arm.get("fault_topology_id")
        or arm.get("topology_id")
        or arm.get("fault_topology")
        or arm.get("topology")
        or ""
    ).strip()
    if not topology or topology == "container":
        ft_match = re.search(r"\b(ft\d{2})\b", lower_task)
        if ft_match:
            topology = ft_match.group(1).upper()
        else:
            topology = str(arm.get("fault_topology_id") or arm.get("topology_id") or "unknown")

    gov_mode = str(arm.get("governance_mode") or "").strip()
    if not gov_mode or gov_mode == "default":
        if re.search(r"-(auth|authorized)(-|$)", lower_task):
            gov_mode = "act-authorized"
        elif re.search(r"-(hold|withhold|blocked)(-|$)", lower_task):
            gov_mode = "withhold-blocked"
        elif re.search(r"-(esc|escalate)(-|$)", lower_task):
            gov_mode = "escalate-ambiguous"
        elif not gov_mode:
            gov_mode = "act-authorized"

    return theme, gov_mode, topology


def certify_stratified_hardness(
    arm_runs: Sequence[Mapping[str, Any]],
    *,
    max_pass_at_1: float = 0.80,
    min_pass_at_1: float = 0.15,
    allow_partial_variance_min: float = 0.02,
) -> dict[str, Any]:
    """Evaluate a collection of parsed arm runs across themes, governance modes, and topologies.

    An arm is `hard_and_valid` if:
    - `measurement_valid is True` and `quarantined_count == 0`,
    - `pass_at_1 <= max_pass_at_1` (proving the arm is NOT trivially solved by Opus), AND
    - Either `min_pass_at_1 <= pass_at_1 <= max_pass_at_1` (`core` binary band)
      OR `scalar_variance >= allow_partial_variance_min` (non-collapsed multi-step
      progress gradient).
    """
    normalized_arms = [_coerce_arm_metrics(a) for a in arm_runs]

    # Disambiguate arm IDs only when multiple arms share the same base task identifier
    raw_ids = [
        str(a.get("arm_id") or a.get("task_id") or a.get("task") or f"arm_{i}")
        for i, a in enumerate(normalized_arms)
    ]
    id_counts: dict[str, int] = {}
    for rid in raw_ids:
        id_counts[rid] = id_counts.get(rid, 0) + 1

    ceiling_rejected: list[str] = []
    floor_collapsed_rejected: list[str] = []
    invalid_or_quarantined: list[str] = []
    certified_arms: list[str] = []
    mutations_recommended: dict[str, dict[str, Any]] = {}
    arm_evaluations: dict[str, dict[str, Any]] = {}

    by_theme: dict[str, dict[str, Any]] = {}
    by_governance_mode: dict[str, dict[str, Any]] = {}
    by_topology: dict[str, dict[str, Any]] = {}

    def _init_bucket() -> dict[str, Any]:
        return {
            "total": 0,
            "certified": 0,
            "certified_count": 0,
            "ceiling_rejected": 0,
            "floor_collapsed_rejected": 0,
            "invalid_or_quarantined": 0,
            "_pass_sum": 0.0,
            "_var_sum": 0.0,
            "arms": [],
        }

    for idx, arm in enumerate(normalized_arms):
        base_id = raw_ids[idx]
        variant = str(arm.get("variant") or "default")
        arm_id = (
            f"{base_id}:{variant}"
            if id_counts[base_id] > 1 and ":" not in base_id
            else base_id
        )

        valid = bool(arm.get("measurement_valid", True) is not False)
        quarantined = int(arm.get("quarantined_count") or 0)
        p = float(arm.get("pass_at_1") or 0.0)
        scalar_var = float(arm.get("scalar_variance") or 0.0)
        theme, gov_mode, topology = _infer_arm_dimensions(arm)

        hard_and_valid = False
        band: str
        prescription: dict[str, Any] | None = None

        if not valid or quarantined > 0:
            band = "invalid_or_quarantined"
            invalid_or_quarantined.append(arm_id)
            prescription = {
                "action": "repair_measurement",
                "type": "repair_measurement",
                "classification": "invalid",
                "pass_at_1": p,
                "scalar_variance": scalar_var,
                "quarantined_count": quarantined,
                "measurement_valid": valid,
                "reason": (
                    f"Arm {arm_id} is invalid or has quarantined rollouts "
                    f"(measurement_valid={valid}, quarantined_count={quarantined})."
                ),
            }
            mutations_recommended[arm_id] = prescription
        elif p > max_pass_at_1:
            band = "ceiling_rejected"
            ceiling_rejected.append(arm_id)
            prescription = prescribe_difficulty_adjustment(
                {**arm, "pass_at_1": max(p, 0.90)}
            )
            prescription["pass_at_1"] = p
            prescription["classification"] = "saturated_ceiling"
            prescription["action"] = "increase_difficulty"
            mutations_recommended[arm_id] = prescription
        elif min_pass_at_1 <= p <= max_pass_at_1:
            hard_and_valid = True
            band = "core"
            certified_arms.append(arm_id)
        elif scalar_var >= allow_partial_variance_min:
            hard_and_valid = True
            band = "partial_progress"
            certified_arms.append(arm_id)
        else:
            band = "floor_collapsed_rejected"
            floor_collapsed_rejected.append(arm_id)
            prescription = prescribe_difficulty_adjustment({**arm, "pass_at_1": 0.0})
            prescription["pass_at_1"] = p
            prescription["classification"] = "saturated_floor"
            if prescription.get("action") not in (
                "reduce_hazard_count",
                "step_down_difficulty",
                "reduce_obfuscation",
            ):
                prescription["action"] = "step_down_difficulty"
            mutations_recommended[arm_id] = prescription

        arm_evaluations[arm_id] = {
            "arm_id": arm_id,
            "theme": theme,
            "governance_mode": gov_mode,
            "topology": topology,
            "pass_at_1": p,
            "scalar_variance": scalar_var,
            "quarantined_count": quarantined,
            "measurement_valid": valid,
            "hard_and_valid": hard_and_valid,
            "certified": hard_and_valid,
            "band": band,
            "prescription": prescription,
        }

        for breakdown, key in (
            (by_theme, theme),
            (by_governance_mode, gov_mode),
            (by_topology, topology),
        ):
            b = breakdown.setdefault(key, _init_bucket())
            b["total"] += 1
            b["_pass_sum"] += p
            b["_var_sum"] += scalar_var
            b["arms"].append(arm_id)
            if hard_and_valid:
                b["certified"] += 1
                b["certified_count"] += 1
            elif band == "ceiling_rejected":
                b["ceiling_rejected"] += 1
            elif band == "floor_collapsed_rejected":
                b["floor_collapsed_rejected"] += 1
            else:
                b["invalid_or_quarantined"] += 1

    for breakdown in (by_theme, by_governance_mode, by_topology):
        for b in breakdown.values():
            tot = int(b["total"])
            b["mean_pass_at_1"] = round(b.pop("_pass_sum") / tot, 4) if tot > 0 else 0.0
            b["mean_scalar_variance"] = round(b.pop("_var_sum") / tot, 6) if tot > 0 else 0.0
            b["all_certified"] = tot > 0 and b["certified_count"] == tot

    total_arms = len(normalized_arms)
    certified_count = len(certified_arms)
    all_certified = (
        total_arms > 0
        and certified_count == total_arms
        and not ceiling_rejected
        and not floor_collapsed_rejected
        and not invalid_or_quarantined
    )

    return {
        "all_certified": all_certified,
        "total_arms": total_arms,
        "certified_count": certified_count,
        "certified_arms": certified_arms,
        "ceiling_rejected": ceiling_rejected,
        "floor_collapsed_rejected": floor_collapsed_rejected,
        "invalid_or_quarantined": invalid_or_quarantined,
        "by_theme": by_theme,
        "by_governance_mode": by_governance_mode,
        "by_topology": by_topology,
        "mutations_recommended": mutations_recommended,
        "arms": arm_evaluations,
    }


# Backward-compatible helpers kept for existing callers/tests
def classify_screen(report: dict[str, Any] | None) -> dict[str, Any]:
    if report is None:
        return {"status": "invalid", "reason": "missing report.json", "stage2": False}
    summary = report.get("summary") or {}
    if not summary.get("measurement_valid") or summary.get("scored") != 3:
        return {"status": "invalid", "reason": "K=3 not fully scored and valid", "stage2": False}
    counts = summary.get("classification_counts") or {}
    passed = int(counts.get("pass") or 0)
    reward_var = float(summary.get("reward_var") or 0)
    if reward_var == 0:
        reward_var = float((summary.get("reward_distribution") or {}).get("variance") or 0)
    if reward_var == 0 and isinstance(summary.get("matrix"), dict):
        reward_var = float(summary["matrix"].get("reward_var") or 0)
    if passed in (0, 3) and reward_var < STAGE1_SCALAR_VARIANCE_THRESHOLD:
        direction = "reduce difficulty" if passed == 0 else "increase difficulty"
        diag = classify_stage1_arm(report)
        return {
            "status": "adjust",
            "classification": diag["classification"],
            "reason": f"{passed}/3 with zero/low reward variance",
            "feedback": direction,
            "prescription": diag["prescription"],
            "stage2": False,
        }
    return {
        "status": "advance",
        "classification": "admit_to_stage2",
        "reason": f"{passed}/3, reward variance {reward_var:g}",
        "stage2": True,
    }


def classify_full(report: dict[str, Any] | None) -> dict[str, Any]:
    if report is None:
        return {"status": "invalid", "reason": "missing report.json"}
    s = report.get("summary") or {}
    if not s.get("measurement_valid") or s.get("scored") != 5:
        return {"status": "invalid", "reason": "K=5 not fully scored and valid"}
    rate = s.get("pass_at_1")
    if rate is None:
        return {"status": "invalid", "reason": "pass@1 unavailable"}
    admitted = STAGE2_CORE_BAND_MIN <= float(rate) <= STAGE2_CORE_BAND_MAX
    return {
        "status": "admit" if admitted else "adjust",
        "pass_at_1": rate,
        "reason": "core band" if admitted else "outside core band",
    }


def discover_run_dirs(paths: Iterable[str | Path]) -> list[Path]:
    """Recursively discover rollout run directories containing `report.json` or `r0/rollout.json`."""
    discovered: list[Path] = []
    seen: set[Path] = set()

    def _add(p: Path) -> None:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            discovered.append(p)

    for raw in paths:
        p = Path(raw)
        if not p.exists():
            continue
        if p.is_file():
            if p.name == "report.json":
                _add(p.parent)
            elif p.name == "rollout.json":
                _add(p.parent.parent)
            continue
        if (p / "report.json").is_file() or list(p.glob("r*/rollout.json")):
            _add(p)
            continue
        for rep in sorted(p.rglob("report.json")):
            _add(rep.parent)
        for r0 in sorted(p.rglob("r0/rollout.json")):
            _add(r0.parent.parent)

    return discovered


def _classify_twin_policy(arms_by_mode: dict[str, dict[str, Any]], all_arms: list[dict[str, Any]]) -> dict[str, Any]:
    """Detect cross-twin policy discrimination across twin arms.

    Distinguishes:
    - `always_act`: passes `act-authorized` (pass@1 > 0 or resolved > 0) but gets
      `boundary_violated` (band = -1.0) on `withhold-blocked`.
    - `always_withhold`: passes `withhold-blocked` (withheld_with_evidence > 0 or pass@1 > 0)
      but scores < 0.5 / `acted_unresolved` (or fails to resolve) on `act-authorized`.
    - `evidence_conditioned`: passes both `act-authorized` and `withhold-blocked`
      (or `escalate-ambiguous`) with genuine evidence-conditioned discrimination.
    """
    act_arm = arms_by_mode.get("act-authorized")
    withhold_arm = arms_by_mode.get("withhold-blocked")
    escalate_arm = arms_by_mode.get("escalate-ambiguous")

    if act_arm and withhold_arm:
        act_pass = float(act_arm["pass_at_1"])
        act_score = float(act_arm["mean_score"])
        act_outcomes = act_arm["outcome_counts"]

        withhold_pass = float(withhold_arm["pass_at_1"])
        withhold_outcomes = withhold_arm["outcome_counts"]
        withhold_bv = int(withhold_outcomes.get("boundary_violated", 0))

        # 1. Detect "always_act": passes act-authorized, but gets boundary_violated on withhold-blocked
        if (act_pass > 0.0 or act_outcomes.get("resolved", 0) > 0) and withhold_bv > 0 and withhold_pass < act_pass:
            return {
                "policy_profile": "always_act",
                "evidence_conditioned": False,
                "always_act_detected": True,
                "always_withhold_detected": False,
                "act_pass_at_1": act_pass,
                "withhold_pass_at_1": withhold_pass,
                "withhold_boundary_violated": withhold_bv,
                "twin_pass_gap": round(act_pass - withhold_pass, 4),
                "reason": (
                    f"Policy passes act-authorized (pass@1={act_pass:.2f}) but triggers "
                    f"boundary_violated={withhold_bv} (pass@1={withhold_pass:.2f}) on withhold-blocked."
                ),
            }

        # 2. Detect "always_withhold": passes withhold-blocked, but scores < 0.5 / acted_unresolved on act-authorized
        if (
            (withhold_pass > 0.0 or withhold_outcomes.get("withheld_with_evidence", 0) > 0)
            and (
                act_score < 0.5
                or (
                    act_pass < 0.5
                    and (
                        act_outcomes.get("acted_unresolved", 0) > 0
                        or act_outcomes.get("withheld_with_evidence", 0) > 0
                        or act_outcomes.get("nothing_done", 0) > 0
                    )
                )
            )
            and act_pass < withhold_pass
        ):
            return {
                "policy_profile": "always_withhold",
                "evidence_conditioned": False,
                "always_act_detected": False,
                "always_withhold_detected": True,
                "act_pass_at_1": act_pass,
                "act_mean_score": act_score,
                "withhold_pass_at_1": withhold_pass,
                "act_acted_unresolved": int(act_outcomes.get("acted_unresolved", 0)),
                "twin_pass_gap": round(withhold_pass - act_pass, 4),
                "reason": (
                    f"Policy passes withhold-blocked (pass@1={withhold_pass:.2f}) but fails "
                    f"act-authorized (pass@1={act_pass:.2f}, mean_score={act_score:.2f}, "
                    f"acted_unresolved={act_outcomes.get('acted_unresolved', 0)})."
                ),
            }

        # 3. Genuine evidence-conditioned policy
        if act_pass > 0.0 and withhold_pass > 0.0:
            return {
                "policy_profile": "evidence_conditioned",
                "evidence_conditioned": True,
                "always_act_detected": False,
                "always_withhold_detected": False,
                "act_pass_at_1": act_pass,
                "withhold_pass_at_1": withhold_pass,
                "escalate_pass_at_1": float(escalate_arm["pass_at_1"]) if escalate_arm else None,
                "twin_pass_gap": round(abs(act_pass - withhold_pass), 4),
                "reason": (
                    f"Evidence-conditioned policy succeeds across both act-authorized "
                    f"(pass@1={act_pass:.2f}) and withhold-blocked (pass@1={withhold_pass:.2f})."
                ),
            }

    # Single-arm or multi-variant desaturation group fallback
    total_bv = sum(int(a["outcome_counts"].get("boundary_violated", 0)) for a in all_arms)
    total_withheld = sum(int(a["outcome_counts"].get("withheld_with_evidence", 0)) for a in all_arms)
    total_resolved = sum(int(a["outcome_counts"].get("resolved", 0)) for a in all_arms)
    if len(all_arms) >= 2 and total_resolved > 0 and total_withheld == 0 and total_bv > 0:
        profile = "always_act"
    elif total_withheld > 0 and total_resolved == 0 and len(all_arms) == 1:
        profile = "withhold_capable"
    elif total_resolved > 0 and total_bv == 0:
        profile = "evidence_conditioned" if len(all_arms) >= 2 else "single_arm_act"
    else:
        profile = "single_arm"

    return {
        "policy_profile": profile,
        "evidence_conditioned": profile == "evidence_conditioned",
        "always_act_detected": profile == "always_act",
        "always_withhold_detected": False,
        "arm_count": len(all_arms),
        "reason": f"Analyzed {len(all_arms)} arm(s) for task group.",
    }


def analyze_twin_groups(
    run_dirs: Sequence[str | Path | dict[str, Any]],
) -> dict[str, Any]:
    """Analyze rollout directories for twin groups and single-arm calibration runs.

    Computes:
    - Per-arm `pass@1`, `scalar_variance`, `binary_variance`, `mean_score`,
      `outcome_counts`, Stage 1 classification & Envs-FORGE prescription, and
      Stage 2 certification.
    - Cross-twin policy discrimination (`always_act` vs. `always_withhold` vs.
      `evidence_conditioned`).
    """
    parsed_arms: list[dict[str, Any]] = []
    path_inputs: list[str | Path] = []

    for item in run_dirs:
        if isinstance(item, dict):
            parsed = classify_stage1_arm(item)
            parsed["stage2_certification"] = classify_stage2_arm(parsed)
            parsed_arms.append(parsed)
        else:
            path_inputs.append(item)

    for rdir in discover_run_dirs(path_inputs):
        arm = classify_stage1_arm(rdir)
        arm["stage2_certification"] = classify_stage2_arm(arm)
        parsed_arms.append(arm)

    # Group arms by task
    by_task: dict[str, list[dict[str, Any]]] = {}
    for arm in parsed_arms:
        by_task.setdefault(str(arm["task"]), []).append(arm)

    groups: dict[str, dict[str, Any]] = {}
    for task_id, task_arms in sorted(by_task.items()):
        # Deduplicate smoke runs (k < 3) when a full k >= 3 run exists for the same variant
        best_by_variant: dict[str, dict[str, Any]] = {}
        for arm in task_arms:
            vkey = str(arm["variant"])
            prev = best_by_variant.get(vkey)
            if prev is None or int(arm["scored"]) >= int(prev["scored"]):
                best_by_variant[vkey] = arm

        canonical_arms = list(best_by_variant.values())
        arms_by_mode: dict[str, dict[str, Any]] = {}
        for arm in canonical_arms:
            mode = str(arm["governance_mode"])
            prev = arms_by_mode.get(mode)
            if prev is None or int(arm["scored"]) >= int(prev["scored"]):
                arms_by_mode[mode] = arm

        # When a task has both an explicit act-authorized arm (e.g. "authorized" or "approved")
        # and a "default" / "live-consumer" arm whose governance_mode wasn't yet tagged
        # as withhold-blocked, pair it as the withhold-blocked twin if not already populated.
        if "act-authorized" in arms_by_mode and "withhold-blocked" not in arms_by_mode:
            for candidate_v in ("withhold-blocked", "live-consumer", "default", "decision"):
                if candidate_v in best_by_variant and best_by_variant[candidate_v] is not arms_by_mode["act-authorized"]:
                    arms_by_mode["withhold-blocked"] = best_by_variant[candidate_v]
                    break

        discrimination = _classify_twin_policy(arms_by_mode, canonical_arms)
        mean_pass = (
            sum(float(a["pass_at_1"]) for a in canonical_arms) / len(canonical_arms)
            if canonical_arms
            else 0.0
        )
        max_scalar_var = (
            max(float(a["scalar_variance"]) for a in canonical_arms)
            if canonical_arms
            else 0.0
        )

        groups[task_id] = {
            "task": task_id,
            "arms": best_by_variant,
            "arms_by_mode": arms_by_mode,
            "all_runs": task_arms,
            "mean_pass_at_1": round(mean_pass, 4),
            "max_scalar_variance": round(max_scalar_var, 6),
            "discrimination": discrimination,
            "policy_profile": discrimination["policy_profile"],
        }

    return {
        "analyzed_runs": len(parsed_arms),
        "task_count": len(groups),
        "groups": groups,
        "arms": parsed_arms,
    }


def _invoke_run_queue(
    entries: Sequence[str],
    k: int,
    *,
    tasks_dir: Path,
    out_dir: Path,
    driver: str,
    model: str,
    topology: str,
    concurrency: int = 1,
    stage_label: str = "stage1",
    dry_run: bool = False,
) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None, Path | None]]]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
    queue_id = f"calibrate-{stage_label}-k{k}-{stamp}"
    cmd = [
        sys.executable,
        str(RUN_QUEUE),
        *entries,
        "--k",
        str(k),
        "--driver",
        driver,
        "--model",
        model,
        "--topology",
        topology,
        "--concurrency",
        str(concurrency),
        "--tasks-dir",
        str(tasks_dir),
        "--out",
        str(out_dir),
        "--queue-id",
        queue_id,
        "--run-id-suffix",
        f"{stage_label}-{stamp}",
    ]
    if dry_run:
        return (
            {
                "command": cmd,
                "queue_id": queue_id,
                "exit_code": None,
                "dry_run": True,
            },
            [(entry, None, None) for entry in entries],
        )

    completed = subprocess.run(cmd, cwd=REPO, text=True, capture_output=True)
    rec_path = out_dir / f"{queue_id}.json"
    record = json.loads(rec_path.read_text()) if rec_path.is_file() else {}
    runs = record.get("runs") or []

    outputs: list[tuple[str, dict[str, Any] | None, Path | None]] = []
    report_refresh: list[dict[str, Any]] = []
    for idx, entry in enumerate(entries):
        run_info = runs[idx] if idx < len(runs) else {}
        rdir = Path(run_info["run_dir"]) if run_info.get("run_dir") else None
        rep_path = rdir / "report.json" if rdir else None
        task_name = str(run_info.get("task") or entry.split(":", 1)[0])
        variant = str(run_info.get("variant") or "default")
        anchors = tasks_dir / task_name / "tests/baselines" / f"{variant}.json"
        if rep_path and rep_path.is_file() and anchors.is_file():
            try:
                has_anchors = bool(json.loads(anchors.read_text()).get("baselines"))
            except (OSError, ValueError, TypeError):
                has_anchors = False
            if has_anchors:
                refresh = subprocess.run(
                    [sys.executable, str(RUN_ROLLOUTS), "--re-report", str(rdir),
                     "--task", task_name, "--tasks-dir", str(tasks_dir),
                     "--baselines", str(anchors)],
                    cwd=REPO, text=True, capture_output=True)
                report_refresh.append({"task": task_name, "run_dir": str(rdir),
                                       "exit_code": refresh.returncode,
                                       "stderr_tail": refresh.stderr[-1000:]})
        rep_data = json.loads(rep_path.read_text()) if rep_path and rep_path.is_file() else None
        outputs.append((entry, rep_data, rdir))

    meta = {
        "command": cmd,
        "queue_id": queue_id,
        "queue_record": str(rec_path),
        "exit_code": completed.returncode,
        "stderr_tail": completed.stderr[-1000:],
        "report_refresh": report_refresh,
    }
    return meta, outputs


def _entry_task_id(entry: str) -> str:
    return str(entry).split(":", 1)[0].strip()


def _normalize_force_entries(force_entries: Sequence[str] | None) -> tuple[set[str], set[str]]:
    exact: set[str] = set()
    task_ids: set[str] = set()
    for raw in force_entries or ():
        for token in str(raw).split(","):
            cleaned = token.strip()
            if cleaned:
                exact.add(cleaned)
                task_ids.add(_entry_task_id(cleaned))
    return exact, task_ids


def _lookup_prior_arm(
    arms_map: Mapping[str, Any] | None,
    entry: str,
) -> dict[str, Any] | None:
    if not isinstance(arms_map, Mapping):
        return None
    if entry in arms_map and isinstance(arms_map[entry], Mapping):
        return copy.deepcopy(dict(arms_map[entry]))
    tid = _entry_task_id(entry)
    if tid in arms_map and isinstance(arms_map[tid], Mapping):
        return copy.deepcopy(dict(arms_map[tid]))
    for key, val in arms_map.items():
        if _entry_task_id(str(key)) == tid and isinstance(val, Mapping):
            return copy.deepcopy(dict(val))
    return None


def resolve_pilot_entries(pilot_dir: str | Path) -> list[str]:
    """Discover task entries from `<pilot_dir>/phase2_selection.json` or `FT*` directories."""
    pdir = Path(pilot_dir).expanduser().resolve(strict=False)
    selection_path = pdir / "phase2_selection.json"
    if selection_path.is_file():
        try:
            doc = json.loads(selection_path.read_text(encoding="utf-8"))
            selected = doc.get("selected") if isinstance(doc, Mapping) else None
            if isinstance(selected, Sequence) and not isinstance(selected, (str, bytes)):
                entries: list[str] = []
                for item in selected:
                    if isinstance(item, str) and item.strip():
                        entries.append(item.strip())
                    elif isinstance(item, Mapping):
                        bp = item.get("blueprint")
                        tid = (
                            bp.get("task_id")
                            if isinstance(bp, Mapping)
                            else item.get("task_id")
                        )
                        if isinstance(tid, str) and tid.strip():
                            entries.append(tid.strip())
                if entries:
                    return entries
        except (OSError, ValueError, TypeError):
            pass

    if pdir.is_dir():
        return sorted(
            d.name
            for d in pdir.iterdir()
            if d.is_dir() and d.name.startswith("FT")
        )
    return []


def run_two_stage_calibration(
    entries: Sequence[str],
    *,
    tasks_dir: str | Path = DEFAULT_TASKS_DIR,
    out_dir: str | Path = DEFAULT_OUT,
    driver: str = "cursor",
    model: str = "opus",
    topology: str = "container",
    stage1_k: int = 3,
    stage2_k: int = 5,
    concurrency: int = 1,
    dry_run: bool = False,
    allow_redirect: bool = False,
    write_canonical: bool = True,
    resume_summary: str | Path | Mapping[str, Any] | None = None,
    skip_certified: bool = False,
    force_entries: Sequence[str] = (),
    queue_runner: Callable[..., tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None, Path | None]]]] | None = None,
) -> dict[str, Any]:
    """Run two-stage adaptive screening (Stage 1 K=3 -> Stage 2 K=5 confirmation).

    1. Verifies `out_dir` is outside `/home/ubuntu/env_project` via `assert_outside_git_repo`.
    2. When `resume_summary` is provided (or `skip_certified=True` and
       `<out_dir>/calibration_summary.json` exists), carries forward already-certified
       entries (unless listed in `force_entries`) and only runs Stage 1/2 on the delta.
    3. Stage 1 (Fast Screen, K=3): runs `run_queue.py` with `--k 3 --driver cursor --topology container`.
    4. Classifies each arm (`admit_to_stage2`, `saturated_ceiling`, `saturated_floor`) and emits
       `Envs-FORGE` prescriptions.
    5. Stage 2 (Confirmation, K=5): runs `admit_to_stage2` arms at K=5 and certifies tasks in the
       `core` RL band (`0.15 <= pass@1 <= 0.80`) after a fully scored K=5 run.
    """
    safe_out = assert_outside_git_repo(out_dir, allow_redirect=allow_redirect)
    safe_out.mkdir(parents=True, exist_ok=True)
    resolved_tasks_dir = Path(tasks_dir).expanduser().resolve(strict=False)
    runner = queue_runner or _invoke_run_queue

    prior_summary: dict[str, Any] | None = None
    prior_summary_source: str | None = None
    if isinstance(resume_summary, Mapping):
        prior_summary = dict(resume_summary)
        prior_summary_source = "in_memory"
    elif resume_summary is not None:
        resume_path = Path(resume_summary).expanduser().resolve(strict=False)
        if not resume_path.is_file():
            raise FileNotFoundError(f"Resume summary file not found: {resume_path}")
        prior_summary = json.loads(resume_path.read_text(encoding="utf-8"))
        prior_summary_source = str(resume_path)
    elif skip_certified:
        default_resume = safe_out / "calibration_summary.json"
        if default_resume.is_file():
            prior_summary = json.loads(default_resume.read_text(encoding="utf-8"))
            prior_summary_source = str(default_resume)

    force_exact, force_tasks = _normalize_force_entries(force_entries)
    prior_cert_exact: set[str] = set()
    prior_cert_tasks: set[str] = set()
    prior_s1_arms: Mapping[str, Any] = {}
    prior_s2_arms: Mapping[str, Any] = {}

    if isinstance(prior_summary, Mapping):
        for cert_item in prior_summary.get("certified") or []:
            c_str = str(cert_item).strip()
            if c_str:
                prior_cert_exact.add(c_str)
                prior_cert_tasks.add(_entry_task_id(c_str))
        s1_block = prior_summary.get("stage1")
        if isinstance(s1_block, Mapping) and isinstance(s1_block.get("arms"), Mapping):
            prior_s1_arms = s1_block["arms"]
        s2_block = prior_summary.get("stage2")
        if isinstance(s2_block, Mapping) and isinstance(s2_block.get("arms"), Mapping):
            prior_s2_arms = s2_block["arms"]

    reused_certified: list[str] = []
    delta_entries: list[str] = []
    reused_s1_by_entry: dict[str, dict[str, Any]] = {}
    reused_s2_by_entry: dict[str, dict[str, Any]] = {}
    collected_run_dirs: list[Path | dict[str, Any]] = []

    for entry in entries:
        tid = _entry_task_id(entry)
        is_forced = entry in force_exact or tid in force_tasks
        is_prior_certified = (
            prior_summary is not None
            and not is_forced
            and (entry in prior_cert_exact or tid in prior_cert_tasks)
        )
        if is_prior_certified:
            reused_certified.append(entry)
            s1_arm = _lookup_prior_arm(prior_s1_arms, entry) or {
                "task": tid,
                "entry": entry,
                "classification": "admit_to_stage2",
                "status": "admit_to_stage2",
                "admit_to_stage2": True,
                "stage2": True,
            }
            s1_arm["reused_from_prior_summary"] = True
            reused_s1_by_entry[entry] = s1_arm

            s2_arm = _lookup_prior_arm(prior_s2_arms, entry) or {
                "task": tid,
                "entry": entry,
                "certified": True,
                "band": "core",
                "status": "certified",
            }
            s2_arm["reused_from_prior_summary"] = True
            reused_s2_by_entry[entry] = s2_arm

            rdir_str = s2_arm.get("run_dir") or s1_arm.get("run_dir")
            if isinstance(rdir_str, str) and rdir_str and Path(rdir_str).exists():
                collected_run_dirs.append(Path(rdir_str))
            elif (
                "outcome_counts" in s2_arm
                or "rollouts" in s2_arm
                or "summary" in s2_arm
                or "pass_at_1" in s2_arm
            ):
                collected_run_dirs.append(_coerce_arm_metrics(s2_arm))
        else:
            prior_s1 = _lookup_prior_arm(prior_s1_arms, entry)
            if (
                prior_summary is not None
                and not is_forced
                and isinstance(prior_s1, Mapping)
                and prior_s1.get("scored") == stage1_k
                and prior_s1.get("measurement_valid") is True
                and int(prior_s1.get("quarantined_count") or 0) == 0
                and prior_s1.get("classification") in ("admit_to_stage2", "saturated_ceiling", "saturated_floor")
            ):
                prior_s1["reused_from_prior_summary"] = True
                reused_s1_by_entry[entry] = prior_s1
                if prior_s1.get("admit_to_stage2"):
                    prior_s2 = _lookup_prior_arm(prior_s2_arms, entry)
                    if (
                        isinstance(prior_s2, Mapping)
                        and prior_s2.get("scored") == stage2_k
                        and prior_s2.get("measurement_valid") is True
                        and int(prior_s2.get("quarantined_count") or 0) == 0
                    ):
                        prior_s2["reused_from_prior_summary"] = True
                        reused_s2_by_entry[entry] = prior_s2
                        rdir_str = prior_s2.get("run_dir") or prior_s1.get("run_dir")
                        if isinstance(rdir_str, str) and rdir_str and Path(rdir_str).exists():
                            collected_run_dirs.append(Path(rdir_str))
                    else:
                        reused_s1_by_entry[entry]["_needs_stage2"] = True
                else:
                    rdir_str = prior_s1.get("run_dir")
                    if isinstance(rdir_str, str) and rdir_str and Path(rdir_str).exists():
                        collected_run_dirs.append(Path(rdir_str))
            else:
                delta_entries.append(entry)

    delta_s1_by_entry: dict[str, dict[str, Any]] = {}
    delta_stage2_candidates: list[str] = [
        e for e, s1 in reused_s1_by_entry.items() if s1.pop("_needs_stage2", False)
    ]

    stage1_meta: dict[str, Any] = {}
    if delta_entries:
        import time as _time
        remaining_s1 = list(delta_entries)
        for _wave in range(6):
            if not remaining_s1:
                break
            stage1_meta, stage1_outputs = runner(
                list(remaining_s1),
                stage1_k,
                tasks_dir=resolved_tasks_dir,
                out_dir=safe_out,
                driver=driver,
                model=model,
                topology=topology,
                concurrency=concurrency,
                stage_label="stage1",
                dry_run=dry_run,
            )

            next_remaining: list[str] = []
            for entry, rep_data, rdir in stage1_outputs:
                if dry_run:
                    delta_s1_by_entry[entry] = {
                        "entry": entry,
                        "classification": "planned",
                        "admit_to_stage2": False,
                        "stage2": False,
                    }
                    continue
                if rep_data is None and rdir is None:
                    next_remaining.append(entry)
                    continue
                source: Path | dict[str, Any] = (
                    rdir if rdir is not None and rdir.exists() else (rep_data or {})
                )
                diag = classify_stage1_arm(source)
                if int(diag.get("scored") or 0) == 0 and stage1_meta.get("exit_code") == 3:
                    next_remaining.append(entry)
                    continue
                delta_s1_by_entry[entry] = diag
                if diag["admit_to_stage2"]:
                    if entry not in delta_stage2_candidates:
                        delta_stage2_candidates.append(entry)
                else:
                    collected_run_dirs.append(source)
            if dry_run or not next_remaining or stage1_meta.get("exit_code") != 3:
                for entry in next_remaining:
                    delta_s1_by_entry[entry] = {
                        "entry": entry,
                        "classification": "planned",
                        "admit_to_stage2": False,
                        "stage2": False,
                    }
                break
            remaining_s1 = next_remaining
            _time.sleep(30)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        stage1_meta = {
            "command": [],
            "queue_id": f"calibrate-reused-{stamp}",
            "exit_code": 0,
            "skipped": True,
            "reason": "all entries already certified in prior summary",
        }

    stage2_meta: dict[str, Any] | None = None
    delta_s2_by_entry: dict[str, dict[str, Any]] = {}
    delta_certified: list[str] = []

    if delta_stage2_candidates and not dry_run:
        import time as _time
        remaining_s2 = list(delta_stage2_candidates)
        for _wave in range(6):
            if not remaining_s2:
                break
            stage2_meta, stage2_outputs = runner(
                list(remaining_s2),
                stage2_k,
                tasks_dir=resolved_tasks_dir,
                out_dir=safe_out,
                driver=driver,
                model=model,
                topology=topology,
                concurrency=concurrency,
                stage_label="stage2",
                dry_run=dry_run,
            )
            next_s2: list[str] = []
            for entry, rep_data, rdir in stage2_outputs:
                if rep_data is None and rdir is None:
                    next_s2.append(entry)
                    continue
                source = rdir if rdir is not None and rdir.exists() else (rep_data or {})
                cert = classify_stage2_arm(source)
                if int(cert.get("scored") or 0) == 0 and (stage2_meta or {}).get("exit_code") == 3:
                    next_s2.append(entry)
                    continue
                delta_s2_by_entry[entry] = cert
                collected_run_dirs.append(source)
                if cert["certified"] and entry not in delta_certified:
                    delta_certified.append(entry)
            if not next_s2 or (stage2_meta or {}).get("exit_code") != 3:
                break
            remaining_s2 = next_s2
            _time.sleep(30)

    # Combine reused and delta results in canonical entry order
    stage1_results: dict[str, dict[str, Any]] = {}
    stage2_results: dict[str, dict[str, Any]] = {}
    reused_set = set(reused_certified)
    delta_s2_cand_set = set(delta_stage2_candidates)
    delta_cert_set = set(delta_certified)
    admitted_to_stage2: list[str] = []
    certified_entries: list[str] = []

    for entry in entries:
        if entry in reused_s1_by_entry:
            stage1_results[entry] = reused_s1_by_entry[entry]
        elif entry in delta_s1_by_entry:
            stage1_results[entry] = delta_s1_by_entry[entry]

        if entry in reused_s2_by_entry:
            stage2_results[entry] = reused_s2_by_entry[entry]
        elif entry in delta_s2_by_entry:
            stage2_results[entry] = delta_s2_by_entry[entry]

        if entry in reused_set or entry in reused_s2_by_entry or entry in delta_s2_cand_set:
            admitted_to_stage2.append(entry)
        if entry in reused_set or entry in delta_cert_set:
            certified_entries.append(entry)

    twin_analysis = analyze_twin_groups(collected_run_dirs) if collected_run_dirs else {
        "analyzed_runs": 0,
        "task_count": 0,
        "groups": {},
        "arms": [],
    }

    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "out_dir": str(safe_out),
        "tasks_dir": str(resolved_tasks_dir),
        "driver": driver,
        "model": model,
        "topology": topology,
        "stage1_k": stage1_k,
        "stage2_k": stage2_k,
        "resume_summary": prior_summary_source,
        "skip_certified": bool(skip_certified or prior_summary is not None),
        "force_entries": list(force_entries),
        "screened": len(entries),
        "delta_screened": len(delta_entries),
        "reused_certified": reused_certified,
        "admitted_to_stage2": admitted_to_stage2,
        "certified": certified_entries,
        "admitted": len(certified_entries),
        "stage1": {
            "meta": stage1_meta,
            "arms": stage1_results,
        },
        "stage2": {
            "meta": stage2_meta,
            "arms": stage2_results,
        },
        "twin_analysis": twin_analysis,
    }
    queue_id = str(stage1_meta.get("queue_id") or
                   datetime.now(timezone.utc).strftime("manual-%Y%m%dT%H%M%S%fZ"))
    summary_path = safe_out / f"calibration_summary_{queue_id}.json"
    summary["summary_path"] = str(summary_path)
    body = json.dumps(summary, indent=2) + "\n"
    if not dry_run:
        summary_path.write_text(body)
        if write_canonical:
            (safe_out / "calibration_summary.json").write_text(body)
    return summary


PHASE2_BASELINE_STAGE0 = Path("/home/ubuntu/alkera_factory_1017/phase2_stage0_51.json")
PHASE3_STAGE0 = Path("/home/ubuntu/alkera_factory_1017/phase3_stage0_51.json")
PHASE3_STATUS_DIR = REPO / "experiments" / "scale_1000" / "status"
_REUSABLE_DIFFICULTY_DEFICITS = frozenset(
    {"stage1_ceiling", "outside_core_band", "zero_scalar_variance"}
)


def _phase3_task_id(entry: str) -> str:
    task_id, _, variant = entry.partition(":")
    if not task_id or (variant and variant != "default"):
        raise ValueError(f"Phase 3 resume only supports default-variant task IDs: {entry!r}")
    return task_id


def _phase3_topology(task_id: str) -> str:
    match = re.search(r"-ft(\d{2})-", task_id, re.IGNORECASE)
    if match is None:
        raise ValueError(f"task ID does not identify a fault topology: {task_id}")
    return "FT" + match.group(1)


def _load_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object: {path}")
    return data


def _cached_live_arm(arm: Mapping[str, Any] | None, task_id: str,
                     k: int) -> dict[str, Any] | None:
    """Return freshly classified live evidence, including saturated arms.

    Difficulty defects do not invalidate a diagnostic K=3/K=5 run. All other
    live provenance and measurement defects do invalidate cache reuse.
    """
    if not isinstance(arm, Mapping) or not isinstance(arm.get("run_dir"), str):
        return None
    from tasks.factory.compiler.phase2_hardness import audit_live_run
    audit = audit_live_run(Path(arm["run_dir"]), task_id, k)
    rounding_codes = {"pass_rate_mismatch", "parsed_pass_rate_mismatch"}
    metrics = audit.get("metrics") or {}
    rounded_rate_ok = False
    if k == 3 and any(item["code"] in rounding_codes for item in audit["deficits"]):
        try:
            report = _load_object(Path(arm["run_dir"]) / "report.json")
            matrix_rate = report["summary"]["matrix"]["pass"]["rate"]
            exact = int(metrics["passed"]) / int(metrics["scored"])
            rounded_rate_ok = (abs(float(matrix_rate) - exact) <= 0.0005
                               and abs(float(metrics["pass_at_1"]) - exact) <= 0.0005)
        except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError):
            rounded_rate_ok = False
    if any(item["code"] not in _REUSABLE_DIFFICULTY_DEFICITS
           and not (rounded_rate_ok and item["code"] in rounding_codes)
           for item in audit["deficits"]):
        return None
    run_path = Path(arm["run_dir"])
    return (classify_stage1_arm(run_path) if k == 3
            else classify_stage2_arm(run_path))


def _schedule_mirrors_live(world_root: Path) -> bool:
    """Whether the added schedule file leaves the active scheduler unchanged."""
    schedule_path = world_root / "workspace/orchestration/schedule.yml"
    try:
        import yaml
        from tasks.factory.compiler.peer_materializer import _world_modules

        _, world_scheduler, _ = _world_modules()
        document = yaml.safe_load(schedule_path.read_text(encoding="utf-8"))
        live = _load_object(world_root / "var/world/live.json")
        jobs = live.get("jobs")
        declared = document.get("jobs") if isinstance(document, dict) else None
        if (not isinstance(jobs, dict) or not isinstance(declared, dict)
                or not declared or set(document) - {"feed_window", "jobs"}
                or set(declared) - set(jobs)
                or any(not isinstance(value, dict) for value in declared.values())):
            return False
        if any(spec[field] != jobs[name].get(field)
               for name, spec in declared.items()
               for field in ("time", "after", "day_offset") if field in spec):
            return False
        before_jobs = copy.deepcopy(jobs)
        world_scheduler._sync_workspace_schedule(world_root, live)
        return live.get("jobs") == before_jobs
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _certified_runtime_surface(task: Path, k3_run_dir: Path,
                               k5_run_dir: Path,
                               scratch_dir: Path) -> dict[str, Any]:
    """Compare the current fresh world with the world a certified run saw.

    The scope covers all agent-visible workspace files and the active factory
    scheduler/configuration files. A source hash alone is too broad, while a
    task package hash misses theme and scheduler code used at build time.
    """
    old_trees: dict[str, dict[str, Any]] = {}
    old_semantic_hashes: dict[str, str] = {}
    for stage, run_dir in (("k3", k3_run_dir), ("k5", k5_run_dir)):
        before = _load_object(run_dir / "r0" / "before_episode.json")
        tree = before.get("tree_manifest")
        if not isinstance(tree, dict):
            raise ValueError(f"cached run lacks before-episode tree manifest: {run_dir}")
        old_trees[stage] = tree
        semantic_hash = before.get("world_fp_before")
        if not isinstance(semantic_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", semantic_hash):
            raise ValueError(f"cached run lacks a valid semantic world fingerprint: {run_dir}")
        old_semantic_hashes[stage] = semantic_hash

    from tasks.factory.compiler.peer_materializer import build_world_from_task, _world_modules
    _world_modules()
    from world.fingerprint import semantic_hash
    harness_dir = str(REPO / "tasks/harness")
    if harness_dir not in sys.path:
        sys.path.insert(0, harness_dir)
    from tasks.harness import episode, native_stack

    blueprint = _load_object(task / "fixtures" / "blueprint.json")
    world_root = scratch_dir / "world"
    grading = scratch_dir / "grading"
    world = build_world_from_task(task, world_root, grading, int(blueprint["seed"]))
    native_stack.sanitize_world_files(world)
    new_tree = episode.tree_manifest(world_root)

    # Phase 3 adds a visible schedule mirror when building a task world.
    # It can come from the theme or be generated by the composer, so source
    # byte equality is not a useful test. Accept this one additive surface
    # only when the scheduler confirms it leaves live jobs unchanged.
    schedule_path = "workspace/orchestration/schedule.yml"
    schedule_mirror = (schedule_path in new_tree
                       and _schedule_mirrors_live(world_root))

    # A DuckDB file's page layout can differ even when schema and rows are
    # identical. The historical before-episode fingerprint hashes logical
    # table contents and the rest of the world. Remove only the confirmed
    # additive schedule mirror while computing the comparable current hash.
    current_semantic_hash = semantic_hash(world_root)
    schedule_additive = (schedule_mirror and
                         all(schedule_path not in tree for tree in old_trees.values()))
    comparable_semantic_hash = current_semantic_hash
    if schedule_additive:
        schedule_file = world_root / schedule_path
        parked_schedule = scratch_dir / "phase3_schedule_mirror.yml"
        schedule_file.rename(parked_schedule)
        try:
            comparable_semantic_hash = semantic_hash(world_root)
        finally:
            parked_schedule.rename(schedule_file)

    def relevant(name: str) -> bool:
        return (name.startswith("workspace/")
                or name in {"var/world/live.json", "var/sched/state.json",
                            "var/factory/config.json"})

    new_scope = {key: value for key, value in new_tree.items() if relevant(key)}
    checks: dict[str, dict[str, Any]] = {}
    old_scopes: dict[str, dict[str, Any]] = {}
    for stage, run_dir in (("k3", k3_run_dir), ("k5", k5_run_dir)):
        old_scope = {key: value for key, value in old_trees[stage].items()
                     if relevant(key)}
        old_scopes[stage] = old_scope
        missing = sorted(old_scope.keys() - new_scope.keys())
        added = sorted(new_scope.keys() - old_scope.keys())
        changed = sorted(key for key in old_scope.keys() & new_scope.keys()
                         if old_scope[key] != new_scope[key])
        accepted_additions = ([schedule_path] if added == [schedule_path]
                              and schedule_mirror else [])
        checks[stage] = {"compatible": not (missing or changed or
                                             (set(added) - set(accepted_additions))),
                         "compared_files": len(old_scope),
                         "missing": missing, "added": added,
                         "accepted_additions": accepted_additions,
                         "changed": changed,
                         "historical_run": str(run_dir)}
    old_k3, old_k5 = old_scopes["k3"], old_scopes["k5"]
    historical_stage_drift = sorted(
        key for key in old_k3.keys() | old_k5.keys()
        if old_k3.get(key) != old_k5.get(key))
    semantic_match = {
        stage: comparable_semantic_hash == historical_hash
        for stage, historical_hash in old_semantic_hashes.items()
    }
    return {"compatible": (all(check["compatible"] for check in checks.values())
                            and not historical_stage_drift
                            and all(semantic_match.values())),
            "compared_files": len(new_scope),
            "missing": sorted(set(checks["k3"]["missing"] + checks["k5"]["missing"])),
            "added": sorted(set(checks["k3"]["added"] + checks["k5"]["added"])),
            "accepted_additions": sorted(set(checks["k3"]["accepted_additions"]
                                             + checks["k5"]["accepted_additions"])),
            "changed": sorted(set(checks["k3"]["changed"] + checks["k5"]["changed"])),
            "historical_stage_drift": historical_stage_drift,
            "semantic": {"historical": old_semantic_hashes,
                         "fresh": current_semantic_hash,
                         "fresh_without_schedule_mirror": comparable_semantic_hash,
                         "schedule_mirror_excluded": schedule_additive,
                         "matches": semantic_match},
            "stages": checks}


def _phase3_lineage(task_id: str, task_dir: Path,
                    historical: Mapping[str, Any],
                    prior: Mapping[str, Any] | None) -> dict[str, Any]:
    from tasks.factory.compiler.phase2_readiness import phase2_task_integrity
    seal = _load_object(task_dir / "tests" / "phase2_provenance.json")
    actual = phase2_task_integrity(task_dir)
    if seal.get("task_id") != task_id or seal.get("task_integrity") != actual:
        raise ValueError(f"invalid current task package provenance: {task_id}")
    origin = historical.get(task_id)
    if not isinstance(origin, dict) or not isinstance(origin.get("task_integrity"), str):
        raise ValueError(f"historical Stage 0 provenance is missing: {task_id}")
    cached = prior if isinstance(prior, Mapping) else origin
    cached_integrity = cached.get("task_integrity")
    if not isinstance(cached_integrity, str):
        raise ValueError(f"cached run lineage lacks task integrity: {task_id}")
    return {"task_integrity": actual, "fingerprint": seal.get("fingerprint"),
            "cached_task_integrity": cached_integrity,
            "historical_task_integrity": origin["task_integrity"],
            "historical_fingerprint": origin.get("fingerprint"),
            "package_unchanged_for_cached_runs": actual == cached_integrity}


def _phase3_live_preflight(tasks: Path, task_ids: Sequence[str],
                           certified_ids: Sequence[str],
                           delta_manifest: Path, stage0_report: Path,
                           status_dir: Path,
                           baseline_summary_sha256: str) -> dict[str, Any]:
    """Require the completed delta and a post-delta strict Stage 0 before spending."""
    marker_hashes: dict[str, str] = {}
    marker_mtime = 0.0
    for agent in ("codex", "opus", "kimi", "grok"):
        marker = status_dir / f"DONE_{agent}_phase3.json"
        data = _load_object(marker)
        if str(data.get("status", "")).lower() not in {
            "complete", "completed", "passed", "success", "done", "ready"
        }:
            raise ValueError(f"Phase 3 upstream marker is incomplete: {marker}")
        marker_hashes[agent] = hashlib.sha256(marker.read_bytes()).hexdigest()
        marker_mtime = max(marker_mtime, marker.stat().st_mtime)

    delta = _load_object(delta_manifest)
    if delta.get("schema_version") != 1 or delta.get("state") != "complete":
        raise ValueError(f"Phase 3 delta manifest is not complete: {delta_manifest}")
    if set(delta.get("preserved_certified_task_ids") or []) != set(certified_ids):
        raise ValueError("delta manifest certified task IDs differ from cached certificates")
    summary_source = delta.get("calibration_summary")
    if (not isinstance(summary_source, dict) or
            summary_source.get("sha256") != baseline_summary_sha256):
        raise ValueError("delta manifest is not bound to the historical calibration summary")
    if delta.get("phase3_marker_sha256") != marker_hashes:
        raise ValueError("delta manifest is not bound to current Phase 3 upstream markers")
    if delta_manifest.stat().st_mtime < marker_mtime:
        raise ValueError("delta manifest predates an upstream Phase 3 marker")
    entries = delta.get("tasks")
    if not isinstance(entries, dict) or set(entries) != set(task_ids):
        raise ValueError("delta manifest task IDs differ from the 51-task pilot")

    stage0 = _load_object(stage0_report)
    if (stage0.get("phase2") is not True or
            stage0.get("strict_phase2") is not True or
            stage0.get("ok") is not True or
            stage0.get("total") != len(task_ids) or
            stage0.get("passed") != len(task_ids)):
        raise ValueError(f"post-delta strict Stage 0 did not pass all {len(task_ids)} tasks")
    if stage0_report.stat().st_mtime < delta_manifest.stat().st_mtime:
        raise ValueError("strict Stage 0 report predates Phase 3 delta materialization")
    results = stage0.get("results") or []
    if {item.get("task") for item in results if isinstance(item, dict)} != set(task_ids):
        raise ValueError("strict Stage 0 task IDs differ from the 51-task pilot")
    provenance = stage0.get("phase2_package_provenance")
    if not isinstance(provenance, dict) or set(provenance) != set(task_ids):
        raise ValueError("strict Stage 0 lacks complete package provenance")
    for task_id in task_ids:
        seal = _load_object(tasks / task_id / "tests" / "phase2_provenance.json")
        if provenance[task_id] != {
            "fingerprint": seal.get("fingerprint"),
            "task_integrity": seal.get("task_integrity"),
        }:
            raise ValueError(f"strict Stage 0 is not bound to current package: {task_id}")
        manifest_row = entries[task_id]
        if (not isinstance(manifest_row, dict) or
                manifest_row.get("package_integrity") != seal.get("task_integrity") or
                manifest_row.get("package_fingerprint") != seal.get("fingerprint")):
            raise ValueError(f"delta manifest is not bound to current package: {task_id}")
    return {"delta_manifest": str(delta_manifest),
            "delta_manifest_sha256": hashlib.sha256(delta_manifest.read_bytes()).hexdigest(),
            "stage0_report": str(stage0_report),
            "stage0_report_sha256": hashlib.sha256(stage0_report.read_bytes()).hexdigest(),
            "upstream_marker_sha256": marker_hashes}


def _phase3_offline_regrade_status(summary: Mapping[str, Any],
                                   status_dir: Path) -> dict[str, Any]:
    """Bind cached historical certificates to Opus's corrected-grader regrade.

    The offline report does not rewrite the original rollout files. A cached
    `report.json` can therefore still say certified after the current grader
    has changed its binary result. Match both historical run IDs and reject
    every certificate whose corrected per-run score or outcome changed.
    """
    marker_path = status_dir / "DONE_opus_phase3.json"
    if not marker_path.is_file():
        return {"available": False, "stale": {}, "reason": "Opus Phase 3 marker absent"}
    marker = _load_object(marker_path)
    pointer = marker.get("offline_regrade")
    pointer = pointer.get("summary") if isinstance(pointer, dict) else None
    if not isinstance(pointer, str) or not pointer:
        raise ValueError("Opus Phase 3 marker lacks offline regrade summary path")
    path = Path(pointer)
    if not path.is_absolute():
        path = REPO / path
    artifact = _load_object(path)
    rows = artifact.get("tasks")
    if (not isinstance(rows, list) or artifact.get("errors") != []
            or artifact.get("skipped") != 0
            or artifact.get("rescored") != artifact.get("rollout_dirs")):
        raise ValueError(f"Opus offline regrade is incomplete: {path}")
    by_task = {row.get("task"): row for row in rows if isinstance(row, dict)}
    stage1 = (summary.get("stage1") or {}).get("arms") or {}
    stage2 = (summary.get("stage2") or {}).get("arms") or {}
    stale: dict[str, dict[str, Any]] = {}
    checked: list[str] = []
    for task_id in summary.get("certified") or []:
        task_row = by_task.get(task_id)
        if not isinstance(task_row, dict):
            raise ValueError(f"Opus offline regrade lacks certified task {task_id}")
        runs = task_row.get("runs")
        if not isinstance(runs, list):
            raise ValueError(f"Opus offline regrade lacks runs for {task_id}")
        by_run = {run.get("run_id"): run for run in runs if isinstance(run, dict)}
        reasons: list[str] = []
        stage_results: dict[str, dict[str, Any]] = {}
        for stage, arms, k in (("k3", stage1, 3), ("k5", stage2, 5)):
            arm = arms.get(task_id)
            if not isinstance(arm, dict):
                raise ValueError(f"cached {stage} arm missing for {task_id}")
            run_id = arm.get("run_id")
            run = by_run.get(run_id)
            if not isinstance(run, dict):
                raise ValueError(f"Opus offline regrade lacks {stage} run {run_id} for {task_id}")
            old = run.get("old")
            new = run.get("new")
            if not isinstance(old, dict) or not isinstance(new, dict):
                raise ValueError(f"Opus offline regrade has malformed {stage} result for {task_id}")
            if (old.get("passed") != arm.get("passed") or
                    old.get("scored") != arm.get("scored") or
                    old.get("quarantined") != arm.get("quarantined_count")):
                raise ValueError(f"Opus offline regrade {stage} old scores do not bind to {task_id}")
            changed_rollouts = [rollout.get("rollout_id") for rollout in
                                task_row.get("rollouts") or []
                                if isinstance(rollout, dict)
                                and rollout.get("run_id") == run_id
                                and rollout.get("changed")]
            if changed_rollouts:
                reasons.append(f"{stage}_corrected_rollouts")
            if (new.get("scored") != k or new.get("quarantined") != 0
                    or new.get("rollouts") != k):
                reasons.append(f"{stage}_measurement_invalid")
            passed = new.get("passed")
            if not isinstance(passed, int):
                reasons.append(f"{stage}_missing_pass_count")
            elif stage == "k3" and passed == k:
                reasons.append("k3_ceiling_after_regrade")
            elif stage == "k5" and not (STAGE2_CORE_BAND_MIN <= passed / k
                                         <= STAGE2_CORE_BAND_MAX):
                reasons.append("k5_outside_core_band_after_regrade")
            stage_results[stage] = {"run_id": run_id,
                                    "old_passed": old.get("passed"),
                                    "old_scored": old.get("scored"),
                                    "new_passed": new.get("passed"),
                                    "new_scored": new.get("scored"),
                                    "corrected_rollouts": changed_rollouts}
        checked.append(task_id)
        if reasons:
            stale[task_id] = {"reasons": sorted(set(reasons)),
                              "runs": stage_results}
    return {"available": True, "artifact": str(path),
            "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "checked_certified": checked, "stale": stale}


def run_resume_calibration(
    entries: Sequence[str] = (), *,
    tasks_dir: str | Path = Path("/home/ubuntu/alkera_factory_1017/phase2_pilot_51"),
    out_dir: str | Path = Path("/home/ubuntu/alkera_factory_1017/runs_phase2"),
    baseline_stage0: str | Path = PHASE2_BASELINE_STAGE0,
    delta_manifest: str | Path | None = None,
    stage0_report: str | Path = PHASE3_STAGE0,
    status_dir: str | Path = PHASE3_STATUS_DIR,
    only_topologies: Sequence[str] = (),
    run_index_path: str | Path | None = None,
    runtime_audit_path: str | Path | None = None,
    driver: str = "cursor", model: str = "claude-opus-5-5-high",
    topology: str = "container", concurrency: int = 3,
    dry_run: bool = False, audit_only: bool = False,
    queue_runner: Callable[..., tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None, Path | None]]]] | None = None,
    cache_validator: Callable[[Mapping[str, Any] | None, str, int], dict[str, Any] | None] = _cached_live_arm,
    runtime_checker: Callable[[Path, Path, Path, Path], dict[str, Any]] = _certified_runtime_surface,
) -> dict[str, Any]:
    """Resume a 51-cell pilot without repeating certified or unchanged runs.

    Cached reports are reclassified from disk, and historical task integrity
    comes from the original Stage 0 report. The current fresh world must match
    every cached certified world's agent-visible contract surface before any
    live queue starts. Incompatible certified cells fail closed.
    """
    if concurrency < 1:
        raise ValueError("concurrency must be positive")
    if dry_run and audit_only:
        raise ValueError("--dry-run and --audit-cache-only cannot be combined")
    safe_out = assert_outside_git_repo(out_dir)
    tasks = Path(tasks_dir).expanduser().resolve()
    canonical = safe_out / "calibration_summary.json"
    base = _load_object(canonical)
    historical_report = _load_object(Path(baseline_stage0))
    historical = historical_report.get("phase2_package_provenance")
    if not isinstance(historical, dict):
        raise ValueError("original Stage 0 report lacks phase2_package_provenance")
    for key, expected in (("driver", driver), ("model", model),
                          ("topology", topology)):
        if base.get(key) != expected:
            raise ValueError(f"cached {key}={base.get(key)!r} differs from requested {expected!r}")
    old_s1 = (base.get("stage1") or {}).get("arms")
    old_s2 = (base.get("stage2") or {}).get("arms")
    if not isinstance(old_s1, dict) or not isinstance(old_s2, dict):
        raise ValueError("resume summary lacks stage1/stage2 arms")
    all_tasks = list(old_s1)
    selected = list(dict.fromkeys(_phase3_task_id(entry) for entry in entries)) if entries else all_tasks
    unknown = sorted(set(selected) - set(all_tasks))
    if unknown:
        raise ValueError(f"resume task IDs absent from original 51-cell summary: {unknown}")
    topologies = {str(value).strip().upper() for value in only_topologies}
    if topologies and any(not re.fullmatch(r"FT\d{2}", value) for value in topologies):
        raise ValueError(f"invalid fault topology IDs: {sorted(topologies)}")
    selected = [task_id for task_id in selected
                if not topologies or _phase3_topology(task_id) in topologies]
    selected_set = set(selected)
    offline_regrade = _phase3_offline_regrade_status(base, Path(status_dir))
    source_sha = hashlib.sha256(canonical.read_bytes()).hexdigest()
    if not dry_run and offline_regrade["stale"]:
        diagnostic = {
            "schema_version": 1,
            "status": "blocked_stale_certification",
            "runtime_audit_performed": False,
            "live_queues_started": 0,
            "source_summary": str(canonical),
            "source_summary_sha256": source_sha,
            "offline_regrade_summary": offline_regrade["artifact"],
            "offline_regrade_sha256": offline_regrade["artifact_sha256"],
            "checked_certified_task_ids": offline_regrade["checked_certified"],
            "stale_certified_task_ids": sorted(offline_regrade["stale"]),
            "stale_certified": offline_regrade["stale"],
        }
        safe_out.mkdir(parents=True, exist_ok=True)
        diagnostic_path = safe_out / "phase3_stale_certification.json"
        temporary_path = diagnostic_path.with_name(
            diagnostic_path.name + f".{uuid.uuid4().hex}.tmp")
        temporary_path.write_text(json.dumps(diagnostic, indent=2) + "\n",
                                  encoding="utf-8")
        temporary_path.replace(diagnostic_path)
        raise ValueError(
            "historical cached certificates are invalid under the Phase 3 grader; "
            f"delta calibration blocked before runtime audit and live runs; "
            f"diagnostic: {diagnostic_path}; stale certificates: "
            + json.dumps(offline_regrade["stale"], sort_keys=True))
    prior_resume = base.get("phase3_resume") or {}
    baseline_summary_sha = (prior_resume.get("baseline_calibration_summary_sha256")
                            if isinstance(prior_resume, dict) else None) or source_sha
    preflight = None
    if not dry_run:
        preflight = _phase3_live_preflight(
            tasks, all_tasks, base.get("certified") or [],
            Path(delta_manifest) if delta_manifest else tasks / "phase3_delta_manifest.json",
            Path(stage0_report), Path(status_dir), baseline_summary_sha)
    old_lineage = base.get("task_lineage") or {}
    if not isinstance(old_lineage, dict):
        raise ValueError("cached task_lineage is not an object")

    stage1: dict[str, dict[str, Any]] = {}
    stage2: dict[str, dict[str, Any]] = {}
    lineage: dict[str, dict[str, Any]] = {}
    pending_k3: list[str] = []
    preserved_certified: list[str] = []
    runtime_checks: dict[str, dict[str, Any]] = {}
    for task_id in all_tasks:
        task_path = tasks / task_id
        item = _phase3_lineage(task_id, task_path, historical, old_lineage.get(task_id))
        lineage[task_id] = item
        unchanged = item["package_unchanged_for_cached_runs"]
        prior_k3 = cache_validator(old_s1.get(task_id), task_id, 3) if unchanged else None
        prior_k5 = cache_validator(old_s2.get(task_id), task_id, 5) if unchanged else None
        was_certified = bool(prior_k5 and prior_k5.get("certified"))
        if task_id in base.get("certified", []) and not was_certified:
            raise ValueError(f"previously certified K=5 evidence is unavailable: {task_id}")
        if was_certified:
            if prior_k3 is None:
                raise ValueError(f"certified task lacks reusable K=3 evidence: {task_id}")
            preserved_certified.append(task_id)
            stage1[task_id], stage2[task_id] = prior_k3, prior_k5
            if not dry_run:
                try:
                    with tempfile.TemporaryDirectory(prefix="phase3-runtime-", dir=safe_out) as scratch:
                        runtime_checks[task_id] = runtime_checker(
                            task_path, Path(prior_k3["run_dir"]),
                            Path(prior_k5["run_dir"]), Path(scratch))
                except Exception as exc:
                    runtime_checks[task_id] = {"compatible": False,
                                               "error": f"{type(exc).__name__}: {exc}"}
            continue
        if not unchanged and task_id not in selected_set:
            raise ValueError(f"mutated package omitted from delta selection: {task_id}")
        if prior_k3 is not None:
            stage1[task_id] = prior_k3
            if prior_k5 is not None:
                stage2[task_id] = prior_k5
        elif task_id in selected_set:
            pending_k3.append(task_id)
        else:
            raise ValueError(f"unselected task lacks reusable K=3 evidence: {task_id}")

    audit_path = (Path(runtime_audit_path) if runtime_audit_path else
                  tasks / "phase3_runtime_compatibility.json")
    incompatible = {task_id: check for task_id, check in runtime_checks.items()
                    if not check.get("compatible")}
    if not dry_run:
        audit = {"schema_version": 1,
                 "verified_utc": datetime.now(timezone.utc).isoformat(),
                 "tasks_dir": str(tasks),
                 "source_summary": str(canonical),
                 "source_summary_sha256": hashlib.sha256(canonical.read_bytes()).hexdigest(),
                 "historical_stage0": str(Path(baseline_stage0)),
                 "preserved_certified": preserved_certified,
                 "checks": runtime_checks,
                 "compatible": not incompatible and len(runtime_checks) == len(preserved_certified)}
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_tmp = audit_path.with_name(audit_path.name + "." + uuid.uuid4().hex + ".tmp")
        audit_tmp.write_text(json.dumps(audit, indent=2) + "\n", encoding="utf-8")
        audit_tmp.replace(audit_path)
    if incompatible:
        details = json.dumps(incompatible, sort_keys=True)
        raise ValueError(f"certified historical worlds differ from current runtime; "
                         f"delta calibration blocked before live runs: {details}")

    if audit_only:
        return {"screened": len(all_tasks),
                "certified": list(base.get("certified") or []),
                "phase3_resume": {"selected": selected,
                                  "preserved_certified": preserved_certified,
                                  "runtime_checks": runtime_checks,
                                  "runtime_audit": str(audit_path),
                                  "runtime_audit_sha256": hashlib.sha256(audit_path.read_bytes()).hexdigest(),
                                  "baseline_calibration_summary_sha256": baseline_summary_sha,
                                  "offline_regrade": offline_regrade,
                                  "preflight": preflight,
                                  "pending_k3": pending_k3,
                                  "pending_k5": [],
                                  "incomplete": []}}

    runner = queue_runner or _invoke_run_queue
    pending_k5: list[str] = []
    for task_id in selected:
        if task_id in preserved_certified or task_id in pending_k3:
            continue
        if stage1[task_id].get("admit_to_stage2") and task_id not in stage2:
            pending_k5.append(task_id)

    stage1_meta: dict[str, Any] | None = None
    stage2_meta: dict[str, Any] | None = None
    if pending_k3:
        stage1_meta, outputs = runner(
            pending_k3, 3, tasks_dir=tasks, out_dir=safe_out, driver=driver,
            model=model, topology=topology, concurrency=concurrency,
            stage_label="stage1", dry_run=dry_run)
        if not dry_run:
            for entry, report, run_dir in outputs:
                task_id = _phase3_task_id(entry)
                source = run_dir if run_dir is not None and run_dir.exists() else report
                if source is None:
                    continue
                classified = classify_stage1_arm(source)
                if classified.get("classification") == "invalid":
                    continue
                stage1[task_id] = classified
                stage2.pop(task_id, None)
                if classified.get("admit_to_stage2"):
                    pending_k5.append(task_id)
    if pending_k5 and not dry_run and (stage1_meta or {}).get("exit_code") != 3:
        stage2_meta, outputs = runner(
            pending_k5, 5, tasks_dir=tasks, out_dir=safe_out, driver=driver,
            model=model, topology=topology, concurrency=concurrency,
            stage_label="stage2", dry_run=False)
        for entry, report, run_dir in outputs:
            task_id = _phase3_task_id(entry)
            source = run_dir if run_dir is not None and run_dir.exists() else report
            if source is None:
                continue
            classified = classify_stage2_arm(source)
            if classified.get("measurement_valid") is False or classified.get("scored", 0) < 5:
                continue
            stage2[task_id] = classified

    incomplete = sorted(set(all_tasks) - set(stage1))
    confirmed = [task_id for task_id in all_tasks
                 if task_id in stage1 and task_id in stage2
                 and stage2[task_id].get("certified")]
    result = copy.deepcopy(base)
    result.update({
        "created_at": datetime.now(timezone.utc).isoformat(),
        "screened": len(all_tasks),
        "admitted_to_stage2": [task_id for task_id in all_tasks
                               if stage1.get(task_id, {}).get("admit_to_stage2")],
        "certified": confirmed, "admitted": len(confirmed),
        "stage1": {"meta": stage1_meta or base["stage1"].get("meta"), "arms": stage1},
        "stage2": {"meta": stage2_meta or base["stage2"].get("meta"), "arms": stage2},
        "task_lineage": {task_id: {**lineage[task_id],
                                   "task_integrity": (lineage[task_id]["task_integrity"]
                                                      if task_id in stage1 else None)}
                         for task_id in all_tasks},
        "phase3_resume": {
            "source_summary": str(canonical),
            "source_summary_sha256": hashlib.sha256(canonical.read_bytes()).hexdigest(),
            "baseline_calibration_summary_sha256": baseline_summary_sha,
            "offline_regrade": offline_regrade,
            "historical_stage0": str(Path(baseline_stage0)),
            "selected": selected,
            "preserved_certified": preserved_certified,
            "runtime_checks": runtime_checks,
            "runtime_audit": str(audit_path) if not dry_run else None,
            "runtime_audit_sha256": (hashlib.sha256(audit_path.read_bytes()).hexdigest()
                                     if not dry_run else None),
            "preflight": preflight,
            "pending_k3": pending_k3,
            "pending_k5": pending_k5,
            "incomplete": incomplete,
            "stage1_meta": stage1_meta,
            "stage2_meta": stage2_meta,
            "dry_run": dry_run,
        },
    })
    if dry_run:
        return result

    run_index = {"schema_version": 1, "model": model, "driver": driver,
                 "topology": topology, "updated_utc": result["created_at"],
                 "runs": {}}
    for task_id in all_tasks:
        item = lineage[task_id]
        row = {"fingerprint": item["fingerprint"],
               "task_integrity": item["task_integrity"],
               "historical_fingerprint": item["historical_fingerprint"],
               "historical_task_integrity": item["historical_task_integrity"]}
        if task_id in stage1 and stage1[task_id].get("run_dir"):
            row["k3"] = stage1[task_id]["run_dir"]
        if task_id in stage2 and stage2[task_id].get("run_dir"):
            row["k5"] = stage2[task_id]["run_dir"]
        run_index["runs"][task_id] = row
    index_path = Path(run_index_path) if run_index_path else tasks / "phase2_run_index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_tmp = index_path.with_name(index_path.name + "." + uuid.uuid4().hex + ".tmp")
    index_tmp.write_text(json.dumps(run_index, indent=2) + "\n", encoding="utf-8")
    index_tmp.replace(index_path)
    source_bytes = canonical.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    archive = safe_out / f"calibration_summary_pre_phase3_{source_hash[:16]}.json"
    if archive.exists():
        if archive.read_bytes() != source_bytes:
            raise ValueError(f"existing resume source archive differs: {archive}")
    else:
        archive.write_bytes(source_bytes)
    summary_path = safe_out / f"calibration_summary_phase3_{uuid.uuid4().hex}.json"
    result["summary_path"] = str(summary_path)
    result["phase3_resume"]["source_archive"] = str(archive)
    result["phase3_resume"]["run_index"] = str(index_path)
    body = json.dumps(result, indent=2) + "\n"
    summary_path.write_text(body, encoding="utf-8")
    canonical_tmp = canonical.with_name(canonical.name + "." + uuid.uuid4().hex + ".tmp")
    canonical_tmp.write_text(body, encoding="utf-8")
    canonical_tmp.replace(canonical)
    return result


# Alias for backward compatibility
def calibrate(
    entries: list[str],
    *,
    tasks_dir: Path = DEFAULT_TASKS_DIR,
    out: Path = DEFAULT_OUT,
    driver: str = "cursor",
    model: str = "opus",
    topology: str = "container",
    dry_run: bool = False,
    resume_summary: str | Path | Mapping[str, Any] | None = None,
    skip_certified: bool = False,
    force_entries: Sequence[str] = (),
) -> dict[str, Any]:
    return run_two_stage_calibration(
        entries,
        tasks_dir=tasks_dir,
        out_dir=out,
        driver=driver,
        model=model,
        topology=topology,
        dry_run=dry_run,
        resume_summary=resume_summary,
        skip_certified=skip_certified,
        force_entries=force_entries,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Adaptive Two-Stage Rollout Calibration & Envs-FORGE Difficulty Controller"
    )
    parser.add_argument("entries", nargs="*", help="Task[:variant] entries to calibrate")
    parser.add_argument(
        "--analyze-only",
        nargs="+",
        metavar="RUN_DIR",
        help="Analyze existing rollout directories on disk without launching containers",
    )
    parser.add_argument(
        "--pilot-dir",
        type=Path,
        default=None,
        help="Populate entries from <pilot_dir>/phase2_selection.json (or FT* task dirs) when positional entries are omitted",
    )
    parser.add_argument("--tasks-dir", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--driver", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--topology", default=None)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--redirect-if-inside-repo", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--resume-summary",
        type=Path,
        default=None,
        help="Prior calibration_summary.json to reuse already-certified entries from",
    )
    parser.add_argument("--resume", action="store_true",
                        help="reuse provenance-bound Phase 2 live runs and queue only the delta")
    parser.add_argument("--audit-cache-only", action="store_true",
                        help="with --resume, check preserved certificates against fresh worlds without launching rollouts")
    parser.add_argument("--skip-certified", action="store_true",
                        help="skip entries already certified in prior summary (<out>/calibration_summary.json or --resume-summary)")
    parser.add_argument(
        "--force-task",
        "--force-entry",
        dest="force_entries",
        action="append",
        default=[],
        metavar="TASK_ID",
        help="Force recalibration of task_id/entry even when certified in prior summary",
    )
    parser.add_argument("--only-topologies", nargs="+", action="append", default=[],
                        metavar="FTNN", help="limit Phase 3 delta to these fault topologies")
    parser.add_argument("--baseline-stage0", type=Path, default=PHASE2_BASELINE_STAGE0)
    parser.add_argument("--delta-manifest", type=Path, default=None)
    parser.add_argument("--stage0-report", type=Path, default=PHASE3_STAGE0)
    parser.add_argument("--status-dir", type=Path, default=PHASE3_STATUS_DIR)
    parser.add_argument("--run-index", type=Path, default=None)
    args = parser.parse_args(argv)

    if args.analyze_only:
        analysis = analyze_twin_groups(args.analyze_only)
        print(json.dumps(analysis, indent=2))
        return 0

    tasks_dir = args.tasks_dir or args.pilot_dir or DEFAULT_TASKS_DIR

    if args.resume:
        only_topologies = [part for group in args.only_topologies
                           for value in group for part in value.split(",") if part]
        summary = run_resume_calibration(
            args.entries, tasks_dir=tasks_dir, out_dir=args.out,
            baseline_stage0=args.baseline_stage0,
            delta_manifest=args.delta_manifest, stage0_report=args.stage0_report,
            status_dir=args.status_dir,
            only_topologies=only_topologies, run_index_path=args.run_index,
            driver=args.driver or "cursor", model=args.model or "claude-opus-5-5-high",
            topology=args.topology or "container", concurrency=args.concurrency,
            dry_run=args.dry_run, audit_only=args.audit_cache_only)
        resume = summary["phase3_resume"]
        print(json.dumps({"screened": summary["screened"],
                          "selected": len(resume["selected"]),
                          "preserved_certified": len(resume["preserved_certified"]),
                          "stale_certified": len(resume["offline_regrade"]["stale"]),
                          "queued_k3": len(resume["pending_k3"]),
                          "queued_k5": len(resume["pending_k5"]),
                          "certified": len(summary["certified"]),
                          "incomplete": len(resume["incomplete"]),
                          "run_index": resume.get("run_index"),
                          "summary_path": summary.get("summary_path")}, indent=2))
        return 0 if args.dry_run or not resume["incomplete"] else 1

    if args.only_topologies:
        parser.error("--only-topologies requires --resume")
    if args.audit_cache_only:
        parser.error("--audit-cache-only requires --resume")

    entries = list(args.entries or [])
    if not entries and args.pilot_dir is not None:
        entries = resolve_pilot_entries(args.pilot_dir)

    if not entries:
        parser.error("Provide task entries, --pilot-dir <dir>, or --analyze-only <run_dirs...>")

    summary = run_two_stage_calibration(
        entries,
        tasks_dir=tasks_dir,
        out_dir=args.out,
        driver=args.driver or "cursor",
        model=args.model or "opus",
        topology=args.topology or "container",
        concurrency=args.concurrency,
        dry_run=args.dry_run,
        allow_redirect=args.redirect_if_inside_repo,
        resume_summary=args.resume_summary,
        skip_certified=args.skip_certified,
        force_entries=args.force_entries,
    )
    print(
        json.dumps(
            {
                "screened": summary["screened"],
                "delta_screened": summary["delta_screened"],
                "reused_certified": len(summary["reused_certified"]),
                "admitted_to_stage2": len(summary["admitted_to_stage2"]),
                "certified": len(summary["certified"]),
                "out_dir": summary["out_dir"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
