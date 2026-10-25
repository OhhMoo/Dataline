"""receipt_engine + replay_isolation: synthetic duckdb worlds, then real GT009.

The synthetic half builds a world the real `world.scheduler` can advance (a
state.json, a live.json with one `script` job that publishes, a warehouse) and
a grading zone with an oracle and a rival trajectory, so every verdict the
engine can give - oracle, rival, tie, named difference, gated - is produced by
the real clock and the real ledger, in well under a second per test.

The real half reads recorded GT009 rollouts (skipped when the recordings are
not on this host) and requires the engine, driven only by a spec built from
GT009's `fixture_contract.CONTRACT`, to give the verdicts the rollout's own
grade recorded. `gt009_spec` is that builder; it is GT009 knowledge, so it
lives here and not in the engine.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import sys
import textwrap
from pathlib import Path

import duckdb
import pytest

REPO = Path(__file__).resolve().parents[4]
if str(REPO) not in sys.path:
    sys.path.append(str(REPO))

from tasks.factory.grader import kind_receipts as engine           # noqa: E402
from tasks.factory.grader import kind_replay as iso                # noqa: E402

GT009 = REPO / "tasks" / "GT009-settlement-cutoff"
ROLLOUTS = Path("/home/ubuntu/alkera_rollouts/GT009-settlement-cutoff/"
                "default-20261017-k5-opus55h")
DESAT = Path("/home/ubuntu/desaturation_experiment_20261017")
E2_R1 = DESAT / "runs/GT009-settlement-cutoff/e2-noisy-surface-20261017-k5-opus55h/r1"


# ===========================================================================
# the GT009 spec, built from its CONTRACT
# ===========================================================================
_FIXTURE_MODULES = ("fixture_contract", "make_fixture_world", "schedule_model")


def _load_contract(fixtures: Path):
    """(CONTRACT, make_fixture_world) from one fixtures tree, without leaving
    its modules bound for the next tree (base and overlay share names)."""
    saved = {m: sys.modules.pop(m) for m in _FIXTURE_MODULES if m in sys.modules}
    sys.path.insert(0, str(fixtures))
    try:
        fc = importlib.import_module("fixture_contract")
        return fc.CONTRACT, sys.modules["make_fixture_world"]
    finally:
        sys.path.remove(str(fixtures))
        for m in _FIXTURE_MODULES:
            sys.modules.pop(m, None)
        sys.modules.update(saved)


def gt009_fixtures(task_dir: Path, variant: str | None) -> Path:
    """The fixtures tree an arm grades with (base, or the staged overlay)."""
    if not variant or variant == "default":
        return Path(task_dir) / "fixtures"
    sys.path.insert(0, str(REPO / "tasks" / "harness"))
    try:
        import taskspec                                            # noqa: PLC0415
        return Path(taskspec.fixtures_dir(Path(task_dir), variant))
    finally:
        sys.path.remove(str(REPO / "tasks" / "harness"))


def gt009_spec(fixtures: Path) -> dict:
    contract, mfw = _load_contract(Path(fixtures))
    tg, lay = contract["targets"], contract["layout"]
    book = tg.get("book")
    tables = [tg["handoff"], tg["window"], tg["facts"], tg["corrections"],
              tg["checkpoint"]] + ([book] if book else [])
    fixtures = Path(fixtures).resolve()
    try:
        module_dir = fixtures.relative_to(REPO).as_posix()
    except ValueError:
        module_dir = str(fixtures)
    return {
        "task": contract["task"],
        "targets": {"handoff": tg["handoff"], "window": tg["window"]},
        "consumer_targets": [book] if book else [],
        "nights": int(contract["plant_nights"]),
        "virtual_now": contract["virtual_now"],
        "hours_per_night": float(contract["advance_hours_per_night"]),
        "plant_dates": list(contract["plant_dates"]),
        "schedule_relpath": lay["schedule_relpath"],
        "layout": {"warehouse_relpath": lay["warehouse_relpath"],
                   "scheduler_relpath": lay["scheduler_relpath"],
                   "live_relpath": lay["live_relpath"]},
        "ddl": {t: " ".join(mfw.DDL[t].split()) for t in tables if t in mfw.DDL},
        "trajectories": {"dir": "expected", "names": list(contract["variants"]),
                         "pays": "oracle", "file": "variant_{name}.json",
                         "nights_key": "nights", "night_field": "night",
                         "t0_file": "t0.json", "t0_blocks": ["t0", "oracle_t0"],
                         "aliases": {"fct": tg["facts"]},
                         "hash_tables": [book] if book else []},
        "t0_tables": [
            {"table": tg["facts"], "reference": "oracle_t0", "mode": "exact",
             "grain_key": ["payment_id"]},
            {"table": tg["handoff"], "reference": "oracle_t0", "mode": "exact",
             "grain_key": ["settlement_day", "merchant_id"],
             "groups": {"column": "settlement_day",
                        "values": list(contract["gap_days"]),
                        "field": "gap_days_handed_over"}},
            {"table": tg["window"], "reference": "t0", "mode": "superset",
             "grain_key": ["closed_on", "settlement_day"]},
        ],
        "replay": {
            "plant": [{"from": "next_nights/payments_feed/dt={date}",
                       "to": f"workspace/{lay['feed_relpath']}/dt={{date}}"}],
            "platform_restore": [{"from": "platform",
                                  "to": f"workspace/{lay['platform_relpath']}"}],
            "deploy": {"strategy": "schedule_model", "module_dir": module_dir,
                       "durations": dict(contract["durations"]),
                       "default_duration_s": int(contract["default_duration_s"]),
                       "earliest_start": contract["jobs"]["earliest_start"],
                       "probe_day": "2026-01-01"},
            "writer_token": "{db}",
            "liveness_exclude_jobs": [contract["jobs"]["close"]],
            "final_tables": tables,
            "copy_ignore": [".git", "*.wal"],
        },
    }


# ===========================================================================
# a synthetic world the real scheduler can advance
# ===========================================================================
TARGET = "mart.totals"
DDL = {TARGET: "CREATE TABLE mart.totals (day DATE, n BIGINT)",
       "mart.notes": "CREATE TABLE mart.notes (day DATE, note VARCHAR)"}
VNOW = "2026-01-01T09:00:00Z"
DATES = ["2026-01-01", "2026-01-02"]

LOADER = textwrap.dedent('''
    import sys, pathlib, duckdb
    db, ws = sys.argv[1], pathlib.Path(sys.argv[2])
    FACTOR = {factor}
    rows = []
    for folder in sorted((ws / "feed").glob("dt=*")):
        rows.append((folder.name[3:], int((folder / "n.txt").read_text()) * FACTOR))
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{{db}}' AS wh")
    con.execute("USE wh")
    con.execute("CREATE SCHEMA IF NOT EXISTS mart")
    con.execute("DROP TABLE IF EXISTS mart.totals")
    con.execute("{ddl}")
    con.executemany("INSERT INTO mart.totals VALUES (?, ?)", rows)
    con.execute("CHECKPOINT")
    con.close()
''')

SCHEDULE_MODEL = textwrap.dedent('''
    """A one-job schedule model with the shape replay_isolation's
    `schedule_model` strategy calls: deployed_jobs, state_jobs, ScheduleError."""
    class ScheduleError(ValueError):
        pass

    def deployed_jobs(doc, durations, default_duration_s):
        jobs = {}
        for name, spec in (doc.get("jobs") or {}).items():
            if "script" not in spec:
                raise ScheduleError(f"jobs.{name} declares no script")
            jobs[name] = {"command": name, "duration_s": int(default_duration_s),
                          "detail": "dt={day}", "time": spec["time"], "day_offset": 0,
                          "exec": {"kind": "script", "path": spec["script"],
                                   "args": ["{db}", "{workspace}"], "detail": name},
                          "publishes": [{"target": "mart.totals", "kind": "table",
                                         "consumer": "bank"}]}
        return jobs

    def state_jobs(jobs):
        return {n: {"command": s["command"], "duration_s": s["duration_s"],
                    "time": s["time"], "day_offset": s["day_offset"]}
                for n, s in jobs.items()}
''')


def _write_json(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(root).as_posix().encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


def make_world(tmp: Path, *, factor: int = 1, layout: str | None = None,
               time: str = "10:00") -> dict:
    """World root + grading zone + spec. `factor` is the agent's bug (2 = the
    `doubled` rival); `layout` replaces the loader's DDL (a layout change)."""
    root, grading = tmp / "world", tmp / "grading"
    ws = root / "workspace"
    (ws / "feed" / "dt=2025-12-31").mkdir(parents=True)
    (ws / "feed" / "dt=2025-12-31" / "n.txt").write_text("3")
    (ws / "jobs").mkdir()
    (ws / "jobs" / "load.py").write_text(
        LOADER.format(factor=factor, ddl=layout or DDL[TARGET]), encoding="utf-8")
    (ws / "schedule.yml").write_text(
        f"jobs:\n  load:\n    time: \"{time}\"\n    script: jobs/load.py\n",
        encoding="utf-8")
    _write_json(root / "var/sched/state.json", {"vnow": VNOW, "runs": [], "jobs": {}})
    _write_json(root / "var/world/live.json",
                {"vtime_start": "2025-12-01T00:00:00Z", "jobs": {}})
    (root / "var/warehouse").mkdir(parents=True)
    con = duckdb.connect(str(root / "var/warehouse/warehouse.duckdb"))
    con.execute("CREATE SCHEMA mart")
    con.execute(DDL[TARGET])
    con.execute("INSERT INTO mart.totals VALUES ('2025-12-31', 3)")
    con.execute(DDL["mart.notes"])
    con.execute("INSERT INTO mart.notes VALUES ('2025-12-31', 'seeded'), "
                "('2025-12-30', 'seeded')")
    con.close()
    for k, (day, n) in enumerate(zip(DATES, (5, 7)), start=1):
        folder = grading / "next_nights" / "feed" / f"dt={day}"
        folder.mkdir(parents=True)
        (folder / "n.txt").write_text(str(n))
    days = [("2025-12-31", 3), ("2026-01-01", 5), ("2026-01-02", 7)]

    def nights(mult):
        return {"nights": [{"night": k, TARGET: [[d, n * mult] for d, n in days[:k + 1]]}
                           for k in (1, 2)]}
    _write_json(grading / "expected" / "variant_oracle.json", nights(1))
    _write_json(grading / "expected" / "variant_doubled.json", nights(2))
    # `twin` agrees with the oracle on night 1 only: a night that does not
    # separate two decisions must still be reported as the paying one.
    twin = nights(1)
    twin["nights"][1][TARGET][-1][1] = 99
    _write_json(grading / "expected" / "variant_twin.json", twin)
    _write_json(grading / "expected" / "t0.json", {
        "t0": {TARGET: [["2025-12-31", 3]],
               "mart.notes": [["2025-12-31", "seeded"], ["2025-12-30", "seeded"]]},
        "oracle_t0": {TARGET: [["2025-12-31", 3]]}})
    module_dir = tmp / "model"
    module_dir.mkdir()
    (module_dir / "schedule_model.py").write_text(SCHEDULE_MODEL, encoding="utf-8")
    spec = {
        "targets": {"totals": TARGET}, "nights": 2, "virtual_now": VNOW,
        "hours_per_night": 24, "plant_dates": DATES,
        "schedule_relpath": "schedule.yml", "ddl": DDL,
        "trajectories": {"names": ["oracle", "doubled", "twin"], "pays": "oracle",
                         "t0_file": "t0.json", "t0_blocks": ["t0", "oracle_t0"]},
        "t0_tables": [{"table": TARGET, "reference": "oracle_t0", "grain_key": ["day"]},
                      {"table": "mart.notes", "reference": "t0", "mode": "superset"}],
        "replay": {"plant": [{"from": "next_nights/feed/dt={date}",
                              "to": "workspace/feed/dt={date}"}],
                   "deploy": {"strategy": "schedule_model",
                              "module_dir": str(module_dir),
                              "default_duration_s": 30},
                   "final_tables": ["mart.notes"]},
    }
    return {"root": root, "grading": grading, "spec": spec}


def grade(world: dict, **kw) -> tuple[dict, dict, dict]:
    traj = engine.load_trajectories(world["grading"], world["spec"])
    replayed = iso.replay(world["root"], world["root"] / "workspace",
                          world["grading"], world["spec"], **kw)
    decisions = engine.classify_all(replayed["receipts"], traj,
                                    replayed["snapshots"], world["spec"])
    return traj, replayed, decisions


# ===========================================================================
# synthetic: the hash rule
# ===========================================================================
def test_canonical_hash_is_the_world_ledgers(tmp_path):
    db = tmp_path / "var/warehouse/warehouse.duckdb"
    db.parent.mkdir(parents=True)
    con = duckdb.connect(str(db))
    con.execute("CREATE SCHEMA s")
    con.execute("CREATE TABLE s.t (a INTEGER, b VARCHAR, c DATE, d DOUBLE, "
                "e DECIMAL(10,2), f TIMESTAMP)")
    con.execute("INSERT INTO s.t VALUES (2, NULL, '2026-01-02', 1.5, 3.10, "
                "'2026-01-01 10:00:00'), (1, 'x', NULL, NULL, NULL, NULL)")
    mine = engine.canonical_hash(con, "s.t")
    con.close()
    world_pkg = str(REPO / "world")
    sys.path.insert(0, world_pkg)
    try:
        iso._scheduler()
        from world import ledger                                   # noqa: PLC0415
        theirs = ledger.table_contents(tmp_path, "s.t")
    finally:
        sys.path.remove(world_pkg) if world_pkg in sys.path else None
    assert mine["canonical_hash"] == theirs["canonical_hash"]
    assert mine["row_count"] == 2 and mine["sample"] == theirs["sample"]
    con = duckdb.connect(str(db))
    assert engine.canonical_hash(con, "s.missing")["canonical_hash"] is None
    con.close()


def test_night_spans_are_half_open_and_last_receipt_wins():
    spec = {"targets": ["a.x", "a.y"], "nights": 2, "virtual_now": VNOW,
            "hours_per_night": 24}
    spans = engine.night_spans(spec)
    assert [s.isoformat() for s in spans[0]] == ["2026-01-01T09:00:00+00:00",
                                                 "2026-01-02T09:00:00+00:00"]

    def r(seq, vnow, target, h):
        return {"seq": seq, "vnow": vnow, "target": target, "canonical_hash": h}
    receipts = [r(1, "2026-01-01T10:00:00Z", "a.x", "h1"),
                r(2, "2026-01-01T23:00:00Z", "a.x", "h2"),
                r(3, "2026-01-02T09:00:00Z", "a.x", "h3"),   # boundary: night 2
                r(4, "2026-01-01T11:00:00Z", "a.z", "hz"),   # not governed
                r(5, "2026-01-03T09:00:00Z", "a.y", "late")]  # past the last night
    by = engine.receipts_by_night(receipts, None, spec)
    assert by[1]["a.x"]["canonical_hash"] == "h2" and "a.z" not in by[1]
    assert by[2] == {"a.x": receipts[2]}


# ===========================================================================
# synthetic: classification, audit, t0, progress
# ===========================================================================
def test_oracle_replay_pays_every_night_and_leaves_the_world_alone(tmp_path):
    w = make_world(tmp_path)
    before = _tree_digest(w["root"])
    traj, replayed, decisions = grade(w)
    assert _tree_digest(w["root"]) == before, "the t0 world was advanced"
    assert replayed["fatal"] is None and replayed["copy"] is None
    assert replayed["planned"] == {"load": "10:00"}
    assert replayed["jobs_ok"] == [True, True]
    assert len(replayed["receipts"]) == 2
    assert [decisions[k]["matched"] for k in (1, 2)] == ["oracle", "oracle"]
    assert "indistinguishable tonight from ['oracle', 'twin']" in \
        decisions[1]["per_target"][TARGET]["detail"]
    assert decisions[1]["totals"] == "oracle"
    assert replayed["totals_moved"] is True
    assert replayed["final"]["mart.notes"] == [["2025-12-30", "seeded"],
                                               ["2025-12-31", "seeded"]]
    audit = engine.audit_hash_rule(replayed["receipts"], traj)
    assert audit["audited"] and audit["matched"] == "oracle/night1"
    t0 = engine.score_t0(w["root"] / "var/warehouse/warehouse.duckdb", traj, w["spec"])
    assert t0["passed"] and t0["fraction"] == 1.0
    assert engine.progress_quantities(t0, decisions, w["spec"]) == \
        {"q0": 1.0, "q1": 1.0, "q2": 1.0}


def test_rival_is_named_and_does_not_pay(tmp_path):
    w = make_world(tmp_path, factor=2)
    traj, replayed, decisions = grade(w)
    assert [decisions[k]["matched"] for k in (1, 2)] == ["doubled", "doubled"]
    assert decisions[1]["receipts_matching"] == 0
    q = engine.progress_quantities({"fraction": 0.5}, decisions, w["spec"])
    assert q == {"q0": 0.5, "q1": 0.0, "q2": 0.0}


def test_unmatched_receipt_is_read_off_the_snapshot(tmp_path):
    w = make_world(tmp_path, layout="CREATE TABLE mart.totals (day DATE, n INTEGER)")
    _, replayed, decisions = grade(w)
    info = decisions[1]["per_target"][TARGET]
    assert info["variant"] == "oracle_content_other_layout"
    assert "retyped ['n INTEGER (oracle BIGINT)']" in info["detail"]
    assert decisions[1]["matched"] == "oracle_content_other_layout"


def test_classify_night_requires_every_target_to_agree():
    def h(x):
        return {"canonical_hash": x, "row_count": 1}
    traj = {"variants": {"oracle": {1: {"a.x": h("ox"), "a.y": h("oy")}},
                         "rival": {1: {"a.x": h("rx"), "a.y": h("oy")}}}}
    spec = {"targets": {"left": "a.x", "right": "a.y"}, "nights": 1,
            "trajectories": {"names": ["oracle", "rival"]}}

    def receipt(t, x):
        return {"target": t, "canonical_hash": x, "row_count": 1, "vnow": VNOW}
    split = engine.classify_night(1, {"a.x": receipt("a.x", "rx"),
                                      "a.y": receipt("a.y", "oy")}, traj, {}, spec)
    assert split["matched"] is None
    assert (split["left"], split["right"]) == ("rival", "oracle")
    assert split["receipts_matching"] == 1 and split["fraction"] == 0.5
    missing = engine.classify_night(1, {"a.x": receipt("a.x", "ox")}, traj, {}, spec)
    assert missing["matched"] is None
    assert missing["per_target"]["a.y"]["detail"].startswith("no receipt")
    unknown = engine.classify_night(1, {"a.x": receipt("a.x", "zz"),
                                        "a.y": receipt("a.y", "oy")}, traj, {}, spec)
    assert unknown["left"] is None and "unrecognised" in \
        unknown["per_target"]["a.x"]["detail"]
    assert engine.audit_hash_rule([{**receipt("a.x", "zz"), "seq": 1}],
                                  traj)["audited"] is False


def test_score_t0_fraction_grain_and_superset(tmp_path):
    w = make_world(tmp_path)
    db = w["root"] / "var/warehouse/warehouse.duckdb"
    con = duckdb.connect(str(db))
    con.execute("INSERT INTO mart.totals VALUES ('2025-12-29', 1)")
    con.execute("DELETE FROM mart.notes WHERE day = '2025-12-30'")
    con.close()
    traj = engine.load_trajectories(w["grading"], w["spec"])
    t0 = engine.score_t0(db, traj, w["spec"])
    assert t0["passed"] is False
    totals, notes = t0["per_table"]
    assert totals["keys_matching"] == 1 and totals["fraction"] == 0.5
    assert totals["row_fraction"] == 0.5
    assert notes["mode"] == "superset" and notes["fraction"] == 0.5
    assert t0["fraction"] == 0.5
    spec = {**w["spec"], "t0_tables": [
        {**w["spec"]["t0_tables"][0],
         "groups": {"column": "day", "values": ["2025-12-31", "2025-12-29"],
                    "field": "days_right"}}]}
    assert engine.score_t0(db, traj, spec)["days_right"] == 1
    gone = engine.score_t0(tmp_path / "nope.duckdb", traj, w["spec"])
    assert gone["passed"] is False and gone["fraction"] == 0.0


def test_undeployable_schedule_is_fatal_and_never_copies(tmp_path):
    w = make_world(tmp_path)
    (w["root"] / "workspace" / "schedule.yml").unlink()
    _, replayed, decisions = grade(w)
    assert replayed["fatal"] == "schedule.yml is gone; nothing can be deployed"
    assert decisions == {} and replayed["receipts"] == []
    assert engine.progress_quantities({"fraction": 1.0}, decisions, w["spec"]) == \
        {"q0": 1.0, "q1": 0.0, "q2": 0.0}


def test_custom_deploy_callable_and_keep(tmp_path):
    w = make_world(tmp_path, time="08:00")
    seen = {}

    def deploy(workspace, grading, spec):
        def install(root):
            live = json.loads((root / "var/world/live.json").read_text())
            live["jobs"] = {"load": {"command": "load", "duration_s": 10,
                                     "time": "12:00", "day_offset": 0,
                                     "exec": {"kind": "script", "path": "jobs/load.py",
                                              "args": ["{db}", "{workspace}"]},
                                     "publishes": [{"target": TARGET, "kind": "table"}]}}
            (root / "var/world/live.json").write_text(json.dumps(live))
            seen["root"] = root
        return {"jobs": None, "planned": {"load": "12:00"}, "install": install}

    _, replayed, decisions = grade(w, deploy=deploy, keep=True)
    assert replayed["planned"] == {"load": "12:00"}
    assert Path(replayed["copy"]) == seen["root"] and seen["root"].is_dir()
    assert [decisions[k]["matched"] for k in (1, 2)] == ["oracle", "oracle"]
    assert replayed["jobs_ok"] == [False, False]   # `jobs` unknown: nothing judged


def test_missing_plant_is_a_packaging_error(tmp_path):
    w = make_world(tmp_path)
    import shutil
    shutil.rmtree(w["grading"] / "next_nights")
    with pytest.raises(iso.PackagingError):
        grade(w)


# ===========================================================================
# real GT009
# ===========================================================================
def _recorded(rollout: Path) -> dict:
    return json.loads((rollout / "rollout.json").read_text(encoding="utf-8"))["score"]


def _check(score: dict, cid: str) -> dict:
    return next(c for c in score["checks"] if c["id"] == cid)


needs_r0 = pytest.mark.skipif(not (ROLLOUTS / "r0" / "rollout.json").is_file(),
                              reason="recorded GT009 rollouts are not on this host")


@needs_r0
def test_gt009_spec_t0_matches_recorded_c0():
    r0 = ROLLOUTS / "r0"
    spec = gt009_spec(gt009_fixtures(GT009, "default"))
    traj = engine.load_trajectories(r0 / "grading", spec)
    assert set(traj["variants"]) == set(spec["trajectories"]["names"])
    assert set(traj["variants"]["oracle"][1]) == set(engine.targets_of(spec))
    t0 = engine.score_t0(r0 / "world/var/warehouse/warehouse.duckdb", traj, spec)
    recorded = _check(_recorded(r0), "C0")
    assert t0["passed"] == recorded["passed"]
    assert t0["gap_days_handed_over"] == recorded["detail"]["gap_days_handed_over"]
    assert t0["fraction"] == 1.0


def _assert_replay_matches(rollout: Path, task_dir: Path) -> dict:
    variant = json.loads((rollout / "grading/variant.json").read_text())["variant"]
    spec = gt009_spec(gt009_fixtures(task_dir, variant))
    world = rollout / "world"
    before = _tree_digest(world / "var")
    traj = engine.load_trajectories(rollout / "grading", spec)
    replayed = iso.replay(world, world / "workspace", rollout / "grading", spec)
    assert _tree_digest(world / "var") == before, "the recorded world was advanced"
    decisions = engine.classify_all(replayed["receipts"], traj,
                                    replayed["snapshots"], spec)
    recorded = _recorded(rollout)
    assert replayed["fatal"] == recorded["replay"]["fatal"]
    assert replayed["planned"] == recorded["replay"]["planned"]
    assert [d["matched"] for d in recorded["decisions"]] == \
        [decisions[k]["matched"] for k in sorted(decisions)]
    for d in recorded["decisions"]:
        ours = decisions[d["night"]]
        assert {t: i["variant"] for t, i in d["per_target"].items()} == \
            {t: i["variant"] for t, i in ours["per_target"].items()}
        assert _check(recorded, f"R{d['night']}")["passed"] == (ours["matched"] == "oracle")
        assert _check(recorded, f"R{d['night']}")["detail"]["receipts_matching"] == \
            ours["receipts_matching"]
    audit = engine.audit_hash_rule(replayed["receipts"], traj)
    assert audit == recorded["ledger"]["hash_rule_audited"]
    return decisions


@needs_r0
def test_gt009_r0_replay_reproduces_recorded_nights():
    decisions = _assert_replay_matches(ROLLOUTS / "r0", GT009)
    assert all(d["matched"] == "oracle" for d in decisions.values())


@pytest.mark.skipif(not (E2_R1 / "rollout.json").is_file(),
                    reason="the desaturation recordings are not on this host")
def test_gt009_e2_r1_unsaturated_replay_reproduces_recorded_nights():
    decisions = _assert_replay_matches(E2_R1, DESAT / "tasks/GT009-settlement-cutoff")
    assert decisions[3]["matched"] != "oracle" and decisions[4]["matched"] != "oracle"
    q = engine.progress_quantities({"fraction": 1.0}, decisions,
                                   {"targets": ["a", "b"], "nights": 4})
    assert q["q1"] == q["q2"] == 1.0 and q["q3"] < 1.0
