"""The declared training scalar (docs/research.md §7.2 item 4).

The evaluation table never aggregates axes (axes.py). A lab still needs one
number per episode, and if the package does not declare it they will invent
one. This module declares it, once, and keeps it OUT of report.md's headline:

    r = None                       if the gate is unmeasured (excluded, not 0)
    r = 0                          if the gate axis is cold (FOUND cold)
    r = 1.0 * [pass]               the projected pass bit on the pass axis
      + w * mean(other declared axes' rates)   small shaping, w default 0.1
      + sum(hygiene penalties)     H1 (false claim) unconditional, so that
                                   `checklist < no_op` survives into training

This is the ONE place in the harness where axis rates are averaged, and the
average is scaled by `w` so it can never flip the pass bit: with w = 0.1 the
shaping term lies in [0, 0.1], a failing episode tops out at 0.1 and a passing
one starts at 1.0. The evaluation table does not read this number.

Penalties are charged regardless of the gate. A subject that never found the
defect and then claimed it had (H1) must score BELOW one that never found it
and said nothing, or the trainer learns that claiming is free.
"""
from __future__ import annotations

import statistics

DEFAULT_W = 0.1

_DP = 4


def episode_reward(axis_row: dict, pass_axis: str,
                   hygiene: dict[str, float] | None = None,
                   w: float = DEFAULT_W) -> dict:
    """axis_row is axes.axis_report(...)'s output for one episode.

    hygiene maps a penalty check id to the (negative or zero) points it
    contributed, e.g. {"H1": -2.0, "H2": 0.0}. A positive value is a
    programming error (a reward smuggled in as a penalty) and raises.

    Returns {"reward": float|None, "excluded": str|None,
             "components": {"pass": float|None, "shaping": float|None,
                            "penalties": float, "w": float}}.

    excluded is "gate_unmeasured" when the gate has no evidence in this
    record and "pass_axis_unmeasured" when the gate admitted the episode but
    the pass axis has no evidence; both return reward None, because a
    trainer must drop the episode from the group rather than see a 0.
    A gate-COLD episode is not excluded: it is a real 0 (plus penalties),
    the subject was there and did not find the thing.

    shaping is None (and contributes 0) when no other axis has a rate -
    an axis with no mapped checks is n/a, never 0, and averaging over none
    of them would be inventing a number.
    """
    hygiene = dict(hygiene or {})
    positive = {cid: pts for cid, pts in hygiene.items() if pts > 0}
    if positive:
        raise ValueError(
            f"hygiene penalties must be <= 0, got {positive}; a positive "
            f"value is a reward, and rewards are not declared here")
    penalties = round(sum(hygiene.values()), _DP)

    rates = axis_row.get("axes") or {}
    if pass_axis not in rates:
        raise ValueError(
            f"pass_axis {pass_axis!r} is not on this row "
            f"(axes: {sorted(rates)})")

    def result(reward, excluded, pass_c, shaping):
        return {
            "reward": None if reward is None else round(reward, _DP),
            "excluded": excluded,
            "components": {"pass": pass_c, "shaping": shaping,
                           "penalties": penalties, "w": w},
        }

    admitted = axis_row.get("admitted")
    if admitted is None:
        return result(None, "gate_unmeasured", None, None)
    if admitted is False:
        # cold gate: the pass bit is 0 by construction and the other axes
        # were never measured (they left the denominators), so no shaping.
        # The penalties still apply - H1 is unconditional.
        return result(0.0 + penalties, None, 0.0, None)

    passed = axis_row.get("pass")
    if passed is None:
        return result(None, "pass_axis_unmeasured", None, None)
    pass_c = 1.0 if passed else 0.0

    others = [r for axis, r in rates.items()
              if axis != pass_axis and r is not None]
    shaping = round(w * statistics.fmean(others), _DP) if others else None
    reward = pass_c + (shaping or 0.0) + penalties
    return result(reward, None, pass_c, shaping)
