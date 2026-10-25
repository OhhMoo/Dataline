"""C0 accepts an in-range factory checkpoint only when the data tables match."""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import yaml

from tasks.factory.grader.declarative_grade import grade
from tasks.factory.grader.receipt_engine import compare_t0

RAW = "raw.factory_events"
FACT = "warehouse.fct_factory_events"
HANDOFF = "warehouse.factory_handoff"
CHECKPOINT = "warehouse.factory_checkpoint"
FEED = "2026-06-14"
T0 = "2026-06-15"
ORACLE_RAW = [["e1", 10]]
ORACLE_FACT = [["b1", 10]]
ORACLE_HANDOFF = [["2026-06-14", "ORG", 10]]


def _spec() -> dict:
    return {"tables": [
        {"table": RAW, "grain_key": ["event_id"]},
        {"table": FACT, "grain_key": ["business_id"]},
        {"table": HANDOFF, "grain_key": ["business_day", "entity_id"]},
        {"table": CHECKPOINT, "grain_key": ["through_day"]},
    ]}


def _oracle(grading: Path) -> None:
    grading.mkdir(parents=True, exist_ok=True)
    (grading / "oracle_t0.json").write_text(json.dumps({
        RAW: {"columns": ["event_id", "amount"], "rows": ORACLE_RAW},
        FACT: {"columns": ["business_id", "amount"], "rows": ORACLE_FACT},
        HANDOFF: {"columns": ["business_day", "entity_id", "net_cents"],
                  "rows": ORACLE_HANDOFF},
        CHECKPOINT: {"columns": ["through_day"], "rows": [[FEED]]},
    }), encoding="utf-8")


def _world(root: Path, *, raw, fact, handoff, through, vnow="2026-06-15T00:00:00Z",
           checkpoint_rows=1) -> None:
    db = root / "var/warehouse/warehouse.duckdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:")
    con.execute(f"ATTACH '{db.as_posix()}' AS wh")
    con.execute("CREATE SCHEMA wh.raw")
    con.execute("CREATE SCHEMA wh.warehouse")
    con.execute("CREATE TABLE wh.raw.factory_events (event_id VARCHAR, amount INTEGER)")
    con.execute(
        "CREATE TABLE wh.warehouse.fct_factory_events (business_id VARCHAR, amount INTEGER)")
    con.execute(
        "CREATE TABLE wh.warehouse.factory_handoff "
        "(business_day VARCHAR, entity_id VARCHAR, net_cents INTEGER)")
    con.execute("CREATE TABLE wh.warehouse.factory_checkpoint (through_day DATE)")
    if raw:
        con.executemany("INSERT INTO wh.raw.factory_events VALUES (?, ?)", raw)
    if fact:
        con.executemany("INSERT INTO wh.warehouse.fct_factory_events VALUES (?, ?)", fact)
    if handoff:
        con.executemany(
            "INSERT INTO wh.warehouse.factory_handoff VALUES (?, ?, ?)", handoff)
    rows = [through] * checkpoint_rows if through is not None else []
    if rows:
        con.executemany(
            "INSERT INTO wh.warehouse.factory_checkpoint VALUES (?)",
            [(day,) for day in rows])
    con.close()
    state = root / "var/sched/state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"vnow": vnow}), encoding="utf-8")


def _grade(root: Path, grading: Path, **world) -> dict:
    _oracle(grading)
    _world(root, **world)
    return compare_t0(root, grading, _spec())


def test_in_range_watermark_passes_when_data_tables_match(tmp_path):
    root, grading = tmp_path / "world", tmp_path / "grading"
    exact = _grade(root, grading, raw=[("e1", 10)], fact=[("b1", 10)],
                   handoff=[("2026-06-14", "ORG", 10)], through=FEED)
    assert exact["passed"] is True and exact["fraction"] == 1.0
    assert "checkpoint_watermark" not in exact

    close = _grade(tmp_path / "close", grading, raw=[("e1", 10)], fact=[("b1", 10)],
                   handoff=[("2026-06-14", "ORG", 10)], through=T0)
    assert close["passed"] is True
    assert close["fraction"] == 1.0
    assert close["issues"] == []
    assert close["checkpoint_watermark"]["through_day"] == T0
    assert close["checkpoint_watermark"]["feed_day"] == FEED
    assert close["checkpoint_watermark"]["t0_day"] == T0


def test_watermark_outside_the_window_still_fails(tmp_path):
    grading = tmp_path / "grading"
    for day in ("2026-06-13", "2026-06-16"):
        result = _grade(tmp_path / day, grading, raw=[("e1", 10)], fact=[("b1", 10)],
                        handoff=[("2026-06-14", "ORG", 10)], through=day)
        assert result["passed"] is False, day
        assert any(CHECKPOINT in issue for issue in result["issues"])


def test_wrong_fact_does_not_pass_on_a_close_watermark(tmp_path):
    result = _grade(tmp_path / "world", tmp_path / "grading",
                    raw=[("e1", 10)], fact=[("b1", 1)],
                    handoff=[("2026-06-14", "ORG", 10)], through=T0)
    assert result["passed"] is False
    assert "checkpoint_watermark" not in result
    assert any(FACT in issue for issue in result["issues"])


def test_no_op_and_partial_fix_fail_c0(tmp_path):
    grading = tmp_path / "grading"
    no_op = _grade(tmp_path / "no_op", grading, raw=[], fact=[], handoff=[], through=T0)
    partial = _grade(tmp_path / "partial", grading, raw=[("e1", 10)], fact=[],
                     handoff=[("2026-06-14", "ORG", 10)], through=T0)
    assert no_op["passed"] is False
    assert partial["passed"] is False
    assert any(FACT in issue for issue in partial["issues"])
    assert "checkpoint_watermark" not in no_op
    assert "checkpoint_watermark" not in partial


def test_five_baseline_arms_stay_separable(tmp_path):
    grading = tmp_path / "grading"
    data = dict(raw=[("e1", 10)], fact=[("b1", 10)],
                handoff=[("2026-06-14", "ORG", 10)])
    arms = {
        "oracle": {**data, "through": FEED},
        "no_op": {"raw": [], "fact": [], "handoff": [], "through": T0},
        "wrong_precedence": {**data, "fact": [("b1", 1)], "through": T0},
        "duplicate_counted": {**data, "raw": [("e1", 10), ("e1", 10)], "through": T0},
        "boundary_violated": {**data, "handoff": [("2026-06-14", "ORG", 999)], "through": T0},
    }
    results = {name: _grade(tmp_path / name, grading, **world) for name, world in arms.items()}
    assert results["oracle"]["passed"] is True
    failed = ("no_op", "wrong_precedence", "duplicate_counted", "boundary_violated")
    assert all(results[name]["passed"] is False for name in failed)

    def tables(name):
        return frozenset(issue.split()[0].rstrip(":") for issue in results[name]["issues"])

    assert len({tables(name) for name in failed}) == len(failed)
    assert FACT in tables("wrong_precedence")
    assert RAW in tables("duplicate_counted")
    assert HANDOFF in tables("boundary_violated")
    assert {RAW, FACT, HANDOFF} <= tables("no_op")
    assert tables("wrong_precedence") != tables("duplicate_counted")
    assert tables("duplicate_counted") != tables("boundary_violated")
    assert tables("boundary_violated") != tables("no_op")


def test_spec_window_is_what_declarative_grade_uses(tmp_path):
    """Dates declared on the task spec outrank a scheduler clock that has moved."""
    root, grading = tmp_path / "world", tmp_path / "grading"
    _oracle(grading)
    _world(root, raw=[("e1", 10)], fact=[("b1", 10)],
           handoff=[("2026-06-14", "ORG", 10)], through=T0,
           vnow="2026-08-01T00:00:00Z")
    spec = {"schema_version": 1, "task_id": "watermark-fixture",
            "feed_day": FEED, "t0_day": T0, "t0": _spec(),
            "checks": {"C0": {"kind": "t0_parity"}},
            "replay": {"nights": [], "targets": [RAW, FACT, HANDOFF]}}
    (grading / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    manifest = {"task_id": spec["task_id"], "checks": [
        {"id": "C0", "points": 2, "in_pass_criterion": True}]}
    (grading / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    passed = grade(root, grading, None, "default")
    c0 = next(check for check in passed["checks"] if check["id"] == "C0")
    assert c0["passed"] is True
    assert passed["passed"] is True
    assert c0["detail"]["checkpoint_watermark"]["source"] == "spec"

    broken = tmp_path / "broken"
    _world(broken, raw=[("e1", 10)], fact=[("b1", 99)],
           handoff=[("2026-06-14", "ORG", 10)], through=T0,
           vnow="2026-08-01T00:00:00Z")
    failed = grade(broken, grading, None, "default")
    c0_failed = next(check for check in failed["checks"] if check["id"] == "C0")
    assert c0_failed["passed"] is False
    assert failed["passed"] is False
    assert any(FACT in issue for issue in c0_failed["detail"]["issues"])


LOADER = '''import json, sys
from pathlib import Path
import duckdb
root = Path(sys.argv[2])
con = duckdb.connect(":memory:")
con.execute("ATTACH '" + str(root / "var/warehouse/warehouse.duckdb") + "' AS wh")
con.execute("USE wh")
through = str(con.execute("SELECT MAX(through_day) FROM warehouse.factory_checkpoint").fetchone()[0])
{advance}
con.execute("DELETE FROM warehouse.factory_checkpoint")
con.execute("INSERT INTO warehouse.factory_checkpoint VALUES (?)", [through])
con.close()
'''
CALENDAR = ('vnow = json.loads((root / "var/sched/state.json").read_text())["vnow"][:10]\n'
            'through = max(through, vnow)')


def _deploy_loader(root: Path, advance: str) -> None:
    script = root / "workspace/pipelines/ingest_feed.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(LOADER.format(advance=advance), encoding="utf-8")
    live = root / "var/world/live.json"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(json.dumps({"jobs": {"factory_load": {
        "command": "factory_load",
        "exec": {"kind": "script", "path": "pipelines/ingest_feed.py",
                 "args": ["load", "{root}"]}}}}), encoding="utf-8")


def _exact_world(root: Path) -> None:
    _world(root, raw=ORACLE_RAW, fact=ORACLE_FACT, handoff=ORACLE_HANDOFF, through=T0)


def test_stale_watermark_passes_when_the_loader_no_longer_advances(tmp_path):
    root, grading = tmp_path / "world", tmp_path / "grading"
    _oracle(grading)
    _exact_world(root)
    _deploy_loader(root, "")
    result = compare_t0(root, grading, {**_spec(), "feed_day": FEED, "t0_day": T0})
    assert result["passed"], result
    probe = result["checkpoint_watermark"]["probe"]
    assert probe["ran"] and probe["advances_on_calendar"] is False


def test_calendar_advancing_loader_is_not_excused(tmp_path):
    root, grading = tmp_path / "world", tmp_path / "grading"
    _oracle(grading)
    _exact_world(root)
    _deploy_loader(root, CALENDAR)
    result = compare_t0(root, grading, {**_spec(), "feed_day": FEED, "t0_day": T0})
    assert not result["passed"]
    assert "checkpoint_watermark" not in result
    assert any("advances it without data" in issue for issue in result["issues"])
    assert (root / "var/sched/state.json").read_text().find("2026-06-15") >= 0
