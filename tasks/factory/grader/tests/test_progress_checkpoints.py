"""q0..q6 progress checkpoints: synthetic worlds plus one real factory task.

The real-data test copies nothing into the Phase 2 task directory: it
rebuilds the task's t0 world into a temp dir, plants two extra held-out
hazards (H04 on night 3, H09 on night 4) next to the task's own H01 (night 2),
recaptures the oracle, and grades five worlds - oracle, no_op, and three
agents whose scheduled 3-module pipeline is the oracle's minus the handling of
exactly one hazard. Set PROGRESS_REAL_DIR to reuse a built fixture.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from tasks.factory.grader import declarative_grade
from tasks.factory.grader import progress_checkpoints as pc
from tasks.factory.grader import receipt_engine as receipts
from tasks.harness.progress import (anchors_from_baselines, episode_progress,
                                    parse_progress_spec)

REAL_TASK = Path("/home/ubuntu/alkera_factory_1017/phase2_pilot_51/"
                 "FT0001-settlement-clearing-ft01-auth-a")


# ------------------------------------------------------------ synthetic

RAW, FACT, HAND = "landing.ledger_lines", "mart.fct_ledger", "mart.gl_extract"
SPEC = {
    "task_id": "SYN-1",
    "t0": {"oracle": "oracle_t0.json", "tables": [
        {"table": RAW, "grain_key": ["line_id"]},
        {"table": FACT, "grain_key": ["doc_id"]},
        {"table": HAND, "grain_key": ["posting_day", "unit"]}]},
    "replay": {"nights": [{}, {}], "targets": [RAW, FACT, HAND],
               "oracle_receipts": "oracle_receipts.json"},
    "progress": {"raw_table": RAW},
}


def _state(lines):
    """lines: (line_id, doc_id, day, unit, version, amount) -> table rows."""
    raw = [list(x) for x in lines]
    latest = {}
    for line in lines:
        if line[1] not in latest or line[4] > latest[line[1]][4]:
            latest[line[1]] = line
    fact = [[d, x[2], x[3], x[5], x[4]] for d, x in latest.items()]
    agg = {}
    for _, day, unit, amount, _ in fact:
        n, s = agg.get((day, unit), (0, 0))
        agg[(day, unit)] = (n + 1, s + amount)
    hand = [[day, unit, n, s] for (day, unit), (n, s) in agg.items()]
    return {RAW: (["line_id", "doc_id", "posting_day", "unit", "version", "amount"], raw),
            FACT: (["doc_id", "posting_day", "unit", "amount", "version"], fact),
            HAND: (["posting_day", "unit", "lines", "amount"], hand)}


def _snap(state):
    return {t: {"columns": cols, "rows": sorted(rows, key=pc._key)}
            for t, (cols, rows) in state.items()}


def _write_world(root: Path, state) -> Path:
    db = root / "var/warehouse/warehouse.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db))
    types = {"version": "INTEGER", "amount": "BIGINT", "lines": "INTEGER"}
    for table, (cols, rows) in state.items():
        schema = table.split(".")[0]
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        con.execute(f"CREATE TABLE {table} ("
                    + ", ".join(f"{c} {types.get(c, 'VARCHAR')}" for c in cols) + ")")
        if rows:
            con.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(cols))})", rows)
    con.close()
    return root


T0 = [("L1", "D1", "2026-01-01", "U1", 1, 100), ("L2", "D1", "2026-01-01", "U1", 2, 150),
      ("L3", "D2", "2026-01-01", "U2", 1, 70), ("L4", "D3", "2026-01-02", "U1", 1, 40)]
N1 = T0 + [("L5", "D4", "2026-01-03", "U1", 1, 10), ("L6", "D5", "2026-01-03", "U2", 1, 20),
           ("L7", "D6", "2026-01-03", "U2", 1, 30)]
N2 = N1 + [("L8", "D7", "2026-01-04", "U1", 1, 5), ("L9", "D8", "2026-01-04", "U2", 1, 6)]


@pytest.fixture()
def synthetic(tmp_path):
    grading = tmp_path / "grading"
    grading.mkdir()
    oracle = _write_world(tmp_path / "oracle", _state(T0))
    t0 = receipts.snapshot_t0(oracle, SPEC["t0"]["tables"])
    (grading / "oracle_t0.json").write_text(json.dumps(t0))
    nights = [{"night": i, "receipts": [], "runs": [],
               "snapshots": {pc.PREFIX + t: v for t, v in _snap(_state(s)).items()}}
              for i, s in enumerate((N1, N2), 1)]
    pc.write_oracle_rows(grading, SPEC, oracle, nights)

    def run(t0_lines, night_lines):
        world = _write_world(tmp_path / f"w{len(list(tmp_path.iterdir()))}", _state(t0_lines))
        agent_nights = [{"night": i, "receipts": [], "runs": [],
                         "snapshots": {pc.PREFIX + t: v for t, v in _snap(_state(s)).items()}}
                        for i, s in enumerate(night_lines, 1)]
        return pc.checkpoints(world_root=world, grading=grading, spec=SPEC,
                              nights=agent_nights, lazy=False)
    return run


def test_roles_follow_spec_not_names():
    spec = json.loads(json.dumps(SPEC))
    del spec["progress"]
    spec["checks"] = {"H2": {"kind": "closed_window_boundary", "handoff_table": HAND,
                             "day_column": "posting_day"}}
    role = pc.roles(spec)
    assert (role["raw"], role["fact"], role["handoff"]) == (RAW, FACT, HAND)
    assert pc._day_column(spec, FACT, ["doc_id", "posting_day"]) == "posting_day"
    assert set(pc.snapshot_specs(SPEC)) == {pc.PREFIX + t for t in (RAW, FACT, HAND)}


def test_oracle_scores_one(synthetic):
    out = synthetic(T0, [N1, N2])
    assert all(out[k] == 1.0 for k in pc.KEYS[:5]), out
    # a two-night spec has no R3/R4: absent, not zero
    assert out["q5_r3_parity"] is None and out["q6_r4_parity"] is None
    assert out["basis"]["night_rows"] == pc.ROWS_FILE


def test_no_op_scores_low(synthetic):
    out = synthetic([], [[], []])
    assert all(out[k] == 0.0 for k in pc.KEYS[:5]), out


def test_t0_checkpoints_separate_loading_from_precedence(synthetic):
    # every raw line loaded, but precedence by lowest version
    wrong = [x if x[0] != "L2" else ("L2", "D1", "2026-01-01", "U1", 0, 150) for x in T0]
    out = synthetic(wrong, [N1, N2])
    assert out["q0_raw_loaded"] == 1.0
    assert out["q1_t0_dedup_and_precedence"] == pytest.approx(2 / 3)
    assert 0 < out["q2_t0_handoff_parity"] < 1


def test_night_failure_is_partial_and_not_charged_to_later_nights(synthetic):
    # night 1 loses one of three new lines; night 2 is right on its own rows
    bad_n1 = [x for x in N1 if x[0] != "L7"]
    bad_n2 = [x for x in N2 if x[0] != "L7"]
    out = synthetic(T0, [bad_n1, bad_n2])
    assert 0 < out["q3_r1_parity"] < 1
    assert out["q4_r2_parity"] == 1.0


def test_night_parity_is_monotone_in_corrupted_rows(synthetic):
    scores = []
    for drop in (0, 1, 2, 3):
        kept = N1[:len(N1) - drop] if drop else N1
        scores.append(synthetic(T0, [kept, kept + N2[len(N1):]])["q3_r1_parity"])
    assert scores == sorted(scores, reverse=True) and scores[0] == 1.0 and scores[-1] < 0.2


def test_extra_junk_rows_cost_credit(synthetic):
    junk = N1 + [("JUNK", "DX", "2026-01-03", "U9", 1, 1)]
    assert synthetic(T0, [junk, junk + N2[len(N1):]])["q3_r1_parity"] < 1.0


def test_missing_table_is_zero_not_crash(synthetic, tmp_path):
    world = tmp_path / "empty"
    (world / "var/warehouse").mkdir(parents=True)
    duckdb.connect(str(world / "var/warehouse/warehouse.duckdb")).close()
    out = pc.checkpoints(world_root=world, grading=tmp_path / "grading", spec=SPEC,
                         nights=[{"snapshots": {}, "receipts": []}] * 2, lazy=False)
    assert all(out[k] == 0.0 for k in pc.KEYS[:3])
    assert all(out[k] is None for k in pc.KEYS[3:5])


def test_receipt_fallback_without_oracle_rows(tmp_path):
    grading = tmp_path / "g"
    grading.mkdir()
    (grading / "oracle_t0.json").write_text("{}")
    want = [[{"target": RAW, "canonical_hash": "a", "row_count": 10},
             {"target": FACT, "canonical_hash": "b", "row_count": 4}]] * 2
    (grading / "oracle_receipts.json").write_text(json.dumps({"nights": want}))
    nights = [{"receipts": [{"target": RAW, "canonical_hash": "a", "row_count": 10},
                            {"target": FACT, "canonical_hash": "x", "row_count": 2}]},
              {"receipts": []}]
    spec = dict(SPEC, replay=dict(SPEC["replay"], targets=[RAW, FACT]))
    out = pc.checkpoints(world_root=tmp_path, grading=grading, spec=spec, nights=nights,
                         cache_dir=tmp_path / "cache", lazy=False)
    assert out["basis"]["night_rows"].startswith("receipts_approx")
    assert out["basis"]["nights"][0]["tables"] == {RAW: 1.0, FACT: 0.25}
    assert out["q3_r1_parity"] is None and out["q4_r2_parity"] is None


def test_attach_and_recommended_block_feed_progress_py(synthetic, tmp_path):
    block = pc.recommended_progress_block()
    spec = parse_progress_spec(block, {"C0", "R1", "R2", "R3", "R4"})
    assert abs(sum(spec.weights.values()) - 1) < 1e-9 and len(spec.quantities) == 7
    result = {"checks": [{"id": i, "detail": {}} for i in ("C0", "R1", "R2")],
              "progress_quantities": {"q0": 0.5}}
    out = synthetic(T0, [N1, N2])
    nights = [{"snapshots": {pc.PREFIX + t: v for t, v in _snap(_state(s)).items()}}
              for s in (N1, N2)]
    world = _write_world(tmp_path / "attach_world", _state(T0))
    pc.attach(result, world_root=world, grading=tmp_path / "grading", spec=SPEC,
              nights=nights, t0=None, lazy=False)
    assert result["progress_quantities"]["q0"] == 0.5
    c0 = result["checks"][0]["detail"]
    assert c0["q0_raw_loaded"] == out["q0_raw_loaded"] == 1.0
    assert result["checks"][2]["detail"]["q4_r2_parity"] == 1.0
    assert "q3_r1_parity" not in c0


# ------------------------------------------------------------ real data

# ingest_feed.py edits that each drop ONE hazard's handling from the oracle.
# Every anchor must occur exactly once in the oracle module.
HAZARD_PATCHES = {
    # no sidecar filter: _delivery_manifest.csv (night 2) is read as events
    "miss_H01": {"pipelines/ingest_feed.py": [
        ('folder.glob("events*.csv")', 'folder.glob("*.csv")')]},
    # in-flight transfer files are not excluded: events.csv.part (night 3) is read
    "miss_H04": {"pipelines/ingest_feed.py": [
        ('folder.glob("events*.csv")', 'folder.glob("events*.csv*")')]},
    # dedup on the whole stored row, so the retry copy (another file) survives (night 4)
    "miss_H09": {"pipelines/ingest_feed.py": [
        ("unique.setdefault(event[0], event)", "unique.setdefault(event, event)")]},
}


def _run(cmd, cwd=None):
    proc = subprocess.run(cmd, cwd=cwd or pc.REPO, text=True, capture_output=True)
    if proc.returncode:
        raise RuntimeError(proc.stderr[-800:])


def _scheduled_scripts(world: Path) -> set[str]:
    """Workspace-relative scripts the deployed schedule executes."""
    live = json.loads((world / "var/world/live.json").read_text())
    return {job["exec"]["path"] for job in live["jobs"].values()
            if (job.get("exec") or {}).get("kind") == "script"}


def _truncate_part_mid_row(future: Path) -> Path:
    # The library picks H04's night itself; the misses must land on distinct
    # nights, so its files are moved to night 3 whichever night it chose.
    planted = sorted(future.glob("R*/dt=*/events.csv.part"))
    assert len(planted) == 1, planted
    night3 = next(future.glob("R3/dt=*/events.csv")).parent
    for name in ("events.csv.part", "._events.csv"):
        src = planted[0].parent / name
        if src.is_file() and src.parent != night3:
            src.replace(night3 / name)
    part = night3 / "events.csv.part"
    # The library cuts events.csv.part inside the header line, so no reader
    # ever sees a row from it; a real interrupted transfer stops mid-record.
    lines = (part.parent / "events.csv").read_text().splitlines()
    cut = lines[2][:len(lines[2]) // 2]
    assert cut.count(",") < lines[0].count(","), cut
    part.write_text("\n".join(lines[:2] + [cut]))
    return part


def build_real(dest: Path) -> dict:
    """Stage oracle / no_op / three single-hazard-miss worlds from REAL_TASK."""
    from tasks.factory.compiler.task_materializer import build_world_from_task
    from tasks.factory.hazards.arrival_file_hazards import apply_arrival_hazards
    from tasks.factory.hazards.temporal_contract_hazards import apply_temporal_hazards
    from tasks.factory.grader.replay_isolation import advance_nights

    done = dest / "built.json"
    if done.is_file():
        info = json.loads(done.read_text())
        if info.get("task") == str(REAL_TASK):
            return info
    if dest.exists():
        shutil.rmtree(dest)
    blueprint = json.loads((REAL_TASK / "fixtures/blueprint.json").read_text())
    nights = {h["hazard_id"][:3]: h["night"] for h in blueprint["r_hazards"]}
    assert nights.get("H01") == 2, nights
    seed, grading = int(blueprint["seed"]), dest / "grading"
    base = dest / "worlds/no_op"
    build_world_from_task(REAL_TASK, base, grading, seed)
    future = grading / "factory_future"
    assert any(future.glob("R2/dt=*/_delivery_manifest.csv"))
    assert any(future.glob("R4/dt=*/events.csv")), "H09 needs a night-4 payload"
    apply_arrival_hazards(future, ["H04"], seed=seed, historical_day=blueprint["closed_day"])
    apply_temporal_hazards(future, ["H09"], seed=seed, historical_day=blueprint["closed_day"])
    _truncate_part_mid_row(future)
    assert any(future.glob("R4/dt=*/events-retry.csv"))
    spec = json.loads((grading / "spec.json").read_text())
    oracle_dir = REAL_TASK / "solution/oracle"
    worlds = {}
    for name in ("oracle", *HAZARD_PATCHES):
        world = dest / "worlds" / name
        shutil.copytree(base, world)
        _run([sys.executable, str(REAL_TASK / "solution/solve.py"), str(world)])
        scheduled = _scheduled_scripts(world)
        for module, edits in HAZARD_PATCHES.get(name, {}).items():
            assert module in scheduled, f"{name}: {module} is not run by the schedule {scheduled}"
            deployed = world / "workspace" / module
            text = (oracle_dir / Path(module).name).read_text()
            assert deployed.read_text() == text, f"{name}: {module} is not the oracle module"
            for old, new in edits:
                assert text.count(old) == 1, f"{name}: patch anchor not unique/present: {old}"
                text = text.replace(old, new)
            deployed.write_text(text)
        worlds[name] = str(world)
    worlds["no_op"] = str(base)
    oracle = Path(worlds["oracle"])
    replay = spec["replay"]
    replayed = advance_nights(oracle, grading, replay["nights"], pc.snapshot_specs(spec),
                              replay.get("prepare"))
    t0 = receipts.snapshot_t0(oracle, spec["t0"]["tables"])
    (grading / "oracle_t0.json").write_text(json.dumps(t0, sort_keys=True, indent=2) + "\n")
    (grading / "oracle_receipts.json").write_text(json.dumps(
        {"nights": [[receipts.receipt_signature(r) for r in n["receipts"]] for n in replayed]},
        sort_keys=True, indent=2) + "\n")
    pc.write_oracle_rows(grading, spec, oracle, replayed)
    info = {"task": str(REAL_TASK), "grading": str(grading), "worlds": worlds}
    done.write_text(json.dumps(info, indent=2))
    return info


def grade_with_hook(world: Path, grading: Path) -> dict:
    """The production path: declarative_grade.grade calls snapshot_specs/attach.

    grade() back-fills any of the 7 keys attach() did not produce with
    receipt-level values, so require that every key is ours and row-level.
    """
    report = world / "workspace/final_report.json"
    result = declarative_grade.grade(world, grading, report if report.is_file() else None)
    assert "progress_error" not in result, result["progress_error"]
    assert result["progress_basis"]["night_rows"] == pc.ROWS_FILE
    details = {c["id"]: c["detail"] for c in result["checks"]}
    owners = dict.fromkeys(pc.T0_KEYS, "C0")
    owners.update({k: f"R{i}" for i, k in enumerate(pc.NIGHT_KEYS, 1)})
    for key, owner in owners.items():
        assert details[owner].get(key) == result["progress_quantities"][key], (key, owner)
    return result


@pytest.fixture(scope="module")
def real(tmp_path_factory):
    if not (REAL_TASK / "solution/oracle/ingest_feed.py").is_file():
        pytest.skip(f"Phase 2 factory task absent: {REAL_TASK}")
    dest = Path(os.environ.get("PROGRESS_REAL_DIR") or tmp_path_factory.mktemp("real"))
    info = build_real(dest)
    grading = Path(info["grading"])
    names = ["oracle", "no_op", *HAZARD_PATCHES]
    results = {n: grade_with_hook(Path(info["worlds"][n]), grading) for n in names}
    baselines = {"baselines": {n: {"checks": {c["id"]: c for c in results[n]["checks"]}}
                               for n in ("oracle", "no_op")}}
    spec = parse_progress_spec(pc.recommended_progress_block(), None)
    anchors = anchors_from_baselines(spec, baselines)
    scalars = {n: episode_progress(spec, {c["id"]: c for c in r["checks"]}, anchors)
               for n, r in results.items()}
    summary = {n: {"q": {k: results[n]["progress_quantities"][k] for k in pc.KEYS},
                   "passed": results[n]["passed"], "scalar": scalars[n],
                   "checks": {c["id"]: c["passed"] for c in results[n]["checks"]}}
               for n in names}
    (dest / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def test_real_oracle_is_all_one(real):
    assert all(v == 1.0 for v in real["oracle"]["q"].values()), real["oracle"]
    assert real["oracle"]["passed"]


def test_real_no_op_is_low(real):
    assert sum(real["no_op"]["q"].values()) / 7 < 0.2, real["no_op"]


def test_real_hazard_misses_fail_on_their_night(real):
    for name, first_bad in (("miss_H01", 2), ("miss_H04", 3), ("miss_H09", 4)):
        row = real[name]
        assert row["checks"]["C0"], (name, row)
        for night in range(1, 5):
            assert row["checks"][f"R{night}"] == (night < first_bad), (name, night, row)
            key = pc.NIGHT_KEYS[night - 1]
            if night < first_bad:
                assert row["q"][key] == 1.0, (name, key, row)
        assert row["q"][pc.NIGHT_KEYS[first_bad - 1]] < 1.0, (name, row)


def test_real_three_distinct_monotone_scalars(real):
    s = {n: real[n]["scalar"] for n in real}
    assert len({s["miss_H01"], s["miss_H04"], s["miss_H09"]}) == 3, s
    assert s["no_op"] < s["miss_H01"] < s["miss_H04"] < s["miss_H09"] < s["oracle"], s
