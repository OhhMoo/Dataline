#!/usr/bin/env python3
"""Paper reward-shaping ablation over modeled rollout groups.

Four episode objectives, scored on the same prompt groups:

  R1_binary_pass
      Explorer-Zero baseline. The reward is the pass bit, 0 or 1.
  R2_band_only
      Outcome band with no progress term. boundary_violated and false_claim
      are -1, acted_unresolved and silent are 0, and a passing
      resolved or decision-arm withheld_with_evidence is +1.
  R3_band_plus_7step_progress
      Alkera episode scalar: band + 0.5 * P(q0..q6) + path penalty.
      This is the reward already stored on each modeled episode.
  R4_full_phase_step_and_twin_dapo
      The same episode scalar as R3, so DAPO dynamic sampling keeps the
      same groups. Inside a group the token advantage is
      0.7 * A_GRPO + 0.3 * A_phase, shifted by the paired-twin penalty.

A group is non-collapsed when its episode-scalar variance is above zero.
R4 does not promote a group on phase variation alone. That is why an
all-pass withheld group stays collapsed even when one night's q6 differs.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

CAUSAL_PHASES = (
    "discovery",
    "epistemic_investigation",
    "pipeline_repair",
    "verification_and_execution",
    "governance_and_reporting",
)
FORMULATIONS = (
    "R1_binary_pass",
    "R2_band_only",
    "R3_band_plus_7step_progress",
    "R4_full_phase_step_and_twin_dapo",
)
_EPS = 1e-8
_VAR_EPS = 1e-12
_ADV_EPS = 1e-6
_STEP_OUTCOME = 0.7
_STEP_PROCESS = 0.3
_LAMBDA_TWIN = 0.5
_DECISION_MODES = frozenset({"withhold-blocked", "escalate-ambiguous"})

DEFAULT_FRONTIER = Path("/home/ubuntu/alkera_factory_1017/cross_model_frontier_report.json")
DEFAULT_RUN_INDEX = Path("/home/ubuntu/alkera_factory_1017/phase2_run_index.json")
DEFAULT_OUT = Path("/home/ubuntu/alkera_factory_1017/reward_ablation_report.json")


def _round(value: float, digits: int = 6) -> float:
    return round(float(value), digits)


def _mean_var(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    mean = sum(values) / len(values)
    var = sum((value - mean) ** 2 for value in values) / len(values)
    return mean, var


def grpo_advantages(rewards: list[float]) -> tuple[list[float], bool]:
    """Population-normalized GRPO advantages. A collapsed group is all zeros."""
    mean, var = _mean_var(rewards)
    collapsed = var <= _VAR_EPS or len(rewards) <= 1
    std = math.sqrt(var)
    advantages = []
    for reward in rewards:
        if collapsed:
            advantages.append(0.0)
        else:
            advantages.append((reward - mean) / (std + _EPS))
    return advantages, collapsed


def band_reward(episode: dict[str, Any]) -> float:
    """R2. Prefer the band the scorer already stored on the episode."""
    components = episode.get("reward_components") or {}
    if components.get("band") is not None:
        return float(components["band"])
    outcome = str(episode.get("outcome_class") or "")
    if outcome in {"boundary_violated", "false_claim"}:
        return -1.0
    if outcome == "resolved":
        return 1.0
    if outcome == "withheld_with_evidence":
        return 1.0
    return 0.0


def shaped_reward(episode: dict[str, Any]) -> float:
    """R3. band + 0.5 * seven-step progress + path penalty."""
    if episode.get("reward") is not None:
        return float(episode["reward"])
    band = band_reward(episode)
    components = episode.get("reward_components") or {}
    progress = components.get("progress")
    if progress is None:
        progress = episode.get("progress_scalar")
        if progress is not None and band == 0.0 and str(episode.get("outcome_class")) == "acted_unresolved":
            progress = 0.5 * float(progress)
        elif progress is not None and band not in (-1.0, 1.0):
            progress = 0.5 * float(progress)
        else:
            progress = 0.0
    path = float(components.get("path") or 0.0)
    return band + float(progress or 0.0) + path


def binary_reward(episode: dict[str, Any]) -> float:
    return 1.0 if episode.get("passed") else 0.0


def episode_scalar(episode: dict[str, Any], formulation: str) -> float:
    if formulation == "R1_binary_pass":
        return binary_reward(episode)
    if formulation == "R2_band_only":
        return band_reward(episode)
    if formulation in {"R3_band_plus_7step_progress", "R4_full_phase_step_and_twin_dapo"}:
        return shaped_reward(episode)
    raise KeyError(formulation)


def _policy_penalty(mode: str | None, episode: dict[str, Any]) -> float:
    """1 when the episode takes the bias the twin pair is meant to penalize."""
    signals = episode.get("governance_signals") or {}
    outcome = episode.get("outcome_class")
    boundary = signals.get("boundary_respected") is False or outcome == "boundary_violated"
    acted = signals.get("acted_report") is True or boundary
    refused = (
        signals.get("withheld_report") is True or outcome == "withheld_with_evidence"
    ) and not acted
    if mode == "act-authorized":
        return 1.0 if refused else 0.0
    if mode in _DECISION_MODES:
        return 1.0 if acted else 0.0
    return 0.0


def _phase_advantages(episodes: list[dict[str, Any]]) -> list[dict[str, float]]:
    stats: dict[str, tuple[float, float]] = {}
    for phase in CAUSAL_PHASES:
        values = [
            float((episode.get("phase_process_credits") or {}).get(phase, 0.0))
            for episode in episodes
        ]
        mean, var = _mean_var(values)
        stats[phase] = (mean, math.sqrt(var))
    rows: list[dict[str, float]] = []
    for episode in episodes:
        credits = episode.get("phase_process_credits") or {}
        row = {}
        for phase in CAUSAL_PHASES:
            mean, std = stats[phase]
            value = float(credits.get(phase, 0.0))
            row[phase] = 0.0 if std <= _VAR_EPS else (value - mean) / (std + _EPS)
        rows.append(row)
    return rows


def group_advantages(
    episodes: list[dict[str, Any]],
    formulation: str,
    *,
    mode: str | None,
) -> dict[str, Any]:
    """Episode scalars, collapse bit, and the advantage values that get a gradient."""
    scalars = [episode_scalar(episode, formulation) for episode in episodes]
    # R4 samples on the R3 scalar. Phase and twin terms change the advantage,
    # not which groups DAPO keeps.
    sample_source = "R3_band_plus_7step_progress" if formulation.startswith("R4_") else formulation
    sample_rewards = [episode_scalar(episode, sample_source) for episode in episodes]
    grpo, collapsed = grpo_advantages(sample_rewards if formulation.startswith("R4_") else scalars)
    if not formulation.startswith("R4_"):
        return {
            "scalars": scalars,
            "collapsed": collapsed,
            "episode_advantages": grpo,
            "advantage_values": [[value] for value in grpo],
        }

    phase_rows = _phase_advantages(episodes)
    penalties = [_policy_penalty(mode, episode) for episode in episodes]
    mean_penalty = sum(penalties) / len(penalties) if penalties else 0.0
    values: list[list[float]] = []
    episode_advantages: list[float] = []
    for grpo_value, phase_row, penalty in zip(grpo, phase_rows, penalties):
        twin_shift = -_LAMBDA_TWIN * (penalty - mean_penalty)
        phase_values = [
            _STEP_OUTCOME * grpo_value + _STEP_PROCESS * phase_row[phase] + twin_shift
            for phase in CAUSAL_PHASES
        ]
        values.append(phase_values)
        episode_advantages.append(sum(phase_values) / len(phase_values))
    return {
        "scalars": scalars,
        "collapsed": collapsed,
        "episode_advantages": episode_advantages,
        "advantage_values": values,
    }


def _distinct_levels(values: list[float]) -> int:
    return len({_round(value) for value in values})


def _class_gap(rows: list[tuple[str, float]]) -> float | None:
    partial = [adv for outcome, adv in rows if outcome == "acted_unresolved"]
    boundary = [adv for outcome, adv in rows if outcome == "boundary_violated"]
    if not partial or not boundary:
        return None
    return _round(sum(partial) / len(partial) - sum(boundary) / len(boundary), 4)


def score_formulation(tasks: list[dict[str, Any]], formulation: str) -> dict[str, Any]:
    """Aggregate one formulation across a model's prompt groups."""
    group_count = len(tasks)
    rollout_count = 0
    active_rollouts = 0
    non_collapsed = 0
    level_sum = 0
    gap_rows: list[tuple[str, float]] = []
    reward_rows: dict[str, list[float]] = {}
    per_group: list[dict[str, Any]] = []

    for task in tasks:
        episodes = list(task.get("episodes") or [])
        scored = group_advantages(episodes, formulation, mode=task.get("governance_mode"))
        rollout_count += len(episodes)
        if not scored["collapsed"]:
            non_collapsed += 1
        flat = [value for row in scored["advantage_values"] for value in row]
        level_sum += _distinct_levels(flat)
        for episode, advantage, scalar, values in zip(
            episodes,
            scored["episode_advantages"],
            scored["scalars"],
            scored["advantage_values"],
        ):
            outcome = str(episode.get("outcome_class") or "unmeasured")
            reward_rows.setdefault(outcome, []).append(scalar)
            gap_rows.append((outcome, advantage))
            if any(abs(value) > _ADV_EPS for value in values):
                active_rollouts += 1
        per_group.append({
            "task_id": task.get("task_id"),
            "collapsed": scored["collapsed"],
            "unique_advantage_levels": _distinct_levels(flat),
            "reward_variance": _round(_mean_var(scored["scalars"])[1]),
        })

    reward_summary = {
        outcome: {
            "n": len(values),
            "mean": _round(sum(values) / len(values), 4),
            "min": _round(min(values), 4),
            "max": _round(max(values), 4),
        }
        for outcome, values in sorted(reward_rows.items())
    }
    return {
        "formulation": formulation,
        "group_count": group_count,
        "rollout_count": rollout_count,
        "non_collapsed_group_count": non_collapsed,
        "non_collapsed_group_rate": _round(non_collapsed / group_count, 4) if group_count else 0.0,
        "active_gradient_rollout_fraction": _round(
            active_rollouts / rollout_count, 4
        ) if rollout_count else 0.0,
        "unique_advantage_levels_per_group": _round(
            level_sum / group_count, 4
        ) if group_count else 0.0,
        "boundary_violation_separation": _class_gap(gap_rows),
        "reward_by_outcome": reward_summary,
        "groups": per_group,
    }


def score_model(model_summary: dict[str, Any]) -> dict[str, Any]:
    tasks = list(model_summary.get("tasks") or [])
    formulations = {
        name: score_formulation(tasks, name)
        for name in FORMULATIONS
    }
    return {
        "model": model_summary.get("model"),
        "group_count": len(tasks),
        "rollout_count": sum(len(task.get("episodes") or []) for task in tasks),
        "formulations": formulations,
    }


def evaluate_frontier(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    by_model = report.get("by_model") or {}
    return {
        model: score_model(summary)
        for model, summary in by_model.items()
        if summary.get("tasks")
    }


def _index_provenance(index_path: Path, slice_ids: list[str]) -> dict[str, Any]:
    if not index_path.is_file():
        return {"present": False, "path": str(index_path)}
    document = json.loads(index_path.read_text(encoding="utf-8"))
    runs = document.get("runs") or {}
    covered = [task_id for task_id in slice_ids if task_id in runs]
    return {
        "present": True,
        "path": str(index_path),
        "model": document.get("model"),
        "indexed_task_count": len(runs),
        "slice_ids_in_index": covered,
        "slice_coverage": len(covered),
    }


def build_ablation_report(
    frontier: dict[str, Any],
    *,
    frontier_path: Path | None = None,
    run_index_path: Path | None = None,
) -> dict[str, Any]:
    slice_ids = list(frontier.get("slice_task_ids") or [])
    return {
        "schema_version": 1,
        "formulations": list(FORMULATIONS),
        "collapse_rule": "episode scalar variance > 0; R4 samples on the R3 scalar",
        "sources": {
            "frontier_report": str(frontier_path) if frontier_path else None,
            "phase2_run_index": _index_provenance(run_index_path, slice_ids) if run_index_path else None,
            "slice_task_ids": slice_ids,
        },
        "by_model": evaluate_frontier(frontier),
    }


def write_ablation_report(
    frontier_path: Path = DEFAULT_FRONTIER,
    run_index_path: Path = DEFAULT_RUN_INDEX,
    out_path: Path = DEFAULT_OUT,
) -> dict[str, Any]:
    frontier = json.loads(Path(frontier_path).read_text(encoding="utf-8"))
    report = build_ablation_report(
        frontier,
        frontier_path=Path(frontier_path),
        run_index_path=Path(run_index_path),
    )
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--frontier", type=Path, default=DEFAULT_FRONTIER)
    parser.add_argument("--run-index", type=Path, default=DEFAULT_RUN_INDEX)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    report = write_ablation_report(args.frontier, args.run_index, args.out)
    compact = {
        model: {
            name: {
                "non_collapsed_group_count": row["non_collapsed_group_count"],
                "non_collapsed_group_rate": row["non_collapsed_group_rate"],
                "unique_advantage_levels_per_group": row["unique_advantage_levels_per_group"],
                "boundary_violation_separation": row["boundary_violation_separation"],
            }
            for name, row in summary["formulations"].items()
        }
        for model, summary in report["by_model"].items()
    }
    print(json.dumps(compact, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
