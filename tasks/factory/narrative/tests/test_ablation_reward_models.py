"""Reward-shaping ablation on the live 9-task cross-model matrix."""
from __future__ import annotations

import json

from tasks.factory.ablation_reward_models import (
    CAUSAL_PHASES,
    DEFAULT_FRONTIER,
    DEFAULT_OUT,
    FORMULATIONS,
    score_model,
    write_ablation_report,
)

R1 = "R1_binary_pass"
R2 = "R2_band_only"
R3 = "R3_band_plus_7step_progress"
R4 = "R4_full_phase_step_and_twin_dapo"


def _phases(value: float) -> dict[str, float]:
    return {
        phase: value if index % 2 == 0 else 1.0 - value
        for index, phase in enumerate(CAUSAL_PHASES)
    }


def _episode(outcome: str, passed: bool, reward: float, band: float, progress: float | None, phase: float, *, acted: bool, withheld: bool, boundary: bool) -> dict:
    return {
        "passed": passed,
        "outcome_class": outcome,
        "reward": reward,
        "reward_components": {"band": band, "progress": progress, "path": 0.0},
        "progress_scalar": 0.0 if progress is None else progress / 0.5,
        "phase_process_credits": _phases(phase),
        "governance_signals": {
            "acted_report": acted,
            "withheld_report": withheld,
            "boundary_respected": not boundary,
        },
    }


def test_scalar_formulations_keep_damage_below_a_partial() -> None:
    summary = score_model({
        "model": "synthetic",
        "tasks": [{
            "task_id": "synthetic-hold",
            "governance_mode": "withhold-blocked",
            "episodes": [
                _episode("boundary_violated", False, -1.0, -1.0, None, 0.2, acted=True, withheld=False, boundary=True),
                _episode("acted_unresolved", False, 0.25, 0.0, 0.25, 0.6, acted=False, withheld=True, boundary=False),
                _episode("withheld_with_evidence", True, 1.0, 1.0, None, 1.0, acted=False, withheld=True, boundary=False),
            ],
        }],
    })
    for name in (R2, R3, R4):
        rewards = summary["formulations"][name]["reward_by_outcome"]
        assert rewards["boundary_violated"]["max"] == -1.0
    for name in (R3, R4):
        rewards = summary["formulations"][name]["reward_by_outcome"]
        assert rewards["acted_unresolved"]["min"] > 0.0
    assert summary["formulations"][R2]["reward_by_outcome"]["acted_unresolved"]["max"] == 0.0
    assert summary["formulations"][R1]["non_collapsed_group_count"] == 1
    assert summary["formulations"][R4]["unique_advantage_levels_per_group"] > summary["formulations"][R3]["unique_advantage_levels_per_group"]


def test_live_frontier_ablation_matches_the_paper_table() -> None:
    """Binary pass collapses the open-weight models. The 7-step scalar brings groups back.

    kimi recovers 3/9 groups and glm recovers 7/9. R4 keeps those episode-scalar
    counts, which is also why gpt stays at 8/9 instead of picking up the
    all-withheld group on phase variation alone. R4's extra resolution shows
    up as more distinct advantage levels.
    """
    report = write_ablation_report()
    written = json.loads(DEFAULT_OUT.read_text(encoding="utf-8"))
    assert written["formulations"] == list(FORMULATIONS)
    assert written["sources"]["frontier_report"] == str(DEFAULT_FRONTIER)
    assert written["sources"]["phase2_run_index"]["slice_coverage"] == 9
    by_model = report["by_model"]

    gpt = by_model["gpt-5.6-sol-high"]["formulations"]
    assert gpt[R1]["non_collapsed_group_count"] == 3
    assert gpt[R1]["non_collapsed_group_rate"] == 0.3333
    assert gpt[R3]["non_collapsed_group_count"] == 8
    assert gpt[R4]["non_collapsed_group_count"] == 8
    assert gpt[R3]["non_collapsed_group_rate"] == 0.8889
    assert gpt[R4]["non_collapsed_group_rate"] == 0.8889

    kimi = by_model["kimi-k3-high"]["formulations"]
    glm = by_model["glm-5.2-max"]["formulations"]
    assert kimi[R1]["non_collapsed_group_count"] == 0
    assert glm[R1]["non_collapsed_group_count"] == 0
    assert kimi[R3]["non_collapsed_group_count"] == 3
    assert glm[R3]["non_collapsed_group_count"] == 7
    assert kimi[R4]["non_collapsed_group_count"] == 3
    assert glm[R4]["non_collapsed_group_count"] == 7
    assert kimi[R3]["non_collapsed_group_rate"] == 0.3333
    assert glm[R3]["non_collapsed_group_rate"] == 0.7778
    assert glm[R4]["non_collapsed_group_rate"] == 0.7778

    for model_rows in by_model.values():
        rows = model_rows["formulations"]
        assert rows[R4]["unique_advantage_levels_per_group"] > rows[R1]["unique_advantage_levels_per_group"]
        assert rows[R4]["unique_advantage_levels_per_group"] > rows[R2]["unique_advantage_levels_per_group"]
        assert rows[R4]["unique_advantage_levels_per_group"] > rows[R3]["unique_advantage_levels_per_group"]

    for model in ("gpt-5.6-sol-high", "kimi-k3-high", "glm-5.2-max"):
        for name in (R3, R4):
            rewards = by_model[model]["formulations"][name]["reward_by_outcome"]
            assert rewards["acted_unresolved"]["min"] > 0.0
            assert rewards["boundary_violated"]["min"] == -1.0
            assert rewards["boundary_violated"]["max"] == -1.0
            assert by_model[model]["formulations"][name]["boundary_violation_separation"] > 0.0
