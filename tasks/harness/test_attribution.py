"""Exact Shapley attribution of J_noop over the fault set (H4; plan Task 5).

The algebra is tested on monkeypatched value functions; one integration test
runs a real 2-player game (4 twins, 96 h, 24 h ticks) on DW-1.

    TMPDIR=$HOME/.cache/alkera-dyn-twins PYTHONPATH=world:gateway/src \
        gateway/.venv/bin/python -m pytest -q tasks/harness/test_attribution.py
"""
import itertools
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for p in (HERE, REPO / "world"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import attribution  # noqa: E402
import twins  # noqa: E402

TASK_DIR = REPO / "tasks" / "DW001-live-revenue"


# --------------------------------------------------------------------------
# Shapley algebra
# --------------------------------------------------------------------------

def test_additive_game_gives_each_player_its_own_value():
    own = {"a": 3.0, "b": 5.0, "c": 0.0}
    res = attribution.shapley(list(own), lambda S: sum(own[p] for p in S))
    assert res["phi"] == pytest.approx(own)
    assert res["v_empty"] == 0.0 and res["v_full"] == 8.0
    assert res["closure"] <= 1e-12


def test_pure_interaction_is_split_equally():
    # damage only when the defect AND its shock are both present
    v = lambda S: 12.0 if {"defect", "shock"} <= S else 0.0
    res = attribution.shapley(["defect", "shock", "dummy"], v)
    assert res["phi"] == pytest.approx({"defect": 6.0, "shock": 6.0, "dummy": 0.0})


def test_values_are_cached_by_subset():
    calls = []

    def v(S):
        calls.append(S)
        return float(len(S) ** 2)

    res = attribution.shapley(["a", "b", "c", "d"], v)
    assert len(calls) == 16 and len(set(calls)) == 16
    assert len(res["values"]) == 16
    assert res["phi"] == pytest.approx({p: 4.0 for p in "abcd"})


def test_gate_closes_and_flags_unattributed_damage():
    v = lambda S: 2.0 * len(S) + 1.5            # 1.5 of damage with no fault
    res = attribution.shapley(["a", "b"], v)
    g = attribution.gate(res)
    assert g["closure_ok"] is True               # efficiency holds by construction
    assert g["null_ok"] is False and g["passed"] is False
    assert g["unattributed"] == 1.5
    ok = attribution.gate(attribution.shapley(["a", "b"], lambda S: 2.0 * len(S)))
    assert ok["passed"] is True


def test_closure_detects_a_corrupted_phi():
    res = attribution.shapley(["a", "b"], lambda S: float(len(S)))
    res["phi"]["a"] += 1e-3
    assert attribution.gate(attribution.recheck(res))["closure_ok"] is False


# --------------------------------------------------------------------------
# coalitions -> worlds
# --------------------------------------------------------------------------

def _sched():
    s = lambda n, kind, sup=False: {"n": n, "id": f"hawkes:{kind}:{n}", "kind": kind,
                                    "key": f"day:{30 + n}", "suppressed": sup,
                                    "target": {"arrival": "daily_revenue"},
                                    "vts": "2026-03-04T00:00:00Z", "params": {}}
    return {"fault_seed": 7, "shocks": [s(0, "late_arrival"), s(1, "duplicate_arrival"),
                                        s(2, "late_arrival", True)]}


def test_players_are_defects_then_executed_shock_kinds():
    task = twins.load_task(TASK_DIR)
    assert attribution.players(task, _sched()) == [
        "defect:exactly_once", "defect:liveness", "defect:consumer_consistency",
        "shock:duplicate_arrival", "shock:late_arrival"]


def test_coalition_schedule_drops_disabled_kinds_only():
    out = attribution.coalition_schedule(_sched(), {"late_arrival"})
    assert [s["id"] for s in out["shocks"]] == ["hawkes:late_arrival:0",
                                                "hawkes:late_arrival:2"]
    # a suppressed shock stays suppressed: disabling the kind that won its
    # occurrence does not make it happen
    assert out["shocks"][1]["suppressed"] is True
    assert attribution.coalition_schedule(_sched(), set())["shocks"] == []


# --------------------------------------------------------------------------
# integration: a real 2-player game on DW-1
# --------------------------------------------------------------------------

def test_real_two_player_game(tmp_path):
    task = twins.load_task(TASK_DIR)
    params = {k["kind"]: k["params"] for k in task.shock_spec["kinds"]}
    sched = {"fault_seed": 7, "shocks": [
        {"n": 0, "id": "hawkes:duplicate_arrival:0", "kind": "duplicate_arrival",
         "key": "day:31", "target": {"arrival": "daily_revenue"},
         "vts": "2026-03-05T00:00:00Z", "parent": None, "generation": 0,
         "suppressed": False, "params": params["duplicate_arrival"]}]}
    ts = twins.post_tick_schedule("2026-03-03T00:00:00Z", 24, 72)
    res = attribution.attribute(
        TASK_DIR, sched, ts, tmp_path,
        players=["defect:exactly_once", "shock:duplicate_arrival"], jobs=4)
    v = res["values"]
    assert v[""] == 0.0                                    # nothing on: no damage
    assert v["defect:exactly_once"] == 0.0                 # a defect alone is inert
    assert v["shock:duplicate_arrival"] == 0.0             # a shock on fixed code too
    both = v["defect:exactly_once|shock:duplicate_arrival"]
    assert both > 0
    assert res["phi"] == pytest.approx({"defect:exactly_once": both / 2,
                                        "shock:duplicate_arrival": both / 2})
    assert res["gate"]["passed"] is True
    # cached: a second call runs no twin
    again = attribution.attribute(
        TASK_DIR, sched, ts, tmp_path,
        players=["defect:exactly_once", "shock:duplicate_arrival"],
        value_fn=lambda *a, **k: pytest.fail("cache miss"))
    assert again["phi"] == res["phi"]
