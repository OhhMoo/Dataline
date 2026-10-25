"""Stage 0 selection and the compiler calls the Phase 2 lanes actually ship."""
from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

from tasks.factory.compiler.blueprint_matrix import (
    FAULT_TOPOLOGIES,
    GOVERNANCE_MODES,
    THEMES,
    generate_catalog,
)
from tasks.factory.compiler.peer_materializer import (
    _bind_composer_runtime,
    _bind_executed_schema,
    _expose_duplicate_counted,
    _stage_hazard_breadcrumbs_if_available,
    _write_boundary_rival,
    materialize,
    select_stage0_blueprints,
    stage0_coverage,
)


def test_stage0_selection_covers_twins_and_settlement_topologies() -> None:
    catalog = generate_catalog()
    selected, already = select_stage0_blueprints(catalog)
    coverage = stage0_coverage(selected, already)

    assert len(selected) == coverage["task_count"]
    assert {row["theme"] for row in selected} == set(THEMES)
    assert {row["governance_mode"] for row in selected} == set(GOVERNANCE_MODES)
    assert coverage["topologies_covered"] == len(FAULT_TOPOLOGIES)
    assert coverage["governance_modes_covered"] == 3
    assert len({row["task_id"] for row in selected}) == len(selected)

    stratified = selected[:36]
    assert len(stratified) == 36
    assert len({(row["theme"], row["governance_mode"]) for row in stratified}) == 36

    settlement_topologies = {
        row["fault_topology_id"] for row in selected
        if row["theme"] == "settlement_clearing" and row["governance_mode"] == "act-authorized"
    }
    assert settlement_topologies == set(FAULT_TOPOLOGIES)
    covered_ids = {item["fault_topology_id"] for item in already}
    added = selected[36:]
    assert covered_ids.isdisjoint({row["fault_topology_id"] for row in added})
    assert {row["schedule_seed_id"] for row in added} == {"H4_seedA"}
    assert all(row["theme"] == "settlement_clearing" for row in added)
    assert all(row["governance_mode"] == "act-authorized" for row in added)
    assert len(added) + len(already) == len(FAULT_TOPOLOGIES)


def test_breadcrumb_call_passes_historical_day(tmp_path: Path) -> None:
    blueprint = {
        "theme": "settlement_clearing",
        "seed": 11,
        "hazards": ["H02", "H08"],
        "closed_day": "2026-06-09",
    }
    written = _stage_hazard_breadcrumbs_if_available(tmp_path, blueprint)
    assert {item["id"] for item in written} == {"H02", "H08"}
    contract = (tmp_path / "workspace/docs/partner_feed_contract.md").read_text(encoding="utf-8")
    assert "2026-06-09" in (tmp_path / "var/logs/ingest_daemon.log").read_text(encoding="utf-8")
    assert "header name is what identifies a field" in contract
    assert "factory_future" not in contract


def test_executed_schema_uses_physical_feed_columns(tmp_path: Path) -> None:
    task = tmp_path / "task"
    (task / "tests").mkdir(parents=True)
    (task / "tests/factory_sidecar_spec.json").write_text(
        '{"adapter": "factory_sidecar", "tables": ["warehouse.fct_payments"]}\n',
        encoding="utf-8")
    composition = {"physical_tables": {
        "raw": "raw.factory_events",
        "fact": "warehouse.fct_factory_events",
        "handoff": "warehouse.factory_handoff",
        "close": "warehouse.factory_close",
        "corrections": "warehouse.factory_corrections",
        "checkpoint": "warehouse.factory_checkpoint",
    }}
    spec = _bind_executed_schema(task, composition)
    assert spec["day_column"] == "business_day"
    assert spec["handoff_table"] == "warehouse.factory_handoff"
    assert spec["corrections_table"] == "warehouse.factory_corrections"
    assert spec["amount_column"] == "net_cents"


def test_materialize_passes_hazard_ids_to_narrative() -> None:
    source = inspect.getsource(materialize)
    tree = ast.parse(source)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "write_narrative"
    ]
    assert len(calls) == 1
    keywords = {keyword.arg for keyword in calls[0].keywords}
    assert "hazard_ids" in keywords
    assert "variant" in keywords
    rival_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_write_boundary_rival"
    ]
    assert len(rival_calls) == 1


def test_duplicate_counted_fold_keeps_every_raw_event(tmp_path: Path) -> None:
    fold = tmp_path / "solution" / "duplicate_counted" / "fold_ledger.py"
    fold.parent.mkdir(parents=True)
    fold.write_text(
        "events = []\n"
        "        latest = {}\n"
        "        for event in events:\n"
        "            key = event[3]\n"
        "            order = (event[4], event[1], event[0])\n"
        "            if key not in latest or order > latest[key][0]:\n"
        "                latest[key] = (order, event)\n"
        "        selected = [value[1] for value in latest.values()]\n",
        encoding="utf-8",
    )
    _expose_duplicate_counted(tmp_path)
    text = fold.read_text(encoding="utf-8")
    assert "selected = list(events)" in text
    assert "latest[key]" not in text
    _expose_duplicate_counted(tmp_path)
    assert fold.read_text(encoding="utf-8") == text


def test_composer_runtime_binds_missing_duckdb() -> None:
    import tasks.factory.stressors.oracle_composer as composer

    previous = getattr(composer, "duckdb", None)
    composer.duckdb = None
    try:
        _bind_composer_runtime()
        assert composer.duckdb is not None
        assert callable(composer.duckdb.connect)
    finally:
        composer.duckdb = previous


def test_boundary_rival_dirties_closed_day_and_fact(tmp_path: Path) -> None:
    task = tmp_path / "task"
    (task / "tests").mkdir(parents=True)
    (task / "solution").mkdir(parents=True)
    (task / "tests/factory_sidecar_spec.json").write_text(json.dumps({
        "handoff_table": "warehouse.factory_handoff",
        "close_table": "warehouse.factory_close",
        "fact_table": "warehouse.fct_factory_events",
        "day_column": "business_day",
        "entity_column": "entity_id",
        "amount_column": "net_cents",
    }), encoding="utf-8")
    rival = task / "solution/boundary_violated.py"
    rival.write_text("#!/usr/bin/env python3\nprint('base rival')\n", encoding="utf-8")
    _write_boundary_rival(task)
    wrapped = rival.read_text(encoding="utf-8")
    assert "UPDATE warehouse.factory_handoff SET net_cents = net_cents + 1" in wrapped
    assert "UPDATE warehouse.fct_factory_events SET amount_cents = amount_cents + 1" in wrapped
    assert "var/factory/dirty_days.json" in wrapped
