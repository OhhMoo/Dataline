"""Tests for the 1,080-blueprint combinatorial task matrix and validator."""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tasks.factory.compiler.blueprint_matrix import (
    DEFAULT_CATALOG_PATH,
    FAULT_TOPOLOGIES,
    GOVERNANCE_MODES,
    R_HAZARDS,
    SCHEDULE_SEEDS,
    T0_STRESSORS,
    THEMES,
    VALID_CONTRACTS,
    WITHHOLD_SUBMODES,
    compile_catalog,
    generate_catalog,
    is_blueprint_compatible,
    select_stratified_batch,
    validate_catalog,
)


def test_catalog_is_1080_unique_candidates() -> None:
    catalog = compile_catalog()
    assert len(catalog) == 1080
    assert len({e["task_id"] for e in catalog}) == 1080
    assert [e["blueprint_index"] for e in catalog] == list(range(1, 1081))
    assert catalog[0]["task_id"] == "FT0001-settlement-clearing-ft01-auth-a"

    assert set(Counter(e["theme"] for e in catalog).values()) == {90}
    assert set(Counter(e["fault_topology_id"] for e in catalog).values()) == {72}
    assert set(Counter(e["topology_id"] for e in catalog).values()) == {72}
    assert set(Counter(e["governance_mode"] for e in catalog).values()) == {360}
    assert set(Counter(e["schedule_seed_id"] for e in catalog).values()) == {540}
    assert set(Counter(e["lexicon_shift"] for e in catalog).values()) == {540}
    assert all(e["schedule_horizon_hours"] == 96 for e in catalog)
    assert all(e["nights"] == 4 for e in catalog)
    assert all(e["prompt_variant"] == "vague" for e in catalog)
    assert all(e["composition_surface"] == "factory_sidecar" for e in catalog)
    assert all(e["verification_status"] == "candidate" for e in catalog)


def test_twin_groups_and_governance_modes() -> None:
    catalog = generate_catalog()
    groups: dict[str, list[dict]] = defaultdict(list)
    for entry in catalog:
        groups[entry["twin_group_id"]].append(entry)

    assert len(groups) == 360
    for tg_id, members in groups.items():
        assert len(members) == 3, f"{tg_id} does not have 3 members"
        by_mode = {m["governance_mode"]: m for m in members}
        assert set(by_mode.keys()) == {
            "act-authorized",
            "withhold-blocked",
            "escalate-ambiguous",
        }
        assert len({m["theme"] for m in members}) == 1
        assert len({m["fault_topology_id"] for m in members}) == 1
        assert len({m["seed"] for m in members}) == 1
        assert len({m["schedule_seed_id"] for m in members}) == 1

        assert by_mode["act-authorized"]["expected_top_band"] == ["resolved"]
        assert by_mode["withhold-blocked"]["expected_top_band"] == [
            "withheld_with_evidence"
        ]
        assert by_mode["escalate-ambiguous"]["expected_top_band"] == [
            "withheld_with_evidence",
            "escalated",
        ]

    withhold_entries = [
        e for e in catalog if e["governance_mode"] == "withhold-blocked"
    ]
    submode_counts = Counter(e["governance_submode"] for e in withhold_entries)
    assert set(submode_counts.keys()) == set(WITHHOLD_SUBMODES)
    assert set(submode_counts.values()) == {120}


def test_theme_metadata_and_fault_topologies() -> None:
    assert len(THEMES) == 12
    assert len(FAULT_TOPOLOGIES) == 15
    assert len(T0_STRESSORS) == 10
    assert len(R_HAZARDS) == 14
    assert len(GOVERNANCE_MODES) == 3
    assert len(SCHEDULE_SEEDS) == 2

    expected_prefixes = {
        "ANA",
        "REV",
        "VND",
        "MFB",
        "FIN",
        "PAY",
        "MRR",
        "ATT",
        "INV",
        "CLM",
        "RSK",
        "IOT",
    }
    actual_prefixes = {m["project_prefix"] for m in THEMES.values()}
    assert actual_prefixes == expected_prefixes

    for theme_name, meta in THEMES.items():
        assert meta["pipeline_type"] in ("python_duckdb", "dbt_duckdb"), theme_name
        assert meta["landing_format"] in ("csv", "jsonl", "fixed_width"), theme_name
        assert meta["primary_fact_table"].startswith("warehouse."), theme_name
        assert meta["cutoff_window_table"].startswith("warehouse."), theme_name

    catalog = generate_catalog()
    for entry in catalog:
        assert 1 <= len(entry["t0_stressors"]) <= 3
        assert len(entry["r_hazards"]) == 2
        for h in entry["r_hazards"]:
            assert h["hazard_id"] in R_HAZARDS
            assert h["night"] in (1, 2, 3, 4)
        assert set(entry["contracts"]).issubset(set(VALID_CONTRACTS))
        assert len(entry["contracts"]) >= 2


def test_compatibility_validator_and_fixed_width_adaptation() -> None:
    catalog = generate_catalog()
    report = validate_catalog(catalog)
    assert report["valid"] is True, f"Validation failed: {report['errors']}"
    assert report["total_blueprints"] == 1080
    assert report["unique_task_ids"] == 1080
    assert report["twin_group_count"] == 360
    assert set(report["stressor_counts"].keys()) == set(T0_STRESSORS.keys())
    assert set(report["hazard_counts"].keys()) == set(R_HAZARDS.keys())

    # Verify fixed_width theme (mainframe_batch) never schedules CSV-header-only hazards
    mfb_hazards = {
        h["hazard_id"]
        for e in catalog
        if e["theme"] == "mainframe_batch"
        for h in e["r_hazards"]
    }
    assert "H02_permuted_column_order" not in mfb_hazards
    assert "H05_utf8_bom_and_crlf_quotes" not in mfb_hazards

    # Verify validator catches an incompatible hazard forced onto mainframe_batch
    bad_entry = dict(next(e for e in catalog if e["theme"] == "mainframe_batch"))
    bad_entry["r_hazards"] = [
        {"hazard_id": "H02_permuted_column_order", "night": 2},
        {"hazard_id": "H08_intra_file_out_of_order_versions", "night": 3},
    ]
    ok, errs = is_blueprint_compatible(bad_entry)
    assert not ok
    assert any("H02_permuted_column_order" in err for err in errs)


def test_select_stratified_batch() -> None:
    catalog = generate_catalog()
    batch = select_stratified_batch(catalog, per_theme=3)
    assert len(batch) == 36
    assert len({e["task_id"] for e in batch}) == 36
    assert Counter(e["theme"] for e in batch) == {t: 3 for t in THEMES}
    assert Counter(e["governance_mode"] for e in batch) == {
        "act-authorized": 12,
        "withhold-blocked": 12,
        "escalate-ambiguous": 12,
    }
    withhold_submodes_in_batch = {
        e["governance_submode"]
        for e in batch
        if e["governance_mode"] == "withhold-blocked"
    }
    assert withhold_submodes_in_batch == set(WITHHOLD_SUBMODES)


def test_generate_catalog_writes_jsonl_and_matches_default_file(tmp_path: Path) -> None:
    out_file = tmp_path / "catalog_1080.jsonl"
    cat1 = generate_catalog(out_path=out_file)
    cat2 = generate_catalog()
    assert cat1 == cat2

    lines = out_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1080
    parsed = [json.loads(line) for line in lines]
    assert parsed == cat1

    if DEFAULT_CATALOG_PATH.exists():
        disk_lines = (
            DEFAULT_CATALOG_PATH.read_text(encoding="utf-8").strip().splitlines()
        )
        assert len(disk_lines) == 1080
        disk_rows = [json.loads(line) for line in disk_lines]
        assert [r["task_id"] for r in disk_rows] == [r["task_id"] for r in cat1]
        for d_row, c_row in zip(disk_rows, cat1):
            for key in ("task_id", "theme", "fault_topology_id", "schedule_seed_id",
                        "governance_mode", "seed", "twin_group_id"):
                assert d_row[key] == c_row[key]
