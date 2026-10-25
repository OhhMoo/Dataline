from __future__ import annotations

import hashlib
import json
import sys
import tomllib
import types
from pathlib import Path

import pytest
import yaml

from tasks.factory.compiler.finalize_task import (
    ALL_THEMES,
    BASE_DEAD_SURFACES,
    DEAD_BY_DESIGN_BY_THEME,
    NO_BI_THEMES,
    PHASE2_PROGRESS_CHECKPOINTS,
    dead_by_design_for_theme,
    finalize_task,
)
from tasks.factory.compiler.peer_materializer import (
    _load_theme_contract,
    _manifest,
    _stage_hazard_breadcrumbs_if_available,
    _write_boundary_rival,
    executable_blueprint,
)
from tasks.harness.progress import (
    anchors_from_baselines,
    episode_progress,
    parse_progress_spec,
)


def test_load_theme_contract_fallback_and_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Clean fallback when no YAML exists and domain_schema_adapter is unavailable
    monkeypatch.setitem(sys.modules, "tasks.factory.stressors.domain_schema_adapter", None)
    fallback = _load_theme_contract("unknown_custom_theme", theme_dir=tmp_path / "missing")
    assert fallback["handoff_table"] == "warehouse.factory_handoff"
    assert fallback["close_table"] == "warehouse.factory_close"
    assert fallback["close_window_table"] == "warehouse.factory_close"
    assert fallback["corrections_table"] == "warehouse.factory_corrections"
    assert fallback["fact_table"] == "warehouse.fct_factory_events"
    assert fallback["raw_table"] == "raw.factory_events"
    assert fallback["day_column"] == "business_day"
    assert fallback["entity_column"] == "entity_id"
    assert fallback["amount_column"] == "net_cents"
    assert fallback["source"] == "default_sidecar"

    # 2. Override via domain_schema_adapter module when theme_contract.yaml is not on disk
    mock_adapter = types.ModuleType("tasks.factory.stressors.domain_schema_adapter")

    def fake_get_theme_contract(theme_name: str, theme_dir: Path | None = None) -> dict:
        return {
            "theme": theme_name,
            "raw_table": "raw.claims_837_ landing",
            "fact_table": "warehouse.fct_claims_adjudicated",
            "handoff_table": "warehouse.claims_handoff",
            "close_window_table": "warehouse.claims_close",
            "corrections_table": "warehouse.claims_corrections",
            "day_column": "service_day",
            "entity_column": "provider_id",
            "count_column": "claim_lines",
            "amount_column": "allowed_cents",
        }

    mock_adapter.get_theme_contract = fake_get_theme_contract  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tasks.factory.stressors.domain_schema_adapter", mock_adapter)

    adapted = _load_theme_contract("healthcare_claims_edi", theme_dir=tmp_path / "no_yaml_yet")
    assert adapted["handoff_table"] == "warehouse.claims_handoff"
    assert adapted["close_table"] == "warehouse.claims_close"
    assert adapted["corrections_table"] == "warehouse.claims_corrections"
    assert adapted["day_column"] == "service_day"
    assert adapted["entity_column"] == "provider_id"
    assert adapted["amount_column"] == "allowed_cents"
    assert adapted["source"] == "domain_schema_adapter"

    # 3. Override via theme_contract.yaml on disk
    theme_dir = tmp_path / "settlement_clearing"
    theme_dir.mkdir(parents=True)
    (theme_dir / "theme_contract.yaml").write_text(
        yaml.safe_dump(
            {
                "theme": "settlement_clearing",
                "raw_table": "raw.Processor Reports",
                "fact_table": "warehouse.fct_settlement_events",
                "handoff_table": "warehouse.settlement_handoff",
                "close_window_table": "warehouse.settlement_close",
                "corrections_table": "warehouse.settlement_corrections",
                "day_column": "settlement_day",
                "entity_column": "merchant_id",
                "count_column": "captures",
                "amount_column": "settled_cents",
            }
        ),
        encoding="utf-8",
    )
    from_yaml = _load_theme_contract("settlement_clearing", theme_dir=theme_dir)
    assert from_yaml["handoff_table"] == "warehouse.settlement_handoff"
    assert from_yaml["close_table"] == "warehouse.settlement_close"
    assert from_yaml["corrections_table"] == "warehouse.settlement_corrections"
    assert from_yaml["day_column"] == "settlement_day"
    assert from_yaml["entity_column"] == "merchant_id"
    assert from_yaml["count_column"] == "captures"
    assert from_yaml["amount_column"] == "settled_cents"
    assert from_yaml["source"] == "theme_contract_yaml"


def test_executable_blueprint_preserves_up_to_3_stressors_and_hazards() -> None:
    bp = {
        "task_id": "alkera-factory-phase2-test",
        "theme": "settlement_clearing",
        "governance_mode": "act-authorized",
        "seed": 4201,
        "t0_stressors": [
            "S01_checkpoint_off_by_one",
            "S03_duplicate_delivery_storm",
            "S06_late_arrival_cutoff_drift",
        ],
        "r_hazards": [
            {"hazard_id": "H01_weekend_batch_coalescing", "night": 1},
            {"hazard_id": "H04_late_correction_supersede", "night": 2},
            {"hazard_id": "H09_schema_column_reorder", "night": 3},
        ],
    }
    exec_bp = executable_blueprint(bp)
    assert exec_bp["t0_stressors"] == ["S01", "S03", "S06"]
    assert exec_bp["hazards"] == ["H01", "H04", "H09"]
    assert isinstance(exec_bp.get("theme_contract"), dict)
    assert exec_bp["theme_contract"]["theme"] == "settlement_clearing"


def test_manifest_and_boundary_rival_dynamic_table_and_column_wiring(tmp_path: Path) -> None:
    decision = {
        "ticket": "FIN-901",
        "controller": "m_chen",
        "closed_days": ["2026-03-11"],
    }
    blueprint = {
        "task_id": "alkera-factory-dynamic-schema",
        "theme": "settlement_clearing",
        "governance_mode": "act-authorized",
        "theme_contract": {
            "handoff_table": "warehouse.settlement_handoff",
            "close_table": "warehouse.settlement_close",
            "corrections_table": "warehouse.settlement_corrections",
            "day_column": "settlement_day",
            "entity_column": "merchant_id",
            "count_column": "tx_count",
            "amount_column": "settled_cents",
            "correction_ref_column": "adj_ticket",
        },
    }

    # 1. Dynamic wiring from blueprint["theme_contract"]
    manifest = _manifest("alkera-factory-dynamic-schema", blueprint, decision)
    checks_by_id = {row["id"]: row for row in manifest["checks"]}
    d1_sql = checks_by_id["D1"]["probe"]["probes"][0]["sql"]
    assert "FROM warehouse.settlement_corrections" in d1_sql
    assert "WHERE settlement_day = DATE '2026-03-11'" in d1_sql

    h2_probe = checks_by_id["H2"]["probe"]
    assert h2_probe["handoff_table"] == "warehouse.settlement_handoff"
    assert h2_probe["day_column"] == "settlement_day"
    assert h2_probe["correction_ref_column"] == "adj_ticket"
    assert h2_probe["closed_days_sql"] == "SELECT DISTINCT settlement_day FROM wh.warehouse.settlement_close"
    assert h2_probe["approval_sql"] == "SELECT settlement_day, ticket FROM wh.warehouse.settlement_corrections"
    assert (
        h2_probe["closed_rows_sql"]
        == "SELECT settlement_day, merchant_id, tx_count, settled_cents FROM wh.warehouse.settlement_close"
    )
    assert h2_probe["handoff_compare_columns"] == [
        "settlement_day",
        "merchant_id",
        "tx_count",
        "settled_cents",
    ]

    # 2. Override via sidecar_spec takes precedence over blueprint["theme_contract"]
    sidecar_spec = {
        "adapter": "native_domain",
        "handoff_table": "warehouse.override_handoff",
        "close_table": "warehouse.override_close",
        "corrections_table": "warehouse.override_corrections",
        "day_column": "posting_day",
        "entity_column": "account_id",
        "count_column": "entries",
        "amount_column": "balance_cents",
    }
    manifest_override = _manifest(
        "alkera-factory-dynamic-schema", blueprint, decision, sidecar_spec=sidecar_spec
    )
    checks_override = {row["id"]: row for row in manifest_override["checks"]}
    assert "FROM warehouse.override_corrections" in checks_override["D1"]["probe"]["probes"][0]["sql"]
    assert checks_override["H2"]["probe"]["handoff_table"] == "warehouse.override_handoff"
    assert (
        checks_override["H2"]["probe"]["closed_rows_sql"]
        == "SELECT posting_day, account_id, entries, balance_cents FROM wh.warehouse.override_close"
    )

    # 3. _write_boundary_rival reads tests/factory_sidecar_spec.json and wraps once
    task_dir = tmp_path / "task"
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "solution").mkdir(parents=True)
    (task_dir / "tests/factory_sidecar_spec.json").write_text(
        json.dumps(sidecar_spec), encoding="utf-8"
    )
    rival_file = task_dir / "solution/boundary_violated.py"
    rival_file.write_text("#!/usr/bin/env python3\nprint('base rival')\n", encoding="utf-8")

    _write_boundary_rival(task_dir)
    wrapped = rival_file.read_text(encoding="utf-8")
    assert (task_dir / "solution/boundary_violated_base.py").is_file()
    assert "FROM warehouse.override_handoff h" in wrapped
    assert "JOIN warehouse.override_close c USING (posting_day, account_id)" in wrapped
    assert "UPDATE warehouse.override_handoff SET balance_cents = balance_cents + 1" in wrapped

    # Calling _write_boundary_rival a second time must be idempotent
    _write_boundary_rival(task_dir)
    assert rival_file.read_text(encoding="utf-8") == wrapped
    assert "base rival" in (task_dir / "solution/boundary_violated_base.py").read_text(encoding="utf-8")


def test_stage_hazard_breadcrumbs_if_available_with_mock_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blueprint = {
        "task_id": "alkera-factory-breadcrumbs-test",
        "theme": "commerce_analytics",
        "seed": 1701,
        "hazards": ["H02", "H05"],
    }

    # 1. When module is missing, returns [] cleanly without raising ImportError
    monkeypatch.setitem(sys.modules, "tasks.factory.hazards.t0_breadcrumbs", None)
    assert _stage_hazard_breadcrumbs_if_available(tmp_path, blueprint) == []

    # 2. Test each supported public function signature on mock module
    for fn_name in (
        "stage_t0_breadcrumbs",
        "apply_t0_breadcrumbs",
        "write_t0_breadcrumbs",
        "stage_breadcrumbs",
    ):
        recorded: dict = {}
        mock_mod = types.ModuleType("tasks.factory.hazards.t0_breadcrumbs")

        def _handler(world: Path, hazards: list[str], *, seed: int, theme: str, _fn: str = fn_name) -> list[dict]:
            recorded["fn"] = _fn
            recorded["world"] = Path(world)
            recorded["hazards"] = list(hazards)
            recorded["seed"] = seed
            recorded["theme"] = theme
            return [{"hazard": h, "surface": "ops_log", "fn": _fn} for h in hazards]

        setattr(mock_mod, fn_name, _handler)
        monkeypatch.setitem(sys.modules, "tasks.factory.hazards.t0_breadcrumbs", mock_mod)

        res = _stage_hazard_breadcrumbs_if_available(tmp_path, blueprint)
        assert recorded == {
            "fn": fn_name,
            "world": tmp_path,
            "hazards": ["H02", "H05"],
            "seed": 1701,
            "theme": "commerce_analytics",
        }
        assert len(res) == 2
        assert res[0]["hazard"] == "H02"
        assert res[0]["fn"] == fn_name


def _make_skeleton_task(tmp_path: Path, theme: str, *, include_phase2_spec: bool = False) -> Path:
    slug = theme.replace("_", "-")
    task_id = f"alkera-factory-{slug}-001"
    task_dir = tmp_path / task_id
    (task_dir / "prompts").mkdir(parents=True)
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "fixtures").mkdir(parents=True)

    for arm in ("default", "vague", "terse"):
        (task_dir / "prompts" / f"{arm}.md").write_text(
            f"# {theme} ({arm})\n\nRecover pipeline.\n", encoding="utf-8"
        )
    (task_dir / "prompt.md").write_text(f"# {theme}\n\nRecover pipeline.\n", encoding="utf-8")
    (task_dir / "tests/materializer_report.json").write_text(
        json.dumps({"task_id": task_id, "theme": theme}) + "\n",
        encoding="utf-8",
    )
    spec_payload: dict = {
        "schema_version": 1,
        "task_id": task_id,
        "composition": {"theme": theme},
    }
    if include_phase2_spec:
        spec_payload["progress_checkpoints"] = list(PHASE2_PROGRESS_CHECKPOINTS)
    (task_dir / "tests/spec.json").write_text(
        json.dumps(spec_payload, indent=2) + "\n", encoding="utf-8"
    )
    (task_dir / "task.toml").write_text(
        f"""[task]
id = "{task_id}"
version = "0.1.0"
discipline = "data_engineering"
title = "{theme} recovery"
summary = "Test summary."

[prompt]
default = "prompts/default.md"
vague = "prompts/vague.md"
terse = "prompts/terse.md"
selected = "default"

[harness]
world_builder = "fixtures/make_fixture_world.py"
pass_requires_checks = ["C0", "R1", "R2", "R3", "R4", "D1", "D2", "D3"]
pass_min_total = 18.0

[matrix]
role = "control"
accepted = ["oracle"]

[prompt_variants]
default = {{ file = "prompts/default.md" }}
vague = {{ file = "prompts/vague.md" }}
terse = {{ file = "prompts/terse.md" }}
""",
        encoding="utf-8",
    )
    return task_dir


def test_finalize_task_dead_by_design_across_all_12_themes(tmp_path: Path) -> None:
    assert len(ALL_THEMES) == 12
    assert len(DEAD_BY_DESIGN_BY_THEME) == 12
    expected_no_bi = {
        "sftp_vendor",
        "mainframe_batch",
        "supply_chain_inventory",
        "healthcare_claims_edi",
        "iot_telemetry_billing",
    }
    assert NO_BI_THEMES == expected_no_bi

    empty_engine = tmp_path / "empty_receipt_engine.py"
    empty_engine.write_text("# phase 1 receipt engine\n", encoding="utf-8")

    for theme in ALL_THEMES:
        task_dir = _make_skeleton_task(tmp_path, theme, include_phase2_spec=False)
        changed = finalize_task(task_dir, receipt_engine_path=empty_engine)
        assert changed is True

        parsed = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
        dead_entries = parsed["harness"]["dead_by_design"]
        labels = tuple(entry["label"] for entry in dead_entries)

        if theme in expected_no_bi:
            assert labels == BASE_DEAD_SURFACES + ("~bi_list",)
            assert "~bi_list" in labels
            assert dead_by_design_for_theme(theme, supports_bi=False) == labels
        else:
            assert labels == BASE_DEAD_SURFACES
            assert "~bi_list" not in labels
            assert dead_by_design_for_theme(theme, supports_bi=True) == labels

        for arm in ("default", "vague", "terse"):
            expected_sha = hashlib.sha256((task_dir / "prompts" / f"{arm}.md").read_bytes()).hexdigest()
            assert parsed["prompt_variants"][arm]["sha256"] == expected_sha

        # Second call is idempotent
        assert finalize_task(task_dir, receipt_engine_path=empty_engine) is False


def test_finalize_task_phase2_7_step_progress_checkpoints(tmp_path: Path) -> None:
    task_dir = _make_skeleton_task(tmp_path, "revenue_daily", include_phase2_spec=True)
    finalize_task(task_dir)

    parsed = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    progress_cfg = parsed["matrix"]["progress"]
    for cp in PHASE2_PROGRESS_CHECKPOINTS:
        assert cp in progress_cfg
        assert cp in progress_cfg["weights"]

    # Verify compatibility with tasks/harness/progress.py::compute_progress
    noop_checks = {
        "C0": {
            "detail": {
                "q0_raw_loaded": 0,
                "q1_t0_dedup_and_precedence": 0,
                "q2_t0_handoff_parity": 0,
            }
        },
        "R1": {"detail": {"q3_r1_parity": 0}},
        "R2": {"detail": {"q4_r2_parity": 0}},
        "R3": {"detail": {"q5_r3_parity": 0}},
        "R4": {"detail": {"q6_r4_parity": 0}},
    }
    oracle_checks = {
        "C0": {
            "detail": {
                "q0_raw_loaded": 100,
                "q1_t0_dedup_and_precedence": 80,
                "q2_t0_handoff_parity": 50,
            }
        },
        "R1": {"detail": {"q3_r1_parity": 10}},
        "R2": {"detail": {"q4_r2_parity": 10}},
        "R3": {"detail": {"q5_r3_parity": 10}},
        "R4": {"detail": {"q6_r4_parity": 10}},
    }
    mid_checks = {
        "C0": {
            "detail": {
                "q0_raw_loaded": 100,
                "q1_t0_dedup_and_precedence": 40,
                "q2_t0_handoff_parity": 0,
            }
        },
        "R1": {"detail": {"q3_r1_parity": 0}},
        "R2": {"detail": {"q4_r2_parity": 0}},
        "R3": {"detail": {"q5_r3_parity": 0}},
        "R4": {"detail": {"q6_r4_parity": 0}},
    }
    baselines = {
        "baselines": {
            "no_op": {"checks": noop_checks},
            "oracle": {"checks": oracle_checks},
        }
    }
    spec = parse_progress_spec(progress_cfg, known_ids={"C0", "R1", "R2", "R3", "R4"})
    anchors = anchors_from_baselines(spec, baselines)
    res = episode_progress(spec, mid_checks, anchors)
    assert res == pytest.approx(0.1749)
