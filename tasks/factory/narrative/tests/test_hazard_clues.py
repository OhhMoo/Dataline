"""Hazard clues are discoverable, and the repair is not written down."""
import json

import pytest

from tasks.factory.governance.twin_state_machine import compile_twin
from tasks.factory.narrative.corecraft_synthesizer import (
    HAZARD_CLUES, write_narrative,
)
from tasks.factory.narrative.discoverability_and_spoiler_linter import lint_task

FORBIDDEN = (
    "ingest_feed.py", "fold_ledger.py", "publish_handoff.py", "factory_events.py",
    "factory_future", "_future", "decoy",
)


def test_positional_write_narrative_still_works(tmp_path):
    bundle = compile_twin("revenue_daily", "act-authorized", 3)
    written = write_narrative(tmp_path, bundle, "revenue_daily")
    assert (tmp_path / "prompt.md").is_file()
    assert written["workflow"].endswith("workflow")
    result = lint_task(tmp_path)
    assert result.ok, result.as_dict()
    assert "hazard_ids" not in json.loads((tmp_path / "tests" / "governance.json").read_text())


def test_explicit_hazard_ids_are_buried_and_lint_clean(tmp_path):
    bundle = compile_twin("sftp_vendor", "withhold-blocked", 4, submode="threshold_cap_exceeded")
    write_narrative(tmp_path, bundle, "sftp_vendor", hazard_ids=["H01", "H12_idempotent_rerun_double_trigger"])
    result = lint_task(tmp_path)
    assert result.ok, result.as_dict()
    assert set(result.hazard_clues["covered"]) >= {"H01", "H12"}
    text = "\n".join(path.read_text() for path in (tmp_path / "fixtures" / "workflow").glob("*.yaml"))
    assert HAZARD_CLUES["H01"]["marker"] in text
    assert HAZARD_CLUES["H12"]["marker"] in text
    assert "data-ops" in text and "ingest-troubleshooting" in text
    for forbidden in FORBIDDEN:
        assert forbidden not in text


def test_blueprint_hazard_ids_are_picked_up_without_the_argument(tmp_path):
    bundle = compile_twin("mainframe_batch", "escalate-ambiguous", 6)
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "blueprint.json").write_text(json.dumps({
        "hazards": ["H03_extra_nullable_columns", "H08"],
        "r_hazards": [{"hazard_id": "H03_extra_nullable_columns", "night": 2}],
    }))
    write_narrative(tmp_path, bundle, "mainframe_batch")
    result = lint_task(tmp_path)
    assert result.ok, result.as_dict()
    assert {"H03", "H08"} <= set(result.hazard_clues["covered"])


@pytest.mark.parametrize("code", sorted(HAZARD_CLUES))
def test_each_hazard_has_an_operational_clue(code):
    clue = HAZARD_CLUES[code]
    assert clue["marker"] in clue["chat"]
    assert clue["marker"] in clue["kb"]
    for forbidden in FORBIDDEN:
        assert forbidden not in clue["chat"]
        assert forbidden not in clue["kb"]
    assert ".py" not in clue["chat"] and ".py" not in clue["kb"]


def test_all_fourteen_hazards_lint_together(tmp_path):
    bundle = compile_twin("iot_telemetry_billing", "act-authorized", 11)
    write_narrative(tmp_path, bundle, "iot_telemetry_billing", hazard_ids=tuple(sorted(HAZARD_CLUES)))
    result = lint_task(tmp_path)
    assert result.ok, result.as_dict()
    assert set(result.hazard_clues["covered"]) == set(HAZARD_CLUES)


def test_missing_hazard_clue_fails_lint(tmp_path):
    bundle = compile_twin("supply_chain_inventory", "withhold-blocked", 9,
                          submode="conditional_prerequisite_unmet")
    write_narrative(tmp_path, bundle, "supply_chain_inventory", hazard_ids=["H01", "H06"])
    marker = HAZARD_CLUES["H01"]["marker"]
    for path in (tmp_path / "fixtures" / "workflow").glob("*.yaml"):
        path.write_text(path.read_text().replace(marker, "redacted"))
    result = lint_task(tmp_path)
    assert result.ok is False
    assert "hazard:H01" in result.missing
    assert "H06" in result.hazard_clues["covered"]


def test_factory_future_and_decoy_label_are_rejected(tmp_path):
    bundle = compile_twin("fintech_kyc_risk_limits", "act-authorized", 13)
    write_narrative(tmp_path, bundle, "fintech_kyc_risk_limits", hazard_ids=["H05"])
    prompt = tmp_path / "prompt.md"
    prompt.write_text(prompt.read_text() + "\nSee factory_future for the payload.\n")
    assert lint_task(tmp_path).ok is False
    prompt.write_text(prompt.read_text().replace("factory_future", "the landing folder"))
    issues = tmp_path / "fixtures" / "workflow" / "issues.yaml"
    issues.write_text(issues.read_text().replace("- close\n", "- decoy\n", 1))
    rejected = lint_task(tmp_path)
    assert rejected.ok is False
    assert any("decoy" in item for item in rejected.spoilers)
