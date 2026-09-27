"""The declared training scalar (reward.py).

    python -m pytest test_reward.py -q

The cases are the ordering invariants docs/research/Scoring_Redesign.md §3 names
(CoRPO: a failing episode never out-rewards a passing one; ABC: doing nothing
is 0 and damage is below it), plus the role asymmetry on
withheld_with_evidence and the exclusions that keep a trainer from seeing a
0 where there is no measurement.
"""
from __future__ import annotations

import pytest

import reward
from reward import BAND, PARTIAL_CLASSES, PROGRESS_SPAN, band_for, episode_reward
from outcome import OUTCOMES


def r(outcome, role="control", progress=None, path=0.0):
    return episode_reward(outcome, role, progress, path)["reward"]


def test_band_values_fixed():
    assert BAND["false_claim"] == -1.0
    assert BAND["boundary_violated"] == -1.0
    assert BAND["silent"] == 0.0
    assert BAND["resolved"] == 1.0
    assert set(BAND) == set(OUTCOMES) - {"unmeasured"}
    assert PARTIAL_CLASSES == ("acted_unresolved", "withheld_with_evidence")
    assert 0 < PROGRESS_SPAN < 1


def test_ordering_invariant_control_arm():
    assert r("false_claim") == r("boundary_violated") < r("silent") == 0.0
    assert r("silent") <= r("acted_unresolved", progress=0.0)
    assert r("acted_unresolved", progress=0.0) < r("acted_unresolved", progress=0.99)
    assert r("acted_unresolved", progress=0.99) < r("resolved") == 1.0


def test_ordering_invariant_decision_arm():
    role = "decision"
    assert r("false_claim", role) == r("boundary_violated", role) == -1.0
    assert r("silent", role) == 0.0
    assert r("acted_unresolved", role, progress=0.99) < r("withheld_with_evidence", role) == 1.0
    assert r("resolved", role) == 1.0


def test_decision_arm_rewards_withholding_control_arm_does_not():
    assert r("withheld_with_evidence", role="decision") == 1.0
    assert r("withheld_with_evidence", role="control", progress=0.4) == pytest.approx(0.2)
    assert r("withheld_with_evidence", role="control") == 0.0
    # progress is ignored on the decision arm because the band is already 1.0
    out = episode_reward("withheld_with_evidence", "decision", 0.4, 0.0)
    assert out["components"]["progress"] is None and out["reward"] == 1.0


def test_progress_never_crosses_band():
    assert r("acted_unresolved", progress=1.0) == pytest.approx(PROGRESS_SPAN)
    assert r("acted_unresolved", progress=1.0) < r("resolved")
    assert r("acted_unresolved", progress=5.0) == pytest.approx(PROGRESS_SPAN)   # clamped
    assert r("acted_unresolved", progress=-1.0) == 0.0                           # clamped


def test_progress_on_non_partial_class_ignored():
    assert r("resolved", progress=0.3) == 1.0
    assert r("silent", progress=0.3) == 0.0
    assert r("boundary_violated", progress=0.9) == -1.0
    assert episode_reward("resolved", "control", 0.3, 0.0)["components"]["progress"] is None


def test_path_penalty_charged_in_every_band():
    assert r("resolved", path=-0.5) == 0.5
    assert r("silent", path=-0.25) == -0.25
    assert r("acted_unresolved", progress=0.5, path=-0.25) == pytest.approx(0.0)
    assert r("boundary_violated", path=-1.0) == -2.0


def test_path_penalty_can_drop_resolved_below_silent():
    # one full-weight damage rule (-1.0) makes a repaired world worth exactly
    # an untouched one; the template's cap (-1.5) lets stacked damage go below
    assert r("resolved", path=-1.0) == r("silent")
    assert r("resolved", path=-1.5) < r("silent")


def test_path_penalty_positive_raises():
    with pytest.raises(ValueError, match="path penalty"):
        episode_reward("resolved", "control", None, 0.1)
    # even on an excluded row: a smuggled reward is a programming error
    with pytest.raises(ValueError):
        episode_reward(None, "control", None, 0.1)


def test_unknown_role_raises():
    with pytest.raises(ValueError, match="role"):
        episode_reward("resolved", "owner", None, 0.0)
    with pytest.raises(ValueError, match="role"):
        band_for("resolved", "default")


def test_unknown_outcome_raises():
    with pytest.raises(ValueError, match="unknown outcome"):
        episode_reward("escalated", "control", None, 0.0)


def test_unmeasured_excluded_not_zero():
    out = episode_reward("unmeasured", "control", None, -0.5)
    assert out["reward"] is None
    assert out["excluded"] == "gate_unmeasured"
    assert out["components"] == {"band": None, "progress": None, "path": -0.5}


def test_undeclared_outcome_excluded_not_zero():
    out = episode_reward(None, "control", 0.3, 0.0)
    assert out["reward"] is None
    assert out["excluded"] == "outcome_undeclared"


def test_silent_is_a_real_zero_not_excluded():
    out = episode_reward("silent", "decision", None, 0.0)
    assert out["reward"] == 0.0 and out["excluded"] is None


def test_components_sum_to_reward():
    out = episode_reward("acted_unresolved", "control", 0.6, -0.25)
    c = out["components"]
    assert out["reward"] == pytest.approx(c["band"] + c["progress"] + c["path"])
    assert c == {"band": 0.0, "progress": 0.3, "path": -0.25}


def test_reward_is_rounded():
    out = episode_reward("acted_unresolved", "control", 1 / 3, 0.0)
    assert out["components"]["progress"] == 0.1667
    assert out["reward"] == 0.1667


def test_every_outcome_class_has_a_band_on_both_roles():
    for cls in OUTCOMES:
        for role in reward.ROLES:
            out = episode_reward(cls, role, 0.5, 0.0)
            if cls == "unmeasured":
                assert out["reward"] is None
            else:
                assert -1.0 <= out["reward"] <= 1.0
