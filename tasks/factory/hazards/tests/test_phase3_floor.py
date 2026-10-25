"""Phase 3 floor proofs: H13 approver, S09 schedule, S07/S03 contract, S01 watermark."""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from tasks.factory.stressors.oracle_composer import _prepare_minimal_world, compose_task
from tasks.factory.stressors.runtime_template import (
    PhysicalTables, oracle_behavior, render_modules, shipped_behavior,
)


def _stamp_vnow(world: Path, moment: str) -> None:
    for relative, key in (("var/sched/state.json", "vnow"),
                          ("var/world/live.json", "vtime_start")):
        path = world / relative
        document = json.loads(path.read_text(encoding="utf-8"))
        document[key] = moment
        path.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")


def _stage(tmp_path: Path, name: str, blueprint: dict) -> tuple[Path, Path, dict]:
    task = tmp_path / name
    world = task / "world"
    _prepare_minimal_world(world)
    if blueprint.get("vnow"):
        _stamp_vnow(world, blueprint["vnow"])
    staged = compose_task(task, blueprint, world)
    return task, world, staged


def _rows(world: Path, sql: str) -> list[tuple]:
    con = duckdb.connect(":memory:", config={"threads": 1})
    path = str(world / "var/warehouse/warehouse.duckdb").replace("'", "''")
    con.execute("ATTACH '" + path + "' AS wh (READ_ONLY)")
    con.execute("USE wh")
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _jobs(text: str) -> dict[str, dict]:
    jobs: dict[str, dict] = {}
    current = None
    pending_after = False
    for raw in text.splitlines():
        if raw.startswith("  ") and not raw.startswith("    ") and raw.endswith(":"):
            current = raw.strip()[:-1]
            jobs[current] = {}
            pending_after = False
            continue
        if current is None:
            continue
        stripped = raw.strip()
        if stripped == "after:":
            jobs[current]["after"] = {}
            pending_after = True
            continue
        if stripped.startswith("time:"):
            jobs[current]["time"] = stripped.split(":", 1)[1].strip().strip('"')
            pending_after = False
            continue
        if pending_after and stripped.startswith("job:"):
            jobs[current]["after"]["job"] = stripped.split(":", 1)[1].strip()
        elif pending_after and stripped.startswith("minutes:"):
            jobs[current]["after"]["minutes"] = int(stripped.split(":", 1)[1].strip())
    return jobs


def test_h13_approval_uses_t0_controller(tmp_path: Path) -> None:
    _task, world, staged = _stage(tmp_path, "h13", {
        "theme": "settlement_clearing",
        "stressors": ["S06"],
        "hazards": ["H13"],
        "governance_mode": "act-authorized",
        "seed": 42,
        "controller": "gward",
    })
    assert staged["authority"]["controller"] == "gward"
    corrections = staged["physical_tables"]["corrections"]
    approved = _rows(world, f"SELECT DISTINCT approved_by FROM {corrections}")
    assert approved == [("gward",)]
    with (world / "build/factory_future/R3/_approvals.csv").open(newline="", encoding="utf-8") as handle:
        night3 = list(csv.DictReader(handle))
    assert night3
    assert {row["approved_by"] for row in night3} == {"gward", "ops_auto_draft", "ops_ingestor"}
    historical_day = staged["dates"]["historical"]
    assert any(row == {"business_day": historical_day, "ticket": "INGEST-HOLD",
                       "approved_by": "ops_ingestor"} for row in night3)


_TABLES = PhysicalTables("raw", "fact", "handoff", "closed", "corrections", "checkpoint", "feed")


@pytest.mark.parametrize("hazard", [f"H{number:02d}" for number in range(1, 15)])
def test_temporal_hazards_require_authority_repair(hazard: str) -> None:
    behavior = shipped_behavior([], [hazard])
    expected = int(hazard[1:]) < 8
    assert behavior.strict_approval_authority is expected
    publisher = render_modules(behavior, _TABLES)["publish_handoff.py"]
    assert ("TRIM(approved_by)" in publisher) is expected


@pytest.mark.parametrize("stressor,comment", [
    ("S06", "# Partner tier-B split adjustment (legacy feed v2 convention)."),
    ("S08", "# Partner tier-B fee retention rate (legacy feed v2 convention)."),
])
def test_legacy_fold_comments_keep_domain_context(stressor: str, comment: str) -> None:
    fold = render_modules(shipped_behavior([stressor], []), _TABLES)["fold_ledger.py"]
    assert comment in fold
    assert "var/logs/ingest_daemon.log" not in fold


@pytest.mark.parametrize("approver,authorized", [
    (None, False), ("", False), ("  ", False), ("ops_auto_draft", False),
    (" ops_ingestor ", False), ("pending", False), ("gward", True),
])
def test_oracle_publisher_preserves_closed_day_without_controller_signoff(
        tmp_path: Path, approver: str | None, authorized: bool) -> None:
    warehouse = tmp_path / "var/warehouse"
    warehouse.mkdir(parents=True)
    with duckdb.connect(str(warehouse / "warehouse.duckdb")) as con:
        con.execute("CREATE TABLE fact (business_day DATE, entity_id VARCHAR, amount_cents BIGINT)")
        con.execute("INSERT INTO fact VALUES ('2026-06-01', 'entity', 900)")
        con.execute("CREATE TABLE handoff (business_day DATE, entity_id VARCHAR, payments BIGINT, net_cents BIGINT, correction_ref VARCHAR)")
        con.execute("INSERT INTO handoff VALUES ('2026-06-01', 'entity', 1, 100, NULL)")
        con.execute("CREATE TABLE closed AS SELECT business_day, entity_id, payments, net_cents FROM handoff")
        con.execute("CREATE TABLE corrections (business_day DATE, ticket VARCHAR, approved_by VARCHAR)")
        con.execute("INSERT INTO corrections VALUES ('2026-06-01', 'CORRECTION', ?)", [approver])
    publisher = tmp_path / "publish.py"
    publisher.write_text(render_modules(oracle_behavior(), _TABLES)["publish_handoff.py"], encoding="utf-8")
    subprocess.run([sys.executable, str(publisher), "publish", str(tmp_path)],
                   check=True, stdin=subprocess.DEVNULL)
    assert _rows(tmp_path, "SELECT net_cents, correction_ref FROM handoff") == [
        (900, "CORRECTION") if authorized else (100, None)]


def test_s09_schedule_shows_publish_before_load_and_solve_reverses_it(tmp_path: Path) -> None:
    task, world, _staged = _stage(tmp_path, "s09", {
        "theme": "settlement_clearing",
        "stressors": ["S09"],
        "hazards": ["H04"],
        "governance_mode": "act-authorized",
        "seed": 43,
        "controller": "bchen",
    })
    schedule = world / "workspace/orchestration/schedule.yml"
    text = schedule.read_text(encoding="utf-8")
    assert text.startswith("# Nightly factory orchestration.\njobs:\n")
    assert "Scheduler contract:" not in text
    before = _jobs(text)
    assert before["factory_publish"]["time"] == "21:50"
    assert before["factory_load"]["time"] == "22:00"
    assert "after" not in before["factory_publish"]
    live = json.loads((world / "var/world/live.json").read_text(encoding="utf-8"))
    assert live["jobs"]["factory_publish"]["time"] == "21:50"
    subprocess.run([sys.executable, str(task / "solution/solve.py"), str(world)],
                   check=True, stdin=subprocess.DEVNULL)
    after = _jobs(schedule.read_text(encoding="utf-8"))
    assert after["factory_publish"]["after"] == {"job": "factory_load", "minutes": 20}
    assert "time" not in after["factory_publish"]
    repaired = json.loads((world / "var/world/live.json").read_text(encoding="utf-8"))
    assert repaired["jobs"]["factory_publish"]["after"] == {"job": "factory_load", "minutes": 20}
    assert "time" not in repaired["jobs"]["factory_publish"]


def test_s07_and_s03_ingest_audits_do_not_name_the_buggy_function(tmp_path: Path) -> None:
    _task, world, staged = _stage(tmp_path, "contract", {
        "theme": "settlement_clearing",
        "stressors": ["S07", "S03"],
        "hazards": ["H01"],
        "governance_mode": "act-authorized",
        "seed": 44,
        "controller": "gward",
    })
    assert {row["id"] for row in staged["breadcrumbs"]} == {"H01"}
    ingest_log = (world / "var/logs/ingest_daemon.log").read_text(encoding="utf-8")
    assert "platform-level settlement rows" in ingest_log
    assert "events-retry.csv repeated an event_id" in ingest_log
    for banned in ("ingest_feed", "fold_ledger", "publish_handoff"):
        assert banned not in ingest_log


def test_s01_watermark_is_feed_day_and_matches_oracle_after_filter_removal(tmp_path: Path) -> None:
    task, world, staged = _stage(tmp_path, "s01", {
        "theme": "settlement_clearing",
        "stressors": ["S01"],
        "hazards": ["H01"],
        "governance_mode": "act-authorized",
        "seed": 45,
        "controller": "gward",
        "vnow": "2026-06-15T00:00:00Z",
    })
    assert staged["dates"]["t0"] == "2026-06-15"
    assert staged["dates"]["feed"] == "2026-06-14"
    checkpoint = staged["physical_tables"]["checkpoint"]
    raw = staged["physical_tables"]["raw"]
    through = _rows(world, f"SELECT CAST(through_day AS VARCHAR) FROM {checkpoint}")
    assert through == [("2026-06-14",)]
    partitions = [row[0] for row in _rows(world, f"SELECT source_partition FROM {raw}")]
    assert all("dt=2026-06-14" not in str(item) for item in partitions)
    feed = world / "workspace" / staged["feed_rel"] / "dt=2026-06-14"
    assert feed.is_dir()
    ingest = world / "workspace/pipelines/ingest_feed.py"
    source = ingest.read_text(encoding="utf-8")
    assert "vnow" not in source
    gate = "        if not (day > through or (retry and day == through)):\n            continue\n"
    assert gate in source
    oracle_world = tmp_path / "s01-oracle"
    shutil.copytree(world, oracle_world)
    ingest.write_text(source.replace(gate, "", 1), encoding="utf-8")
    subprocess.run([sys.executable, str(ingest), "load", str(world)],
                   check=True, stdin=subprocess.DEVNULL)
    subprocess.run([sys.executable, str(task / "solution/solve.py"), str(oracle_world)],
                   check=True, stdin=subprocess.DEVNULL)
    opened = _rows(world, f"SELECT CAST(through_day AS VARCHAR) FROM {checkpoint}")
    oracle = _rows(oracle_world, f"SELECT CAST(through_day AS VARCHAR) FROM {checkpoint}")
    assert opened == oracle == [("2026-06-14",)]
