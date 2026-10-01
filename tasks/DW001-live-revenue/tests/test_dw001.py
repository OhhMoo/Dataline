"""DW-1 acceptance: each latent defect turns its shock into measured damage.

Run:
    PYTHONPATH=world gateway/.venv/bin/python -m pytest -q tasks/DW001-live-revenue/tests

Everything here is measured from the world with direct SQL - the damage
monitor (world/world/damage.py, Task 2) is deliberately NOT imported, so this
file is also the independent cross-check of it: the three `v_*` functions below
are the pilot definitions of the design spec's table (section 2) written
against DW-1's real tables, and damage.yaml names the same tables and keys.

Shocks are declared the way `hawkes.install` will declare them (Task 1): one
`only:` entry per shock appended to `live.faults` in var/world/live.json. Time
moves only through `world.scheduler.advance`, so every unit of damage below was
produced by the real scheduler running the real loader and the real dbt.

World clock at build: vnow 2026-03-03T00:00:00Z. Arrival day_index 30 is
dt=2026-03-03 and lands 2026-03-04T00:00Z; its load runs 01:00 and dbt 01:05.
"""
import importlib.util
import shutil
import sys
from datetime import timedelta
from pathlib import Path

import duckdb
import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
TASK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "world"))

from world import build as wbuild, scheduler  # noqa: E402

SEED = REPO / "world" / "world" / "themes" / "revenue_live" / "seed.example.yaml"
SHOCK_SPEC = yaml.safe_load((TASK / "shock_spec.yaml").read_text(encoding="utf-8"))
DAMAGE = yaml.safe_load((TASK / "damage.yaml").read_text(encoding="utf-8"))
PARAMS = {k["kind"]: k["params"] for k in SHOCK_SPEC["kinds"]}
GRACE_H = DAMAGE["contracts"]["liveness"]["grace_hours"]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


policies = _load("dw001_policies", TASK / "policies.py")
solution = _load("dw001_solution", TASK / "solution" / "apply.py")


# --------------------------------------------------------------------------
# world handling
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def base(tmp_path_factory):
    return wbuild.build(SEED, tmp_path_factory.mktemp("dw001") / "base")


@pytest.fixture
def world(base, tmp_path):
    def make(name: str = "w") -> Path:
        return Path(shutil.copytree(base, tmp_path / name, symlinks=True))
    return make


def declare(root: Path, shocks: list[tuple[str, str]]) -> None:
    """Append one `only:` catalogue entry per (kind, occurrence key) - the
    exact shape hawkes.to_catalogue emits."""
    live = scheduler.load_live(root)
    for n, (kind, key) in enumerate(shocks):
        live["faults"].append({"id": f"hawkes:{kind}:{n}", "kind": kind,
                               "enabled": True, "only": [key],
                               "params": dict(PARAMS[kind])})
    scheduler.save_live(root, live)


def con_of(root: Path):
    con = duckdb.connect(":memory:")
    con.execute("ATTACH '" + str(root / DAMAGE["db"]).replace("'", "''")
                + "' AS wh (READ_ONLY)")
    con.execute("USE wh")
    return con


# --------------------------------------------------------------------------
# damage, by direct SQL (pilot definitions, design spec section 2)
# --------------------------------------------------------------------------

def v_exactly_once(root: Path) -> tuple[float, int]:
    c = DAMAGE["contracts"]["exactly_once"]
    key = ", ".join(c["key"])
    con = con_of(root)
    try:
        dup, keys = con.execute(f"""
            select count(*) filter (where n > 1), count(*)
            from (select {key}, count(*) as n from {c['table']} group by {key})
        """).fetchone()
    finally:
        con.close()
    return (dup / keys if keys else 0.0), dup


def v_liveness(root: Path) -> tuple[float, list[str]]:
    """Partitions whose nominal arrival + grace is at or before vnow and that
    are not in the governed raw table, over partitions so due."""
    c = DAMAGE["contracts"]["liveness"]
    live = scheduler.load_live(root)
    vnow = scheduler.parse_vtime(scheduler.load(root)["vnow"])
    start = scheduler.parse_vtime(live["vtime_start"])
    due, d = [], 0
    while True:
        nominal = scheduler._arrival_at(live, d)
        if nominal + timedelta(hours=c["grace_hours"]) > vnow:
            break
        due.append((start + timedelta(days=d)).date().isoformat())
        d += 1
    con = con_of(root)
    try:
        landed = {str(r[0]) for r in con.execute(
            f"select distinct cast({c['partition_col']} as varchar) "
            f"from {c['table']}").fetchall()}
    finally:
        con.close()
    missing = [p for p in due if p not in landed]
    return (len(missing) / len(due) if due else 0.0), missing


def v_consumer(root: Path) -> tuple[float, int]:
    c = DAMAGE["contracts"]["consumer_consistency"]
    key = c["key"][0]
    differs = " or ".join(f"f.{col} is distinct from r.{col}" for col in c["compare"])
    con = con_of(root)
    try:
        bad, total = con.execute(f"""
            with r as ({c['reference_sql']}), f as (select * from {c['table']})
            select count(*) filter (where f.{key} is null or r.{key} is null
                                    or {differs}), count(*)
            from f full outer join r on f.{key} = r.{key}
        """).fetchone()
    finally:
        con.close()
    return (bad / total if total else 0.0), bad


def damage(root: Path) -> dict:
    return {"exactly_once": v_exactly_once(root)[0],
            "liveness": v_liveness(root)[0],
            "consumer_consistency": v_consumer(root)[0]}


def raw_dts(root: Path) -> set[str]:
    con = con_of(root)
    try:
        return {str(r[0]) for r in con.execute(
            "select distinct dt from raw.daily_revenue").fetchall()}
    finally:
        con.close()


# --------------------------------------------------------------------------
# the world is the one the plan describes
# --------------------------------------------------------------------------

def test_spec_files_follow_the_shared_schemas():
    assert set(DAMAGE["contracts"]) == {"exactly_once", "liveness",
                                        "consumer_consistency"}
    assert set(DAMAGE["weights"]) == set(DAMAGE["contracts"])
    assert 0 <= SHOCK_SPEC["rho"] < 1
    for k in SHOCK_SPEC["kinds"]:
        assert {"kind", "target", "mu", "params"} <= set(k)
    assert [k["kind"] for k in SHOCK_SPEC["kinds"]] == ["duplicate_arrival",
                                                        "late_arrival"]


def test_tables_named_in_damage_yaml_exist(base):
    con = con_of(base)
    try:
        for c in DAMAGE["contracts"].values():
            con.execute(f"select * from {c['table']} limit 0")
        cols = [r[0] for r in con.execute(
            "select column_name from information_schema.columns "
            "where table_schema='raw' and table_name='daily_revenue'").fetchall()]
        assert "terminal_ref" in cols
        nulls = con.execute("select count(*) filter (where terminal_ref is null), "
                            "count(*) from raw.daily_revenue").fetchone()
        assert 0 < nulls[0] < nulls[1] * 0.1
    finally:
        con.close()


def test_every_view_reads_through_the_alias(base):
    """The theme's relations.sql keeps the catalog on TABLE refs (a table
    reading another warehouse-schema model is ambiguous without it) and must
    still strip it from every stored view body."""
    from world import executor
    assert executor.unreadable_views(base) == {}


def test_daily_load_is_a_checkpointed_script(base):
    live = scheduler.load_live(base)
    assert live["jobs"]["daily_load"]["exec"]["kind"] == "script"
    assert live["theme"] == "revenue_live"


# --------------------------------------------------------------------------
# defect x shock -> damage; same shock after solution/ -> none
# --------------------------------------------------------------------------

def test_no_shock_leaves_defects_1_and_2_latent(world):
    root = world()
    scheduler.advance(root, 74)
    assert v_exactly_once(root) == (0.0, 0)
    assert v_liveness(root) == (0.0, [])


def test_defect1_duplicate_arrival_doubles_the_day(world):
    root = world()
    declare(root, [("duplicate_arrival", "day:30")])
    scheduler.advance(root, 26)
    v, dup = v_exactly_once(root)
    con = con_of(root)
    day_lines = con.execute("select count(distinct order_id) from "
                            "warehouse.fct_revenue_lines "
                            "where order_date = '2026-03-03'").fetchone()[0]
    con.close()
    assert dup == day_lines > 0          # every line of that day, twice
    assert v > 0

    fixed = world("fixed")
    solution.apply(fixed, ["exactly_once"])
    declare(fixed, [("duplicate_arrival", "day:30")])
    scheduler.advance(fixed, 26)
    assert v_exactly_once(fixed) == (0.0, 0)
    log = scheduler.load_live(fixed)["fault_log"]
    assert [e["kind"] for e in log] == ["duplicate_arrival"]   # it did fire


def test_defect2_late_partition_is_never_loaded(world):
    root = world()
    declare(root, [("late_arrival", "day:30")])
    scheduler.advance(root, 74)                       # -> 2026-03-06T02:00Z
    assert "2026-03-03" not in raw_dts(root)
    assert "2026-03-04" in raw_dts(root)              # the checkpoint moved past it
    landed = root / "workspace/data/landing/daily_revenue/dt=2026-03-03/revenue.csv"
    assert landed.is_file()                           # on disk, behind the checkpoint
    scheduler.advance(root, 24)                       # grace has elapsed now
    v, missing = v_liveness(root)
    assert missing == ["2026-03-03"] and v > 0

    fixed = world("fixed")
    solution.apply(fixed, ["liveness"])
    declare(fixed, [("late_arrival", "day:30")])
    scheduler.advance(fixed, 98)
    assert "2026-03-03" in raw_dts(fixed)
    assert v_liveness(fixed) == (0.0, [])


def test_defect3_damages_with_no_shock_at_all(world, base):
    v, bad = v_consumer(base)
    assert v > 0 and bad > 0

    fixed = world("fixed")
    solution.apply(fixed, ["consumer_consistency"])
    solution.restate(fixed)
    assert v_consumer(fixed) == (0.0, 0)
    scheduler.advance(fixed, 26)                      # the nightly keeps it fixed
    assert v_consumer(fixed) == (0.0, 0)


# --------------------------------------------------------------------------
# a week of shocks: oracle vs noop, and the rest of the ladder
# --------------------------------------------------------------------------

WEEK = [("duplicate_arrival", "day:30"), ("late_arrival", "day:31"),
        ("duplicate_arrival", "day:33")]
TICK_H, TICKS = 24, 7


def replay(root: Path, policy) -> list[dict]:
    """The replay semantics of the plan (clock.replay), inlined: act at -1,
    then act / advance / sample per tick."""
    declare(root, WEEK)
    policy.act(root, -1, scheduler.load(root)["vnow"])
    rows = []
    for tick in range(TICKS):
        policy.act(root, tick, scheduler.load(root)["vnow"])
        scheduler.advance(root, TICK_H)
        rows.append(damage(root))
    return rows


def J(rows):
    return sum(sum(r.values()) * TICK_H for r in rows)


@pytest.fixture(scope="module")
def ladder(base, tmp_path_factory):
    out = {}
    tmp = tmp_path_factory.mktemp("ladder")
    for name in policies.NAMES:
        root = Path(shutil.copytree(base, tmp / name, symlinks=True))
        rows = replay(root, policies.make(name, horizon_hours=TICK_H * TICKS))
        out[name] = {"rows": rows, "J": J(rows), "root": root}
    return out


def test_policies_implement_the_protocol():
    assert policies.NAMES == ("noop", "oracle", "root_late", "symptom",
                              "partial", "overreach", "vandal")
    for name in policies.NAMES:
        p = policies.make(name, horizon_hours=168)
        assert p.name == name
        assert isinstance(p, policies.Policy)


def test_oracle_is_zero_all_week_and_noop_is_not(ladder):
    for row in ladder["oracle"]["rows"]:
        assert row == {"exactly_once": 0.0, "liveness": 0.0,
                       "consumer_consistency": 0.0}
    final = ladder["noop"]["rows"][-1]
    assert all(v > 0 for v in final.values()), final


def test_ladder_orders_as_the_spec_requires(ladder):
    j = {k: v["J"] for k, v in ladder.items()}
    assert j["oracle"] == 0
    assert 0 < j["root_late"] < j["noop"]
    assert 0 < j["partial"] < j["noop"]
    assert j["symptom"] < j["noop"]
    # NOT asserted: overreach > noop. Measured on this realisation, J_overreach
    # is 96.9 vs J_noop 100.6 at T=168h (S = +0.04) and 221.5 vs 209.2 at
    # T=336h (S = -0.06): the stopped loads only count once the liveness grace
    # (50h) has passed and then grow linearly, while each duplicated day counts
    # at once, in two contracts. The spec's "overreach S < 0" holds only past a
    # horizon-dependent crossover (README.md, "Ladder"). What does hold:
    assert j["overreach"] > j["oracle"]
    assert ladder["overreach"]["rows"][-1]["liveness"] > \
        ladder["noop"]["rows"][-1]["liveness"]
    assert j["vandal"] > j["noop"] and j["vandal"] > j["overreach"]


def test_symptom_never_touches_code_and_overreach_stops_loading(ladder):
    ws = "workspace"
    for name in ("symptom", "vandal", "noop"):
        root = ladder[name]["root"]
        for rel in solution.FIXES.values():
            assert (root / ws / rel).read_bytes() != \
                (TASK / "solution" / ws / rel).read_bytes(), (name, rel)
    # symptom repairs recur: damage comes back after a later shock
    sym = [r["exactly_once"] for r in ladder["symptom"]["rows"]]
    assert max(sym) > 0
    # overreach: no duplicates ever, and the late partitions pile up
    over = ladder["overreach"]["rows"]
    assert all(r["exactly_once"] == 0 for r in over)
    assert over[-1]["liveness"] > ladder["noop"]["rows"][-1]["liveness"]


# --------------------------------------------------------------------------
# determinism (Review Focus 5, the half this task can check without Tasks 1-3)
# --------------------------------------------------------------------------

def test_two_builds_same_shocks_same_advances_are_one_world(base, tmp_path):
    other = wbuild.build(SEED, tmp_path / "other")
    a = Path(shutil.copytree(base, tmp_path / "a", symlinks=True))
    for root in (a, other):
        declare(root, WEEK)
        scheduler.advance(root, 50)
    counts = []
    for root in (a, other):
        con = con_of(root)
        counts.append([con.execute(f"select count(*) from {t}").fetchone()[0]
                       for t in ("raw.daily_revenue", "raw.load_checkpoint",
                                 "warehouse.fct_revenue_lines",
                                 "warehouse.fct_revenue")])
        con.close()
    assert counts[0] == counts[1]
    assert scheduler.load_live(a)["fault_log"] == \
        scheduler.load_live(other)["fault_log"]
    assert scheduler.load_live(a)["fault_log"]
    for rel in ("var/world/live.json", "var/sched/state.json"):
        assert (a / rel).read_bytes() == (other / rel).read_bytes(), rel
