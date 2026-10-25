#!/usr/bin/env python3
"""Cross-model frontier evaluation matrix and trajectory/loss comparison analyzer.

Evaluates a stratified set of complete epistemic twin triplets (`act-authorized`,
`withhold-blocked`, `escalate-ambiguous`) across multiple frontier and open-weight
model families (`claude-opus-5-5-high`, `gpt-5.6-sol-high`, `kimi-k3-high`,
`glm-5.2-max`) in isolated container topology, and compares:
  1. Binary `pass@1` and unbiased `pass@k` / `pass^k` at uniform K=3,
  2. Outcome-class distributions (`resolved`, `withheld_with_evidence`,
     `acted_unresolved`, `boundary_violated`, `false_claim`, `silent`),
  3. 7-step continuous warehouse/replay progress (`q0_raw_loaded` .. `q6_r4_parity`),
  4. Within-group GRPO/DAPO/RLOO reward variance and non-collapsed group yield,
  5. Paired-Twin Contrastive action vs. refusal discrimination (`twin_suite_action_discrimination`),
  6. Token-level observation masking and 5-phase causal trajectory allocation,
  7. Task package fingerprint & integrity consistency across models.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from tasks.factory.paths import FACTORY_ROOT, resolve_factory_path
from tasks.factory.trajectory_loss_model import (
    CAUSAL_PHASES,
    SEVEN_KEYS,
    compute_twin_contrastive_advantages,
    model_task_group,
)

# 9-task stratified benchmark slice: 3 complete epistemic twin triplets
# spanning 3 themes (commerce_analytics, revenue_daily, excel_close) and
# 3 core fault topologies (FT02, FT03, FT06).
STRATIFIED_TRIPLET_SLICE: tuple[str, ...] = (
    # Triplet 1: FT02 on commerce_analytics (S02 + S06 + S10 | H02 + H09)
    "FT0100-commerce-analytics-ft02-auth-b",
    "FT0101-commerce-analytics-ft02-hold-b",
    "FT0102-commerce-analytics-ft02-esc-b",
    # Triplet 2: FT03 on revenue_daily (S03 | H03 + H10)
    "FT0193-revenue-daily-ft03-auth-a",
    "FT0194-revenue-daily-ft03-hold-a",
    "FT0195-revenue-daily-ft03-esc-a",
    # Triplet 3: FT06 on excel_close (S06 | H06 + H13)
    "FT0484-excel-close-ft06-auth-b",
    "FT0485-excel-close-ft06-hold-b",
    "FT0486-excel-close-ft06-esc-b",
)

CEILING_TRAINING_SLICE: tuple[str, ...] = (
    "FT0001-settlement-clearing-ft01-auth-a",
    "FT0002-settlement-clearing-ft01-hold-a",
    "FT0003-settlement-clearing-ft01-esc-a",
    "FT0385-mainframe-batch-ft05-auth-a",
    "FT0386-mainframe-batch-ft05-hold-a",
    "FT0387-mainframe-batch-ft05-esc-a",
    "FT0577-saas-subscription-mrr-ft07-auth-a",
    "FT0578-saas-subscription-mrr-ft07-hold-a",
    "FT0769-supply-chain-inventory-ft09-auth-a",
    "FT0770-supply-chain-inventory-ft09-hold-a",
    "FT0771-supply-chain-inventory-ft09-esc-a",
    "FT0868-healthcare-claims-edi-ft10-auth-b",
    "FT0869-healthcare-claims-edi-ft10-hold-b",
    "FT0961-fintech-kyc-risk-limits-ft11-auth-a",
    "FT0962-fintech-kyc-risk-limits-ft11-hold-a",
    "FT0963-fintech-kyc-risk-limits-ft11-esc-a",
    "FT1060-iot-telemetry-billing-ft12-auth-b",
    "FT1061-iot-telemetry-billing-ft12-hold-b",
)

DEFAULT_MODELS: tuple[str, ...] = (
    "claude-opus-5-5-high",
    "gpt-5.6-sol-high",
    "kimi-k3-high",
    "glm-5.2-max",
)


def _package_provenance(task_dir: Path) -> dict[str, str | None]:
    prov_path = Path(task_dir) / "tests" / "phase2_provenance.json"
    if not prov_path.is_file():
        return {"package_fingerprint": None, "task_integrity": None}
    try:
        doc = json.loads(prov_path.read_text(encoding="utf-8"))
        return {
            "package_fingerprint": doc.get("fingerprint"),
            "task_integrity": doc.get("task_integrity"),
        }
    except (OSError, ValueError):
        return {"package_fingerprint": None, "task_integrity": None}


def find_latest_model_run(
    runs_root: Path,
    task_id: str,
    model: str,
    *,
    fallback_run_index: dict[str, Any] | None = None,
    prefer_k: int | None = 3,
) -> Path | None:
    """Locate the latest valid container run directory for (task_id, model).

    When ``prefer_k`` is set (e.g. ``3`` for uniform K=3 cross-model comparison),
    prefers run directories with ``scored == prefer_k`` (or ``entry["k3"]`` from
    ``fallback_run_index`` for ``claude-opus-5-5-high``).
    """
    if fallback_run_index and model == "claude-opus-5-5-high":
        entry = (fallback_run_index.get("runs") or {}).get(task_id) or {}
        anchor = Path(runs_root).resolve().parent
        if prefer_k == 3 and entry.get("k3"):
            cand = resolve_factory_path(entry["k3"], anchor_dir=anchor)
            if cand.is_dir():
                return cand
        if prefer_k == 5 and entry.get("k5"):
            cand = resolve_factory_path(entry["k5"], anchor_dir=anchor)
            if cand.is_dir():
                return cand

    task_runs_dir = Path(runs_root) / task_id
    preferred: list[tuple[str, Path]] = []
    any_valid: list[tuple[str, Path]] = []
    if task_runs_dir.is_dir():
        for report_path in sorted(task_runs_dir.glob("*/report.json")):
            try:
                rep = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            summary = rep.get("summary") or {}
            models = summary.get("models") or []
            scored = int(summary.get("scored") or 0)
            if model in models and scored > 0:
                any_valid.append((report_path.parent.name, report_path.parent))
                if prefer_k is None or scored == prefer_k:
                    preferred.append((report_path.parent.name, report_path.parent))
    if preferred:
        preferred.sort(key=lambda item: item[0])
        return preferred[-1][1]
    if any_valid:
        any_valid.sort(key=lambda item: item[0])
        return any_valid[-1][1]

    if fallback_run_index and model == "claude-opus-5-5-high":
        entry = (fallback_run_index.get("runs") or {}).get(task_id) or {}
        path_str = entry.get("k3") if prefer_k == 3 else (entry.get("k5") or entry.get("k3"))
        if path_str:
            cand = resolve_factory_path(path_str, anchor_dir=Path(runs_root).resolve().parent)
            if cand.is_dir():
                return cand
    return None


def analyze_model_on_slice(
    model: str,
    *,
    runs_root: Path,
    pilot_dir: Path,
    task_ids: list[str] | tuple[str, ...] = STRATIFIED_TRIPLET_SLICE,
    fallback_run_index: dict[str, Any] | None = None,
    prefer_k: int | None = 3,
) -> dict[str, Any]:
    """Compute trajectory, progress, outcome, provenance, and RL loss metrics for one model."""
    task_models: list[dict[str, Any]] = []
    package_hashes: dict[str, dict[str, str | None]] = {}
    quarantined_total = 0

    for task_id in task_ids:
        tdir = Path(pilot_dir) / task_id
        run_dir = find_latest_model_run(
            runs_root,
            task_id,
            model,
            fallback_run_index=fallback_run_index,
            prefer_k=prefer_k,
        )
        if run_dir is None:
            continue
        rep_path = run_dir / "report.json"
        if rep_path.is_file():
            try:
                rep_doc = json.loads(rep_path.read_text(encoding="utf-8"))
                quarantined_total += int((rep_doc.get("summary") or {}).get("quarantined") or 0)
            except (OSError, ValueError):
                pass
        prov = _package_provenance(tdir)
        package_hashes[task_id] = prov
        group = model_task_group(run_dir, tdir)
        group["package_fingerprint"] = prov["package_fingerprint"]
        group["task_integrity"] = prov["task_integrity"]
        task_models.append(group)

    if not task_models:
        return {
            "model": model,
            "task_count": 0,
            "total_rollouts": 0,
            "complete_slice_9_of_9": False,
            "uniform_k3": False,
            "zero_quarantines": True,
            "quarantined_rollouts": 0,
            "package_hashes": {},
            "tasks": [],
        }

    suite = {
        "schema_version": 1,
        "model": model,
        "driver": "cursor",
        "topology": "container",
        "task_count": len(task_models),
        "total_rollouts": sum(t["k"] for t in task_models),
        "tasks": task_models,
    }
    twin_info = compute_twin_contrastive_advantages(suite)

    episodes = [ep for t in task_models for ep in t["episodes"]]
    n_eps = max(1, len(episodes))
    outcome_counts: dict[str, int] = {}
    for ep in episodes:
        oc = str(ep.get("outcome_class") or "unmeasured")
        outcome_counts[oc] = outcome_counts.get(oc, 0) + 1

    mean_seven = {
        key: round(
            sum(float(ep["seven_step_progress"].get(key, 0.0)) for ep in episodes) / n_eps,
            4,
        )
        for key in SEVEN_KEYS
    }
    phase_tokens = {
        phase: sum(int(ep["trajectory"]["phase_token_counts"].get(phase, 0)) for ep in episodes)
        for phase in CAUSAL_PHASES
    }
    total_asst_tokens = sum(int(ep["trajectory"]["assistant_tokens"]) for ep in episodes)
    total_masked_tokens = sum(int(ep["trajectory"]["masked_tokens"]) for ep in episodes)
    phase_token_shares = {
        phase: round(phase_tokens[phase] / max(1, total_asst_tokens), 4)
        for phase in CAUSAL_PHASES
    }

    by_mode: dict[str, list[dict[str, Any]]] = {}
    for t in task_models:
        mode = str(t.get("governance_mode") or "unknown")
        by_mode.setdefault(mode, []).append(t)

    mode_pass_at_1 = {
        mode: round(sum(float(t["pass_at_1"]) for t in groups) / max(1, len(groups)), 4)
        for mode, groups in sorted(by_mode.items())
    }

    complete_slice = len(task_models) == len(task_ids) and int(twin_info["complete_triplets"]) == 3
    uniform_k3 = all(int(t["k"]) == 3 for t in task_models)

    return {
        "model": model,
        "task_count": len(task_models),
        "total_rollouts": len(episodes),
        "complete_slice_9_of_9": complete_slice,
        "uniform_k3": uniform_k3,
        "zero_quarantines": quarantined_total == 0,
        "quarantined_rollouts": quarantined_total,
        "package_hashes": package_hashes,
        "mean_pass_at_1": round(
            sum(float(t["pass_at_1"]) for t in task_models) / len(task_models), 4
        ),
        "mode_pass_at_1": mode_pass_at_1,
        "mean_progress_scalar": round(
            sum(float(ep["progress_scalar"] or 0.0) for ep in episodes) / n_eps, 4
        ),
        "mean_declared_reward": round(
            sum(float(ep["reward"] or 0.0) for ep in episodes) / n_eps, 4
        ),
        "mean_shaped_reward_variance": round(
            sum(float(t["shaped_reward_signal"]["variance"] or 0.0) for t in task_models)
            / len(task_models),
            4,
        ),
        "non_collapsed_groups": sum(
            1 for t in task_models if t["invariants"]["non_collapsed_reward_signal"]
        ),
        "non_collapsed_group_rate": round(
            sum(1 for t in task_models if t["invariants"]["non_collapsed_reward_signal"])
            / len(task_models),
            4,
        ),
        "outcome_counts": outcome_counts,
        "boundary_violation_rate": round(
            outcome_counts.get("boundary_violated", 0) / n_eps, 4
        ),
        "mean_seven_step_progress": mean_seven,
        "mean_tool_calls": round(
            sum(int(ep["tool_calls"]) for ep in episodes) / n_eps, 2
        ),
        "mean_wall_clock_s": round(
            sum(float(ep["wall_clock_s"]) for ep in episodes) / n_eps, 2
        ),
        "token_stats": {
            "assistant_tokens": total_asst_tokens,
            "masked_tokens": total_masked_tokens,
            "total_tokens": total_asst_tokens + total_masked_tokens,
            "assistant_token_fraction": round(
                total_asst_tokens / max(1, total_asst_tokens + total_masked_tokens), 4
            ),
            "phase_token_shares": phase_token_shares,
        },
        "twin_contrastive_summary": {
            "complete_triplets": twin_info["complete_triplets"],
            "suite_action_discrimination": twin_info["suite_action_discrimination"],
            "mode_bias_rates": twin_info["mode_bias_rates"],
            "all_invariants_hold": twin_info["all_invariants_hold"],
        },
        "all_invariants_hold": all(t["all_invariants_hold"] for t in task_models)
        and bool(twin_info["all_invariants_hold"]),
        "tasks": task_models,
    }


def build_cross_model_report(
    *,
    models: list[str] | tuple[str, ...] = DEFAULT_MODELS,
    runs_root: Path = FACTORY_ROOT / "runs_cross_model",
    opus_runs_root: Path = FACTORY_ROOT / "runs_phase2",
    pilot_dir: Path = FACTORY_ROOT / "phase2_pilot_51",
    run_index_path: Path = FACTORY_ROOT / "phase2_run_index.json",
    task_ids: list[str] | tuple[str, ...] = STRATIFIED_TRIPLET_SLICE,
    prefer_k: int | None = 3,
) -> dict[str, Any]:
    """Compare multiple frontier model families across the stratified triplet slice."""
    fallback_index = (
        json.loads(Path(run_index_path).read_text(encoding="utf-8"))
        if Path(run_index_path).is_file()
        else None
    )
    by_model: dict[str, dict[str, Any]] = {}
    for model in models:
        root = opus_runs_root if model == "claude-opus-5-5-high" else runs_root
        summary = analyze_model_on_slice(
            model,
            runs_root=root,
            pilot_dir=pilot_dir,
            task_ids=task_ids,
            fallback_run_index=fallback_index,
            prefer_k=prefer_k,
        )
        if summary["task_count"] == 0 and root != opus_runs_root:
            summary = analyze_model_on_slice(
                model,
                runs_root=opus_runs_root,
                pilot_dir=pilot_dir,
                task_ids=task_ids,
                fallback_run_index=fallback_index,
                prefer_k=prefer_k,
            )
        by_model[model] = summary

    evaluated = [m for m, s in by_model.items() if s["task_count"] > 0]
    reference_hashes = by_model[evaluated[0]]["package_hashes"] if evaluated else {}
    hashes_consistent = all(
        by_model[m]["package_hashes"] == reference_hashes
        for m in evaluated
        if by_model[m]["task_count"] == len(task_ids)
    )

    return {
        "schema_version": 1,
        "prefer_k": prefer_k,
        "slice_task_ids": list(task_ids),
        "slice_task_count": len(task_ids),
        "models_evaluated": evaluated,
        "package_hashes_consistent": hashes_consistent,
        "by_model": by_model,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument(
        "--runs-root",
        type=Path,
        default=FACTORY_ROOT / "runs_cross_model",
    )
    parser.add_argument(
        "--opus-runs-root",
        type=Path,
        default=FACTORY_ROOT / "runs_phase2",
    )
    parser.add_argument(
        "--pilot-dir",
        type=Path,
        default=FACTORY_ROOT / "phase2_pilot_51",
    )
    parser.add_argument(
        "--run-index",
        type=Path,
        default=FACTORY_ROOT / "phase2_run_index.json",
    )
    parser.add_argument(
        "--prefer-k",
        type=int,
        default=3,
        help="Target K per task for uniform cross-model comparison (default: 3)",
    )
    parser.add_argument("--out", type=Path, help="Output JSON report path")
    args = parser.parse_args(argv)

    report = build_cross_model_report(
        models=args.models,
        runs_root=args.runs_root,
        opus_runs_root=args.opus_runs_root,
        pilot_dir=args.pilot_dir,
        run_index_path=args.run_index,
        prefer_k=args.prefer_k,
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    compact = {
        m: {
            "task_count": s["task_count"],
            "total_rollouts": s["total_rollouts"],
            "complete_slice_9_of_9": s.get("complete_slice_9_of_9"),
            "uniform_k3": s.get("uniform_k3"),
            "zero_quarantines": s.get("zero_quarantines"),
            "mean_pass_at_1": s.get("mean_pass_at_1"),
            "mode_pass_at_1": s.get("mode_pass_at_1"),
            "mean_progress_scalar": s.get("mean_progress_scalar"),
            "mean_declared_reward": s.get("mean_declared_reward"),
            "non_collapsed_groups": s.get("non_collapsed_groups"),
            "boundary_violation_rate": s.get("boundary_violation_rate"),
            "all_invariants_hold": s.get("all_invariants_hold"),
        }
        for m, s in report["by_model"].items()
    }
    print(json.dumps(compact, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
