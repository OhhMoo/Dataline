"""Twin runner, score S, per-fault timeline (plan Task 5).

Pure tests use synthetic curves; the integration tests build one real DW-1
world (module-scoped) and replay a short, hand-written shock schedule over a
96 h horizon with 24 h ticks, so the file stays well under a minute or two.

    TMPDIR=$HOME/.cache/alkera-dyn-twins PYTHONPATH=world:gateway/src \
        gateway/.venv/bin/python -m pytest -q tasks/harness/test_twins.py
"""
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for p in (HERE, REPO / "world"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import twins  # noqa: E402
import dyn_report  # noqa: E402
from world import damage  # noqa: E402

TASK_DIR = REPO / "tasks" / "DW001-live-revenue"


# --------------------------------------------------------------------------
# score
# --------------------------------------------------------------------------

def test_score_ladder():
    assert twins.score(0.0, 10.0, 0.0) == 1.0          # matches the oracle
    assert twins.score(10.0, 10.0, 0.0) == 0.0         # did nothing
    assert twins.score(5.0, 10.0, 0.0) == 0.5
    assert twins.score(15.0, 10.0, 0.0) == -0.5        # made it worse
    assert twins.score(4.0, 10.0, 2.0) == 0.75


def test_score_degenerate_is_none_not_division():
    # J_noop == J_oracle: the world gives the oracle nothing to beat the
    # no-op by, so S is undefined (every agent is "both 0 and 1").
    assert twins.score(3.0, 7.0, 7.0) is None
    assert twins.score(0.0, 0.0, 0.0) is None
    assert twins.score(1.0, 1e9, 1e9 * (1 + 1e-15)) is None
    assert "J_noop == J_oracle" in twins.DEGENERATE_S


# --------------------------------------------------------------------------
# objective J
# --------------------------------------------------------------------------

def _ticks(n, h=24, start_day=3, last_h=None):
    out, t = [], 0
    for i in range(n):
        step = last_h if (last_h is not None and i == n - 1) else h
        b = f"2026-03-{start_day:02d}T00:00:00Z" if i == 0 else out[-1]["vnow_after"]
        from datetime import timedelta
        from world.scheduler import iso, parse_vtime
        a = iso(parse_vtime(b) + timedelta(hours=step))
        out.append({"tick": i, "phase": "post", "after_command": None,
                    "vnow_before": b, "vnow_after": a})
    return {"m": 1, "h_hours": h, "T_hours": sum(
        (last_h if (last_h is not None and i == n - 1) else h) for i in range(n)),
        "vnow_start": out[0]["vnow_before"], "ticks": out}


def _rows(ts, curves):
    """curves: {contract: [V per tick]} -> damage rows."""
    n = len(ts["ticks"])
    return [{"tick": i, "phase": ts["ticks"][i]["phase"],
             "vnow": ts["ticks"][i]["vnow_after"],
             "V": {c: v[i] for c, v in curves.items()}} for i in range(n)]


def test_objective_uniform_equals_damage_integral():
    ts = _ticks(4, h=6)
    rows = _rows(ts, {"exactly_once": [0.5] * 4, "liveness": [0.0, 0.1, 0.2, 0.3]})
    w = {"exactly_once": 1.0, "liveness": 2.0}
    assert twins.objective(rows, w, ts) == pytest.approx(
        damage.integral(rows, w, 6.0), abs=1e-12)
    assert twins.objective(_rows(ts, {"exactly_once": [0.5] * 4}), {}, ts) \
        == pytest.approx(0.5 * 24 + 0.5)


def test_objective_weights_each_tick_by_its_duration():
    ts = _ticks(3, h=24, last_h=6)                      # last tick shortened
    rows = _rows(ts, {"liveness": [1.0, 1.0, 1.0]})
    assert twins.objective(rows, {}, ts) == pytest.approx(24 + 24 + 6 + 1.0)
    assert twins.objective(rows, {}, ts, gamma=0.0) == pytest.approx(54.0)


def test_objective_refuses_a_missing_tick():
    ts = _ticks(3)
    rows = _rows(ts, {"liveness": [0, 0, 0]})[:2]
    with pytest.raises(ValueError, match="tick 2"):
        twins.objective(rows, {}, ts)


def test_post_tick_schedule_matches_clock_shape():
    ts = twins.post_tick_schedule("2026-03-03T00:00:00Z", 24, 60)
    assert [t["vnow_after"] for t in ts["ticks"]] == [
        "2026-03-04T00:00:00Z", "2026-03-05T00:00:00Z", "2026-03-05T12:00:00Z"]
    assert all(t["phase"] == "post" and t["after_command"] is None for t in ts["ticks"])
    assert ts["vnow_start"] == "2026-03-03T00:00:00Z"


# --------------------------------------------------------------------------
# per-fault timeline (synthetic curves)
# --------------------------------------------------------------------------

DEFECTS = {"exactly_once": "exactly_once", "consumer_consistency": "consumer_consistency"}
OWNERS = {"duplicate_arrival": "exactly_once", "late_arrival": "liveness"}


def _shock(n, kind, vts, suppressed=False):
    return {"n": n, "id": f"hawkes:{kind}:{n}", "kind": kind, "key": f"day:{n}",
            "target": {"arrival": "daily_revenue"}, "vts": vts, "parent": None,
            "generation": 0, "suppressed": suppressed, "params": {}}


@pytest.fixture
def synthetic():
    ts = _ticks(7)          # tick i ends 2026-03-(04+i)
    sched = {"shocks": [
        _shock(0, "duplicate_arrival", "2026-03-05T00:00:00Z"),    # eligible from tick 1
        _shock(1, "duplicate_arrival", "2026-03-08T00:00:00Z"),    # eligible from tick 4
        _shock(2, "late_arrival", "2026-03-05T00:00:00Z"),         # inert: liveness stays 0
        _shock(3, "late_arrival", "2026-03-08T00:00:00Z", suppressed=True)]}
    noop = _rows(ts, {"exactly_once": [0, 0, .1, .1, .1, .2, .2],
                      "liveness": [0] * 7,
                      "consumer_consistency": [.5] * 7})
    return ts, sched, noop


def _by(tl):
    return {e["fault"]: e for e in tl}


def test_timeline_patched_and_missed(synthetic):
    ts, sched, noop = synthetic
    agent = _rows(ts, {"exactly_once": [0, 0, .1, 0, 0, .2, .2],
                       "liveness": [0] * 7,
                       "consumer_consistency": [.5] * 7})
    tl = _by(twins.timeline(sched, ts, noop, agent, DEFECTS, OWNERS))
    s0 = tl["hawkes:duplicate_arrival:0"]
    assert (s0["onset_tick"], s0["first_zero_tick"], s0["status"]) == (2, 3, "patched")
    assert s0["mitigated_tick"] is None and s0["time_to_mitigate_hours"] is None
    s1 = tl["hawkes:duplicate_arrival:1"]
    assert (s1["onset_tick"], s1["status"]) == (5, "missed")
    assert tl["defect:exactly_once"]["onset_tick"] == 2
    assert tl["defect:exactly_once"]["status"] == "patched"
    assert tl["defect:consumer_consistency"]["onset_tick"] == 0
    assert tl["defect:consumer_consistency"]["status"] == "missed"
    assert tl["hawkes:late_arrival:2"]["status"] == "inert"
    assert tl["hawkes:late_arrival:2"]["onset_tick"] is None
    assert "hawkes:late_arrival:3" not in tl          # suppressed: never happened


def test_timeline_fixed_untested_and_continuous(synthetic):
    ts, sched, noop = synthetic
    agent = _rows(ts, {"exactly_once": [0, 0, .1, 0, 0, 0, 0],
                       "liveness": [0] * 7,
                       "consumer_consistency": [.5, .5, 0, 0, 0, 0, 0]})
    tl = _by(twins.timeline(sched, ts, noop, agent, DEFECTS, OWNERS))
    s0 = tl["hawkes:duplicate_arrival:0"]
    # back to 0 at tick 3 and held through a later duplicate: a fix
    assert (s0["first_zero_tick"], s0["mitigated_tick"], s0["status"]) == (3, 3, "fixed")
    assert s0["time_to_mitigate_hours"] == 24.0
    s1 = tl["hawkes:duplicate_arrival:1"]
    # never damaged the agent world, but no later shock tested the fix
    assert (s1["mitigated_tick"], s1["status"], s1["time_to_mitigate_hours"]) \
        == (5, "mitigated", 0.0)
    cc = tl["defect:consumer_consistency"]
    # a shock-free defect is exposed on every tick: holding 0 is a fix
    assert (cc["mitigated_tick"], cc["status"], cc["time_to_mitigate_hours"]) \
        == (2, "fixed", 48.0)


def test_onset_is_a_rise_over_the_pre_shock_level(synthetic):
    ts, sched, noop = synthetic
    # the second duplicate's tick-4 value equals the standing 0.1: not its onset
    tl = _by(twins.timeline(sched, ts, noop, noop, DEFECTS, OWNERS))
    assert tl["hawkes:duplicate_arrival:1"]["onset_tick"] == 5
    assert tl["hawkes:duplicate_arrival:1"]["baseline"] == pytest.approx(0.1)


def test_counterfactual_onsets_override_the_heuristic(synthetic):
    ts, sched, noop = synthetic
    tl = _by(twins.timeline(sched, ts, noop, noop, DEFECTS, OWNERS,
                            onsets={"hawkes:duplicate_arrival:1": None,
                                    "hawkes:late_arrival:2": 3}))
    s1 = tl["hawkes:duplicate_arrival:1"]
    assert (s1["onset_tick"], s1["status"], s1["onset_method"]) \
        == (None, "not_pivotal", "counterfactual")
    assert tl["hawkes:late_arrival:2"]["onset_tick"] == 3
    assert tl["hawkes:duplicate_arrival:0"]["onset_method"] == "heuristic"


def test_time_to_detect_from_audit_rows(synthetic, tmp_path):
    ts, sched, noop = synthetic
    tl = twins.timeline(sched, ts, noop, noop, DEFECTS, OWNERS)
    actions = tmp_path / "actions.jsonl"
    rows = [
        # before onset (tick 2 ends 03-06): does not count even on the footprint
        {"seq": 1, "vts": "2026-03-05T12:00:00Z", "tool": "duckdb.query",
         "targets": ["warehouse.fct_revenue_lines"], "read_targets": []},
        {"seq": 2, "vts": "2026-03-06T06:00:00Z", "tool": "duckdb.query",
         "targets": ["raw.daily_revenue"], "read_targets": ["raw.daily_revenue"]},
        {"seq": 3, "vts": "2026-03-06T09:00:00Z", "tool": "duckdb.query",
         "targets": [], "read_targets": ["WAREHOUSE.FCT_REVENUE_LINES"]},
        {"seq": 4, "vts": "2026-03-07T00:00:00Z", "tool": "fs.write",
         "targets": ["workspace/dbt_project/models/staging/stg_revenue.sql"]},
    ]
    actions.write_text("".join(json.dumps(r) + "\n" for r in rows))
    footprints = {"duplicate_arrival": ["warehouse.fct_revenue_lines"],
                  "defect:exactly_once": ["warehouse.fct_revenue_lines"],
                  "defect:consumer_consistency": ["models/staging/stg_revenue.sql"]}
    out = _by(twins.time_to_detect(tl, actions, footprints))
    s0 = out["hawkes:duplicate_arrival:0"]
    assert (s0["detect_seq"], s0["time_to_detect_hours"]) == (3, 9.0)
    assert out["defect:exactly_once"]["detect_seq"] == 3
    # onset at tick 0 (ends 03-04); the stg file is first touched by seq 4
    assert out["defect:consumer_consistency"]["detect_seq"] == 4
    assert out["defect:consumer_consistency"]["time_to_detect_hours"] == 72.0
    # onset at tick 5 (03-09): nothing after it touches the footprint
    assert out["hawkes:duplicate_arrival:1"]["detect_seq"] is None
    # no footprint, or no onset: not measured
    assert out["hawkes:late_arrival:2"]["detect_seq"] is None


# --------------------------------------------------------------------------
# report section
# --------------------------------------------------------------------------

def test_render_damage_section(synthetic):
    ts, sched, noop = synthetic
    agent = _rows(ts, {"exactly_once": [0, 0, .1, 0, 0, 0, 0],
                       "liveness": [0] * 7, "consumer_consistency": [.5] * 7})
    oracle = _rows(ts, {"exactly_once": [0] * 7, "liveness": [0] * 7,
                        "consumer_consistency": [0] * 7})
    w = {}
    doc = {"J_agent": twins.objective(agent, w, ts), "J_noop": twins.objective(noop, w, ts),
           "J_oracle": 0.0, "weights": w, "tick_schedule": ts,
           "curves": {k: twins.curve(r, w) for k, r in
                      (("agent", agent), ("noop", noop), ("oracle", oracle))},
           "timeline": twins.timeline(sched, ts, noop, agent, DEFECTS, OWNERS)}
    doc["S"] = twins.score(doc["J_agent"], doc["J_noop"], doc["J_oracle"])
    md = dyn_report.render(doc)
    assert md.startswith("## Damage curves")
    assert f"S = {doc['S']:.3f}" in md
    assert md.count("\n| ") >= 7 + 2          # header rows + one per tick
    assert "hawkes:duplicate_arrival:0" in md and "fixed" in md
    assert dyn_report.sparkline([0, 0.5, 1.0]) == "▁▅█"
    degenerate = dict(doc, S=None)
    assert "undefined" in dyn_report.render(degenerate)


# --------------------------------------------------------------------------
# integration: real DW-1 twins on a short horizon
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def dw1(tmp_path_factory):
    work = tmp_path_factory.mktemp("twins")
    task = twins.load_task(TASK_DIR)
    base = twins.base_world(task.seed, work)
    params = {k["kind"]: k["params"] for k in task.shock_spec["kinds"]}
    sched = {"fault_seed": 7, "rho_target": 0.0, "rho_empirical": None,
             "beta_per_hour": 0.05, "A": [], "alpha": [], "shocks": [
                 dict(_shock(0, "late_arrival", "2026-03-04T00:00:00Z"),
                      key="day:30", params=params["late_arrival"]),
                 dict(_shock(1, "duplicate_arrival", "2026-03-05T00:00:00Z"),
                      key="day:31", params=params["duplicate_arrival"])]}
    from world import scheduler
    ts = twins.post_tick_schedule(scheduler.load(base)["vnow"], 24, 96)
    return task, work, sched, ts


def test_task_derives_defects_and_owners(dw1):
    task = dw1[0]
    assert task.defects == {"exactly_once": "exactly_once", "liveness": "liveness",
                            "consumer_consistency": "consumer_consistency"}
    assert task.shock_owner == {"duplicate_arrival": "exactly_once",
                                "late_arrival": "liveness"}


def test_episode_scores_end_to_end(dw1):
    task, work, sched, ts = dw1
    ep = work / "episode"
    agent = twins.scripted_episode(task, sched, ts, "symptom", ep, work)
    for f in ("shock_schedule.json", "tick_schedule.json", "damage.jsonl"):
        assert (ep / f).is_file()
    out = twins.episode_scores(ep, TASK_DIR, work / "scores", exact_onset=True, jobs=2)
    on_disk = json.loads((ep / "twins.json").read_text())
    assert on_disk["S"] == out["S"]
    assert out["J_agent"] == pytest.approx(agent["J"], abs=0)
    assert out["J_oracle"] == 0.0                   # oracle: no damage at all
    assert out["J_noop"] > out["J_agent"] > out["J_oracle"]
    assert 0.0 < out["S"] < 1.0
    for k in ("agent", "noop", "oracle"):
        assert [r["tick"] for r in out["curves"][k]] == [0, 1, 2, 3]
    tl = _by(out["timeline"])
    # the late day:30 partition is skipped by the checkpointed loader: liveness
    # damage appears once it is due (+50 h grace) in the no-op twin
    assert tl["hawkes:late_arrival:0"]["onset_tick"] == 3
    assert tl["hawkes:late_arrival:0"]["onset_method"] == "counterfactual"
    assert tl["hawkes:duplicate_arrival:1"]["onset_tick"] is not None
    assert tl["defect:consumer_consistency"]["onset_tick"] == 0
    md = dyn_report.render(out)
    assert "## Damage curves" in md


def test_twin_is_deterministic(dw1):
    task, work, sched, ts = dw1
    a = twins.run_twin(task.seed, sched, ts, task.policies.make("noop"),
                       task.damage_spec, work, name="det-a")
    b = twins.run_twin(task.seed, sched, ts, task.policies.make("noop"),
                       task.damage_spec, work, name="det-b")
    assert a["rows"] == b["rows"] and a["J"] == b["J"] > 0
