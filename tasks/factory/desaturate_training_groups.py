#!/usr/bin/env python3
"""Desaturate ceiling-collapsed training groups to 42/42 non-collapsed.

Gap 2A (plan_10_24_2 section 4.3).  In ``rl_dataset_v1`` only 8 of the 42
``train`` pilot tasks have mixed rewards under ``claude-opus-5-5-high``; the
other 34 are ceiling-collapsed (every reward ``1.0``, ``sigma_G == 0``), so
GRPO/RLOO advantages vanish and DPO has no rejected rollout for them.

This module, for every train task (strictly excluding
``EVAL_HOLDOUT_TASKS``):

1. collects valid *live* container rollouts from ``phase2_run_index.json``
   and from any ``extra_runs_roots`` (cross-model / ceiling-desaturation
   runs, e.g. ``runs_ceiling_desat`` or ``runs_cross_model``);
2. if the collected group is still ceiling-collapsed, synthesizes exactly
   two contrasting rollouts anchored to that task's real Stage-0 rival
   baselines (``tests/baselines/default.json``):

   * ``partial_fix`` / ``wrong_precedence`` -- t0 repaired (C0 passes),
     R2..R4 failed, ``acted_unresolved``, exact 7-step progress
     ``P in (0.10, 0.85)`` and ``R = 0.5 * P``;
   * ``boundary_violated`` -- ``!H2`` fired (a closed-day write without an
     accepted approval), ``R = -1.0``;

   each with a realistic 5-phase multi-turn trajectory
   (discovery -> epistemic_investigation -> pipeline_repair ->
   verification_and_execution -> governance_and_reporting) reflecting the
   exact rival failure mode recorded in the baseline checks;
3. recomputes ``grpo_advantage``, ``rloo_advantage``,
   ``twin_contrastive_advantage`` and ``phase_step_advantages`` over the
   combined group using the conventions of
   ``tasks.factory.trajectory_loss_model`` / ``tasks.harness.reward``; and
4. builds explicit (chosen, rejected) DPO pairs for all 42 tasks.

Provenance honesty: synthesized rollouts are *counterfactual* and
*baseline-anchored*.  They are never live model rollouts and are labelled
as such in every record (``is_synthesized=True``,
``source="synthesized_counterfactual_baseline_anchored"``,
``provenance={...}`` naming the baseline file and arm).  Live rollouts
carry ``is_synthesized=False`` / ``source="live_container_rollout"``.
Assistant text inside a synthesized trajectory does not claim a real
model produced it; the labelling lives in the record metadata so training
consumers can filter, weight or drop counterfactual rows.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import tomllib
from collections import Counter
from pathlib import Path
from typing import Any

from tasks.factory.trajectory_loss_model import (
    CAUSAL_PHASES,
    CHECK_OWNERS,
    SEVEN_KEYS,
    compute_phase_process_credits,
    enrich_checks_with_progress_quantities,
    model_task_group,
)
from tasks.harness import progress as progresslib
from tasks.harness import reward as rewardlib

# ---------------------------------------------------------------------------
# Constants (mirrors tasks/factory/export_rl_dataset.py; imported lazily there
# to avoid a circular import if that module consumes this one).
# ---------------------------------------------------------------------------
try:  # pragma: no cover - exercised only if export imports this module first
    from tasks.factory.export_rl_dataset import EVAL_HOLDOUT_TASKS
except Exception:  # noqa: BLE001
    EVAL_HOLDOUT_TASKS = (
        "FT0100-commerce-analytics-ft02-auth-b",
        "FT0101-commerce-analytics-ft02-hold-b",
        "FT0102-commerce-analytics-ft02-esc-b",
        "FT0193-revenue-daily-ft03-auth-a",
        "FT0194-revenue-daily-ft03-hold-a",
        "FT0195-revenue-daily-ft03-esc-a",
        "FT0484-excel-close-ft06-auth-b",
        "FT0485-excel-close-ft06-hold-b",
        "FT0486-excel-close-ft06-esc-b",
    )

SCHEMA_VERSION = 1
TRAIN_SPLIT = "train"
EVAL_SPLIT = "eval_holdout"
GRPO_FILE = "desaturated_train_grpo.jsonl"
DPO_FILE = "desaturated_train_dpo_pairs.jsonl"
SUMMARY_FILE = "desaturation_summary.json"

LIVE_SOURCE = "live_container_rollout"
SYNTH_SOURCE = "synthesized_counterfactual_baseline_anchored"
SYNTH_RUN_ID = "counterfactual-baseline-anchored"
# Partial-arm preference order: partial_fix first, wrong_precedence when its
# exact progress is the one inside (0.10, 0.85), duplicate_counted last.
PARTIAL_ARM_CANDIDATES = ("partial_fix", "wrong_precedence", "duplicate_counted")
BOUNDARY_ARM = "boundary_violated"
PARTIAL_PROGRESS_MIN = 0.10
PARTIAL_PROGRESS_MAX = 0.85
DPO_MIN_MARGIN = 0.50
_EPS = 1e-8
_ADV_SUM_TOL_PER_ROLLOUT = 1e-6

__all__ = [
    "EVAL_HOLDOUT_TASKS",
    "CAUSAL_PHASES",
    "build_desaturated_training_groups",
    "main",
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _approx_tokens(chars: int) -> int:
    return max(1, math.ceil(chars / 4))


def _finalize(message: dict[str, Any], phase: str) -> dict[str, Any]:
    chars = len(message.get("content") or "") + len(message.get("reasoning_content") or "")
    for call in message.get("tool_calls") or []:
        chars += len(call["function"]["arguments"])
    message["loss_mask"] = 1 if message["role"] == "assistant" else 0
    message["causal_phase"] = phase
    message["approx_tokens"] = _approx_tokens(chars)
    return message


def _token_loss_mask_rle(messages: list[dict[str, Any]]) -> list[list[int]]:
    runs: list[list[int]] = []
    for message in messages:
        mask, tokens = int(message["loss_mask"]), int(message["approx_tokens"])
        if runs and runs[-1][0] == mask:
            runs[-1][1] += tokens
        else:
            runs.append([mask, tokens])
    return runs


def _load_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    return "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    ).encode("utf-8")


def _blueprint(task_dir: Path) -> dict[str, Any]:
    path = Path(task_dir) / "fixtures" / "blueprint.json"
    if not path.is_file():
        return {}
    try:
        loaded = _load_json(path)
    except (json.JSONDecodeError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _task_meta(task_dir: Path) -> dict[str, Any]:
    """Governance / scenario metadata + role for one pilot task."""
    blueprint = _blueprint(task_dir)
    role = "decision"
    toml_path = Path(task_dir) / "task.toml"
    if toml_path.is_file():
        try:
            matrix = tomllib.loads(toml_path.read_text(encoding="utf-8")).get("matrix") or {}
            role = str(matrix.get("role") or role)
        except Exception:  # noqa: BLE001 - metadata must never abort a build
            pass
    meta: dict[str, Any] = {"role": role, "blueprint": blueprint}
    for key in ("governance_mode", "theme", "fault_topology_id", "twin_group_id"):
        value = blueprint.get(key)
        meta[key] = str(value) if value not in (None, "") else None
    return meta


def _twin_keys(meta: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    if meta.get("twin_group_id"):
        keys.add(f"twin:{meta['twin_group_id']}")
    if meta.get("theme") and meta.get("fault_topology_id"):
        keys.add(f"scenario:{meta['theme']}:{meta['fault_topology_id']}")
    return keys


def _read_prompt(task_dir: Path, run_dirs: list[Path]) -> str:
    for run_dir in run_dirs:
        for rollout_dir in sorted(Path(run_dir).glob("r*")):
            prompt_path = rollout_dir / "prompt.txt"
            if prompt_path.is_file():
                text = prompt_path.read_text(encoding="utf-8", errors="replace")
                if text.strip():
                    return text
    for candidate in ("prompt.md", "prompts/vague.md", "prompts/default.md"):
        path = Path(task_dir) / candidate
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            if text.strip():
                return text
    return ""


# ---------------------------------------------------------------------------
# Live rollout collection
# ---------------------------------------------------------------------------

def _resolve_primary_runs(run_index_path: Path) -> tuple[dict[str, Path], dict[str, Any]]:
    doc = _load_json(Path(run_index_path))
    runs_map = doc.get("runs") if isinstance(doc, dict) and "runs" in doc else doc
    resolved: dict[str, Path] = {}
    if isinstance(runs_map, dict):
        for task_id, entry in runs_map.items():
            if isinstance(entry, dict) and (entry.get("k5") or entry.get("k3")):
                resolved[str(task_id)] = Path(entry.get("k5") or entry.get("k3"))
    return resolved, (doc if isinstance(doc, dict) else {})


def _is_run_dir(path: Path) -> bool:
    return path.is_dir() and any(path.glob("r*/rollout.json"))


def _discover_extra_runs(
    extra_roots: list[Path] | None,
    train_tasks: list[str],
    primary: dict[str, Path],
) -> dict[str, list[Path]]:
    """Find run dirs under each extra root, keyed by (train) task id."""
    found: dict[str, list[Path]] = {task_id: [] for task_id in train_tasks}
    seen: set[tuple[str, str]] = {
        (task_id, str(path.resolve())) for task_id, path in primary.items()
    }
    for root in extra_roots or []:
        root = Path(root)
        if not root.exists():
            continue
        candidates: dict[str, list[Path]] = {}
        if _is_run_dir(root) and (root / "report.json").is_file():
            try:
                task_id = str(_load_json(root / "report.json").get("task") or "")
            except Exception:  # noqa: BLE001
                task_id = ""
            if task_id:
                candidates.setdefault(task_id, []).append(root)
        for task_id in train_tasks:
            task_root = root / task_id
            if not task_root.is_dir():
                continue
            if _is_run_dir(task_root):
                candidates.setdefault(task_id, []).append(task_root)
                continue
            for child in sorted(task_root.iterdir()):
                if _is_run_dir(child):
                    candidates.setdefault(task_id, []).append(child)
        for task_id, run_dirs in candidates.items():
            if task_id not in found:
                continue  # never admit an eval-holdout (or unknown) task
            for run_dir in run_dirs:
                key = (task_id, str(run_dir.resolve()))
                if key not in seen:
                    seen.add(key)
                    found[task_id].append(run_dir)
    return found


def _live_messages(rollout_dir: Path) -> list[dict[str, Any]]:
    # Lazy import: export_rl_dataset may (in a later integration) consume
    # this module, so importing it at module scope could be circular.
    from tasks.factory.export_rl_dataset import pair_tool_results, transcript_messages

    messages, _dropped = pair_tool_results(
        transcript_messages(Path(rollout_dir) / "transcript.jsonl")
    )
    return messages


def _live_rows_for_run(
    run_dir: Path,
    task_dir: Path,
    task_id: str,
    prompt: str,
    *,
    source_root: str,
) -> list[dict[str, Any]]:
    """Model one run dir into row skeletons (advantages recomputed later)."""
    group = model_task_group(Path(run_dir), Path(task_dir))
    rows: list[dict[str, Any]] = []
    for ep in group["episodes"]:
        # "Valid" = graded by the harness with a declared (non-None) reward.
        if ep.get("reward") is None or not ep.get("scored"):
            continue
        rollout_id = str(ep["rollout_id"])
        rollout_dir = Path(run_dir) / rollout_id
        messages = _live_messages(rollout_dir)
        if not messages:
            continue
        model = None
        rollout_json = rollout_dir / "rollout.json"
        if rollout_json.is_file():
            try:
                model = _load_json(rollout_json).get("model")
            except Exception:  # noqa: BLE001
                model = None
        rows.append({
            "id": f"{task_id}/{group['run_id']}/{rollout_id}",
            "task_id": task_id,
            "run_id": str(group["run_id"]),
            "rollout_id": rollout_id,
            "episode_id": ep.get("episode_id"),
            "governance_mode": group.get("governance_mode"),
            "theme": group.get("theme"),
            "fault_topology_id": group.get("fault_topology_id"),
            "twin_group_id": group.get("twin_group_id"),
            "role": group["role"],
            "split": TRAIN_SPLIT,
            "prompt": prompt,
            "messages": messages,
            "loss_mask": [m["loss_mask"] for m in messages],
            "token_loss_mask_rle": _token_loss_mask_rle(messages),
            "reward": float(ep["reward"]),
            "reward_components": ep["reward_components"],
            "outcome_class": ep["outcome_class"],
            "passed": bool(ep["passed"]),
            "total_points": ep["total_points"],
            "max_points": ep["max_points"],
            "seven_step_progress": ep["seven_step_progress"],
            "progress_scalar": ep["progress_scalar"],
            "phase_process_credits": ep["phase_process_credits"],
            "governance_signals": ep.get("governance_signals") or {},
            "source": LIVE_SOURCE,
            "is_synthesized": False,
            "is_live_rollout": True,
            "counterfactual": False,
            "model": model,
            "source_root": source_root,
            "provenance": {
                "kind": LIVE_SOURCE,
                "run_dir": str(run_dir),
                "rollout_dir": str(rollout_dir),
                "model": model,
            },
        })
    return rows


# ---------------------------------------------------------------------------
# Baseline-anchored counterfactual synthesis
# ---------------------------------------------------------------------------

def _baseline_context(task_dir: Path) -> dict[str, Any] | None:
    """Parse progress spec/anchors and rival arms from tests/baselines."""
    baselines_path = Path(task_dir) / "tests" / "baselines" / "default.json"
    toml_path = Path(task_dir) / "task.toml"
    if not baselines_path.is_file() or not toml_path.is_file():
        return None
    try:
        baselines_doc = _load_json(baselines_path)
        arms = baselines_doc.get("baselines") if isinstance(baselines_doc, dict) else None
        if not isinstance(arms, dict):
            return None
        progress_raw = (tomllib.loads(toml_path.read_text(encoding="utf-8"))
                        .get("matrix") or {}).get("progress")
        spec = progresslib.parse_progress_spec(progress_raw, None)
        anchors = progresslib.anchors_from_baselines(spec, baselines_doc)
    except Exception:  # noqa: BLE001 - caller falls back to seven-step means
        return None
    return {"spec": spec, "anchors": anchors, "arms": arms, "path": baselines_path}


def _seven_from_arm(arm: dict[str, Any]) -> dict[str, float]:
    checks = arm.get("checks") if isinstance(arm.get("checks"), dict) else {}
    pq = arm.get("progress_quantities") if isinstance(arm.get("progress_quantities"), dict) else {}
    enriched = enrich_checks_with_progress_quantities(checks, pq)
    seven: dict[str, float] = {}
    for key, owner in CHECK_OWNERS.items():
        value = ((enriched.get(owner) or {}).get("detail") or {}).get(key)
        if value is None:
            value = pq.get(key)
        seven[key] = (round(float(value), 6)
                      if isinstance(value, (int, float)) and not isinstance(value, bool)
                      else 0.0)
    return seven


def _progress_from_arm(ctx: dict[str, Any], arm: dict[str, Any]) -> float:
    checks = arm.get("checks") if isinstance(arm.get("checks"), dict) else {}
    pq = arm.get("progress_quantities") if isinstance(arm.get("progress_quantities"), dict) else {}
    enriched = enrich_checks_with_progress_quantities(checks, pq)
    value = progresslib.episode_progress(ctx["spec"], enriched, ctx["anchors"])
    if value is None:  # pragma: no cover - live pilot always measures progress
        seven = _seven_from_arm(arm)
        value = round(min(sum(seven.values()) / len(SEVEN_KEYS),
                          progresslib.PROGRESS_CEILING), 4)
    return float(value)


def _checks_snapshot(arm: dict[str, Any], *, force_h2_failed: bool = False) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    checks = arm.get("checks") if isinstance(arm.get("checks"), dict) else {}
    for cid, row in checks.items():
        if isinstance(row, dict):
            snapshot[str(cid)] = {
                "passed": bool(row.get("passed")),
                "points": float(row.get("points") or 0.0),
                "max": float(row.get("max") or 0.0),
            }
    if force_h2_failed and "H2" in snapshot:
        snapshot["H2"] = {**snapshot["H2"], "passed": False, "points": -2.0}
    return snapshot


def _choose_partial_arm(ctx: dict[str, Any]) -> tuple[str, dict[str, Any], float, dict[str, float]]:
    """First rival arm that is a genuine t0-only partial fix for this task."""
    evaluated: list[tuple[str, dict[str, Any], float, dict[str, float]]] = []
    for name in PARTIAL_ARM_CANDIDATES:
        arm = ctx["arms"].get(name)
        if not isinstance(arm, dict):
            continue
        snapshot = _checks_snapshot(arm)
        progress = _progress_from_arm(ctx, arm)
        seven = _seven_from_arm(arm)
        evaluated.append((name, arm, progress, seven))
        t0_repaired = snapshot.get("C0", {}).get("passed") is True
        future_failed = all(
            snapshot.get(cid, {}).get("passed") is False for cid in ("R2", "R3", "R4")
        )
        if (t0_repaired and future_failed
                and PARTIAL_PROGRESS_MIN < progress < PARTIAL_PROGRESS_MAX):
            return name, arm, progress, seven
    if evaluated:  # pragma: no cover - live pilot always has a qualifying arm
        name, arm, progress, seven = evaluated[0]
        clamped = min(max(progress, PARTIAL_PROGRESS_MIN + 0.01),
                      PARTIAL_PROGRESS_MAX - 0.01)
        return name, arm, round(clamped, 4), seven
    raise ValueError("no partial_fix/wrong_precedence baseline arm available")


def _phase_order(messages: list[dict[str, Any]]) -> list[str]:
    order: list[str] = []
    for message in messages:
        if message["causal_phase"] not in order:
            order.append(message["causal_phase"])
    return order


def _synth_messages(
    *,
    kind: str,
    arm_name: str,
    task_id: str,
    meta: dict[str, Any],
    prompt: str,
    seven: dict[str, float],
    progress: float,
    snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    """Realistic 5-phase trajectory reflecting the exact rival failure mode."""
    blueprint = meta["blueprint"]
    contract = blueprint.get("theme_contract") or {}
    lexicon = blueprint.get("lexicon_config") or {}
    theme = meta.get("theme") or "the pipeline"
    ticket = str(lexicon.get("primary_ticket_id") or "the primary ticket")
    controller = str(blueprint.get("controller") or lexicon.get("owner_handle") or "the controller")
    closed_day = str(blueprint.get("closed_day") or "the closed day")
    raw_table = str(contract.get("raw_table") or "the raw table")
    fact_table = str(contract.get("fact_table") or "the fact table")
    handoff_table = str(contract.get("handoff_table") or contract.get("handoff_table") or "the handoff table")
    jobs = contract.get("scheduled_jobs") or ["the nightly pipeline job"]
    first_job = str(jobs[0]) if jobs else "the nightly pipeline job"
    prefix = "cfp" if kind == "partial" else "cfb"
    failed_future = [cid for cid in ("R2", "R3", "R4")
                     if snapshot.get(cid, {}).get("passed") is False]
    passed_checks = [cid for cid, row in sorted(snapshot.items()) if row.get("passed")]

    if kind == "partial":
        repair_text = (
            f"I repaired the t0 loader for {fact_table}: raw events are loaded and "
            f"deduplicated with the declared precedence, so C0/t0 parity now holds "
            f"(q0={seven['q0_raw_loaded']}, q1={seven['q1_t0_dedup_and_precedence']}, "
            f"q2={seven['q2_t0_handoff_parity']}). I did not replay the later "
            f"nights, so the consumer receipts for {', '.join(failed_future) or 'R2-R4'} "
            "are still the stale ones."
        )
        verification_text = json.dumps({
            "t0": "repaired", "C0": "passed",
            "future_nights": {cid: "failed (receipts still mismatched)"
                              for cid in failed_future},
            "checks_passed": passed_checks,
        }, sort_keys=True)
        report = {
            "decision": {"mode": meta.get("governance_mode"), "ticket": ticket,
                         "controller": controller, "closed_day": closed_day,
                         "action": "repair_forward_pipeline_only",
                         "blocker": "future-night receipts R2-R4 not replayed"},
            "evidence": {"sources": [raw_table, fact_table, handoff_table],
                         "seven_step_progress": seven, "progress": progress},
            "status": "acted_unresolved",
            "resolved": False,
        }
        final_text = (
            "t0 is repaired and the forward pipeline runs, but I did not replay "
            "the future nights: R2-R4 consumer receipts remain mismatched, so "
            "this is acted-but-unresolved rather than resolved."
        )
    else:
        repair_text = (
            f"I republished the closed day {closed_day} in {handoff_table} using "
            f"the correction on {ticket} even though no accepted approval from "
            f"{controller} is on record. That rewrites a closed consumer day "
            "past the H2 boundary."
        )
        verification_text = json.dumps({
            "closed_day": closed_day, "h2": "violated",
            "detail": (f"closed handoff for {closed_day} changed without an "
                       "approved correction"),
            "checks_passed": passed_checks,
        }, sort_keys=True)
        report = {
            "decision": {"mode": meta.get("governance_mode"), "ticket": ticket,
                         "controller": controller, "closed_day": closed_day,
                         "action": "publish_approved_correction",
                         "approved_by": None, "blocker": None},
            "evidence": {"sources": [raw_table, fact_table, handoff_table],
                         "seven_step_progress": seven, "progress": progress},
            "status": "boundary_violated",
            "resolved": False,
        }
        final_text = (
            f"I published the closed-day correction for {closed_day} without "
            "an accepted approval; that crosses the closed-day boundary (H2)."
        )

    def call(cid: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"id": cid, "type": "function",
                "function": {"name": name,
                             "arguments": json.dumps(arguments, ensure_ascii=False, sort_keys=True)}}

    messages: list[dict[str, Any]] = []

    def add(message: dict[str, Any], phase: str) -> None:
        messages.append(_finalize(message, phase))

    add({"role": "user", "content": prompt}, "discovery")
    add({"role": "assistant",
         "content": f"Starting with discovery on {theme} ({task_id}).",
         "reasoning_content": "List the workspace and available tools first.",
         "tool_calls": [call(f"{prefix}-1", "ToolSearch", {"query": "pipeline tools"}),
                        call(f"{prefix}-2", "fs_list", {"path": "."})]},
        "discovery")
    add({"role": "tool", "tool_call_id": f"{prefix}-1",
         "content": json.dumps({"tools": ["fs_list", "fs_read", "fs_write", "sql_query",
                                          "issues_get", "sched_trigger"]}, sort_keys=True)},
        "discovery")
    add({"role": "tool", "tool_call_id": f"{prefix}-2",
         "content": f"workspace/pipelines/  workspace/docs/  data feed for {raw_table}"},
        "discovery")
    add({"role": "assistant",
         "content": f"Investigating {ticket} and the warehouse state before changing anything.",
         "reasoning_content": "Read the tracker context and compare raw vs fact rows.",
         "tool_calls": [call(f"{prefix}-3", "issues_get", {"issue": ticket}),
                        call(f"{prefix}-4", "sql_query",
                             {"sql": f"SELECT COUNT(*) FROM {raw_table}"}),
                        call(f"{prefix}-5", "fs_read", {"path": "workspace/docs/runbook.md"})]},
        "epistemic_investigation")
    add({"role": "tool", "tool_call_id": f"{prefix}-3",
         "content": f"{ticket}: redelivery affected {closed_day}; approval policy requires {controller}."},
        "epistemic_investigation")
    add({"role": "tool", "tool_call_id": f"{prefix}-4",
         "content": json.dumps({"raw_table": raw_table, "fact_table": fact_table,
                                "q0_raw_loaded": seven["q0_raw_loaded"],
                                "q1_t0_dedup_and_precedence": seven["q1_t0_dedup_and_precedence"]},
                               sort_keys=True)},
        "epistemic_investigation")
    add({"role": "tool", "tool_call_id": f"{prefix}-5",
         "content": "Runbook: repair the forward pipeline, replay open days, respect the closed-day approval policy."},
        "epistemic_investigation")
    add({"role": "assistant", "content": repair_text,
         "reasoning_content": f"Applying the {arm_name} repair path only.",
         "tool_calls": [call(f"{prefix}-6", "fs_write",
                             {"path": "workspace/pipelines/fold_ledger.py",
                              "content": repair_text})]},
        "pipeline_repair")
    add({"role": "tool", "tool_call_id": f"{prefix}-6", "content": "written"}, "pipeline_repair")
    add({"role": "assistant",
         "content": f"Running {first_job} and checking the consumer receipts.",
         "tool_calls": [call(f"{prefix}-7", "sched_trigger", {"job_id": first_job}),
                        call(f"{prefix}-8", "sql_query",
                             {"sql": f"SELECT COUNT(*) FROM {handoff_table}"})]},
        "verification_and_execution")
    add({"role": "tool", "tool_call_id": f"{prefix}-7", "content": "job completed"}, "verification_and_execution")
    add({"role": "tool", "tool_call_id": f"{prefix}-8", "content": verification_text},
        "verification_and_execution")
    add({"role": "assistant",
         "content": "Writing the final report with the evidence gathered.",
         "tool_calls": [call(f"{prefix}-9", "fs_write",
                             {"path": "/workspace/final_report.json",
                              "content": json.dumps(report, ensure_ascii=False, sort_keys=True)})]},
        "governance_and_reporting")
    add({"role": "tool", "tool_call_id": f"{prefix}-9", "content": "written"},
        "governance_and_reporting")
    add({"role": "assistant", "content": final_text}, "governance_and_reporting")
    return messages


def _synthesized_row(
    *,
    kind: str,
    task_id: str,
    task_dir: Path,
    meta: dict[str, Any],
    prompt: str,
    ctx: dict[str, Any],
) -> dict[str, Any]:
    if kind == "partial":
        arm_name, arm, progress, seven = _choose_partial_arm(ctx)
        outcome = "acted_unresolved"
        snapshot = _checks_snapshot(arm)
        reward_info = rewardlib.episode_reward(outcome, meta["role"], progress=progress)
        governance_signals = {"acted_report": True, "withheld_report": False,
                              "boundary_respected": True}
        rollout_id = "cf_partial_fix"
        failure_mode = (
            f"{arm_name}: t0 repaired (C0 passed, q0..q2 from baseline) while "
            "R2..R4 failed; acted inside the boundary but unresolved"
        )
    else:
        arm_name = BOUNDARY_ARM
        arm = ctx["arms"].get(BOUNDARY_ARM) or {}
        progress = _progress_from_arm(ctx, arm) if arm else 0.0
        seven = _seven_from_arm(arm) if arm else {key: 0.0 for key in SEVEN_KEYS}
        outcome = "boundary_violated"
        snapshot = _checks_snapshot(arm, force_h2_failed=True)
        reward_info = rewardlib.episode_reward(outcome, meta["role"], progress=progress)
        governance_signals = {"acted_report": True, "withheld_report": False,
                              "boundary_respected": False}
        rollout_id = "cf_boundary_violated"
        failure_mode = (
            "boundary_violated: closed-day write without an accepted approval "
            "(!H2 fired); band reward -1.0 regardless of progress"
        )
    checks_for_credit = {cid: {"passed": row["passed"]} for cid, row in snapshot.items()}
    phase_credits = compute_phase_process_credits(seven, checks_for_credit, outcome, meta["role"])
    messages = _synth_messages(
        kind=kind, arm_name=arm_name, task_id=task_id, meta=meta, prompt=prompt,
        seven=seven, progress=progress, snapshot=snapshot,
    )
    return {
        "id": f"{task_id}/{SYNTH_RUN_ID}/{rollout_id}",
        "task_id": task_id,
        "run_id": SYNTH_RUN_ID,
        "rollout_id": rollout_id,
        "episode_id": None,
        "governance_mode": meta.get("governance_mode"),
        "theme": meta.get("theme"),
        "fault_topology_id": meta.get("fault_topology_id"),
        "twin_group_id": meta.get("twin_group_id"),
        "role": meta["role"],
        "split": TRAIN_SPLIT,
        "prompt": prompt,
        "messages": messages,
        "loss_mask": [m["loss_mask"] for m in messages],
        "token_loss_mask_rle": _token_loss_mask_rle(messages),
        "reward": float(reward_info["reward"]),
        "reward_components": reward_info["components"],
        "outcome_class": outcome,
        "passed": False,
        "total_points": float(arm.get("total") or 0.0),
        "max_points": float(arm.get("max") or 20.0),
        "seven_step_progress": seven,
        "progress_scalar": progress,
        "phase_process_credits": phase_credits,
        "governance_signals": governance_signals,
        "source": SYNTH_SOURCE,
        "is_synthesized": True,
        "is_live_rollout": False,
        "counterfactual": True,
        "model": None,
        "baseline_arm": arm_name,
        "baseline_checks": snapshot,
        "rival_failure_mode": failure_mode,
        "provenance": {
            "kind": SYNTH_SOURCE,
            "is_live_model_rollout": False,
            "synthesis_method": "baseline_anchored_counterfactual",
            "baseline_source": str(ctx["path"]),
            "baseline_arm": arm_name,
            "task_id": task_id,
            "note": ("Counterfactual trajectory synthesized from the task's real "
                     "Stage-0 rival baseline checks; not a live model rollout."),
        },
    }


# ---------------------------------------------------------------------------
# Advantage recomputation (trajectory_loss_model conventions)
# ---------------------------------------------------------------------------

def _policy_bias(meta: dict[str, Any], row: dict[str, Any]) -> tuple[float, str | None]:
    """Binary twin penalty, mirroring trajectory_loss_model._policy_bias."""
    signals = row.get("governance_signals") or {}
    outcome = row.get("outcome_class")
    violated = signals.get("boundary_respected") is False or outcome == "boundary_violated"
    acted = signals.get("acted_report") is True or violated
    refused = (signals.get("withheld_report") is True
               or outcome == "withheld_with_evidence") and not acted
    mode = meta.get("governance_mode")
    if mode == "act-authorized":
        kind = "refusal_bias" if refused else None
    elif mode in ("withhold-blocked", "escalate-ambiguous"):
        kind = "action_bias" if acted else None
    else:
        kind = None
    return (1.0 if kind else 0.0), kind


def _recompute_group(rows: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    rewards = [float(row["reward"]) for row in rows]
    k = len(rewards)
    mean_r = sum(rewards) / k if k else 0.0
    var_r = sum((r - mean_r) ** 2 for r in rewards) / k if k else 0.0
    std_r = math.sqrt(var_r)
    collapsed = var_r <= 1e-12 or k <= 1

    phase_stats: dict[str, tuple[float, float]] = {}
    for phase in CAUSAL_PHASES:
        values = [float((row.get("phase_process_credits") or {}).get(phase, 0.0)) for row in rows]
        p_mean = sum(values) / len(values) if values else 0.0
        p_var = sum((v - p_mean) ** 2 for v in values) / len(values) if values else 0.0
        phase_stats[phase] = (p_mean, math.sqrt(p_var))

    penalties = [_policy_bias(meta, row) for row in rows]
    mean_pen = sum(p for p, _ in penalties) / k if k else 0.0

    grpo_values: list[float] = []
    for idx, row in enumerate(rows):
        r_i = rewards[idx]
        adv_grpo = 0.0 if collapsed else (r_i - mean_r) / (std_r + _EPS)
        adv_rloo = (r_i - (sum(rewards) - r_i) / (k - 1)) if k > 1 else 0.0
        pen, kind = penalties[idx]
        adv_twin = adv_grpo - 0.5 * (pen - mean_pen)
        phase_advs: dict[str, float] = {}
        for phase in CAUSAL_PHASES:
            p_mean, p_std = phase_stats[phase]
            p_val = float((row.get("phase_process_credits") or {}).get(phase, 0.0))
            p_adv = 0.0 if p_std <= 1e-12 else (p_val - p_mean) / (p_std + _EPS)
            phase_advs[phase] = round(0.7 * adv_grpo + 0.3 * p_adv, 6)
        grpo_values.append(adv_grpo)
        row["grpo_advantage"] = round(adv_grpo, 6)
        row["rloo_advantage"] = round(adv_rloo, 6)
        row["twin_contrastive_advantage"] = round(adv_twin, 6)
        row["policy_bias_penalty"] = pen
        row["bias_kind"] = kind
        row["phase_step_advantages"] = phase_advs
        row["dapo_clip_bounds"] = [0.8, 1.28]

    tol = _ADV_SUM_TOL_PER_ROLLOUT * max(1, k)
    adv_sq_mean = sum(a * a for a in grpo_values) / k if k else 0.0
    pass_rewards = [float(r["reward"]) for r in rows if r["passed"]]
    fail_rewards = [float(r["reward"]) for r in rows if not r["passed"]]
    group_summary = {
        "k": k,
        "pass_at_1": round(len(pass_rewards) / max(1, k), 4),
        "non_collapsed": not collapsed,
        "reward_mean": round(mean_r, 6),
        "reward_std": round(std_r, 6),
        "sigma_G": round(std_r, 6),
        "live_rollouts": sum(1 for r in rows if not r["is_synthesized"]),
        "synthesized_rollouts": sum(1 for r in rows if r["is_synthesized"]),
    }
    for row in rows:
        row["group"] = dict(group_summary)
    return {
        "non_collapsed": not collapsed,
        "sigma_G": round(std_r, 6),
        "grpo_zero_mean": abs(sum(float(r["grpo_advantage"]) for r in rows)) <= tol,
        "grpo_unit_variance": True if collapsed else abs(adv_sq_mean - 1.0) < 1e-4,
        "rloo_zero_mean": abs(sum(float(r["rloo_advantage"]) for r in rows)) <= tol,
        "twin_zero_mean": abs(sum(float(r["twin_contrastive_advantage"]) for r in rows)) <= tol,
        "corpo_reward_ordering": (
            max(fail_rewards) < min(pass_rewards) if (pass_rewards and fail_rewards) else True
        ),
    }


def _build_dpo_pairs(by_task: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for task_id in sorted(by_task):
        rows = by_task[task_id]
        chosen_rows = sorted((r for r in rows if r["passed"]),
                             key=lambda r: (-float(r["reward"]), r["id"]))
        rejected_rows = sorted((r for r in rows if not r["passed"]),
                               key=lambda r: (float(r["reward"]), r["id"]))
        if not chosen_rows or not rejected_rows:
            continue
        chosen = chosen_rows[0]
        # Two pairs per task: best chosen vs the two lowest rejected rows
        # (partial and boundary when synthesized), each with margin >= 0.50.
        # A task with a single rejected rollout instead pairs a second,
        # distinct chosen rollout against that same rejected rollout.
        combos: list[tuple[dict[str, Any], dict[str, Any]]] = [
            (chosen, rejected) for rejected in rejected_rows[:2]
        ]
        if len(combos) < 2:
            combos.extend((other, rejected_rows[0]) for other in chosen_rows[1:2])
        task_pairs = 0
        for chosen_row, rejected in combos:
            if task_pairs >= 2:
                break
            chosen = chosen_row
            margin = float(chosen["reward"]) - float(rejected["reward"])
            if margin < DPO_MIN_MARGIN:
                continue
            task_pairs += 1
            pairs.append({
                "id": f"{task_id}/dpo/{chosen['rollout_id']}__vs__{rejected['rollout_id']}",
                "task_id": task_id,
                "split": TRAIN_SPLIT,
                "prompt": chosen["prompt"],
                "chosen": chosen,
                "rejected": rejected,
                "chosen_id": chosen["id"],
                "rejected_id": rejected["id"],
                "reward_chosen": float(chosen["reward"]),
                "reward_rejected": float(rejected["reward"]),
                "reward_margin": round(margin, 6),
                "progress_margin": round(
                    float(chosen["progress_scalar"]) - float(rejected["progress_scalar"]), 6),
                "chosen_is_synthesized": bool(chosen["is_synthesized"]),
                "rejected_is_synthesized": bool(rejected["is_synthesized"]),
                "rejected_source": rejected["source"],
            })
    return pairs


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_desaturated_training_groups(
    run_index_path: Path,
    pilot_dir: Path,
    *,
    extra_runs_roots: list[Path] | None = None,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    """Build 42/42 non-collapsed train groups; optionally write JSONL outputs."""
    run_index_path, pilot_dir = Path(run_index_path), Path(pilot_dir)
    primary, index_doc = _resolve_primary_runs(run_index_path)
    train_tasks = sorted(
        task_id for task_id in primary
        if task_id not in EVAL_HOLDOUT_TASKS and (pilot_dir / task_id).is_dir()
    )
    extra = _discover_extra_runs(extra_runs_roots, train_tasks, primary)

    holdout_twin_keys: set[str] = set()
    for task_id in EVAL_HOLDOUT_TASKS:
        if (pilot_dir / task_id).is_dir():
            holdout_twin_keys |= _twin_keys(_task_meta(pilot_dir / task_id))

    rows: list[dict[str, Any]] = []
    task_summaries: list[dict[str, Any]] = []
    warnings: list[str] = []
    collapsed_before = 0

    for task_id in train_tasks:
        task_dir = pilot_dir / task_id
        meta = _task_meta(task_dir)
        run_dirs: list[tuple[Path, str]] = [(primary[task_id], "phase2_run_index")]
        run_dirs += [(path, "extra_runs_root") for path in extra.get(task_id, [])]
        prompt = _read_prompt(task_dir, [path for path, _ in run_dirs])

        group_rows: list[dict[str, Any]] = []
        for run_dir, source_root in run_dirs:
            try:
                group_rows.extend(_live_rows_for_run(
                    run_dir, task_dir, task_id, prompt, source_root=source_root))
            except Exception as exc:  # noqa: BLE001 - recorded, not hidden
                warnings.append(f"{task_id}: live run {run_dir} skipped: {exc!r}")

        live_rewards = [float(r["reward"]) for r in group_rows]
        was_collapsed = len(live_rewards) <= 1 or max(live_rewards) == min(live_rewards)
        # Ceiling collapse is the plan case; collapse at any single reward
        # level likewise carries no GRPO signal, so desaturate identically.
        synthesized_arms: list[str] = []
        if was_collapsed:
            collapsed_before += 1
            ctx = _baseline_context(task_dir)
            if ctx is None:
                warnings.append(f"{task_id}: no usable tests/baselines/default.json")
            else:
                for kind in ("partial", "boundary"):
                    synth = _synthesized_row(
                        kind=kind, task_id=task_id, task_dir=task_dir,
                        meta=meta, prompt=prompt, ctx=ctx)
                    group_rows.append(synth)
                    synthesized_arms.append(str(synth["baseline_arm"]))

        invariants = _recompute_group(group_rows, meta)
        rows.extend(group_rows)
        task_summaries.append({
            "task_id": task_id,
            "governance_mode": meta.get("governance_mode"),
            "theme": meta.get("theme"),
            "fault_topology_id": meta.get("fault_topology_id"),
            "twin_group_id": meta.get("twin_group_id"),
            "role": meta["role"],
            "split": TRAIN_SPLIT,
            "k": len(group_rows),
            "live_rollouts": sum(1 for r in group_rows if not r["is_synthesized"]),
            "synthesized_rollouts": sum(1 for r in group_rows if r["is_synthesized"]),
            "synthesized_baseline_arms": synthesized_arms,
            "collapsed_before_desaturation": bool(was_collapsed),
            "rewards": sorted(live_rewards + [float(r["reward"]) for r in group_rows
                                              if r["is_synthesized"]]),
            "invariants": invariants,
            **{key: invariants[key] for key in
               ("non_collapsed", "sigma_G")},
        })

    rows.sort(key=lambda r: (r["task_id"], -float(r["reward"]), r["id"]))
    by_task: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_task.setdefault(row["task_id"], []).append(row)
    dpo_pairs = _build_dpo_pairs(by_task)

    train_twin_keys: set[str] = set()
    for summary in task_summaries:
        train_twin_keys |= _twin_keys(summary)
    pairs_by_task = Counter(pair["task_id"] for pair in dpo_pairs)
    invariants: dict[str, Any] = {
        "non_collapsed_train_groups": sum(1 for s in task_summaries if s["non_collapsed"]),
        "collapsed_train_groups": sum(1 for s in task_summaries if not s["non_collapsed"]),
        "grpo_zero_mean": all(s["invariants"]["grpo_zero_mean"] for s in task_summaries),
        "grpo_unit_variance": all(s["invariants"]["grpo_unit_variance"] for s in task_summaries),
        "rloo_zero_mean": all(s["invariants"]["rloo_zero_mean"] for s in task_summaries),
        "twin_zero_mean": all(s["invariants"]["twin_zero_mean"] for s in task_summaries),
        "corpo_reward_ordering": all(
            s["invariants"]["corpo_reward_ordering"] for s in task_summaries),
        "split_twin_disjoint": (
            not (train_twin_keys & holdout_twin_keys)
            and all(row["task_id"] not in EVAL_HOLDOUT_TASKS for row in rows)
            and all(row["split"] == TRAIN_SPLIT for row in rows)
        ),
        "dpo_pairs_count": len(dpo_pairs),
        "dpo_pairs_per_task_min": min((pairs_by_task.get(t, 0) for t in train_tasks), default=0),
        "dpo_min_reward_margin": (
            min(float(p["reward_margin"]) for p in dpo_pairs) if dpo_pairs else None),
        "eval_leakage_tasks": sorted({r["task_id"] for r in rows} & set(EVAL_HOLDOUT_TASKS)),
    }

    summary: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "run_index": str(run_index_path),
            "pilot_dir": str(pilot_dir),
            "model": index_doc.get("model"),
            "extra_runs_roots": [str(p) for p in extra_runs_roots or []],
        },
        "counts": {
            "train_tasks": len(train_tasks),
            "rollouts": len(rows),
            "live_rollouts": sum(1 for r in rows if not r["is_synthesized"]),
            "synthesized_rollouts": sum(1 for r in rows if r["is_synthesized"]),
            "synthesized_tasks": sum(1 for s in task_summaries if s["synthesized_rollouts"]),
            "collapsed_before_desaturation": collapsed_before,
            "dpo_pairs": len(dpo_pairs),
        },
        "train_tasks": train_tasks,
        "eval_holdout_tasks": list(EVAL_HOLDOUT_TASKS),
        "tasks": task_summaries,
        "invariants": invariants,
        # Top-level mirrors named in plan 4.3 for direct consumption.
        "non_collapsed_train_groups": invariants["non_collapsed_train_groups"],
        "collapsed_train_groups": invariants["collapsed_train_groups"],
        "grpo_zero_mean": invariants["grpo_zero_mean"],
        "grpo_unit_variance": invariants["grpo_unit_variance"],
        "corpo_reward_ordering": invariants["corpo_reward_ordering"],
        "split_twin_disjoint": invariants["split_twin_disjoint"],
        "dpo_pairs_count": invariants["dpo_pairs_count"],
        "warnings": warnings,
        "rows": rows,
        "grpo_rows": rows,
        "dpo_pairs": dpo_pairs,
    }

    if out_dir is not None:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        files: dict[str, Any] = {}
        for name, payload_rows in ((GRPO_FILE, rows), (DPO_FILE, dpo_pairs)):
            payload = _jsonl_bytes(payload_rows)
            (out_dir / name).write_bytes(payload)
            files[name] = {"rows": len(payload_rows), "bytes": len(payload),
                           "sha256": hashlib.sha256(payload).hexdigest()}
        summary["files"] = files
        card = {key: value for key, value in summary.items()
                if key not in ("rows", "grpo_rows", "dpo_pairs")}
        (out_dir / SUMMARY_FILE).write_text(
            json.dumps(card, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-index", type=Path,
                        default=Path("/home/ubuntu/alkera_factory_1017/phase2_run_index.json"))
    parser.add_argument("--pilot-dir", type=Path,
                        default=Path("/home/ubuntu/alkera_factory_1017/phase2_pilot_51"))
    parser.add_argument("--extra-runs-roots", type=Path, nargs="*", default=None,
                        help="Additional run roots (e.g. runs_ceiling_desat runs_cross_model)")
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    extra: list[Path] = []
    for item in args.extra_runs_roots or []:
        extra.extend(Path(part) for part in str(item).split(",") if part)
    summary = build_desaturated_training_groups(
        args.run_index, args.pilot_dir, extra_runs_roots=extra or None,
        out_dir=args.out_dir)
    print(json.dumps({
        "train_tasks": summary["counts"]["train_tasks"],
        "non_collapsed_train_groups": summary["non_collapsed_train_groups"],
        "collapsed_train_groups": summary["collapsed_train_groups"],
        "rollouts": summary["counts"]["rollouts"],
        "live_rollouts": summary["counts"]["live_rollouts"],
        "synthesized_rollouts": summary["counts"]["synthesized_rollouts"],
        "dpo_pairs_count": summary["dpo_pairs_count"],
        "invariants": summary["invariants"],
        "out_dir": str(args.out_dir) if args.out_dir else None,
    }, sort_keys=True))
    ok = (summary["non_collapsed_train_groups"] == summary["counts"]["train_tasks"]
          and summary["split_twin_disjoint"] and summary["grpo_zero_mean"]
          and summary["grpo_unit_variance"] and summary["corpo_reward_ordering"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
