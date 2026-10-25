import json

import pytest

from tasks.factory.governance.twin_state_machine import BLOCKERS, DOMAINS, compile_twin, decision_valid
from tasks.factory.narrative.corecraft_synthesizer import write_narrative
from tasks.factory.narrative.discoverability_and_spoiler_linter import lint_task


@pytest.mark.parametrize("theme", sorted(DOMAINS))
@pytest.mark.parametrize("mode", ["act-authorized", "withhold-blocked", "escalate-ambiguous"])
def test_36_twin_bundles_are_discoverable_and_spoiler_free(tmp_path, theme, mode):
    bundle = compile_twin(theme, mode, 91)
    assert decision_valid(bundle)
    task = tmp_path / f"{theme}-{mode}"
    write_narrative(task, bundle, theme)
    result = lint_task(task)
    assert result.ok, result.as_dict()
    assert result.coverage == 1
    assert result.governance_hops["ok"] is True
    assert result.governance_hops["count"] >= 3
    assert result.hazard_clues["ok"] is True
    assert result.hazard_clues["covered"] == []
    decision = json.loads((task / "tests" / "governance.json").read_text())
    assert decision["mode"] == mode
    visible = "\n".join(path.read_text() for path in (task / "fixtures" / "workflow").glob("*.yaml"))
    for hidden in ("target_outcome", "sql_delta_cents", "warehouse_fact", "evidence_chain"):
        assert hidden not in visible


@pytest.mark.parametrize("blocker", BLOCKERS)
def test_blocker_submodes_are_distinct(tmp_path, blocker):
    bundle = compile_twin("settlement_clearing", "withhold-blocked", 12, submode=blocker)
    assert bundle["decision"]["blocker"] == blocker
    write_narrative(tmp_path, bundle, "settlement_clearing")
    assert lint_task(tmp_path).ok


@pytest.mark.parametrize("mode", ["act-authorized", "withhold-blocked", "escalate-ambiguous"])
def test_each_mode_crosses_at_least_three_surfaces(mode):
    bundle = compile_twin("ad_attribution_events", mode, 21)
    surfaces = {hop["surface"] for hop in bundle["decision"]["hops"]}
    assert len(surfaces) >= 3
    assert decision_valid(bundle)


@pytest.mark.parametrize("blocker", BLOCKERS)
def test_withhold_ticket_looks_approved_until_a_later_surface(blocker):
    bundle = compile_twin("healthcare_claims_edi", "withhold-blocked", 15, submode=blocker)
    assert decision_valid(bundle)
    assert bundle["decision"]["target_outcome"] == "withheld_with_evidence"
    ticket = next(item for item in bundle["issues"]["issues"] if item["id"] == bundle["decision"]["ticket"])
    assert ticket["status"] == "approved"
    chat = "\n".join(message["text"] for channel in bundle["chat"]["channels"] for message in channel["messages"])
    kb = "\n".join(page["body"] for page in bundle["kb"]["pages"])
    if blocker == "delegation_expired_or_revoked":
        end = bundle["decision"]["delegation_end"]
        assert end not in ticket["body"]
        assert end not in chat
        assert f"Window end: {end}." in kb
    elif blocker == "threshold_cap_exceeded":
        delta = str(bundle["decision"]["sql_delta_cents"])
        cap = str(bundle["decision"]["approval_cap_cents"])
        assert cap not in ticket["body"]
        assert cap not in chat
        assert cap in kb
        assert delta not in ticket["body"] and delta not in chat and delta not in kb
        assert bundle["decision"]["sql_delta_cents"] > bundle["decision"]["approval_cap_cents"]
    else:
        prerequisite = bundle["decision"]["prerequisite_ticket"]
        assert prerequisite not in ticket["body"]
        assert prerequisite in chat
        assert bundle["decision"]["warehouse_fact"]["satisfied"] is False


def test_threshold_delta_must_exceed_the_cap():
    bundle = compile_twin("settlement_clearing", "withhold-blocked", 4, submode="threshold_cap_exceeded")
    assert decision_valid(bundle)
    bundle["decision"]["sql_delta_cents"] = 100
    assert decision_valid(bundle) is False


def test_act_authorized_resolves_only_when_the_chain_holds():
    bundle = compile_twin("settlement_clearing", "act-authorized", 5)
    assert decision_valid(bundle)
    decision = bundle["decision"]
    assert decision["target_outcome"] == "resolved"
    issues = {item["id"]: item for item in bundle["issues"]["issues"]}
    hold = issues[decision["superseded_hold"]]
    ticket = issues[decision["ticket"]]
    assert hold["status"] == "wontfix"
    assert "superseded" in hold["body"].lower()
    assert ticket["status"] != "approved"
    assert decision["warehouse_fact"]["sql"] not in ticket["body"]
    assert len([item for item in issues if item not in {hold["id"], ticket["id"]}]) >= 3
    bundle["chat"]["channels"][0]["messages"] = bundle["chat"]["channels"][0]["messages"][:1]
    assert decision_valid(bundle) is False


def test_escalate_names_both_sources_and_withholds_the_choice():
    bundle = compile_twin("revenue_daily", "escalate-ambiguous", 8)
    assert decision_valid(bundle)
    decision = bundle["decision"]
    assert decision["target_outcome"] == "escalated"
    sources = decision["conflict_sources"]
    assert sources[0]["value"] != sources[1]["value"]
    question = decision["exact_question"]
    assert sources[0]["value"] in question and sources[1]["value"] in question
    assert sources[0]["token"] in question and sources[1]["token"] in question
    ticket = next(item for item in bundle["issues"]["issues"] if item["id"] == decision["ticket"])
    assert ticket["status"] != "approved"
    assert sources[0]["value"] not in ticket["body"]
    assert sources[1]["value"] not in ticket["body"]


def test_legacy_withhold_names_compile_to_the_phase2_chain():
    delegated = compile_twin("excel_close", "withhold-blocked", 2, submode="wrong_approver_role")
    assert delegated["decision"]["blocker"] == "delegation_expired_or_revoked"
    assert decision_valid(delegated)
    capped = compile_twin("excel_close", "withhold-blocked", 2, submode="scope_date_mismatch")
    assert capped["decision"]["blocker"] == "threshold_cap_exceeded"
    assert decision_valid(capped)


def test_unknown_blocker_is_rejected():
    with pytest.raises(ValueError):
        compile_twin("excel_close", "withhold-blocked", 2, submode="not-a-blocker")


def test_workflow_contract_accepts_the_chain(tmp_path):
    from world import workflow

    spec = {"dir": "workflow", "actor": "agent",
            "project": {"key": "FAC", "name": "Factory"},
            "statuses": ["approved", "in_progress", "wontfix", "draft", "review"],
            "workspace": "factory", "space": {"key": "FAC", "name": "Factory"}}
    cases = [("act-authorized", None), ("escalate-ambiguous", None),
             * [("withhold-blocked", blocker) for blocker in BLOCKERS]]
    for mode, blocker in cases:
        bundle = compile_twin("settlement_clearing", mode, 91, submode=blocker)
        task = tmp_path / f"{mode}-{blocker}"
        write_narrative(task, bundle, "settlement_clearing", hazard_ids=["H01", "H04"])
        workflow.documents(task / "fixtures", spec, "2026-10-18T09:00:00Z")


def test_literal_solution_leak_is_rejected(tmp_path):
    bundle = compile_twin("settlement_clearing", "act-authorized", 12)
    write_narrative(tmp_path, bundle, "settlement_clearing")
    (tmp_path / "solution").mkdir()
    line = "result = database.execute('SELECT payment_id FROM raw.events').fetchall()"
    (tmp_path / "solution" / "solve.py").write_text(line + "\n")
    (tmp_path / "prompt.md").write_text((tmp_path / "prompt.md").read_text() + line + "\n")
    assert not lint_task(tmp_path).ok
