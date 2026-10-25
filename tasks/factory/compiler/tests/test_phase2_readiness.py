"""Phase 2 must never scale from Phase 1 components by accident."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tasks.factory.compiler.blueprint_matrix import THEMES
from tasks.factory.compiler.phase2_readiness import (inspect_phase2_readiness,
                                                     phase2_fingerprint)
from tasks.factory.compiler.task_materializer import (_check_phase2_composition,
                                                      _phase2_theme_contract,
                                                      materialize)


def _write(root: Path, name: str, body: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _phase2_scaffold(root: Path) -> None:
    for owner in ("codex", "opus", "kimi", "grok"):
        _write(root, f"experiments/scale_1000/status/DONE_{owner}_phase2.json",
               '{"status":"complete"}')
    _write(root, "tasks/factory/stressors/runtime_template.py",
           "ingest_feed.py fold_ledger.py publish_handoff.py")
    _write(root, "tasks/factory/hazards/t0_breadcrumbs.py", "# breadcrumb")
    _write(root, "tasks/factory/stressors/domain_schema_adapter.py", "# schema")
    _write(root, "tasks/factory/grader/receipt_engine.py",
           " ".join(("q0_raw_loaded", "q1_t0_dedup_and_precedence",
                     "q2_t0_handoff_parity", "q3_r1_parity", "q4_r2_parity",
                     "q5_r3_parity", "q6_r4_parity")))
    for theme in THEMES:
        _write(root, f"world/world/themes/{theme}/theme_contract.yaml",
               "landing_dir: data/partner_feed\nraw_table: raw.example\n"
               "fact_table: warehouse.example_fact\n"
               "handoff_table: warehouse.example_handoff\n"
               "close_window_table: warehouse.example_close\n"
               "corrections_table: raw.example_corrections\n"
               "grain_keys: [event_id]\namount_column: amount\n"
               "version_column: version\ncutoff_time_utc: '23:30'\n"
               "scheduled_jobs: [partner_load]\n")
    _write(root, "tasks/factory/narrative/discoverability_and_spoiler_linter.py",
           "hazard clue")
    _write(root, "tasks/factory/governance/twin_state_machine.py", "sql_query # 3-hop")
    _write(root, "experiments/scale_1000/catalog_1080.jsonl",
           "".join(json.dumps({"task_id": f"FT{i:04d}"}) + "\n" for i in range(1080)))


def test_phase2_marker_alone_cannot_admit_phase1_runtime(tmp_path: Path) -> None:
    _phase2_scaffold(tmp_path)
    runtime = tmp_path / "tasks/factory/stressors/runtime_template.py"
    runtime.write_text("POLICY = __POLICY__\nif POLICY['dedup_events']:\n" +
                       runtime.read_text(encoding="utf-8"), encoding="utf-8")
    report = inspect_phase2_readiness(tmp_path)
    assert report["ready"] is False
    assert any("no_policy_oracle_branch" in blocker for blocker in report["blockers"])


def test_phase2_preflight_accepts_complete_structural_inputs(tmp_path: Path) -> None:
    _phase2_scaffold(tmp_path)
    assert inspect_phase2_readiness(tmp_path)["ready"] is True
    status = tmp_path / "experiments/scale_1000/status/DONE_opus_phase2.json"
    status.write_text('{"status":"complete_with_limits"}', encoding="utf-8")
    report = inspect_phase2_readiness(tmp_path)
    assert report["ready"] is False
    assert any("opus.completion" in blocker for blocker in report["blockers"])


def test_codex_completion_marker_cannot_certify_sidecar_fallback(tmp_path: Path) -> None:
    _phase2_scaffold(tmp_path)
    marker = tmp_path / "experiments/scale_1000/status/DONE_codex_phase2.json"
    marker.write_text(json.dumps({"status": "complete", "shipped_module_contract": {
        "fallback": "built native tables execute on the factory sidecar"}}))
    report = inspect_phase2_readiness(tmp_path)
    assert report["ready"] is False
    assert any("codex.native_execution" in blocker for blocker in report["blockers"])


def test_phase2_preflight_preserves_optional_null_and_rejects_missing_key(tmp_path: Path) -> None:
    _phase2_scaffold(tmp_path)
    contract = tmp_path / "world/world/themes/revenue_daily/theme_contract.yaml"
    contract.write_text(contract.read_text().replace(
        "handoff_table: warehouse.example_handoff", "handoff_table: null"))
    assert inspect_phase2_readiness(tmp_path)["ready"] is True
    contract.write_text(contract.read_text().replace("handoff_table: null\n", ""))
    report = inspect_phase2_readiness(tmp_path)
    assert report["ready"] is False
    assert report["checks"]["kimi"]["complete_bindings"]["incomplete"]["revenue_daily"] == [
        "handoff_table"]


def test_materializer_fails_before_writing_without_phase2_inputs(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import tasks.factory.compiler.phase2_readiness as p2r
    monkeypatch.setattr(
        p2r, "inspect_phase2_readiness",
        lambda *a, **kw: {"ready": False, "blockers": ["codex.completion: missing Phase 2 completion marker"]})
    out_root = tmp_path / "tasks"
    with pytest.raises(RuntimeError, match="Phase 2 upstream inputs are incomplete"):
        materialize({"task_id": "test", "theme": "revenue_daily",
                     "governance_mode": "act-authorized", "seed": 1},
                    out_root, phase2=True)
    assert not out_root.exists()


def test_composition_rejects_native_spec_with_sidecar_execution(tmp_path: Path) -> None:
    task, world = tmp_path / "task", tmp_path / "world"
    pipeline = world / "workspace/pipelines"
    pipeline.mkdir(parents=True)
    for module in ("ingest_feed.py", "fold_ledger.py", "publish_handoff.py"):
        (pipeline / module).write_text("pass\n", encoding="utf-8")
    tests = task / "tests"
    tests.mkdir(parents=True)
    tables = ["raw.orders", "warehouse.fct_orders", "warehouse.orders_handoff"]
    (tests / "factory_sidecar_spec.json").write_text(json.dumps({
        "tables": tables, "future_tables": tables}))
    contract = {"raw_table": tables[0], "fact_table": tables[1],
                "handoff_table": tables[2]}
    composition = {"physical_tables": {"raw": "raw.factory_events",
                                        "fact": tables[1], "handoff": tables[2]}}
    with pytest.raises(RuntimeError, match="raw feed executes"):
        _check_phase2_composition(task, world, contract, composition)


def test_composition_requires_executed_and_graded_native_tables(tmp_path: Path) -> None:
    task, world = tmp_path / "task", tmp_path / "world"
    pipeline = world / "workspace/pipelines"
    pipeline.mkdir(parents=True)
    for module in ("ingest_feed.py", "fold_ledger.py", "publish_handoff.py"):
        (pipeline / module).write_text("pass\n", encoding="utf-8")
    tests = task / "tests"
    tests.mkdir(parents=True)
    physical = {"raw": "raw.payments_events", "fact": "warehouse.fct_payments",
                "handoff": "warehouse.settlement_handoff",
                "close": "warehouse.clearing_window",
                "corrections": "raw.settlement_corrections"}
    tables = list(physical.values())
    contract = {"raw_table": physical["raw"], "fact_table": physical["fact"],
                "handoff_table": physical["handoff"],
                "close_window_table": physical["close"],
                "corrections_table": physical["corrections"]}
    spec = {"tables": tables, "execution_tables": tables[:3],
            "future_tables": tables[:3]}
    (tests / "factory_sidecar_spec.json").write_text(json.dumps(spec))
    _check_phase2_composition(task, world, contract, {"physical_tables": physical})
    spec["future_tables"][0] = "raw.factory_events"
    (tests / "factory_sidecar_spec.json").write_text(json.dumps(spec))
    with pytest.raises(RuntimeError, match="future replay"):
        _check_phase2_composition(task, world, contract, {"physical_tables": physical})


def test_theme_binding_preserves_explicitly_absent_native_surfaces() -> None:
    contract = _phase2_theme_contract("revenue_daily")
    assert contract["raw_table"] == "raw.daily_revenue"
    assert contract["fact_table"] == "warehouse.fct_revenue"
    assert contract["handoff_table"] is None
    assert contract["close_window_table"] is None
    assert contract["corrections_table"] is None
    assert contract["version_column"] is None


def test_compiler_binding_change_invalidates_pilot_package(tmp_path: Path) -> None:
    _phase2_scaffold(tmp_path)
    blueprint = {"task_id": "FT0001", "theme": "settlement_clearing"}
    before = phase2_fingerprint(blueprint, tmp_path)
    _write(tmp_path, "tasks/factory/compiler/phase2_manifest_binding.py",
           "# changed native probe binding\n")
    assert phase2_fingerprint(blueprint, tmp_path) != before
