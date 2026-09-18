"""The learning-signal statistics (learning_signal.py), against hand-computed
values.

    python -m pytest test_learning_signal.py -q

Pure functions over plain numbers and synthetic records; no world, no
grader, no model. Every expected value below is worked by hand in a comment
so a regression is a disagreement with arithmetic, not with a snapshot.
"""
from __future__ import annotations

import math

import pytest

import learning_signal as ls


def rec(passed, scored=True, **extra):
    return {"scored": scored, "passed": passed,
            "classification": "pass" if passed else "reasoning", **extra}


# -- estimators ----------------------------------------------------------------

@pytest.mark.parametrize("n, c, k, expected", [
    (5, 2, 1, 0.4),            # pass@1 is the rate
    (5, 2, 2, 0.7),            # 1 - C(3,2)/C(5,2) = 1 - 3/10
    (5, 2, 3, 0.9),            # 1 - C(3,3)/C(5,3) = 1 - 1/10
    (5, 2, 4, 1.0),            # k > n - c: some pass is guaranteed
    (5, 5, 3, 1.0),            # c == n
    (5, 0, 3, 0.0),            # c == 0
    (5, 2, 6, None),           # k > n
    (0, 0, 1, None),           # empty run
    (5, 2, 0, None),           # k < 1 is not a sample
])
def test_pass_at_k_unbiased(n, c, k, expected):
    got = ls.pass_at_k_unbiased(n, c, k)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


@pytest.mark.parametrize("n, c, k, expected", [
    (5, 2, 1, 0.4),            # pass^1 is the rate too
    (5, 2, 2, 0.1),            # C(2,2)/C(5,2) = 1/10
    (5, 2, 3, 0.0),            # k > c: cannot all pass
    (5, 5, 3, 1.0),            # c == n
    (5, 0, 1, 0.0),            # c == 0
    (5, 2, 6, None),           # k > n
    (0, 0, 1, None),
])
def test_pass_hat_k_unbiased(n, c, k, expected):
    got = ls.pass_hat_k_unbiased(n, c, k)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


def test_pass_at_k_is_monotone_in_k_and_bounds_pass_hat_k():
    n, c = 8, 3
    at = [ls.pass_at_k_unbiased(n, c, k) for k in range(1, n + 1)]
    hat = [ls.pass_hat_k_unbiased(n, c, k) for k in range(1, n + 1)]
    assert at == sorted(at)                     # more samples, more chance
    assert hat == sorted(hat, reverse=True)     # more samples, harder to sweep
    assert all(h <= a for h, a in zip(hat, at))


def test_pass_at_k_table_keeps_unsupported_k_as_none():
    t = ls.pass_at_k_table(5, 2)
    assert set(t) == {"pass_at_k", "pass_hat_k"}
    assert t["pass_at_k"][2] == pytest.approx(0.7)
    assert t["pass_hat_k"][2] == pytest.approx(0.1)
    assert t["pass_at_k"][8] is None and t["pass_at_k"][10] is None
    assert set(t["pass_at_k"]) == set(ls.DEFAULT_KS)


def test_estimators_refuse_impossible_counts():
    with pytest.raises(ValueError):
        ls.pass_at_k_unbiased(5, 6, 1)
    with pytest.raises(ValueError):
        ls.pass_hat_k_unbiased(5, -1, 1)


# -- Wilson / pass_at_1 --------------------------------------------------------

@pytest.mark.parametrize("passed, n", [(0, 5), (5, 5), (2, 5), (1, 1), (50, 100)])
def test_wilson_contains_p_and_stays_in_unit_interval(passed, n):
    lo, hi = ls.wilson_interval(passed, n)
    p = passed / n
    assert 0.0 <= lo <= p <= hi <= 1.0
    assert hi > lo                      # never zero-width, even at 0/n and n/n


def test_wilson_none_for_empty():
    assert ls.wilson_interval(0, 0) is None


def test_wilson_known_value():
    # 2/5, z = 1.96: centre (0.4 + 3.8416/10) / (1 + 3.8416/5) = 0.7842/1.7683
    lo, hi = ls.wilson_interval(2, 5)
    assert lo == pytest.approx(0.1176, abs=1e-3)
    assert hi == pytest.approx(0.7693, abs=1e-3)


def test_pass_at_1_over_scored_only():
    records = [rec(True), rec(False), rec(True), rec(True, scored=False),
               rec(False, scored=False)]
    got = ls.pass_at_1(records)
    assert got["n"] == 3 and got["passed"] == 2
    assert got["rate"] == pytest.approx(2 / 3, abs=1e-4)
    # se = sqrt(p(1-p)/n) = sqrt((2/3)(1/3)/3)
    assert got["se"] == pytest.approx(math.sqrt((2 / 3) * (1 / 3) / 3), abs=1e-4)
    lo, hi = got["ci95"]
    assert lo < 2 / 3 < hi


def test_pass_at_1_empty():
    assert ls.pass_at_1([]) == {"rate": None, "n": 0, "passed": 0,
                                "se": None, "ci95": None}
    assert ls.pass_at_1([rec(True, scored=False)])["rate"] is None


# -- group_signal / advantage collapse -----------------------------------------

@pytest.mark.parametrize("rewards, mean, p1p, collapsed", [
    ([0.0, 0.0, 0.0], 0.0, 0.0, True),
    ([1.0, 1.0, 1.0], 1.0, 0.0, True),
    ([1.0, 0.0, 1.0, 0.0], 0.5, 0.25, False),
    ([1.0, 0.0, 0.0, 0.0, 0.0], 0.2, 0.16, False),
    ([1.0], 1.0, 0.0, True),                     # a lone reward has no spread
    ([0.0], 0.0, 0.0, True),
])
def test_group_signal_binary(rewards, mean, p1p, collapsed):
    got = ls.group_signal(rewards)
    assert got["n"] == len(rewards)
    assert got["mean"] == pytest.approx(mean)
    assert got["p_one_minus_p"] == pytest.approx(p1p)
    assert got["collapsed"] is collapsed
    # population variance equals p(1-p) for a binary reward
    assert got["variance"] == pytest.approx(p1p)


def test_group_signal_non_binary_has_no_p_one_minus_p():
    got = ls.group_signal([1.05, 0.1, 0.0, 1.0])
    assert got["p_one_minus_p"] is None
    assert got["collapsed"] is False
    assert got["mean"] == pytest.approx(2.15 / 4)
    assert got["variance"] > 0


def test_group_signal_non_binary_collapsed():
    got = ls.group_signal([0.1, 0.1, 0.1])
    assert got["collapsed"] is True
    assert got["variance"] == 0.0
    assert got["p_one_minus_p"] is None


def test_group_signal_empty():
    assert ls.group_signal([]) == {"n": 0, "mean": None, "variance": None,
                                   "p_one_minus_p": None, "collapsed": None}


def test_advantage_collapse_rate():
    groups = [[0.0, 0.0, 0.0], [1.0, 1.0], [1.0, 0.0], [0.0, 1.0, 1.0], [0.5]]
    # collapsed: 1st, 2nd, 5th -> 3 of 5
    assert ls.advantage_collapse_rate(groups) == pytest.approx(0.6)
    assert ls.advantage_collapse_rate([]) is None
    assert ls.advantage_collapse_rate([[]]) is None      # an empty group is no group
    assert ls.advantage_collapse_rate([[], [1.0, 0.0]]) == 0.0


# -- band ----------------------------------------------------------------------

@pytest.mark.parametrize("pass1, passk, k, expected", [
    (None, None, None, "unmeasured"),
    (None, 0.5, 5, "unmeasured"),           # pass1 None wins regardless
    (0.05, 0.3, 5, "core"),                 # CORE_LOW inclusive
    (0.5, 0.9, 5, "core"),
    (0.70, 1.0, 5, "core"),                 # CORE_HIGH inclusive
    (0.85, 1.0, 5, "core"),                 # (CORE_HIGH, CONTROL_HIGH] is still core
    (0.90, 1.0, 5, "core"),                 # CONTROL_HIGH inclusive
    (0.9001, 1.0, 5, "control"),
    (1.0, 1.0, 5, "control"),
    (0.04, 0.2, 5, "frontier"),             # below CORE_LOW, reachable at k
    (0.01, 0.01, 1, "frontier"),
    (0.04, 0.0, 5, "eval_only"),            # below CORE_LOW, never reached
    (0.0, 0.0, 8, "eval_only"),
    (0.04, None, None, "eval_only"),        # no pass@k evidence
    (0.04, 0.2, None, "eval_only"),         # a passk with no k is not evidence
])
def test_band(pass1, passk, k, expected):
    assert ls.band(pass1, passk, k) == expected
    assert expected in ls.BANDS


# -- clustered_mean ------------------------------------------------------------

def test_clustered_single_cluster_equals_wald():
    got = ls.clustered_mean([(3, 10)])
    assert got["clusters"] == 1 and got["n"] == 10
    assert got["mean"] == pytest.approx(0.3)
    wald = math.sqrt(0.3 * 0.7 / 10)
    assert got["se"] == pytest.approx(wald, abs=1e-4)
    assert got["se_naive"] == pytest.approx(wald, abs=1e-4)
    assert got["ci95"][0] == pytest.approx(0.3 - 1.96 * wald, abs=1e-4)
    assert got["ci95"][1] == pytest.approx(0.3 + 1.96 * wald, abs=1e-4)


def test_clustered_two_disagreeing_clusters_widen_se():
    # (10/10, 0/10): mean 0.5, N 20. Residual sums +5 and -5, so
    # se = sqrt(25 + 25) / 20 = 0.3536; naive sqrt(0.25/20) = 0.1118.
    got = ls.clustered_mean([(10, 10), (0, 10)])
    assert got["clusters"] == 2 and got["n"] == 20
    assert got["mean"] == pytest.approx(0.5)
    assert got["se"] == pytest.approx(math.sqrt(50) / 20, abs=1e-4)
    assert got["se_naive"] == pytest.approx(math.sqrt(0.25 / 20), abs=1e-4)
    assert got["se"] > got["se_naive"]


def test_clustered_agreeing_clusters_have_zero_between_variance():
    # every cluster at the pooled mean: nothing between clusters to add
    got = ls.clustered_mean([(5, 10), (5, 10), (5, 10)])
    assert got["se"] == 0.0
    assert got["mean"] == pytest.approx(0.5)


def test_clustered_empty_and_zero_size_clusters():
    assert ls.clustered_mean([]) == {"mean": None, "se": None, "se_naive": None,
                                     "ci95": None, "clusters": 0, "n": 0}
    got = ls.clustered_mean([(0, 0), (2, 4)])
    assert got["clusters"] == 1 and got["n"] == 4       # (0, 0) is not a cluster


# -- trials_to_separate --------------------------------------------------------

def test_trials_to_separate_known_value():
    # (1.96 + 0.84)^2 = 7.84; 0.3*0.7 + 0.5*0.5 = 0.46; 7.84*0.46/0.04 = 90.16
    assert ls.trials_to_separate(0.3, 0.5) == 91
    assert ls.trials_to_separate(0.5, 0.3) == 91          # symmetric


def test_trials_to_separate_edges():
    assert ls.trials_to_separate(0.4, 0.4) is None
    assert ls.trials_to_separate(0.0, 1.0) == 0           # no variance, no trials
    # a smaller gap needs more trials
    assert ls.trials_to_separate(0.45, 0.5) > ls.trials_to_separate(0.3, 0.5)
    with pytest.raises(ValueError):
        ls.trials_to_separate(1.2, 0.5)


# -- axis_separation -----------------------------------------------------------

def test_axis_separation_found_at_ceiling_pass_below():
    got = ls.axis_separation((5, 5), (1, 5))
    assert got["found_rate"] == 1.0 and got["pass_axis_rate"] == 0.2
    assert got["gap"] == pytest.approx(0.8)
    # se = sqrt(0 + 0.2*0.8/5) = 0.17889; lower = 0.8 - 1.96*0.17889 = 0.4494;
    # the Wald upper bound is 1.1506, which a difference of proportions cannot
    # reach, so it is clamped to 1.0 for printing. The verdict reads the
    # unclamped lower bound.
    lo, hi = got["gap_ci95"]
    assert lo == pytest.approx(0.4494, abs=1e-3)
    assert hi == 1.0
    assert got["separated"] is True


def test_axis_separation_no_gap():
    got = ls.axis_separation((5, 5), (5, 5))
    assert got["gap"] == 0.0
    assert got["gap_ci95"] == [0.0, 0.0]
    assert got["separated"] is False


def test_axis_separation_small_gap_not_separated():
    # 4/5 vs 3/5: gap 0.2, se = sqrt(0.16/5 + 0.24/5) = 0.283, CI crosses 0
    got = ls.axis_separation((4, 5), (3, 5))
    assert got["gap"] == pytest.approx(0.2)
    assert got["gap_ci95"][0] < 0
    assert got["separated"] is False


def test_axis_separation_negative_gap_not_separated():
    got = ls.axis_separation((1, 5), (5, 5))
    assert got["gap"] == pytest.approx(-0.8)
    assert got["separated"] is False


@pytest.mark.parametrize("found, pass_axis", [
    (None, (1, 5)), ((5, 5), None), (None, None), ((0, 0), (1, 5)), ((5, 5), (0, 0)),
])
def test_axis_separation_unmeasured(found, pass_axis):
    got = ls.axis_separation(found, pass_axis)
    assert got == {"found_rate": None, "pass_axis_rate": None, "gap": None,
                   "gap_ci95": None, "separated": None}


# -- summarise -----------------------------------------------------------------

def row(found, pass_, admitted=True, **others):
    axes = {"found": found, "warranted": None, "scoped": None, "consumer": None}
    axes.update(others)
    return {"axes": axes, "admitted": admitted, "pass": pass_}


def matrix_block(per_rollout, pass_axis="warranted"):
    return {"pass_axis": pass_axis, "gate": "found", "per_rollout": per_rollout}


REQUIRED = {"pass_at_1", "pass_at_k", "group", "band", "axis_separation",
            "thresholds"}


def test_summarise_with_matrix_block_excludes_unmeasured_episode():
    # five scored records; the matrix says: r1, r2 pass; r3, r4 admitted fail;
    # r5 gate-cold (pass None) -> reward group is 4 episodes, 2 passes
    records = [rec(True, rollout_id="r1"), rec(True, rollout_id="r2"),
               rec(False, rollout_id="r3"), rec(False, rollout_id="r4"),
               rec(False, rollout_id="r5"), rec(False, rollout_id="r6", scored=False)]
    block = matrix_block({
        "r1": row(1.0, True, warranted=1.0),
        "r2": row(1.0, True, warranted=1.0),
        "r3": row(1.0, False, warranted=0.0),
        "r4": row(1.0, False, warranted=0.5),
        "r5": row(0.0, None, admitted=False),
    })
    got = ls.summarise(records, block)
    assert REQUIRED <= set(got)
    assert got["reward_source"] == "projected_pass"
    assert got["excluded"] == 1
    assert got["pass_at_1"]["n"] == 4 and got["pass_at_1"]["passed"] == 2
    assert got["pass_at_1"]["rate"] == pytest.approx(0.5)
    assert got["group"]["n"] == 4
    assert got["group"]["p_one_minus_p"] == pytest.approx(0.25)
    assert got["group"]["collapsed"] is False
    # n=4, c=2: pass@2 = 1 - C(2,2)/C(4,2) = 1 - 1/6; pass@3 = 1 - 0 = 1.0
    assert got["pass_at_k"]["pass_at_k"][2] == pytest.approx(5 / 6)
    assert got["pass_at_k"]["pass_at_k"][3] == pytest.approx(1.0)
    assert got["pass_at_k"]["pass_at_k"][5] is None
    assert got["k_available"] == 3
    assert got["band"] == "core"
    # FOUND: 4 of the 5 episodes with a found rate are at 1.0; pass axis 2/4
    sep = got["axis_separation"]
    assert sep["found_rate"] == pytest.approx(0.8)
    assert sep["pass_axis_rate"] == pytest.approx(0.5)
    assert sep["gap"] == pytest.approx(0.3)
    assert sep["separated"] is False
    assert got["thresholds"] == {"core_low": 0.05, "core_high": 0.70,
                                 "control_high": 0.90}


def test_summarise_matrix_reward_differs_from_record_passed():
    # the record says passed; the projection says otherwise. The learning
    # signal follows the projection - that is the point of having one.
    records = [rec(True, rollout_id="r1"), rec(True, rollout_id="r2")]
    block = matrix_block({"r1": row(1.0, False, warranted=0.0),
                          "r2": row(1.0, False, warranted=0.0)})
    got = ls.summarise(records, block)
    assert got["pass_at_1"]["rate"] == 0.0
    assert got["group"]["collapsed"] is True
    assert got["band"] == "eval_only"
    assert got["axis_separation"]["separated"] is True     # 2/2 found, 0/2 pass


def test_summarise_without_matrix_block_uses_passed():
    records = [rec(True), rec(False), rec(True), rec(True, scored=False)]
    got = ls.summarise(records, None)
    assert REQUIRED <= set(got)
    assert got["reward_source"] == "passed"
    assert got["excluded"] == 0
    assert got["axis_separation"] is None
    assert got["pass_at_1"] == ls.pass_at_1(records)
    assert got["group"]["n"] == 3
    assert got["group"]["mean"] == pytest.approx(2 / 3, abs=1e-4)
    assert got["k_available"] == 3
    assert got["band"] == "core"


def test_summarise_all_excluded_is_unmeasured():
    records = [rec(False, rollout_id="r1")]
    block = matrix_block({"r1": row(None, None, admitted=None)})
    got = ls.summarise(records, block)
    assert got["excluded"] == 1
    assert got["pass_at_1"]["rate"] is None
    assert got["band"] == "unmeasured"
    assert got["k_available"] == 0
    assert got["group"]["collapsed"] is None
    assert got["axis_separation"]["separated"] is None


def test_summarise_empty():
    got = ls.summarise([], None)
    assert got["band"] == "unmeasured"
    assert got["pass_at_1"]["n"] == 0
    assert got["k_available"] == 0
