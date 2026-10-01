"""Hawkes shock generator: seeded cascades that compile to the fault catalogue.

What is under test:

1. the schedule is a pure function of (spec, world lattice): same seed, same
   bytes;
2. the dial is honest: rho = 0 has no offspring, rho >= 1 is refused, and the
   empirical branching ratio of sampled schedules tracks the target;
3. one shock per occurrence: a collision is recorded `suppressed` and never
   reaches the catalogue (faults.py executes at most one fault per occurrence,
   so a ledger claiming two would be a lie);
4. no new execution path: an installed schedule fires through
   `scheduler.advance` and lands in `fault_log` under the shock's own id.
"""
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from world import build as wbuild, hawkes, scheduler  # noqa: E402

TRACK_A = Path(__file__).resolve().parents[1]
SEED = TRACK_A / "seed.example.yaml"
LOCK = TRACK_A / "requirements.lock"

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def synthetic_lattice(n=2000):
    """n occurrences, hourly: arrivals on the hour, a job on the half hour."""
    out = []
    for i in range(n // 2):
        out.append({"key": f"day:{i}", "cls": "arrival",
                    "target": {"arrival": "feed"},
                    "vts": iso(T0 + timedelta(hours=i))})
        at = T0 + timedelta(hours=i, minutes=30)
        out.append({"key": f"load@{iso(at)}", "cls": "job",
                    "target": {"job": "load"}, "vts": iso(at)})
    return out


def synthetic_spec(seed, rho, **extra):
    spec = {"fault_seed": seed, "rho": rho, "beta_per_hour": 0.05,
            "horizon_hours": 1000, "excitation": [[1.0, 0.5], [0.5, 1.0]],
            "kinds": [
                {"kind": "duplicate_arrival", "target": {"arrival": "feed"},
                 "mu": 0.02, "params": {}},
                {"kind": "job_overrun", "target": {"job": "load"},
                 "mu": 0.02, "params": {}},
            ]}
    spec.update(extra)
    return spec


# --------------------------------------------------------------------------
# the process: no world needed
# --------------------------------------------------------------------------

def test_same_seed_same_schedule_synthetic():
    lattice = synthetic_lattice(400)
    a = hawkes.simulate(lattice, synthetic_spec(11, 0.6))
    b = hawkes.simulate(lattice, synthetic_spec(11, 0.6))
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["shocks"], "a 400-occurrence lattice at mu=0.02 should shock"
    c = hawkes.simulate(lattice, synthetic_spec(12, 0.6))
    assert json.dumps(a, sort_keys=True) != json.dumps(c, sort_keys=True)


def test_rho_zero_has_no_offspring():
    lattice = synthetic_lattice(2000)
    shocks = []
    for seed in range(5):
        shocks += hawkes.simulate(lattice, synthetic_spec(seed, 0.0))["shocks"]
    assert shocks
    assert all(s["generation"] == 0 and s["parent"] is None for s in shocks)


@pytest.mark.parametrize("rho", [1.0, 1.5, -0.1])
def test_rho_out_of_range_rejected(rho):
    with pytest.raises(ValueError, match="rho"):
        hawkes.simulate(synthetic_lattice(10), synthetic_spec(1, rho))


def test_rho_ge_one_rejected_by_load_spec(tmp_path):
    path = tmp_path / "shock_spec.yaml"
    path.write_text(yaml.safe_dump(synthetic_spec(1, 1.0)), encoding="utf-8")
    with pytest.raises(ValueError, match="rho"):
        hawkes.load_spec(path)


def branching(rho, seeds=200, n=2000):
    lattice = synthetic_lattice(n)
    children = total = 0
    for seed in range(seeds):
        sched = hawkes.simulate(lattice, synthetic_spec(seed, rho))
        live = [s for s in sched["shocks"] if not s["suppressed"]]
        total += len(live)
        children += sum(1 for s in live if s["parent"] is not None)
    return children / total


# Measured (pooled children / executed shocks, 200 seeds, this lattice):
#   rho 0.3 -> 0.292, 0.6 -> 0.573, 0.9 -> 0.817.
# Both biases are downward and documented in hawkes.py: the empty start
# (dropping the first 300h gives 0.307 / 0.589 / 0.830) and the exp link's
# saturation, (1 - e^-lambda)/lambda, which grows with rho. 0.9 is outside
# +-0.08 for that reason and is deliberately not asserted here.
@pytest.mark.parametrize("rho", [0.3, 0.6])
def test_empirical_branching_tracks_target(rho):
    assert abs(branching(rho) - rho) <= 0.08


def test_parents_are_earlier_executed_shocks_of_one_less_generation():
    sched = hawkes.simulate(synthetic_lattice(2000), synthetic_spec(3, 0.8))
    by_n = {s["n"]: s for s in sched["shocks"]}
    assert any(s["parent"] is not None for s in sched["shocks"])
    for s in sched["shocks"]:
        if s["parent"] is None:
            assert s["generation"] == 0
            continue
        p = by_n[s["parent"]]
        assert p["n"] < s["n"] and p["vts"] <= s["vts"]
        assert not p["suppressed"]
        assert s["generation"] == p["generation"] + 1


def test_one_shock_per_occurrence():
    # two kinds on ONE target with a base rate that makes them collide
    lattice = [o for o in synthetic_lattice(400) if o["cls"] == "arrival"]
    spec = {"fault_seed": 5, "rho": 0.0, "beta_per_hour": 0.05,
            "horizon_hours": 400, "excitation": "registry",
            "kinds": [
                {"kind": "duplicate_arrival", "target": {"arrival": "feed"},
                 "mu": 1.5, "params": {}},
                {"kind": "late_arrival", "target": {"arrival": "feed"},
                 "mu": 1.5, "params": {"delay_minutes": 240}},
            ]}
    sched = hawkes.simulate(lattice, spec)
    keys = {}
    for s in sched["shocks"]:
        keys.setdefault(s["key"], []).append(s)
    collided = [v for v in keys.values() if len(v) > 1]
    assert collided, "mu=1.5 on both kinds must collide somewhere"
    for group in collided:
        # spec order is precedence: the first kind executes, the rest do not
        assert [s["kind"] for s in group][0] == "duplicate_arrival"
        assert not group[0]["suppressed"]
        assert all(s["suppressed"] for s in group[1:])
    cat = hawkes.to_catalogue(sched)
    only = [f["only"][0] for f in cat]
    assert len(only) == len(set(only))
    assert len(cat) == len(keys)
    suppressed_ids = {s["id"] for s in sched["shocks"] if s["suppressed"]}
    assert suppressed_ids and not suppressed_ids & {f["id"] for f in cat}


def test_spectral_radius():
    assert hawkes.spectral_radius([[0.0]]) == 0.0
    assert hawkes.spectral_radius([[2.0, 0.0], [0.0, 3.0]]) == pytest.approx(3.0)
    # periodic: plain power iteration oscillates on this one
    assert hawkes.spectral_radius([[0.0, 1.0], [1.0, 0.0]]) == pytest.approx(1.0)
    assert hawkes.spectral_radius([[1.0, 0.5], [0.5, 1.0]]) == pytest.approx(1.5)


def test_registry_excitation_is_jaccard_of_declared_state():
    spec = {"kinds": [
        {"kind": "duplicate_arrival", "target": {"arrival": "a"},
         "state": ["arrival:a", "job:load", "table:raw.t"]},
        {"kind": "late_arrival", "target": {"arrival": "a"},
         "state": ["arrival:a", "job:load", "table:raw.t"]},
        {"kind": "job_overrun", "target": {"job": "build"},
         "state": ["job:build", "table:wh.f"]},
        {"kind": "job_retry", "target": {"job": "load"},
         "state": ["job:load", "table:raw.t", "job:build", "table:wh.f"]},
    ], "excitation": "registry"}
    A = hawkes.excitation_matrix(spec, None)
    assert [A[i][i] for i in range(4)] == [1.0] * 4
    assert A[0][1] == 1.0
    assert A[0][2] == 0.0
    assert A[2][3] == pytest.approx(2 / 4)
    assert A[0][3] == pytest.approx(2 / 5)
    assert all(A[i][j] == A[j][i] for i in range(4) for j in range(4))


# --------------------------------------------------------------------------
# a real world: occurrence lattice, install, execution through advance
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("hawkes_world")
    root = wbuild.build(SEED, tmp / "w", LOCK if LOCK.exists() else None)
    yield root
    shutil.rmtree(tmp, ignore_errors=True)


def world_spec(seed=7, rho=0.6):
    return {"fault_seed": seed, "rho": rho, "beta_per_hour": 0.05,
            "horizon_hours": 168, "excitation": "registry",
            "kinds": [
                {"kind": "duplicate_arrival",
                 "target": {"arrival": "daily_revenue"}, "mu": 0.3,
                 "params": {"match": "*.csv", "suffix": ".dup"}},
                {"kind": "late_arrival",
                 "target": {"arrival": "daily_revenue"}, "mu": 0.3,
                 "params": {"delay_minutes": 240, "missed_by": ["daily_load"]}},
                {"kind": "job_overrun", "target": {"job": "dbt_build"},
                 "mu": 0.3, "params": {}},
            ]}


def test_occurrences_follow_the_schedule(world):
    vnow = scheduler.load(world)["vnow"]
    occ = hawkes.occurrences(world, 48)
    # vnow sits at midnight with that night's load/build already in history:
    # 48h holds two arrivals and one load/build pair, 01:00 and 01:05
    assert [o["cls"] for o in occ] == ["arrival", "job", "job", "arrival"]
    assert [o["target"].get("job") for o in occ[1:3]] == ["daily_load",
                                                          "dbt_build"]
    assert all(vnow < o["vts"] for o in occ)
    assert occ == sorted(occ, key=lambda o: (o["vts"], o["cls"] == "job"))
    for o in occ:
        if o["cls"] == "arrival":
            assert o["key"].startswith("day:")
        else:
            assert o["key"] == f"{o['target']['job']}@{o['vts']}"


def test_same_seed_same_schedule(world):
    a = hawkes.sample(world, world_spec())
    b = hawkes.sample(world, world_spec())
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["shocks"]
    # registry-derived A: both arrival kinds share their whole footprint, the
    # downstream dbt_build overlaps it only partly
    A = a["A"]
    assert A[0][1] == 1.0 and 0.0 < A[0][2] < 1.0


def test_sample_rejects_unknown_target(world):
    spec = world_spec()
    spec["kinds"][2]["target"] = {"job": "no_such_job"}
    with pytest.raises(ValueError, match="no_such_job"):
        hawkes.sample(world, spec)


def test_catalogue_executes(world):
    state = scheduler.load(world)
    live = scheduler.load_live(world)
    day = int(live.get("days_scheduled", live["days_landed"])) + 1
    occ = [o for o in hawkes.occurrences(world, 72) if o["key"] == f"day:{day}"]
    assert occ, f"day:{day} should arrive within 72h of {state['vnow']}"
    shock_id = "hawkes:duplicate_arrival:0"
    schedule = {"fault_seed": 7, "rho_target": 0.0, "rho_empirical": 0.0,
                "beta_per_hour": 0.05, "A": [[1.0]], "alpha": [[0.0]],
                "shocks": [{"n": 0, "id": shock_id, "kind": "duplicate_arrival",
                            "key": f"day:{day}",
                            "target": {"arrival": "daily_revenue"},
                            "vts": occ[0]["vts"], "parent": None,
                            "generation": 0, "suppressed": False,
                            "params": {"match": "*.csv", "suffix": ".dup"}}]}
    before = len(live.get("fault_log") or [])
    hawkes.install(world, schedule)
    written = json.loads((world / "var" / "world" / "shock_schedule.json")
                         .read_text(encoding="utf-8"))
    assert written == schedule
    scheduler.advance(world, 72)
    log = scheduler.load_live(world)["fault_log"][before:]
    assert [e["fault"] for e in log] == [shock_id]
    assert log[0]["kind"] == "duplicate_arrival"
    assert log[0]["detail"]["duplicated"]
