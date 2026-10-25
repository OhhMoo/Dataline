"""Shipped pipelines are single-path, and sealed hazards leave a t0 clue.

The composer stages a minimal DuckDB world. These tests do not require dbt.
Worlds are written outside the git repository.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tasks.factory.compiler.blueprint_matrix import FAULT_TOPOLOGIES
from tasks.factory.hazards.t0_breadcrumbs import HAZARD_IDS, SPOILER_PHRASES, stage_breadcrumbs
from tasks.factory.stressors.domain_schema_adapter import (
    DECLARED_FORBIDDEN, THEMES, load_all_bindings, load_theme_binding,
)
from tasks.factory.stressors.oracle_composer import _selection, compose_task
from tasks.factory.stressors.runtime_template import (
    MODULES, PhysicalTables, oracle_behavior, render_modules, shipped_behavior,
)

REPO = Path("/home/ubuntu/env_project").resolve()
SAFE_GLOB = 'events*.csv'
NAIVE_PART = ".csv.part"
VERSION_ORDER = "(event[4], event[1], event[0])"
DELIVERED_ORDER = "(event[1], event[0], event[4])"


def _outside(path: Path) -> None:
    resolved = path.resolve()
    if resolved == REPO or REPO in resolved.parents:
        raise AssertionError(f"world escaped into the repo: {resolved}")


def _ledger():
    try:
        from world import ledger, scheduler
    except ImportError:
        from world.world import ledger, scheduler
    return ledger, scheduler


def _hashes(world: Path, tables: list[str]) -> dict[str, str]:
    ledger, _ = _ledger()
    return {table: ledger.table_contents(world, table)["canonical_hash"] for table in tables}


def _play(task: Path, source: Path, script: Path | None, *, nights: bool,
          tables: list[str], future: list[str]) -> dict:
    _outside(task)
    destination = task / "runs" / (script.stem if script else "no_op")
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)
    if script is not None:
        result = subprocess.run(
            [sys.executable, str(script), str(destination)],
            capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if result.returncode:
            raise AssertionError(f"{script.name} failed\n{result.stdout}\n{result.stderr}")
    t0 = _hashes(destination, tables)
    played = []
    if nights:
        _, scheduler = _ledger()
        for number in range(1, 5):
            outcome = scheduler.advance(destination, 24)
            failed = [event for event in outcome["events"]
                      if event.get("kind") == "job"
                      and str(event.get("name", "")).startswith("factory_")
                      and event.get("status") != "success"]
            played.append({"night": number, "tables": _hashes(destination, future),
                           "failed_jobs": failed})
    return {"t0": t0, "nights": played}


def _assert_single_path(sources: dict[str, str]) -> None:
    joined = "\n".join(sources.values())
    assert "POLICY" not in joined
    assert "if POLICY[" not in joined
    for name, source in sources.items():
        has_safe = SAFE_GLOB in source
        has_naive = NAIVE_PART in source
        assert not (has_safe and has_naive), name
        assert not (VERSION_ORDER in source and DELIVERED_ORDER in source), name


def test_domain_bindings_follow_theme_contracts():
    bindings = load_all_bindings()
    assert set(bindings) == set(THEMES)
    settlement = bindings["settlement_clearing"]
    assert settlement.raw_table == "raw.payments_events"
    assert settlement.fact_table == "warehouse.fct_payments"
    assert settlement.handoff_table == "warehouse.settlement_handoff"
    assert settlement.native_scripts["ingest"] == "pipelines/load_payments.py"
    assert settlement.grain_keys
    commerce = bindings["commerce_analytics"]
    assert commerce.handoff_table is None
    assert commerce.close_table is None
    assert commerce.corrections_table is None
    assert commerce.raw_table == "raw.orders"
    assert commerce.fact_table == "warehouse.fct_orders"
    for binding in bindings.values():
        assert binding.grain_keys
        assert binding.raw_table not in DECLARED_FORBIDDEN
        assert binding.fact_table not in DECLARED_FORBIDDEN
        for name in binding.declared_tables():
            assert name not in DECLARED_FORBIDDEN
        assert "ingest_feed.py" in binding.modules


def test_rendered_shipped_modules_are_single_path():
    tables = PhysicalTables(
        raw="raw.payments_events", fact="warehouse.fct_payments",
        handoff="warehouse.settlement_handoff", close="warehouse.clearing_window",
        corrections="raw.settlement_corrections",
        checkpoint="warehouse.factory_checkpoint", feed_rel="data/payments_feed")
    shipped = render_modules(shipped_behavior(["S04"], ["H01", "H08"]), tables)
    assert set(shipped) == set(MODULES)
    _assert_single_path(shipped)
    assert NAIVE_PART in shipped["ingest_feed.py"]
    assert SAFE_GLOB not in shipped["ingest_feed.py"]
    assert DELIVERED_ORDER in shipped["fold_ledger.py"]
    assert VERSION_ORDER not in shipped["fold_ledger.py"]
    oracle = render_modules(oracle_behavior(), tables)
    _assert_single_path(oracle)
    assert SAFE_GLOB in oracle["ingest_feed.py"]
    assert NAIVE_PART not in oracle["ingest_feed.py"]
    assert VERSION_ORDER in oracle["fold_ledger.py"]
    assert DELIVERED_ORDER not in oracle["fold_ledger.py"]


def test_every_hazard_has_an_indirect_t0_breadcrumb(tmp_path):
    world = tmp_path / "clues"
    staged = stage_breadcrumbs(world, list(HAZARD_IDS), historical_day="2026-06-09", seed=7)
    assert [row["id"] for row in staged] == list(HAZARD_IDS)
    for row in staged:
        assert row["files"]
        for relative in row["files"]:
            path = world / relative
            assert path.is_file(), relative
            assert relative.startswith("workspace/docs/") or relative.startswith("var/logs/")
            text = path.read_text(encoding="utf-8")
            for phrase in SPOILER_PHRASES:
                assert phrase not in text
            assert "EV-FUT-" not in text
            assert "factory_future" not in text
    assert not (world / "workspace/build").exists()
    assert not (world / "build/factory_future").exists()


def test_composer_range_and_canonical_ids(tmp_path):
    one = tmp_path / "one"
    result = compose_task(
        "settlement_clearing",
        ["S01_checkpoint_calendar_advance_on_gap"],
        ["H01_delivery_manifest_sidecar"],
        "act-authorized", 11, one)
    assert result["stressor_ids"] == ["S01"]
    assert result["hazard_ids"] == ["H01"]
    world = Path(result["world"])
    _outside(world)
    pipeline = world / "workspace/pipelines"
    sources = {name: (pipeline / name).read_text(encoding="utf-8") for name in MODULES}
    _assert_single_path(sources)
    assert not (pipeline / "factory_events.py").exists()
    assert {row["id"] for row in result["breadcrumbs"]} == {"H01"}
    visible = []
    landing = Path(result["landing"])
    for path in landing.rglob("*"):
        if path.is_file():
            visible.append(path.read_text(encoding="utf-8", errors="replace"))
    for relative in ("workspace/docs", "var/logs"):
        root = world / relative
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                visible.append(path.read_text(encoding="utf-8", errors="replace"))
    blob = "\n".join(visible)
    assert "EV-FUT-" not in blob
    assert "factory_future" not in blob
    assert not any(path.name.endswith("_future") for path in (world / "workspace").rglob("*") if path.is_dir())
    assert list((world / "build/factory_future").glob("R*"))

    three = tmp_path / "three"
    wide = compose_task(
        "commerce_analytics",
        ["S02", "S05", "S10"],
        ["H03", "H11", "H14"],
        "withhold-blocked", 12, three)
    assert wide["stressor_ids"] == ["S02", "S05", "S10"]
    assert wide["hazard_ids"] == ["H03", "H11", "H14"]
    assert len(wide["modules"]) == 3
    selected = _selection({
        "stressors": ["S01_name"],
        "hazards": ["H01_name"],
        "governance_mode": "act-authorized",
        "seed": 1,
    })
    assert selected[0] == ["S01"]
    assert selected[1] == ["H01"]
    with pytest.raises(ValueError, match="1 to 3"):
        compose_task("settlement_clearing", ["S01"],
                     ["H01", "H02", "H03", "H04"], "act-authorized", 13,
                     tmp_path / "too-many")


def _topologies():
    rows = []
    for index, (key, topo) in enumerate(sorted(FAULT_TOPOLOGIES.items())):
        hazards = []
        for item in topo["r_hazards"]:
            hazards.append(item["hazard_id"] if isinstance(item, dict) else item)
        rows.append(pytest.param(
            topo["t0_stressors"], hazards, 2000 + index, id=key))
    return rows


@pytest.mark.parametrize("stressors,hazards,seed", _topologies())
def test_fault_topology_separates_oracle_noop_and_partial(stressors, hazards, seed, tmp_path):
    task = tmp_path / f"ft-{seed}"
    result = compose_task("settlement_clearing", stressors, hazards,
                          "act-authorized", seed, task)
    world = Path(result["world"])
    _outside(world)
    pipeline = world / "workspace/pipelines"
    sources = {name: (pipeline / name).read_text(encoding="utf-8") for name in MODULES}
    _assert_single_path(sources)
    crumb_ids = {row["id"] for row in result["breadcrumbs"]}
    assert crumb_ids == set(result["hazard_ids"])
    tables = result["t0_tables"]
    future = result["future_tables"]
    oracle = _play(task, world, task / "solution/solve.py", nights=True,
                   tables=tables, future=future)
    for night in oracle["nights"]:
        assert not night["failed_jobs"], night["failed_jobs"]
    no_op = _play(task, world, None, nights=False, tables=tables, future=future)
    assert no_op["t0"] != oracle["t0"]
    partial = _play(task, world, task / "solution/partial_fix.py", nights=True,
                    tables=tables, future=future)
    assert partial["t0"] == oracle["t0"]
    failed = [left["night"] for left, right in zip(oracle["nights"], partial["nights"])
              if left["tables"] != right["tables"]]
    assert set(failed) & {2, 3, 4}, f"partial matched oracle on R2-R4; nights={failed}"


def test_binding_loader_rejects_unknown_theme():
    with pytest.raises(ValueError, match="unknown theme"):
        load_theme_binding("settlement_platform")
