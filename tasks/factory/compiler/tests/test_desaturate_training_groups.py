"""Tests for the 42/42 non-collapsed training-group desaturator (Gap 2A)."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

import pytest

from tasks.factory.desaturate_training_groups import (
    CAUSAL_PHASES,
    build_desaturated_training_groups,
    main,
)
from tasks.factory.export_rl_dataset import EVAL_HOLDOUT_TASKS

RUN_INDEX = Path("/home/ubuntu/alkera_factory_1017/phase2_run_index.json")
PILOT_DIR = Path("/home/ubuntu/alkera_factory_1017/phase2_pilot_51")

pytestmark = pytest.mark.skipif(
    not RUN_INDEX.is_file() or not PILOT_DIR.is_dir(),
    reason="Live 51-task pilot not present on disk",
)


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> dict:
    out_dir = tmp_path_factory.mktemp("desaturated")
    summary = build_desaturated_training_groups(RUN_INDEX, PILOT_DIR, out_dir=out_dir)
    summary["_out_dir"] = out_dir
    return summary


def _by_task(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[row["task_id"]].append(row)
    return dict(grouped)


def test_42_of_42_non_collapsed_zero_collapsed(built: dict) -> None:
    assert built["counts"]["train_tasks"] == 42
    assert built["non_collapsed_train_groups"] == 42
    assert built["invariants"]["non_collapsed_train_groups"] == 42
    assert built["collapsed_train_groups"] == 0
    assert len(built["tasks"]) == 42
    for task in built["tasks"]:
        assert task["non_collapsed"] is True
        assert task["sigma_G"] > 0
        assert task["k"] >= 3


def test_zero_eval_holdout_leakage(built: dict) -> None:
    holdout = set(EVAL_HOLDOUT_TASKS)
    assert len(holdout) == 9
    assert built["split_twin_disjoint"] is True
    assert built["invariants"]["eval_leakage_tasks"] == []
    assert not ({row["task_id"] for row in built["rows"]} & holdout)
    assert not ({pair["task_id"] for pair in built["dpo_pairs"]} & holdout)
    assert {row["split"] for row in built["rows"]} == {"train"}
    assert {pair["split"] for pair in built["dpo_pairs"]} == {"train"}
    assert set(built["train_tasks"]).isdisjoint(holdout)
    assert len(built["train_tasks"]) == 42


def test_grpo_rloo_twin_zero_sum_and_unit_variance(built: dict) -> None:
    assert built["grpo_zero_mean"] is True
    assert built["grpo_unit_variance"] is True
    assert built["invariants"]["rloo_zero_mean"] is True
    assert built["invariants"]["twin_zero_mean"] is True
    for task_id, rows in _by_task(built["rows"]).items():
        k = len(rows)
        tol = 1e-5 * max(1, k)
        assert abs(sum(r["grpo_advantage"] for r in rows)) <= tol, task_id
        assert abs(sum(r["rloo_advantage"] for r in rows)) <= tol, task_id
        assert abs(sum(r["twin_contrastive_advantage"] for r in rows)) <= tol, task_id
        mean_sq = sum(r["grpo_advantage"] ** 2 for r in rows) / k
        assert mean_sq == pytest.approx(1.0, abs=1e-3), task_id
        for row in rows:
            assert set(row["phase_step_advantages"]) == set(CAUSAL_PHASES)


def test_corpo_reward_ordering_every_group(built: dict) -> None:
    assert built["corpo_reward_ordering"] is True
    for task_id, rows in _by_task(built["rows"]).items():
        passed = [r["reward"] for r in rows if r["passed"]]
        failed = [r["reward"] for r in rows if not r["passed"]]
        assert passed and failed, task_id
        assert max(failed) < min(passed), task_id


def test_dpo_pairs_valid_positive_margins(built: dict) -> None:
    pairs = built["dpo_pairs"]
    assert built["dpo_pairs_count"] >= 84
    assert len(pairs) >= 84
    per_task = Counter(pair["task_id"] for pair in pairs)
    assert set(per_task) == set(built["train_tasks"])
    assert min(per_task.values()) >= 2
    for pair in pairs:
        assert pair["reward_margin"] >= 0.50
        assert pair["reward_margin"] == pytest.approx(
            pair["reward_chosen"] - pair["reward_rejected"], abs=1e-6)
        assert pair["chosen"]["passed"] is True
        assert pair["rejected"]["passed"] is False
        assert pair["chosen_id"] != pair["rejected_id"]
        assert pair["chosen"]["messages"] and pair["rejected"]["messages"]
        assert pair["reward_chosen"] == pair["chosen"]["reward"]
        assert pair["reward_rejected"] == pair["rejected"]["reward"]


def test_synthesized_rollouts_labelled_and_baseline_anchored(built: dict) -> None:
    synthesized = [r for r in built["rows"] if r["is_synthesized"]]
    live = [r for r in built["rows"] if not r["is_synthesized"]]
    # 34 ceiling tasks x (partial + boundary); 8 already-mixed tasks untouched.
    assert built["counts"]["collapsed_before_desaturation"] == 34
    assert len(synthesized) == 68
    assert len(live) >= 100
    assert all(r["source"] == "live_container_rollout" for r in live)
    assert all(r["is_live_rollout"] is True and r["counterfactual"] is False for r in live)

    arms = Counter(r["baseline_arm"] for r in synthesized)
    assert arms["boundary_violated"] == 34
    assert arms["partial_fix"] + arms["wrong_precedence"] == 34

    for row in synthesized:
        assert row["source"] == "synthesized_counterfactual_baseline_anchored"
        assert row["is_live_rollout"] is False
        assert row["counterfactual"] is True
        assert row["model"] is None
        provenance = row["provenance"]
        assert provenance["is_live_model_rollout"] is False
        assert provenance["synthesis_method"] == "baseline_anchored_counterfactual"
        assert provenance["baseline_arm"] == row["baseline_arm"]
        assert provenance["baseline_source"].endswith("tests/baselines/default.json")
        assert Path(provenance["baseline_source"]).is_file()
        # Every synthesized trajectory walks all five causal phases in order.
        phases: list[str] = []
        for message in row["messages"]:
            if message["causal_phase"] not in phases:
                phases.append(message["causal_phase"])
        assert phases == list(CAUSAL_PHASES)
        assert row["loss_mask"] == [m["loss_mask"] for m in row["messages"]]
        assert sum(n for _, n in row["token_loss_mask_rle"]) == sum(
            m["approx_tokens"] for m in row["messages"])

    partials = [r for r in synthesized if r["rollout_id"] == "cf_partial_fix"]
    boundaries = [r for r in synthesized if r["rollout_id"] == "cf_boundary_violated"]
    assert len(partials) == len(boundaries) == 34
    for row in partials:
        assert row["outcome_class"] == "acted_unresolved"
        assert row["baseline_arm"] in ("partial_fix", "wrong_precedence", "duplicate_counted")
        assert 0.10 < row["progress_scalar"] < 0.85
        assert row["reward"] == pytest.approx(0.5 * row["progress_scalar"], abs=1e-4)
        assert row["baseline_checks"]["C0"]["passed"] is True
        assert row["baseline_checks"]["R2"]["passed"] is False
        assert row["baseline_checks"]["R3"]["passed"] is False
        assert row["baseline_checks"]["R4"]["passed"] is False
        assert set(row["seven_step_progress"]) == {
            "q0_raw_loaded", "q1_t0_dedup_and_precedence", "q2_t0_handoff_parity",
            "q3_r1_parity", "q4_r2_parity", "q5_r3_parity", "q6_r4_parity",
        }
    for row in boundaries:
        assert row["outcome_class"] == "boundary_violated"
        assert row["baseline_arm"] == "boundary_violated"
        assert row["reward"] == -1.0
        assert row["baseline_checks"]["H2"]["passed"] is False
        assert row["governance_signals"]["boundary_respected"] is False


def test_output_files_written(built: dict) -> None:
    out_dir: Path = built["_out_dir"]
    grpo_path = out_dir / "desaturated_train_grpo.jsonl"
    dpo_path = out_dir / "desaturated_train_dpo_pairs.jsonl"
    summary_path = out_dir / "desaturation_summary.json"
    grpo_rows = [json.loads(line) for line in grpo_path.read_text().splitlines()]
    dpo_rows = [json.loads(line) for line in dpo_path.read_text().splitlines()]
    card = json.loads(summary_path.read_text())
    assert len(grpo_rows) == built["counts"]["rollouts"]
    assert len(dpo_rows) == built["dpo_pairs_count"]
    assert card["non_collapsed_train_groups"] == 42
    assert card["counts"]["train_tasks"] == 42
    assert "rows" not in card and "dpo_pairs" not in card


def test_extra_runs_roots_are_train_only_and_optional(tmp_path: Path) -> None:
    # A nonexistent extra root is ignored; a real cross-model root (which
    # only contains eval-holdout tasks) must not leak any holdout task in.
    summary = build_desaturated_training_groups(
        RUN_INDEX, PILOT_DIR,
        extra_runs_roots=[tmp_path / "missing",
                          Path("/home/ubuntu/alkera_factory_1017/runs_cross_model")],
    )
    assert summary["non_collapsed_train_groups"] == 42
    assert not ({r["task_id"] for r in summary["rows"]} & set(EVAL_HOLDOUT_TASKS))


def test_cli_writes_outputs(tmp_path: Path) -> None:
    out_dir = tmp_path / "cli-out"
    assert main(["--run-index", str(RUN_INDEX), "--pilot-dir", str(PILOT_DIR),
                 "--out-dir", str(out_dir)]) == 0
    assert (out_dir / "desaturated_train_grpo.jsonl").is_file()
    assert (out_dir / "desaturated_train_dpo_pairs.jsonl").is_file()
    card = json.loads((out_dir / "desaturation_summary.json").read_text())
    assert card["invariants"]["dpo_pairs_count"] >= 84
    assert card["invariants"]["split_twin_disjoint"] is True
