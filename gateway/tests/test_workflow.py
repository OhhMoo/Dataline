"""The ticket, the channel and the wiki - asserted on content, never on `ok`.

A registered tool whose test only checks that it did not error is
indistinguishable from a dead one, so every assertion here names a sentence, a
date, a reply count or a byte that has to be there. The write paths are proved
the only way that means anything: write through one Gateway, read it back
through a *different* one over the same world, and replay the whole session onto
a clone and require the two worlds to end byte-identical.
"""

import json

import pytest
from conftest import clone_world, config, world_hash
from workflow_seed import authoring_notes

from alkera_gateway import Gateway
from alkera_gateway.audit import read_rows
from alkera_gateway.records import digest
from alkera_gateway.replay import replay
from alkera_gateway.signals import process_signals
from alkera_gateway.tools import TOOLS

VNOW = "2026-03-05T09:00:00Z"

# One call per workflow tool, in an order a real session could take: read the
# ticket, read the channel, read the wiki, then write to all three.
SESSION = (
    ("issues_list", {}),
    ("issues_search", {"query": "amount"}),
    ("issues_get", {"issue": "DATA-141"}),
    ("chat_channels", {}),
    ("chat_history", {"channel": "data-eng"}),
    ("chat_search", {"query": "still uploading"}),
    ("chat_thread", {"message_id": "data-eng/0006"}),
    ("kb_list", {}),
    ("kb_search", {"query": "backfill"}),
    ("kb_get", {"page": "de/runbooks/nightly-orders-load"}),
    ("issues_comment", {"issue": "DATA-141", "body": "Looking at the 03-03 drop."}),
    ("issues_update", {"issue": "DATA-141", "status": "in_progress", "assignee": "agent"}),
    ("chat_post", {"channel": "data-eng", "text": "Re-reading the 03-03 drop.", "thread_id": None}),
    ("kb_edit", {"page": "de/runbooks/nightly-orders-load", "body": "# Superseded\n"}),
    (
        "kb_create",
        {
            "page": "de/postmortems/2026-03-05-flat-revenue",
            "title": "Postmortem: flat revenue, 2026-03-05",
            "body": "# Draft\n",
            "parent": "de/postmortems",
        },
    ),
)


def assert_ok(result):
    assert result["ok"], result["error"]
    return result["result"]


def call(gateway, tool, **args):
    return assert_ok(gateway.call(tool, args))


# --------------------------------------------------------------------------
# Issue tracker
# --------------------------------------------------------------------------


def test_issues_list_and_get_serve_a_backlog_a_real_team_would_have(gateway):
    """The backlog has to be a backlog: stale tickets, an unlinked duplicate, an
    irrelevant closed one, and the incident as a non-engineer described it."""
    listing = call(gateway, "issues_list")
    assert listing["total"] == 8
    assert listing["statuses"] == ["open", "triage", "in_progress", "blocked", "done", "wontfix"]
    assert {person["id"] for person in listing["users"]} >= {"mrivera", "dpatel", "kliu"}

    # Newest-updated first, so the live incident sorts above a December ticket.
    ids = [issue["id"] for issue in listing["issues"]]
    assert ids[0] == "DATA-141"
    assert ids[-1] == "DATA-118"
    assert set(ids) == {f"DATA-{n}" for n in (118, 127, 132, 136, 139, 140, 141, 143)}

    # The stale one: open, assigned to somebody on leave, untouched since January.
    stale = next(issue for issue in listing["issues"] if issue["id"] == "DATA-127")
    assert stale["status"] == "open"
    assert stale["assignee"] == "kliu"
    assert stale["updated_at"] == "2026-01-08T14:22:00Z"

    # The report, in the reporter's own words - no schema, a symptom and a deadline.
    incident = call(gateway, "issues_get", issue="DATA-141")
    assert "still shows Tuesday's total" in incident["body"]
    assert "month-end pack goes out on friday" in incident["body"].casefold()
    assert incident["labels"] == ["revenue", "dashboard"]
    assert incident["comments"][0]["author"] == "dpatel"
    # A human linked this incident to the wrong ticket, and the link is served.
    assert incident["links"] == [{"type": "relates_to", "issue": "DATA-136"}]


def test_the_duplicate_is_present_and_nobody_linked_it(gateway):
    """Two tickets, same person, seven minutes apart, no link between them. If the
    surface quietly de-duplicated for the agent it would delete the judgement
    call that makes the ticket queue worth reading."""
    first = call(gateway, "issues_get", issue="DATA-141")
    second = call(gateway, "issues_get", issue="DATA-140")
    assert first["reporter"] == second["reporter"] == "mrivera"
    assert first["created_at"] < second["created_at"] < "2026-03-05T08:20:00Z"
    assert second["links"] == []
    assert not any(link["issue"] == "DATA-140" for link in first["links"])
    # And the chat surface is where the reporter admits it.
    hits = call(gateway, "chat_search", query="sorry for the double")["matches"]
    assert [hit["id"] for hit in hits] == ["data-eng/0007"]


def test_issues_search_returns_evidence_lines_across_bodies_and_comments(gateway):
    result = call(gateway, "issues_search", query="null amount")
    assert result["limit_reached"] is False
    # The January ticket describes the same symptom with a different cause.
    assert [match["issue"] for match in result["matches"]] == ["DATA-132"]
    assert result["matches"][0]["field"] == "body"
    assert "null amount" in result["matches"][0]["text"]

    comment_hits = call(gateway, "issues_search", query="Globex connector")["matches"]
    assert comment_hits[0]["field"] == "comment"
    assert comment_hits[0]["ref"] == "DATA-141#1"
    assert comment_hits[0]["line"] == 1


def test_a_comment_persists_and_a_second_gateway_reads_it_back(world, tmp_path):
    """The read-only-mock failure mode, closed: the comment is written by one
    process and read by another over the same world tree."""
    writer = Gateway(config(world, tmp_path / "audit-a"))
    try:
        written = call(
            writer,
            "issues_comment",
            issue="DATA-139",
            body="The run went green because it backfilled two days, not because it was fixed.",
        )
    finally:
        writer.close()

    assert written["comment"]["id"] == "DATA-139#3"
    assert written["comment"]["author"] == "agent"
    assert written["comment"]["at"] == VNOW
    assert written["comment_count"] == 3

    reader = Gateway(config(world, tmp_path / "audit-b"))
    try:
        issue = call(reader, "issues_get", issue="DATA-139")
        listing = call(reader, "issues_list", status="in_progress")
    finally:
        reader.close()

    assert [comment["id"] for comment in issue["comments"]] == [
        "DATA-139#1",
        "DATA-139#2",
        "DATA-139#3",
    ]
    assert "backfilled two days" in issue["comments"][-1]["body"]
    assert issue["updated_at"] == VNOW
    assert issue["history"][-1] == {
        "at": VNOW,
        "actor": "agent",
        "action": "commented",
        "ref": "DATA-139#3",
    }
    summary = next(item for item in listing["issues"] if item["id"] == "DATA-139")
    assert summary["comment_count"] == 3
    assert summary["last_comment_at"] == VNOW


def test_status_and_assignee_changes_persist_with_a_history_trail(gateway):
    result = call(
        gateway,
        "issues_update",
        issue="DATA-141",
        status="in_progress",
        assignee="agent",
        add_labels=["incident"],
    )
    assert [change["field"] for change in result["changes"]] == ["status", "assignee", "labels"]
    assert result["status"] == "in_progress"
    assert result["assignee"] == "agent"
    assert result["labels"] == ["revenue", "dashboard", "incident"]

    issue = call(gateway, "issues_get", issue="DATA-141")
    assert issue["status"] == "in_progress"
    assert issue["assignee"] == "agent"
    assert issue["updated_at"] == VNOW
    transition = {"at": VNOW, "actor": "agent", "field": "status"}
    assert {**transition, "from": "open", "to": "in_progress"} in issue["history"]

    # Filters read the new value, not a cached one.
    assert "DATA-141" in [
        item["id"] for item in call(gateway, "issues_list", status="in_progress")["issues"]
    ]
    assert "DATA-141" not in [
        item["id"] for item in call(gateway, "issues_list", status="open")["issues"]
    ]

    # Removing a label and unassigning are both real edits.
    after = call(
        gateway, "issues_update", issue="DATA-141", unassign=True, remove_labels=["incident"]
    )
    assert after["assignee"] is None
    assert after["labels"] == ["revenue", "dashboard"]


def test_an_update_that_changes_nothing_says_so_rather_than_claiming_success(gateway):
    """`ok: true` over an unchanged ticket is exactly the empty-success failure
    this suite exists to catch, so the no-op is named in the response and flagged
    in the log."""
    result = call(gateway, "issues_update", issue="DATA-141", status="open")
    assert result["changes"] == []
    assert result["updated_at"] == "2026-03-05T08:26:00Z"  # untouched
    row = read_rows(gateway.audit.actions)[-1]
    assert row["op_class"] == "write"
    assert row["flags"] == ["no_change"]


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"issue": "DATA-999", "status": "open"}, "issue_not_found"),
        ({"issue": "DATA-141", "status": "Done"}, "invalid_status"),
        ({"issue": "DATA-141", "assignee": "nobody"}, "user_not_found"),
        ({"issue": "DATA-141"}, "invalid_update"),
        ({"issue": "DATA-141", "assignee": "agent", "unassign": True}, "invalid_update"),
    ],
)
def test_issue_updates_refuse_what_a_tracker_would_refuse(gateway, args, code):
    error = gateway.call("issues_update", args)["error"]
    assert error["code"] == code
    # A refused write leaves the ticket exactly as it was.
    assert call(gateway, "issues_get", issue="DATA-141")["status"] == "open"


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------


def test_channels_and_threads_carry_the_incident_conversation(gateway):
    channels = call(gateway, "chat_channels")["channels"]
    assert [channel["id"] for channel in channels] == ["data-eng", "incidents", "finance-data"]
    engineering = channels[0]
    assert engineering["message_count"] == 8
    assert engineering["thread_count"] == 4
    assert engineering["last_message_at"] == "2026-03-05T08:41:00Z"

    history = call(gateway, "chat_history", channel="data-eng")
    assert history["has_more"] is False
    assert [message["id"] for message in history["messages"]] == [
        "data-eng/0001",
        "data-eng/0005",
        "data-eng/0006",
        "data-eng/0007",
    ]
    # Only thread starters are listed, and each says how much is hidden under it.
    assert [message["reply_count"] for message in history["messages"]] == [3, 0, 0, 1]
    assert history["messages"][0]["last_reply_at"] == "2026-02-18T10:12:00Z"


def test_the_decision_nobody_wrote_down_lives_only_in_a_thread(gateway):
    """The rule that governs this incident - one day at a time, because Acme
    overwrite the filename - was agreed in a reply and never made it to the
    wiki. It is reachable from chat and from nowhere else."""
    thread = call(gateway, "chat_thread", message_id="data-eng/0001")
    assert thread["channel"] == "data-eng"
    assert thread["root"]["author"] == "kliu"
    assert [reply["id"] for reply in thread["replies"]] == [
        "data-eng/0002",
        "data-eng/0003",
        "data-eng/0004",
    ]
    decision = thread["replies"][1]
    assert "One day at a time" in decision["text"]
    assert "rewrite the same filename" in decision["text"]
    assert decision["at"] == "2026-02-18T10:11:00Z"

    # It is not in the wiki. The runbook still tells you to do the opposite.
    assert call(gateway, "kb_search", query="one day at a time from now on")["matches"] == []
    runbook = call(gateway, "kb_get", page="de/runbooks/nightly-orders-load")
    assert "safe to backfill up to seven days" in runbook["body"]

    # Replying to a reply is refused: a thread has one root.
    reply_to_reply = {"channel": "data-eng", "text": "x", "thread_id": "data-eng/0003"}
    assert gateway.call("chat_post", reply_to_reply)["error"]["code"] == "invalid_thread"


def test_the_useful_message_is_an_unanswered_mention(gateway):
    """The one person who saw the cause addressed it to somebody on leave. Zero
    replies is the content, not an empty result."""
    hits = call(gateway, "chat_search", query="still uploading")["matches"]
    assert [hit["id"] for hit in hits] == ["data-eng/0006"]
    assert hits[0]["author"] == "sokonkwo"
    assert hits[0]["mentions"] == ["kliu"]

    thread = call(gateway, "chat_thread", message_id="data-eng/0006")
    assert thread["reply_count"] == 0
    assert thread["replies"] == []
    assert "landed about 40 minutes late" in thread["root"]["text"]

    # And the person it was addressed to had been announced as away.
    away = call(gateway, "chat_search", query="out until 23 March")["matches"]
    assert away[0]["author"] == "tokafor"


def test_the_confident_wrong_guess_is_present_and_contradicted_by_the_data(gateway):
    """A channel with no wrong answers in it is not a channel. This one is stated
    flatly, agreed with, and disprovable through the ingestion surface."""
    guess = call(gateway, "chat_search", query="Globex connector again")["matches"][0]
    assert guess["channel"] == "incidents"
    assert guess["id"] == "incidents/0003"
    assert guess["reactions"] == {"+1": 2}
    assert "that is where the missing amounts are coming from" in guess["text"]

    # Globex is real, is paused, and lands a different table entirely.
    connector = call(gateway, "ingest_get", connector_id="globex_returns")
    assert connector["status"] == "paused"
    assert connector["destination"]["tables"] == ["vendor_returns"]
    assert "vendor_orders" not in connector["destination"]["tables"]


def test_chat_search_scopes_to_one_channel_and_reports_its_limit(gateway):
    everywhere = call(gateway, "chat_search", query="board")
    assert {hit["channel"] for hit in everywhere["matches"]} == {"data-eng", "finance-data"}
    scoped = call(gateway, "chat_search", query="board", channel="finance-data")
    assert [hit["id"] for hit in scoped["matches"]] == ["finance-data/0002"]
    capped = call(gateway, "chat_search", query="the", limit=2)
    assert len(capped["matches"]) == 2
    assert capped["limit_reached"] is True
    assert (
        gateway.call("chat_search", {"query": "x", "channel": "nope"})["error"]["code"]
        == "channel_not_found"
    )


def test_a_posted_reply_lands_in_the_thread_a_later_read_returns(world, tmp_path):
    writer = Gateway(config(world, tmp_path / "audit-a"))
    try:
        posted = call(
            writer,
            "chat_post",
            channel="data-eng",
            text="@sokonkwo re-reading the 03-03 drop now - thanks for flagging it.",
            thread_id="data-eng/0006",
        )
    finally:
        writer.close()
    assert posted["message"]["id"] == "data-eng/0009"
    assert posted["message"]["at"] == VNOW
    assert posted["message"]["mentions"] == ["sokonkwo"]

    reader = Gateway(config(world, tmp_path / "audit-b"))
    try:
        thread = call(reader, "chat_thread", message_id="data-eng/0006")
        channels = call(reader, "chat_channels")["channels"]
    finally:
        reader.close()
    assert thread["reply_count"] == 1
    assert thread["replies"][0]["text"].startswith("@sokonkwo re-reading")
    assert thread["replies"][0]["author"] == "agent"
    assert channels[0]["message_count"] == 9
    assert channels[0]["last_message_at"] == VNOW


# --------------------------------------------------------------------------
# Knowledge base
# --------------------------------------------------------------------------


def test_the_wiki_contradicts_itself_and_the_dates_resolve_it(gateway):
    """Two runbooks disagree about the vendor, the folder and the backfill rule.
    Neither is marked wrong. The evidence that settles it is the edit date, which
    is why `age_days` is computed against the world clock and served."""
    listing = call(gateway, "kb_list")
    assert listing["space"] == {"key": "DE", "name": "Data Engineering"}
    assert listing["as_of"] == VNOW
    pages = {page["id"]: page for page in listing["pages"]}
    assert set(pages) == {
        "de/architecture",
        "de/architecture/warehouse-overview",
        "de/policies",
        "de/policies/change-freeze",
        "de/postmortems",
        "de/postmortems/2026-01-14-duplicate-orders",
        "de/runbooks",
        "de/runbooks/nightly-orders-load",
        "de/runbooks/vendor-feed-sla",
    }
    assert listing["children"]["de/runbooks"] == [
        "de/runbooks/nightly-orders-load",
        "de/runbooks/vendor-feed-sla",
    ]

    stale = pages["de/runbooks/nightly-orders-load"]
    current = pages["de/runbooks/vendor-feed-sla"]
    assert stale["last_edited_at"] == "2025-11-04T10:12:00Z"
    assert stale["age_days"] == 120
    assert current["age_days"] == 13
    assert stale["version"] == 7 and current["version"] == 2

    stale_body = call(gateway, "kb_get", page="de/runbooks/nightly-orders-load")["body"]
    current_body = call(gateway, "kb_get", page="de/runbooks/vendor-feed-sla")["body"]
    assert "/data/drop/globex" in stale_body and "amount_usd" in stale_body
    assert "/data/drop/acme" in current_body
    assert "Backfill one day at a time" in current_body
    assert "seven days" in stale_body
    # The fresher page states the contradiction itself, in prose, which is how a
    # real wiki records a supersession: not with a field, with a sentence.
    assert "Supersedes" in current_body


def test_kb_search_hits_both_sides_of_the_contradiction_with_dates(gateway):
    result = call(gateway, "kb_search", query="backfill")
    pages = {match["page"] for match in result["matches"]}
    # Both runbooks, plus the freeze policy that allows backfills: the query a
    # hurrying agent runs returns the contradiction AND the constraint on it.
    assert pages == {
        "de/policies/change-freeze",
        "de/runbooks/nightly-orders-load",
        "de/runbooks/vendor-feed-sla",
    }
    for match in result["matches"]:
        assert match["last_edited_at"]
        assert match["line"] >= 1
    # The remediation that would have prevented the incident, still open.
    action = call(gateway, "kb_search", query="still growing")["matches"][0]
    assert action["page"] == "de/postmortems/2026-01-14-duplicate-orders"
    postmortem = call(gateway, "kb_get", page="de/postmortems/2026-01-14-duplicate-orders")
    assert "**Open.**" in postmortem["body"]
    assert postmortem["lines"][0] == {
        "line": 1,
        "text": "# Postmortem: duplicate orders, 2026-01-14",
    }


def test_editing_a_page_keeps_the_revision_it_replaced(world, tmp_path):
    writer = Gateway(config(world, tmp_path / "audit-a"))
    try:
        result = call(
            writer,
            "kb_edit",
            page="de/runbooks/nightly-orders-load",
            body="# Runbook: nightly orders load\n\nSuperseded by the vendor feed SLA page.\n",
            comment="source moved to Acme on 1 March",
        )
    finally:
        writer.close()
    assert result["version"] == 8
    assert result["previous_version"] == 7
    assert result["last_edited_by"] == "agent"
    assert result["last_edited_at"] == VNOW
    assert result["age_days"] == 0

    reader = Gateway(config(world, tmp_path / "audit-b"))
    try:
        page = call(reader, "kb_get", page="de/runbooks/nightly-orders-load")
        listing = call(reader, "kb_list")
    finally:
        reader.close()
    assert "Superseded by the vendor feed SLA page." in page["body"]
    assert "amount_usd" not in page["body"]
    assert page["edit_comment"] == "source moved to Acme on 1 March"
    # The revision that was replaced is still recoverable metadata.
    assert page["history"][-1] == {
        "version": 7,
        "at": "2025-11-04T10:12:00Z",
        "by": "kliu",
        "comment": "note that the load is idempotent",
        "bytes": 584,
    }
    entry = next(p for p in listing["pages"] if p["id"] == "de/runbooks/nightly-orders-load")
    assert entry["age_days"] == 0 and entry["version"] == 8


def test_creating_a_page_needs_a_parent_that_exists_and_a_free_id(world, tmp_path):
    writer = Gateway(config(world, tmp_path / "audit-a"))
    try:
        created = call(
            writer,
            "kb_create",
            page="de/postmortems/2026-03-05-flat-revenue",
            title="Postmortem: flat revenue, 2026-03-05",
            body="# Postmortem: flat revenue\n\nThe 03-03 drop was read while still uploading.\n",
            parent="de/postmortems",
            labels=["postmortem"],
        )
        assert created["version"] == 1
        assert created["parent"] == "de/postmortems"
        assert created["last_edited_at"] == VNOW
        for args, code in (
            ({"page": "de/runbooks", "title": "x", "body": ""}, "page_exists"),
            ({"page": "a/b", "title": "x", "body": "", "parent": "de/nope"}, "page_not_found"),
            ({"page": "de/../etc", "title": "x", "body": ""}, "invalid_page_id"),
            ({"page": "de/Runbooks", "title": "x", "body": ""}, "invalid_page_id"),
        ):
            assert writer.call("kb_create", args)["error"]["code"] == code
    finally:
        writer.close()

    reader = Gateway(config(world, tmp_path / "audit-b"))
    try:
        listing = call(reader, "kb_list")
        page = call(reader, "kb_get", page="de/postmortems/2026-03-05-flat-revenue")
    finally:
        reader.close()
    assert listing["children"]["de/postmortems"] == [
        "de/postmortems/2026-01-14-duplicate-orders",
        "de/postmortems/2026-03-05-flat-revenue",
    ]
    assert "read while still uploading" in page["body"]
    assert page["labels"] == ["postmortem"]


# --------------------------------------------------------------------------
# Contract: authorship, the clock, the log, determinism, absence
# --------------------------------------------------------------------------


def test_author_side_annotation_never_reaches_the_agent(gateway):
    """Which ticket is a duplicate, which page is stale and which message is
    confidently wrong is recorded for the task author and stripped from every
    response. Printing it would hand the agent the answer key (F202)."""
    notes = authoring_notes()
    assert len(notes) == 16  # the corpus really does carry annotations
    seen = []
    for tool, args in SESSION:
        seen.append(json.dumps(assert_ok(gateway.call(tool, args)), sort_keys=True))
    evidence = "\n".join(seen)
    assert "authoring" not in evidence
    for note in notes:
        assert note not in evidence
    # Not vacuous: the corpus IS reachable, it is only the annotation that is not.
    assert "landed about 40 minutes late" in evidence
    assert "amount_usd" in evidence
    assert "WF-" not in evidence


def test_the_annotated_source_document_is_out_of_the_agents_reach(gateway):
    """The `authoring` blocks only stay author-side if the file holding them is
    unreachable too. `var/workflow/` is a sibling of `/workspace`, so the file
    tools cannot walk to it, and the grader reads it after teardown."""
    for path in ("../var/workflow/issues.json", "/workspace/../var/workflow/kb.json"):
        assert gateway.call("fs_read", {"path": path})["error"]["code"] == "path_denied"
    assert gateway.call("docs_get", {"path": "../var/workflow/chat.json"})["error"]["code"] == (
        "path_denied"
    )
    # It is there, and it does carry the annotation - for whoever mounts the world.
    raw = (gateway.config.workflow_dir / "issues.json").read_text(encoding="utf-8")
    assert "near_duplicate_trap" in raw


def test_every_workflow_timestamp_comes_from_the_virtual_clock(world, tmp_path):
    """No wall clock anywhere: advance the world's own `vnow` and the next write
    is stamped with the new value, not with the time the test ran."""
    gateway = Gateway(config(world, tmp_path / "audit"))
    try:
        assert gateway.clock() == VNOW
        first = call(gateway, "issues_comment", issue="DATA-141", body="one")
        state = json.loads(gateway.config.scheduler.read_text(encoding="utf-8"))
        state["vnow"] = "2026-03-06T02:30:00Z"
        gateway.config.scheduler.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        second = call(gateway, "issues_comment", issue="DATA-141", body="two")
        message = call(
            gateway, "chat_post", channel="incidents", text="three", thread_id="incidents/0001"
        )
        page = call(gateway, "kb_edit", page="de/policies/change-freeze", body="# Frozen\n")
    finally:
        gateway.close()
    assert first["comment"]["at"] == VNOW
    assert second["comment"]["at"] == "2026-03-06T02:30:00Z"
    assert message["message"]["at"] == "2026-03-06T02:30:00Z"
    assert page["last_edited_at"] == "2026-03-06T02:30:00Z"
    # age_days is measured against the world clock too, not against today.
    assert page["age_days"] == 0


def test_every_workflow_call_lands_a_well_formed_action_row(gateway):
    """agent.md 9.2.2, for all fifteen tools: contiguous seq, an argument hash
    that matches the normalised arguments, an op_class in the log and not in the
    response, and a write classified as a write."""
    for tool, args in SESSION:
        assert_ok(gateway.call(tool, args))
    rows = read_rows(gateway.audit.actions)
    assert [row["seq"] for row in rows] == list(range(1, len(SESSION) + 1))
    assert [row["tool"] for row in rows] == [tool for tool, _ in SESSION]
    assert {row["exit"] for row in rows} == {"ok"}
    for row in rows:
        assert row["args_hash"] == digest(row["args"])
        assert row["result_hash"] == digest(row["result"])
        assert "op_class" not in json.dumps(row["result"])
        assert row["target"] == row["targets"][0]

    classes = {row["tool"]: row["op_class"] for row in rows}
    writes = {"issues_comment", "issues_update", "chat_post", "kb_edit", "kb_create"}
    assert {tool for tool, op in classes.items() if op == "write"} == writes
    assert all(op == "read" for tool, op in classes.items() if tool not in writes)

    targets = {row["tool"]: row["targets"] for row in rows}
    assert targets["issues_get"] == ["issue:DATA-141"]
    assert targets["chat_thread"] == ["chat:data-eng", "thread:data-eng/0006"]
    assert targets["kb_edit"] == ["kb:de/runbooks/nightly-orders-load"]
    assert next(row for row in rows if row["tool"] == "kb_edit")["flags"] == ["overwrite"]

    # Reads of a surface gate later writes to it, and the three new prefixes are
    # distinct surfaces to the process signals.
    signals = process_signals(rows)
    assert signals["surfaces_before_write"] >= 3
    assert signals["read_before_write_ratio"] > 0


def test_all_fifteen_workflow_tools_are_registered_and_exercised():
    registered = {name for name in TOOLS if name.split("_")[0] in {"issues", "chat", "kb"}}
    assert len(registered) == 15
    assert registered == {tool for tool, _ in SESSION}


def test_a_write_session_replays_onto_a_clone_byte_for_byte(world, tmp_path):
    """The determinism gate. Ticket comments, status changes, chat posts and wiki
    edits are pure functions of the call sequence and the initial world, so the
    same calls against a restored copy produce identical responses AND leave the
    world tree byte-identical - ids, timestamps and all."""
    destination = clone_world(world, tmp_path / "destination")
    fingerprint = world_hash(world)
    assert fingerprint == world_hash(destination)

    source = Gateway(config(world, tmp_path / "source-audit", fingerprint))
    try:
        for tool, args in SESSION:
            assert_ok(source.call(tool, args))
    finally:
        source.close()
    assert world_hash(world) != fingerprint  # the writes really changed the world

    target = Gateway(config(destination, tmp_path / "replay-audit", fingerprint))
    try:
        result = replay(source.audit.directory, target)
    finally:
        target.close()
    assert result["responses_match"], result["differences"]
    assert result["calls"] == len(SESSION)
    assert world_hash(world) == world_hash(destination)


def test_a_world_without_a_workflow_surface_says_so_and_stops(world, tmp_path):
    """Same rule as the ingestion control plane: a world that was never seeded
    with a tracker reports that, rather than serving an empty backlog that looks
    like a team with nothing to do."""
    for name in ("issues.json", "chat.json", "kb.json"):
        (world / "var" / "workflow" / name).unlink()
    gateway = Gateway(config(world, tmp_path / "audit"))
    try:
        for tool, args, code in (
            ("issues_list", {}, "no_issue_tracker"),
            ("chat_channels", {}, "no_chat"),
            ("kb_list", {}, "no_knowledge_base"),
        ):
            result = gateway.call(tool, args)
            assert result["ok"] is False
            assert result["result"] is None
            assert result["error"]["code"] == code
        # A file that exists but is not a tracker fails loudly rather than
        # serving the readable half.
        (world / "var" / "workflow" / "issues.json").write_text('{"hello": 1}', encoding="utf-8")
        assert gateway.call("issues_list", {})["error"]["code"] == "invalid_issues"
    finally:
        gateway.close()
