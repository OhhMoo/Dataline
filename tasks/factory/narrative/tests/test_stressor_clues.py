"""Floor-stressor clues stay indirect, and S07/S09 must be discoverable."""
import inspect
import json

import yaml

from tasks.factory.governance.twin_state_machine import compile_twin, decision_valid
from tasks.factory.narrative.corecraft_synthesizer import (
    STRESSOR_CLUES, write_narrative,
)
from tasks.factory.narrative.discoverability_and_spoiler_linter import lint_task

FORBIDDEN = (
    "ingest_feed", "fold_ledger", "publish_handoff", "factory_events.py",
    "factory_future", "_future", "decoy",
)
WORKFLOW_SPEC = {
    "dir": "workflow", "actor": "agent",
    "project": {"key": "FAC", "name": "Factory"},
    "statuses": ["approved", "in_progress", "wontfix", "draft", "review"],
    "workspace": "factory", "space": {"key": "FAC", "name": "Factory"},
}


def _channels(task):
    loaded = yaml.safe_load((task / "fixtures" / "workflow" / "chat.yaml").read_text())
    return {channel["id"]: channel for channel in loaded["channels"]}


def _channel_text(task, channel_id):
    channel = _channels(task)[channel_id]
    return "\n".join(message["text"] for message in channel["messages"])


def _kb_text(task):
    loaded = yaml.safe_load((task / "fixtures" / "workflow" / "kb.yaml").read_text())
    return "\n".join(page["body"] for page in loaded["pages"])


def _visible(task):
    return "\n".join(path.read_text() for path in (task / "fixtures" / "workflow").glob("*.yaml"))


def _redact(task, marker):
    """Remove a clue phrase from workflow scalars, including lines YAML wrapped."""
    def walk(node):
        if isinstance(node, str):
            return node.replace(marker, "redacted")
        if isinstance(node, dict):
            return {key: walk(value) for key, value in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    for path in (task / "fixtures" / "workflow").glob("*.yaml"):
        loaded = yaml.safe_load(path.read_text())
        path.write_text(yaml.safe_dump(walk(loaded), sort_keys=False, allow_unicode=False),
                        encoding="utf-8")


def test_write_narrative_accepts_optional_stressors():
    signature = inspect.signature(write_narrative)
    assert list(signature.parameters)[:3] == ["task_dir", "bundle", "theme"]
    for name in ("variant", "hazard_ids", "stressors"):
        parameter = signature.parameters[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["variant"].default == "default"
    assert signature.parameters["hazard_ids"].default is None
    assert signature.parameters["stressors"].default is None


def test_positional_call_still_omits_stressor_ids(tmp_path):
    bundle = compile_twin("excel_close", "act-authorized", 2)
    write_narrative(tmp_path, bundle, "excel_close")
    decision = json.loads((tmp_path / "tests" / "governance.json").read_text())
    assert "stressor_ids" not in decision
    assert "hazard_ids" not in decision
    result = lint_task(tmp_path)
    assert result.ok, result.as_dict()
    assert result.stressor_clues["covered"] == []
    assert result.hazard_clues["covered"] == []


def test_caller_stressors_land_in_the_named_channels(tmp_path):
    bundle = compile_twin("commerce_analytics", "act-authorized", 5)
    write_narrative(
        tmp_path, bundle, "commerce_analytics",
        stressors=["S09_scheduler_dag_dependency_inverted", "S07", "S03"],
    )
    alerts = _channel_text(tmp_path, "platform-alerts")
    ops = _channel_text(tmp_path, "data-ops")
    kb = _kb_text(tmp_path)
    assert "about 10 minutes before the 22:00 UTC feed loader finished" in alerts
    assert "orchestration/schedule.yml pinned factory_publish at 21:50 instead of after factory_load" in alerts
    assert STRESSOR_CLUES["S09"]["marker"] not in ops
    assert "blank entity_id" in ops
    assert "unattributed house bucket" in ops
    assert "fct_factory_events" in ops and "factory_handoff" in ops
    assert "raw.factory_events is unique on event_id before folding, including retry files." in ops
    for code in ("S09", "S07", "S03"):
        assert STRESSOR_CLUES[code]["marker"] in kb
        assert STRESSOR_CLUES[code]["marker"] in STRESSOR_CLUES[code]["chat"]
        assert STRESSOR_CLUES[code]["marker"] in STRESSOR_CLUES[code]["kb"]
    visible = _visible(tmp_path)
    for forbidden in FORBIDDEN:
        assert forbidden not in visible
    decision = json.loads((tmp_path / "tests" / "governance.json").read_text())
    assert decision["stressor_ids"] == ["S09", "S07", "S03"]
    assert decision_valid(bundle)


def test_omitted_stressors_are_read_from_the_blueprint(tmp_path):
    bundle = compile_twin("revenue_daily", "withhold-blocked", 8, submode="delegation_expired_or_revoked")
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "blueprint.json").write_text(json.dumps({
        "t0_stressors": ["S09_scheduler_dag_dependency_inverted", "S10_closed_window"],
        "t0_stressor_codes": ["S09", "S10"],
        "hazards": ["H04_part_and_temp_files"],
    }))
    write_narrative(tmp_path, bundle, "revenue_daily")
    result = lint_task(tmp_path)
    assert result.ok, result.as_dict()
    assert result.stressor_clues["covered"] == ["S09"]
    assert "H04" in result.hazard_clues["covered"]
    assert STRESSOR_CLUES["S09"]["marker"] in _channel_text(tmp_path, "platform-alerts")
    assert "unique on event_id" not in _visible(tmp_path)
    assert decision_valid(bundle)


def test_bundle_t0_stressors_are_used_when_the_argument_is_omitted(tmp_path):
    bundle = compile_twin("healthcare_claims_edi", "escalate-ambiguous", 6)
    bundle["t0_stressors"] = ["S07_quality_gate_inner_join_drops_valid_null_fk"]
    write_narrative(tmp_path, bundle, "healthcare_claims_edi")
    assert STRESSOR_CLUES["S07"]["marker"] in _channel_text(tmp_path, "data-ops")
    assert "platform-alerts" not in _channels(tmp_path)
    assert lint_task(tmp_path).ok


def test_passed_stressors_do_not_hide_a_blueprint_code(tmp_path):
    bundle = compile_twin("mainframe_batch", "act-authorized", 4)
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "blueprint.json").write_text(json.dumps({"t0_stressors": ["S09"]}))
    write_narrative(tmp_path, bundle, "mainframe_batch", stressors=["S07"])
    result = lint_task(tmp_path)
    assert result.ok, result.as_dict()
    assert result.stressor_clues["covered"] == ["S09"]
    assert STRESSOR_CLUES["S07"]["marker"] in _channel_text(tmp_path, "data-ops")
    assert STRESSOR_CLUES["S09"]["marker"] in _channel_text(tmp_path, "platform-alerts")


def test_unclued_stressors_do_not_raise_or_invent_notes(tmp_path):
    bundle = compile_twin("supply_chain_inventory", "withhold-blocked", 9,
                          submode="conditional_prerequisite_unmet")
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "blueprint.json").write_text(json.dumps({
        "t0_stressors": ["S01_checkpoint_calendar_advance_on_gap", "S10"],
    }))
    write_narrative(tmp_path, bundle, "supply_chain_inventory", stressors=())
    visible = _visible(tmp_path)
    assert "unattributed house bucket" not in visible
    assert "factory_publish at 21:50" not in visible
    assert "unique on event_id" not in visible
    decision = json.loads((tmp_path / "tests" / "governance.json").read_text())
    assert "stressor_ids" not in decision
    result = lint_task(tmp_path)
    assert result.ok, result.as_dict()
    assert result.stressor_clues["covered"] == []


def test_s03_is_written_and_not_required_by_the_linter(tmp_path):
    bundle = compile_twin("sftp_vendor", "escalate-ambiguous", 3)
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "blueprint.json").write_text(json.dumps({
        "t0_stressors": ["S01", "S03_retry_batch_append_without_dedup"],
    }))
    write_narrative(tmp_path, bundle, "sftp_vendor")
    assert STRESSOR_CLUES["S03"]["chat"] in _channel_text(tmp_path, "data-ops")
    _redact(tmp_path, STRESSOR_CLUES["S03"]["marker"])
    result = lint_task(tmp_path)
    assert result.ok, result.as_dict()
    assert result.stressor_clues["missing"] == []


def test_s07_and_s09_clues_are_required_on_kb_chat_or_docs(tmp_path):
    bundle = compile_twin("ad_attribution_events", "withhold-blocked", 15,
                          submode="threshold_cap_exceeded")
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "blueprint.json").write_text(json.dumps({
        "t0_stressors": [
            "S07_quality_gate_inner_join_drops_valid_null_fk",
            "S09",
            "S03",
        ],
    }))
    write_narrative(tmp_path, bundle, "ad_attribution_events")
    result = lint_task(tmp_path)
    assert result.ok, result.as_dict()
    assert result.coverage == 1
    assert result.governance_hops["ok"] is True
    for code in ("S07", "S09"):
        _redact(tmp_path, STRESSOR_CLUES[code]["marker"])
    stripped = lint_task(tmp_path)
    assert stripped.ok is False
    assert "stressor:S07" in stripped.missing
    assert "stressor:S09" in stripped.missing
    assert "stressor:S03" not in stripped.missing
    docs = tmp_path / "workspace" / "docs"
    docs.mkdir(parents=True)
    (docs / "partner_feed_contract.md").write_text(
        "Partner rows with a blank entity_id belong in the unattributed house bucket.\n"
        "orchestration/schedule.yml pinned factory_publish at 21:50 instead of after factory_load.\n"
    )
    restored = lint_task(tmp_path)
    assert restored.ok, restored.as_dict()
    assert set(restored.stressor_clues["covered"]) == {"S07", "S09"}


def test_prompt_and_ticket_text_do_not_satisfy_a_stressor_clue(tmp_path):
    bundle = compile_twin("fintech_kyc_risk_limits", "act-authorized", 11)
    write_narrative(tmp_path, bundle, "fintech_kyc_risk_limits")
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir(exist_ok=True)
    (fixtures / "blueprint.json").write_text(json.dumps({"t0_stressors": ["S07", "S09"]}))
    prompt = tmp_path / "prompt.md"
    prompt.write_text(prompt.read_text() + "\n" + STRESSOR_CLUES["S07"]["marker"] + "\n")
    issues = fixtures / "workflow" / "issues.yaml"
    issues.write_text(issues.read_text() + "\n# " + STRESSOR_CLUES["S09"]["marker"] + "\n")
    result = lint_task(tmp_path)
    assert result.ok is False
    assert "stressor:S07" in result.missing
    assert "stressor:S09" in result.missing
    assert result.governance_hops["ok"] is True
    assert not any(item.startswith("pattern:") or item.startswith("payload:") for item in result.spoilers)


def test_stressor_notes_keep_the_workflow_contract(tmp_path):
    from world import workflow

    bundle = compile_twin("iot_telemetry_billing", "withhold-blocked", 12,
                          submode="threshold_cap_exceeded")
    write_narrative(
        tmp_path, bundle, "iot_telemetry_billing",
        hazard_ids=["H01", "H12"],
        stressors=("S03_retry_batch_append_without_dedup", "S07", "S09"),
    )
    workflow.documents(tmp_path / "fixtures", WORKFLOW_SPEC, "2026-10-18T09:00:00Z")
    result = lint_task(tmp_path)
    assert result.ok, result.as_dict()
    assert {"H01", "H12"} <= set(result.hazard_clues["covered"])
    assert decision_valid(bundle)
