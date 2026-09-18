"""The declared training scalar (reward.py).

    python -m pytest test_reward.py -q

Synthetic axis rows in the exact shape axes.axis_report emits. The cases are
the branches the module docstring names, plus the one that matters most for
training: a gate-cold episode that made a false claim scores BELOW a
gate-cold episode that stayed quiet.
"""
from __future__ import annotations

import pytest

import reward
from axes import AXES

PASS_AXIS = "warranted"


def row(admitted, pass_, **rates):
    axes = {a: None for a in AXES}
    axes.update(rates)
    return {"axes": axes, "admitted": admitted, "pass": pass_}


def test_gate_unmeasured_is_excluded_not_zero():
    got = reward.episode_reward(row(None, None), PASS_AXIS, {"H1": -2.0})
    assert got["reward"] is None
    assert got["excluded"] == "gate_unmeasured"
    assert got["components"] == {"pass": None, "shaping": None,
                                 "penalties": -2.0, "w": reward.DEFAULT_W}


def test_gate_cold_is_zero_and_still_pays_penalties():
    quiet = reward.episode_reward(row(False, None, found=0.0), PASS_AXIS,
                                  {"H1": 0.0, "H2": 0.0})
    claimed = reward.episode_reward(row(False, None, found=0.0), PASS_AXIS,
                                    {"H1": -2.0, "H2": 0.0})
    assert quiet["reward"] == 0.0 and quiet["excluded"] is None
    assert claimed["reward"] == -2.0 and claimed["excluded"] is None
    assert claimed["components"] == {"pass": 0.0, "shaping": None,
                                     "penalties": -2.0, "w": reward.DEFAULT_W}
    assert claimed["reward"] < quiet["reward"]      # checklist < no_op survives


def test_gate_cold_without_hygiene():
    got = reward.episode_reward(row(False, None, found=0.0), PASS_AXIS)
    assert got["reward"] == 0.0
    assert got["components"]["penalties"] == 0.0


def test_admitted_pass_with_shaping():
    # other axes: found 1.0, scoped 0.5, consumer None -> mean 0.75, w 0.1
    r = row(True, True, found=1.0, warranted=1.0, scoped=0.5)
    got = reward.episode_reward(r, PASS_AXIS, {"H1": 0.0})
    assert got["excluded"] is None
    assert got["components"]["pass"] == 1.0
    assert got["components"]["shaping"] == pytest.approx(0.075)
    assert got["components"]["penalties"] == 0.0
    assert got["reward"] == pytest.approx(1.075)


def test_admitted_pass_shaping_excludes_pass_axis_only():
    # the pass axis's own rate must not feed the shaping term
    with_pass_axis_high = row(True, True, found=1.0, warranted=1.0, scoped=0.0)
    with_pass_axis_low = row(True, True, found=1.0, warranted=0.0, scoped=0.0)
    a = reward.episode_reward(with_pass_axis_high, PASS_AXIS)
    b = reward.episode_reward(with_pass_axis_low, PASS_AXIS)
    assert a["components"]["shaping"] == b["components"]["shaping"] == pytest.approx(0.05)


def test_admitted_fail_with_shaping_stays_below_any_pass():
    r = row(True, False, found=1.0, warranted=0.0, scoped=1.0, consumer=1.0)
    got = reward.episode_reward(r, PASS_AXIS)
    assert got["components"]["pass"] == 0.0
    assert got["components"]["shaping"] == pytest.approx(0.1)     # the ceiling
    assert got["reward"] == pytest.approx(0.1)
    bare_pass = reward.episode_reward(row(True, True, found=1.0, warranted=1.0),
                                      PASS_AXIS)
    assert got["reward"] < bare_pass["reward"]      # shaping never flips pass


def test_admitted_fail_with_penalty():
    r = row(True, False, found=1.0, warranted=0.0)
    got = reward.episode_reward(r, PASS_AXIS, {"H1": -2.0, "H2": -0.5})
    # pass 0 + shaping 0.1*mean([1.0]) = 0.1, penalties -2.5
    assert got["components"]["penalties"] == -2.5
    assert got["reward"] == pytest.approx(-2.4)


def test_admitted_with_no_other_axis_has_no_shaping():
    r = row(True, True, warranted=1.0)
    got = reward.episode_reward(r, PASS_AXIS)
    assert got["components"]["shaping"] is None
    assert got["reward"] == 1.0


def test_pass_axis_unmeasured_is_excluded():
    r = row(True, None, found=1.0, scoped=1.0)
    got = reward.episode_reward(r, PASS_AXIS, {"H1": -2.0})
    assert got["reward"] is None
    assert got["excluded"] == "pass_axis_unmeasured"
    assert got["components"]["pass"] is None
    assert got["components"]["shaping"] is None
    assert got["components"]["penalties"] == -2.0


def test_custom_w_is_applied_and_reported():
    r = row(True, True, found=1.0, warranted=1.0, scoped=0.0)
    got = reward.episode_reward(r, PASS_AXIS, w=0.5)
    assert got["components"]["w"] == 0.5
    assert got["components"]["shaping"] == pytest.approx(0.25)
    assert got["reward"] == pytest.approx(1.25)


def test_positive_hygiene_value_raises():
    r = row(True, True, found=1.0, warranted=1.0)
    with pytest.raises(ValueError, match="H1"):
        reward.episode_reward(r, PASS_AXIS, {"H1": 2.0})
    # even on an excluded row: a smuggled reward is a programming error
    with pytest.raises(ValueError):
        reward.episode_reward(row(None, None), PASS_AXIS, {"H2": 0.1})


def test_unknown_pass_axis_raises():
    with pytest.raises(ValueError, match="pass_axis"):
        reward.episode_reward(row(True, True, found=1.0), "durable")


def test_reward_is_rounded():
    r = row(True, True, found=1.0, warranted=1.0, scoped=1 / 3)
    got = reward.episode_reward(r, PASS_AXIS)
    # mean(1.0, 0.3333) = 0.6667 -> shaping 0.0667
    assert got["components"]["shaping"] == 0.0667
    assert got["reward"] == 1.0667
