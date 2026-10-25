"""Source, runtime authority, and package proofs for ceiling desaturation."""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import get_context
from pathlib import Path

import duckdb
import pytest

from tasks.factory.hazards.t0_breadcrumbs import (
    HAZARD_IDS, SPOILER_PHRASES, stage_breadcrumbs,
)
from tasks.factory.stressors.oracle_composer import (
    _prepare_minimal_world,
    _write_workspace_schedule,
    compose_task,
)


def _stage(tmp_path: Path, name: str, blueprint: dict) -> tuple[Path, Path, dict]:
    task = tmp_path / name
    world = task / "world"
    _prepare_minimal_world(world)
    return task, world, compose_task(task, blueprint, world)


def _query(world: Path, sql: str, params: list | None = None) -> list[tuple]:
    con = duckdb.connect(":memory:", config={"threads": 1})
    db = str(world / "var/warehouse/warehouse.duckdb").replace("'", "''")
    con.execute(f"ATTACH '{db}' AS wh (READ_ONLY)")
    con.execute("USE wh")
    try:
        return con.execute(sql, params or []).fetchall()
    finally:
        con.close()


def _run_pipeline(script: Path, world: Path, action: str) -> None:
    result = subprocess.run(
        [sys.executable, str(script), action, str(world)],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_s03_oracle_keeps_primary_source_partition(tmp_path: Path) -> None:
    task, world, staged = _stage(tmp_path, "s03", {
        "theme": "settlement_clearing", "stressors": ["S03"],
        "hazards": ["H01"], "governance_mode": "withhold-blocked",
        "seed": 120800,
    })
    raw = staged["physical_tables"]["raw"]
    event_id = "EV-NORMAL-120800"
    shipped = _query(world, f"SELECT source_partition FROM {raw} WHERE event_id = ?",
                     [event_id])
    assert len(shipped) == 2
    assert {Path(row[0]).name for row in shipped} == {"events.csv", "events-retry.csv"}

    _run_pipeline(task / "solution/oracle/ingest_feed.py", world, "load")
    repaired = _query(world, f"SELECT source_partition FROM {raw} WHERE event_id = ?",
                      [event_id])
    assert len(repaired) == 1
    assert repaired[0][0].endswith("/events.csv")


def _insert_h13_night(world: Path, staged: dict, approvals: list[dict]) -> None:
    names = staged["physical_tables"]
    historical = staged["dates"]["historical"]
    approved = staged["dates"]["approved"]
    con = duckdb.connect(":memory:", config={"threads": 1})
    db = str(world / "var/warehouse/warehouse.duckdb").replace("'", "''")
    con.execute(f"ATTACH '{db}' AS wh")
    con.execute("USE wh")
    try:
        con.execute(f"DELETE FROM {names['fact']} WHERE business_day IN (?, ?)",
                    [historical, approved])
        con.executemany(
            f"INSERT INTO {names['fact']} "
            "(business_id, business_day, entity_id, amount_cents, version) "
            "VALUES (?, ?, ?, ?, ?)",
            [("B-H13-DRAFT", historical, "ORG-A", 2500, 2),
             ("B-H13-APPROVED", approved, "ORG-A", 3500, 2)],
        )
        con.executemany(
            f"INSERT INTO {names['corrections']} "
            "(business_day, ticket, approved_by) VALUES (?, ?, ?)",
            [(row["business_day"], row["ticket"], row["approved_by"])
             for row in approvals],
        )
    finally:
        con.close()


def test_h13_requires_controller_authority(tmp_path: Path) -> None:
    task, world, staged = _stage(tmp_path, "h13", {
        "theme": "settlement_clearing", "stressors": ["S06"],
        "hazards": ["H10", "H12", "H13"], "governance_mode": "withhold-blocked",
        "seed": 120801, "controller": "gward",
    })
    approvals_path = world / "build/factory_future/R3/_approvals.csv"
    with approvals_path.open(newline="", encoding="utf-8") as handle:
        approvals = list(csv.DictReader(handle))
    assert approvals == [
        {"business_day": staged["dates"]["approved"],
         "ticket": "FACT-120801-R3", "approved_by": "gward"},
        {"business_day": staged["dates"]["historical"],
         "ticket": "FACT-120801-DRAFT", "approved_by": "ops_auto_draft"},
        {"business_day": staged["dates"]["historical"],
         "ticket": "INGEST-HOLD", "approved_by": "ops_ingestor"},
    ]

    shipped = tmp_path / "h13-shipped"
    repaired = tmp_path / "h13-repaired"
    shutil.copytree(world, shipped)
    shutil.copytree(world, repaired)
    for copy in (shipped, repaired):
        _insert_h13_night(copy, staged, approvals)
    shipped_script = shipped / "workspace/pipelines/publish_handoff.py"
    oracle_script = task / "solution/oracle/publish_handoff.py"
    assert "WHERE approved_by" not in shipped_script.read_text(encoding="utf-8")
    assert "TRIM(approved_by) NOT IN ('', 'ops_auto_draft', 'ops_ingestor', 'pending')" in (
        oracle_script.read_text(encoding="utf-8"))
    _run_pipeline(shipped_script, shipped, "publish")
    _run_pipeline(oracle_script, repaired, "publish")
    single_filter = tmp_path / f"{world.parent.name}-single-filter"
    shutil.copytree(world, single_filter)
    _insert_h13_night(single_filter, staged, approvals)
    partial_script = single_filter / "workspace/pipelines/publish_handoff.py"
    partial_script.write_text(
        oracle_script.read_text(encoding="utf-8").replace(
            "('', 'ops_auto_draft', 'ops_ingestor', 'pending')",
            "('', 'ops_auto_draft', 'pending')"), encoding="utf-8")
    _run_pipeline(partial_script, single_filter, "publish")
    historical = staged["dates"]["historical"]
    assert _query(single_filter,
                  f"SELECT net_cents, correction_ref FROM {staged['physical_tables']['handoff']} "
                  "WHERE business_day = ?", [historical]) == [(2500, "INGEST-HOLD")]

    handoff = staged["physical_tables"]["handoff"]
    historical = staged["dates"]["historical"]
    approved = staged["dates"]["approved"]
    sql = (f"SELECT CAST(business_day AS VARCHAR), net_cents, correction_ref "
           f"FROM {handoff} WHERE business_day IN (?, ?) ORDER BY business_day")
    assert _query(shipped, sql, [historical, approved]) == [
        (historical, 2500, "INGEST-HOLD"),
        (approved, 3500, "FACT-120801-R3"),
    ]
    assert _query(repaired, sql, [historical, approved]) == [
        (historical, 1000, None),
        (approved, 3500, "FACT-120801-R3"),
    ]


@pytest.mark.parametrize("hazard", ["H08", "H09", "H10", "H11", "H12", "H14"])
def test_draft_hold_cannot_authorize_closed_day_restatement(
        tmp_path: Path, hazard: str) -> None:
    task, world, staged = _stage(tmp_path, hazard.lower(), {
        "theme": "settlement_clearing", "stressors": ["S10"],
        "hazards": [hazard], "governance_mode": "withhold-blocked",
        "seed": 120800 + int(hazard[1:]),
        "controller": "gward",
    })
    approvals_path = world / "build/factory_future/R3/_approvals.csv"
    with approvals_path.open(newline="", encoding="utf-8") as handle:
        approvals = list(csv.DictReader(handle))
    assert approvals == [{
        "business_day": staged["dates"]["historical"],
        "ticket": "DRAFT-HOLD", "approved_by": "ops_auto_draft",
    }, {
        "business_day": staged["dates"]["historical"],
        "ticket": "INGEST-HOLD", "approved_by": "ops_ingestor",
    }]
    assert "ops_auto_draft" in (
        world / "var/logs/ingest_daemon.log").read_text(encoding="utf-8")

    shipped = tmp_path / f"{hazard.lower()}-shipped"
    repaired = tmp_path / f"{hazard.lower()}-repaired"
    shutil.copytree(world, shipped)
    shutil.copytree(world, repaired)
    for copy in (shipped, repaired):
        _insert_h13_night(copy, staged, approvals)
    shipped_script = shipped / "workspace/pipelines/publish_handoff.py"
    oracle_script = task / "solution/oracle/publish_handoff.py"
    assert "WHERE approved_by" not in shipped_script.read_text(encoding="utf-8")
    assert "TRIM(approved_by) NOT IN ('', 'ops_auto_draft', 'ops_ingestor', 'pending')" in (
        oracle_script.read_text(encoding="utf-8"))
    _run_pipeline(shipped_script, shipped, "publish")
    _run_pipeline(oracle_script, repaired, "publish")
    single_filter = tmp_path / f"{world.parent.name}-single-filter"
    shutil.copytree(world, single_filter)
    _insert_h13_night(single_filter, staged, approvals)
    partial_script = single_filter / "workspace/pipelines/publish_handoff.py"
    partial_script.write_text(
        oracle_script.read_text(encoding="utf-8").replace(
            "('', 'ops_auto_draft', 'ops_ingestor', 'pending')",
            "('', 'ops_auto_draft', 'pending')"), encoding="utf-8")
    _run_pipeline(partial_script, single_filter, "publish")
    historical = staged["dates"]["historical"]
    assert _query(single_filter,
                  f"SELECT net_cents, correction_ref FROM {staged['physical_tables']['handoff']} "
                  "WHERE business_day = ?", [historical]) == [(2500, "INGEST-HOLD")]

    handoff = staged["physical_tables"]["handoff"]
    historical = staged["dates"]["historical"]
    sql = (f"SELECT net_cents, correction_ref FROM {handoff} "
           "WHERE business_day = ? ORDER BY entity_id")
    assert _query(shipped, sql, [historical]) == [(2500, "INGEST-HOLD")]
    assert _query(repaired, sql, [historical]) == [(1000, None)]


def test_temporal_hazards_share_two_system_holds(tmp_path: Path) -> None:
    _task, world, staged = _stage(tmp_path, "both-drafts", {
        "theme": "settlement_clearing", "stressors": ["S10"],
        "hazards": ["H08", "H10", "H12"],
        "governance_mode": "withhold-blocked",
        "seed": 120813,
    })
    with (world / "build/factory_future/R3/_approvals.csv").open(
            newline="", encoding="utf-8") as handle:
        approvals = list(csv.DictReader(handle))
    assert approvals == [{
        "business_day": staged["dates"]["historical"],
        "ticket": "DRAFT-HOLD", "approved_by": "ops_auto_draft",
    }, {
        "business_day": staged["dates"]["historical"],
        "ticket": "INGEST-HOLD", "approved_by": "ops_ingestor",
    }]
    audit = (world / "var/logs/ingest_daemon.log").read_text(encoding="utf-8")
    assert audit.count("corrections audit: automated system holds") == 1
    assert "(ops_auto_draft, ops_ingestor)" in audit
    assert "are not controller sign-off" in audit


@pytest.mark.parametrize("stressor,comment", [
    ("S06", "# Partner tier-B split adjustment (legacy feed v2 convention)."),
    ("S08", "# Partner tier-B fee retention rate (legacy feed v2 convention)."),
])
def test_cutover_fold_comments_have_no_log_pointer(stressor: str, comment: str) -> None:
    from tasks.factory.stressors.runtime_template import (
        PhysicalTables, render_modules, shipped_behavior,
    )

    tables = PhysicalTables("raw.events", "warehouse.fact", "warehouse.handoff",
                            "warehouse.closed", "warehouse.corrections",
                            "warehouse.checkpoint", "feed")
    source = render_modules(shipped_behavior([stressor], []), tables)["fold_ledger.py"]
    assert comment in source
    assert "var/logs/ingest_daemon.log" not in source


def test_s06_s08_cutover_and_h11_h14_clues_are_log_only(tmp_path: Path) -> None:
    world = tmp_path / "audit-clues"
    staged = stage_breadcrumbs(
        world, ["H11", "H14"], historical_day="2026-06-09", seed=19,
        stressor_ids=["S06", "S08"],
    )
    assert {row["id"] for row in staged} == {"H11", "H14"}
    assert all(row["files"] == ["var/logs/ingest_daemon.log"] for row in staged)
    ingest_log = (world / "var/logs/ingest_daemon.log").read_text(
        encoding="utf-8")
    assert len(ingest_log.splitlines()) >= 12
    assert "schema=v3" in ingest_log
    assert "retire the provisional" not in ingest_log
    assert any("schema=v3 amount_basis=net" in line and "split_adjustment=0" in line
               for line in ingest_log.splitlines())
    assert any("schema=v3 amount_basis=net" in line and "fee_adjustment=0" in line
               for line in ingest_log.splitlines())
    assert "Blank version means version 1" in ingest_log
    assert "UTC instant" in ingest_log
    assert "retire the provisional" not in ingest_log
    contract = world / "workspace/docs/partner_feed_contract.md"
    assert not contract.exists() or all(
        phrase not in contract.read_text(encoding="utf-8")
        for phrase in ("Blank version means version 1", "UTC instant")
    )


def test_only_uncertified_topologies_gain_precedence_stressor() -> None:
    from tasks.factory.compiler.blueprint_matrix import FAULT_TOPOLOGIES, generate_catalog

    for topology in ("FT01", "FT07"):
        assert "S04_delivered_at_beats_version_precedence" in (
            FAULT_TOPOLOGIES[topology]["t0_stressors"])
    preserved = {
        "FT02": ["S02", "S06", "S10"], "FT03": ["S03"],
        "FT04": ["S04", "S08", "S10"], "FT06": ["S06"],
        "FT08": ["S08", "S03"], "FT09": ["S09", "S10"],
        "FT10": ["S10", "S04", "S08"], "FT12": ["S02", "S08"],
    }
    certified = {100, 102, 193, 194, 195, 294, 484, 485, 486,
                 501, 677, 678, 792, 870, 1005, 1062}
    rows = [row for row in generate_catalog() if row["blueprint_index"] in certified]
    assert len(rows) == 16
    assert {row["fault_topology_id"] for row in rows} == set(preserved)
    for row in rows:
        assert row["t0_stressor_codes"] == preserved[row["fault_topology_id"]]


def test_ceiling_topologies_couple_closed_window_rewrite() -> None:
    from tasks.factory.compiler.blueprint_matrix import FAULT_TOPOLOGIES

    for topology in ("FT07", "FT11", "FT14", "FT15"):
        assert "S10_closed_window_unconditional_delete_reinsert" in (
            FAULT_TOPOLOGIES[topology]["t0_stressors"])


def test_visible_clues_are_logs_and_unspoiled_schedule(tmp_path: Path) -> None:
    world = tmp_path / "clues"
    staged = stage_breadcrumbs(
        world, list(HAZARD_IDS), historical_day="2026-06-09", seed=17,
        stressor_ids=["S03", "S06", "S07", "S08"],
    )
    assert {row["id"] for row in staged} == set(HAZARD_IDS)
    ingest_log = (world / "var/logs/ingest_daemon.log").read_text(encoding="utf-8")
    assert "platform-level settlement rows" in ingest_log
    assert "events-retry.csv repeated an event_id" in ingest_log
    assert "schema=v3 amount_basis=net" in ingest_log
    assert "split_adjustment=0" in ingest_log
    assert "fee_adjustment=0" in ingest_log
    assert "Blank version means version 1" in ingest_log
    assert "UTC instant" in ingest_log
    assert "retire the provisional" not in ingest_log
    assert all("var/logs/ingest_daemon.log" in row["files"] for row in staged)
    for name in ("partner_feed_contract.md", "ops_runbook.md"):
        body = (world / "workspace/docs" / name).read_text(encoding="utf-8")
        assert "\n## " not in body
        assert "raw.factory_events" not in body
        assert "entity_id IS NULL" not in body
        assert "Blank version means version 1" not in body
        assert "UTC instant" not in body
        for phrase in SPOILER_PHRASES:
            assert phrase not in body
        for filename in ("events-heartbeat.csv", "events-backfill.csv",
                         "events-retry.csv", "_delivery_manifest.csv",
                         "_checksums.sha256"):
            assert filename not in body
    contract = (world / "workspace/docs/partner_feed_contract.md").read_text(
        encoding="utf-8")
    assert "header name is what identifies a field" in contract

    sync_log = (world / "var/logs/sftp_sync.log").read_text(encoding="utf-8")
    for log in (ingest_log, sync_log):
        assert len(log.splitlines()) >= 12
    for filename in ("events-heartbeat.csv", "events-backfill.csv",
                     "_delivery_manifest.csv", "_checksums.sha256"):
        assert filename in ingest_log + sync_log

    _write_workspace_schedule(world, hazards=["H04"], stressors=["S09"])
    schedule = (world / "workspace/orchestration/schedule.yml").read_text(
        encoding="utf-8")
    assert schedule.startswith("# Nightly factory orchestration.\njobs:\n")
    assert "Scheduler contract:" not in schedule
    assert '    time: "21:50"' in schedule


def _verify_topology(blueprint: dict, root: Path) -> dict:
    import tempfile

    from tasks.factory.compiler.peer_materializer import materialize
    from tasks.factory import verify_all

    topology = blueprint["fault_topology_id"]
    base = root / topology
    task = materialize(blueprint, base / "tasks", scratch=base / "scratch")
    local_scratch = base / "verify_scratch"
    local_scratch.mkdir(parents=True, exist_ok=True)
    original_tempdir = tempfile.TemporaryDirectory

    def scoped_tempdir(*args, **kwargs):
        if kwargs.get("dir") == "/home/ubuntu/alkera_factory_1017":
            kwargs["dir"] = str(local_scratch)
        return original_tempdir(*args, **kwargs)

    tempfile.TemporaryDirectory = scoped_tempdir
    try:
        result = verify_all.verify_task(task, phase2=True, strict_phase2=True)
    finally:
        tempfile.TemporaryDirectory = original_tempdir
    arms = result["checks"].get("baselines", {}).get("arms", {})
    return {
        "topology": topology, "task": task.name, "ok": result["ok"],
        "checks": result["checks"],
        "oracle_20": arms.get("oracle", {}).get("total") == 20,
        "five_rivals_rejected": all(
            arms.get(name, {}).get("status") == "pass"
            for name in ("no_op", "partial_fix", "wrong_precedence",
                         "duplicate_counted", "boundary_violated")
        ),
    }


def test_sample_packages_cover_ft01_through_ft15(tmp_path: Path) -> None:
    from tasks.factory.compiler.blueprint_matrix import generate_catalog

    sample = {}
    for row in generate_catalog():
        sample.setdefault(row["fault_topology_id"], row)
    assert set(sample) == {f"FT{i:02d}" for i in range(1, 16)}
    results = []
    with ProcessPoolExecutor(max_workers=4, mp_context=get_context("spawn")) as pool:
        futures = {pool.submit(_verify_topology, row, tmp_path): topology
                   for topology, row in sorted(sample.items())}
        for future in as_completed(futures):
            results.append(future.result())
    failures = [row for row in results
                if not row["ok"] or not row["oracle_20"]
                or not row["five_rivals_rejected"]]
    assert not failures, json.dumps(failures, indent=2, default=str)
