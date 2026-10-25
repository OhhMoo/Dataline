"""Seeded lexicon: same seed is stable, and identity is not one frozen cast."""
import datetime as dt

from tasks.factory.governance.lexicon_rebinder import make_lexicon, rebind, ticket_ids
from tasks.factory.governance.twin_state_machine import compile_twin


def test_lexicon_and_rebind_are_deterministic():
    first = make_lexicon(91, theme="settlement_clearing")
    assert first == make_lexicon(91, theme="settlement_clearing")
    assert first == make_lexicon("91", theme="settlement_clearing")
    sample = "Ask @rkeane about PAY-3305 on 2026-06-11."
    tickets = {"PAY-3305": "CLM-4091"}
    handles = {"rkeane": "cpatel"}
    rebound = rebind(sample, first, tickets=tickets, handles=handles)
    assert rebound == rebind(sample, first, tickets=tickets, handles=handles)
    twin = compile_twin("settlement_clearing", "withhold-blocked", 91)
    assert twin == compile_twin("settlement_clearing", "withhold-blocked", 91)


def test_handles_tickets_and_calendar_are_not_constant():
    lexes = [make_lexicon(seed, theme="settlement_clearing") for seed in range(24)]
    controllers = {lex["roles"]["controller"] for lex in lexes}
    tickets = {ticket_ids(lex, 1)[0] for lex in lexes}
    shifts = {lex["day_shift"] for lex in lexes}
    assert len(controllers) > 1
    assert controllers.isdisjoint({"rkeane", "nbrennan"})
    assert len(tickets) > 1
    assert len({ticket.split("-", 1)[0] for ticket in tickets}) > 1
    assert len(shifts) > 1

    handles, ids, days = set(), set(), set()
    for seed in range(16):
        bundle = compile_twin("fintech_kyc_risk_limits", "act-authorized", seed)
        handles.add(bundle["decision"]["controller"])
        ids.add(bundle["decision"]["ticket"])
        days.add(bundle["decision"]["closed_days"][0])
    assert len(handles) > 1
    assert handles.isdisjoint({"rkeane", "nbrennan"})
    assert len(ids) > 1
    assert len(days) > 1


def test_rebind_replaces_handle_ticket_and_calendar_anchor():
    lex = dict(make_lexicon(4, theme="revenue_daily"))
    lex["day_shift"] = 11
    source = "Approver @nbrennan signed PAY-3305 for 2026-06-11 only."
    rebound = rebind(
        source, lex, tickets={"PAY-3305": "INV-1140"}, handles={"nbrennan": "emorgan"})
    assert rebound == "Approver @emorgan signed INV-1140 for 2026-06-22 only."
    shifted = (dt.date(2026, 6, 11) + dt.timedelta(days=11)).isoformat()
    assert shifted == "2026-06-22"
