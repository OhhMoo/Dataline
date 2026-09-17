"""The human plane's three fake vendors, asserted on content and on rows.

Three things are being proved here, and none of them is `ok`:

1. **The real client works.** `jira`, `slack_sdk` and `atlassian-python-api`
   drive the apps over a real socket. A vendor imitation that only ever answers
   curl has not been shown to answer the library an agent would actually reach
   for, so the compatibility tests use the library and assert on a sentence, a
   status, a version number or a reply count that has to be there.
2. **The world is one world.** A comment posted through Jira is read back
   through a *different* `Gateway` over the same tree, exactly the way
   `test_workflow.py` proves the MCP write path.
3. **The action log does not care which transport wrote it.** For every pair in
   `EQUIVALENT` the same action is performed natively and through MCP over a
   pristine clone, and the two rows' `op_class` and `targets` are diffed. That
   agreement is no longer a coincidence to be policed: a handler hands its
   `records.Observation` to the same `Operations` method the MCP gateway hands
   it and the row is written off it, so the 16 pairs measure a property the code
   produces rather than a table someone kept up to date. What the table still
   catches is a *vendor* mistranslation - the wrong key, the wrong channel - and
   `test_an_endpoint_that_ran_no_module_call_cannot_ship_a_row` catches the
   thing it used to be the only guard against.

`httpx.ASGITransport` is used wherever the client is ours; uvicorn on a free
localhost port is used for the three vendor clients, which open real sockets.
"""

import json
import socket
import threading
import time
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
import workflow_seed
from atlassian import Confluence
from conftest import clone_world, config
from fastapi import Request
from jira import JIRA, JIRAError
from requests import HTTPError
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from workflow_seed import authoring_notes

from alkera_gateway import Gateway
from alkera_gateway.audit import read_rows
from alkera_gateway.native import audit as native_audit
from alkera_gateway.native import context as native_context
from alkera_gateway.native.context import Ledger
from alkera_gateway.native.services import confluence as confluence_service
from alkera_gateway.native.services import jira as jira_service
from alkera_gateway.native.services import slack as slack_service
from alkera_gateway.records import UnclassifiedObservation

RUNBOOK = "de/runbooks/nightly-orders-load"
RUNBOOK_ID = confluence_service.content_id(RUNBOOK)
POSTMORTEM_TITLE = "Postmortem flat revenue 2026-03-05"
POSTMORTEM = "de/postmortems/postmortem-flat-revenue-2026-03-05"
EMAIL = "agent@alkera.local"


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


def seed_message(channel_id: str, message_id: str) -> dict:
    """One seeded chat message, so a test can compute its Slack `ts` offline."""
    for channel in workflow_seed.CHAT["channels"]:
        if channel["id"] == channel_id:
            for message in channel["messages"]:
                if message["id"] == message_id:
                    return message
    raise AssertionError(f"the reference corpus has no {message_id}")


# A *reply*, not a root: `chat_thread` resolves either to the same thread, and
# `conversations.replies` has to do the same or the two surfaces disagree.
THREAD_TS = slack_service.message_ts(seed_message("data-eng", "data-eng/0002"))


class Plane:
    """One world, the three apps pointed at it, and the row file they write."""

    def __init__(self, root, settings, rows):
        self.root, self.settings, self.rows = root, settings, rows

    def written(self) -> list[dict]:
        if not self.rows.exists():
            return []
        return [json.loads(line) for line in
                self.rows.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def plane(world, tmp_path, monkeypatch):
    """Point every service at the fixture world through the documented env vars."""
    audit = tmp_path / "native-audit"
    audit.mkdir()
    settings = config(world, audit)
    written = tmp_path / "gateway.json"
    written.write_text(settings.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv(native_context.CONFIG_ENV, str(written))
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(audit / "actions.jsonl"))
    # `native.audit` stamps `vts` from the scheduler, not from this module's
    # config, so the row's virtual time is the world's and not a default.
    monkeypatch.setenv("ALKERA_SCHEDULER", str(settings.scheduler))
    return Plane(world, settings, audit / "actions.jsonl")


@pytest.fixture
def mirror(plane, tmp_path):
    """A pristine clone of the same world behind an MCP `Gateway`.

    Pristine because the equivalence diff has to compare two runs of the same
    action against the same starting state, and a write changes the state.
    """
    instance = Gateway(config(clone_world(plane.root, tmp_path / "mirror"),
                              tmp_path / "mirror-audit"))
    yield instance
    instance.close()


def client(module, **kwargs) -> httpx.AsyncClient:
    """An in-process client for one app, carrying that vendor's credentials."""
    auth = None if module is slack_service else (EMAIL, module.TOKEN)
    headers = {"Authorization": f"Bearer {module.TOKEN}"} if module is slack_service else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                             base_url=f"http://{module.HOST}", auth=auth,
                             headers=headers, **kwargs)


@contextmanager
def serving(app):
    """Run one app on a free localhost port, for clients that need a real socket."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("the service did not come up")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=30)


@pytest.fixture
def jira_client(plane):
    with serving(jira_service.app) as base:
        yield JIRA(server=base, basic_auth=(EMAIL, jira_service.TOKEN),
                   options={"rest_api_version": "3"})


@pytest.fixture
def slack_client(plane):
    with serving(slack_service.app) as base:
        yield WebClient(base_url=f"{base}/api/", token=slack_service.TOKEN)


@pytest.fixture
def confluence_client(plane):
    with serving(confluence_service.app) as base:
        yield Confluence(url=f"{base}/wiki", username=EMAIL,
                         password=confluence_service.TOKEN)


# --------------------------------------------------------------------------
# Jira, driven by the python `jira` package
# --------------------------------------------------------------------------


def test_jira_client_reads_the_backlog_the_workflow_tools_serve(jira_client):
    """The real client's handshake, search and fetch, against the seeded corpus."""
    info = jira_client.server_info()
    assert info["deploymentType"] == "Cloud"
    assert info["serverTitle"] == "Data platform"
    assert jira_client.myself()["accountId"] == "agent"

    found = jira_client.search_issues("project = DATA ORDER BY updated DESC")
    assert [issue.key for issue in found][0] == "DATA-141"
    assert len(found) == 8

    narrowed = jira_client.search_issues('status = done AND text ~ "freeze"')
    assert [issue.key for issue in narrowed] == ["DATA-118"]

    issue = jira_client.issue("DATA-141")
    assert issue.fields.status.name == "open"
    assert issue.fields.status.statusCategory.key == "new"
    assert issue.fields.reporter.accountId == "mrivera"
    # v3 serves ADF, so the text is under the document tree, not the field.
    assert "revenue" in jira_service.plain(issue.raw["fields"]["description"]).lower()
    assert issue.fields.comment.total >= 1


def test_jira_client_paginates_with_start_at_and_max_results(plane):
    """`GET /rest/api/3/search` with the offset paging NATIVE_SURFACES.md names.

    Driven by the same library with `get_server_info=False`, which is what makes
    it take the non-Cloud path; the Cloud path is `/search/jql` and the test
    above covers it.
    """
    with serving(jira_service.app) as base:
        client_ = JIRA(server=base, basic_auth=(EMAIL, jira_service.TOKEN),
                       options={"rest_api_version": "3"}, get_server_info=False)
        first = client_.search_issues("project = DATA ORDER BY key ASC", maxResults=3)
        second = client_.search_issues("project = DATA ORDER BY key ASC", startAt=3,
                                       maxResults=3)
    assert [issue.key for issue in first] == ["DATA-118", "DATA-127", "DATA-132"]
    assert first.total == 8
    assert [issue.key for issue in second] == ["DATA-136", "DATA-139", "DATA-140"]


def test_jira_comment_and_transition_land_in_the_world(jira_client, plane, tmp_path):
    """Write through Jira, read back through a different Gateway over the same tree."""
    created = jira_client.add_comment("DATA-141", "Looking at the 03-03 drop.")
    assert jira_service.plain(created.raw["body"]) == "Looking at the 03-03 drop."

    transitions = {item["name"]: item["id"] for item in jira_client.transitions("DATA-141")}
    assert "open" not in transitions  # the status it already has is not a transition
    jira_client.transition_issue("DATA-141", transitions["in_progress"])

    reader = Gateway(config(plane.root, tmp_path / "reader-audit"))
    try:
        issue = reader.call("issues_get", {"issue": "DATA-141"})["result"]
    finally:
        reader.close()
    assert issue["status"] == "in_progress"
    assert issue["comments"][-1]["body"] == "Looking at the 03-03 drop."
    assert issue["comments"][-1]["author"] == "agent"
    # The virtual clock, not the wall clock: the world is still on its own time.
    assert issue["comments"][-1]["at"] == "2026-03-05T09:00:00Z"


def test_jira_refuses_a_wrong_token_and_an_unparseable_jql(plane):
    with serving(jira_service.app) as base:
        with pytest.raises(JIRAError) as refused:
            JIRA(server=base, basic_auth=(EMAIL, "not-the-token"),
                 options={"rest_api_version": "3"})
        assert refused.value.status_code == 401

        client_ = JIRA(server=base, basic_auth=(EMAIL, jira_service.TOKEN),
                       options={"rest_api_version": "3"})
        with pytest.raises(JIRAError) as rejected:
            client_.search_issues("assignee changed FROM dpatel")
    assert rejected.value.status_code == 400
    assert "Unsupported JQL clause" in str(rejected.value) or "Cannot parse" in str(rejected.value)


async def test_jira_unassign_and_labels_go_through_issues_update(plane):
    """`PUT /issue/{key}` expresses both of `issues_update`'s label operations."""
    async with client(jira_service) as http:
        put = await http.put("/rest/api/3/issue/DATA-139",
                             json={"fields": {"assignee": None},
                                   "update": {"labels": [{"add": "native"}]}})
        assert put.status_code == 204
        issue = (await http.get("/rest/api/3/issue/DATA-139")).json()
    assert issue["fields"]["assignee"] is None
    assert "native" in issue["fields"]["labels"]


# --------------------------------------------------------------------------
# Slack, driven by slack_sdk
# --------------------------------------------------------------------------


def test_slack_client_reads_the_channel_the_chat_tools_serve(slack_client):
    identity = slack_client.auth_test()
    assert identity["team"] == "acme-data"
    assert identity["user"] == "agent"

    channels = {channel["name"]: channel for channel in
                slack_client.conversations_list()["channels"]}
    assert "data-eng" in channels and "incidents" in channels
    assert channels["data-eng"]["topic"]["value"]

    history = slack_client.conversations_history(channel="data-eng", limit=3)
    assert len(history["messages"]) == 3
    assert history["has_more"] is True
    # Newest first, the way Slack serves it.
    stamps = [float(message["ts"]) for message in history["messages"]]
    assert stamps == sorted(stamps, reverse=True)

    thread = slack_client.conversations_replies(channel="data-eng", ts=THREAD_TS)
    assert [message["username"] for message in thread["messages"]] == [
        "kliu", "dpatel", "kliu", "tokafor"]
    assert thread["messages"][0]["reply_count"] == 3
    assert all(message["thread_ts"] == thread["messages"][0]["ts"]
               for message in thread["messages"])

    people = {person["name"] for person in slack_client.users_list()["members"]}
    assert {"dpatel", "kliu", "agent"} <= people

    hits = slack_client.search_messages(query="in:#data-eng still uploading")
    assert hits["messages"]["total"] == 1
    assert "still uploading" in hits["messages"]["matches"][0]["text"]


def test_slack_post_lands_in_the_world_and_threads(slack_client, plane, tmp_path):
    posted = slack_client.chat_postMessage(channel="data-eng",
                                           text="Re-reading the 03-03 drop.")
    reply = slack_client.chat_postMessage(channel="data-eng", text="Found it.",
                                          thread_ts=posted["ts"])
    assert reply["message"]["thread_ts"] == posted["ts"]

    reader = Gateway(config(plane.root, tmp_path / "reader-audit"))
    try:
        thread = reader.call("chat_thread", {"message_id": "data-eng/0009"})["result"]
    finally:
        reader.close()
    assert thread["root"]["text"] == "Re-reading the 03-03 drop."
    assert [message["text"] for message in thread["replies"]] == ["Found it."]
    assert thread["root"]["author"] == "agent"


def test_slack_reports_a_bad_token_and_an_unknown_channel_the_slack_way(plane):
    with serving(slack_service.app) as base:
        wrong = WebClient(base_url=f"{base}/api/", token="xoxb-not-the-token")
        with pytest.raises(SlackApiError) as refused:
            wrong.auth_test()
        assert refused.value.response["error"] == "invalid_auth"

        right = WebClient(base_url=f"{base}/api/", token=slack_service.TOKEN)
        with pytest.raises(SlackApiError) as missing:
            right.conversations_history(channel="does-not-exist")
    assert missing.value.response["error"] == "channel_not_found"
    assert missing.value.response.status_code == 200  # Slack says so in the body


async def test_slack_history_pages_with_a_cursor(plane):
    """`limit` plus `next_cursor` walks the channel without repeating a message."""
    async with client(slack_service) as http:
        first = (await http.post("/api/conversations.history",
                                 data={"channel": "data-eng", "limit": "2"})).json()
        cursor = first["response_metadata"]["next_cursor"]
        second = (await http.post("/api/conversations.history",
                                  data={"channel": "data-eng", "limit": "2",
                                        "cursor": cursor})).json()
    assert cursor
    assert len(first["messages"]) == len(second["messages"]) == 2
    assert not {message["ts"] for message in first["messages"]} & {
        message["ts"] for message in second["messages"]}


# --------------------------------------------------------------------------
# Confluence, driven by atlassian-python-api
# --------------------------------------------------------------------------


def test_confluence_client_reads_the_wiki_the_kb_tools_serve(confluence_client):
    pages = list(confluence_client.get_all_pages_from_space("DE"))
    assert {page["title"] for page in pages} >= {"Runbook: nightly orders load"}

    by_title = confluence_client.get_page_by_title("DE", "Runbook: nightly orders load")
    assert by_title["results"][0]["id"] == RUNBOOK_ID

    page = confluence_client.get_page_by_id(RUNBOOK_ID, expand="body.storage")
    assert page["version"]["number"] >= 1
    assert "nightly orders load" in page["body"]["storage"]["value"].lower()
    # The edit date is the evidence that settles a contradiction between pages,
    # so it has to survive the translation.
    assert page["version"]["when"].startswith("2025-11-04")

    found = confluence_client.search_content('text ~ "backfill"')
    assert found["totalSize"] >= 1
    assert all(result["type"] == "page" for result in found["results"])


def test_confluence_update_bumps_the_version_and_lands_in_the_world(
        confluence_client, plane, tmp_path):
    before = confluence_client.get_page_by_id(RUNBOOK_ID)["version"]["number"]
    confluence_client.update_page(RUNBOOK_ID, "Runbook: nightly orders load",
                                  body="# Superseded\n\nSee the vendor feed SLA page.")
    after = confluence_client.get_page_by_id(RUNBOOK_ID)["version"]["number"]
    assert after == before + 1

    reader = Gateway(config(plane.root, tmp_path / "reader-audit"))
    try:
        page = reader.call("kb_get", {"page": RUNBOOK})["result"]
    finally:
        reader.close()
    assert page["body"].startswith("# Superseded")
    assert page["version"] == after
    assert page["last_edited_by"] == "agent"
    assert page["age_days"] == 0  # edited on the world clock, not months ago


def test_confluence_creates_a_page_under_an_existing_parent(confluence_client, plane,
                                                            tmp_path):
    created = confluence_client.create_page(
        "DE", POSTMORTEM_TITLE, "# Draft\n",
        parent_id=confluence_service.content_id("de/postmortems"))
    assert created["id"] == confluence_service.content_id(POSTMORTEM)

    reader = Gateway(config(plane.root, tmp_path / "reader-audit"))
    try:
        listing = reader.call("kb_list", {})["result"]
    finally:
        reader.close()
    entry = next(page for page in listing["pages"] if page["id"] == POSTMORTEM)
    assert entry["title"] == POSTMORTEM_TITLE
    assert entry["parent"] == "de/postmortems"


async def test_confluence_refuses_a_stale_version_and_a_rename(plane):
    """Two silent successes this app does not offer."""
    async with client(confluence_service) as http:
        stale = await http.put(f"/wiki/rest/api/content/{RUNBOOK_ID}",
                               json={"version": {"number": 1}, "title": None,
                                     "body": {"storage": {"value": "x"}}})
        rename = await http.put(f"/wiki/rest/api/content/{RUNBOOK_ID}",
                                json={"title": "Something else",
                                      "body": {"storage": {"value": "x"}}})
        nonsense = await http.get("/wiki/rest/api/content/search",
                                  params={"cql": 'creator = "kliu"'})
    assert stale.status_code == 409
    assert rename.status_code == 400 and "rename" in rename.json()["message"]
    assert nonsense.status_code == 400


# --------------------------------------------------------------------------
# One action, two transports, one row shape
# --------------------------------------------------------------------------

EQUIVALENT = (
    (jira_service, "GET", "/rest/api/3/issue/DATA-141", None, None,
     "issues_get", {"issue": "DATA-141"}),
    (jira_service, "GET", "/rest/api/3/search", None, {"jql": 'text ~ "amount"'},
     "issues_search", {"query": "amount"}),
    (jira_service, "GET", "/rest/api/3/search", None, {"jql": ""},
     "issues_list", {}),
    (jira_service, "POST", "/rest/api/3/issue/DATA-141/comment", {"body": "Looking."},
     None, "issues_comment", {"issue": "DATA-141", "body": "Looking."}),
    (jira_service, "POST", "/rest/api/3/issue/DATA-141/transitions",
     {"transition": {"id": "3"}}, None,
     "issues_update", {"issue": "DATA-141", "status": "in_progress"}),
    (jira_service, "PUT", "/rest/api/3/issue/DATA-141", {"fields": {"assignee": "agent"}},
     None, "issues_update", {"issue": "DATA-141", "assignee": "agent"}),
    (slack_service, "POST", "/api/conversations.list", None, None, "chat_channels", {}),
    (slack_service, "POST", "/api/conversations.history", None, {"channel": "data-eng"},
     "chat_history", {"channel": "data-eng"}),
    (slack_service, "POST", "/api/conversations.replies", None,
     {"channel": "data-eng", "ts": THREAD_TS},
     "chat_thread", {"message_id": "data-eng/0002"}),
    (slack_service, "POST", "/api/search.messages", None,
     {"query": "in:#data-eng still uploading"},
     "chat_search", {"query": "still uploading", "channel": "data-eng"}),
    (slack_service, "POST", "/api/chat.postMessage",
     {"channel": "data-eng", "text": "Re-reading."}, None,
     "chat_post", {"channel": "data-eng", "text": "Re-reading."}),
    (confluence_service, "GET", "/wiki/rest/api/content", None, None, "kb_list", {}),
    (confluence_service, "GET", f"/wiki/rest/api/content/{RUNBOOK_ID}", None, None,
     "kb_get", {"page": RUNBOOK}),
    (confluence_service, "GET", "/wiki/rest/api/content/search", None,
     {"cql": 'text ~ "backfill"'}, "kb_search", {"query": "backfill"}),
    (confluence_service, "PUT", f"/wiki/rest/api/content/{RUNBOOK_ID}",
     {"body": {"storage": {"value": "# Superseded\n"}}}, None,
     "kb_edit", {"page": RUNBOOK, "body": "# Superseded\n"}),
    (confluence_service, "POST", "/wiki/rest/api/content",
     {"title": POSTMORTEM_TITLE, "body": {"storage": {"value": "# Draft\n"}},
      "ancestors": [{"id": confluence_service.content_id("de/postmortems")}]}, None,
     "kb_create", {"page": POSTMORTEM, "title": POSTMORTEM_TITLE, "body": "# Draft\n",
                   "parent": "de/postmortems"}),
)


@pytest.mark.parametrize(
    "module,method,path,body,params,tool,args", EQUIVALENT,
    ids=[f"{case[0].NAME}:{case[5]}" for case in EQUIVALENT])
async def test_one_action_two_transports_one_classification(
        plane, mirror, module, method, path, body, params, tool, args):
    """The row native mode writes is the row MCP writes, but for tool and transport.

    This is the property the whole native experiment rests on: if the two
    transports classify the same action differently, every blindness verdict and
    every containment proof measured under one of them stops meaning anything
    under the other.
    """
    async with client(module) as http:
        response = await http.request(method, path, json=body, params=params)
    assert response.status_code < 400, response.text

    native = next(row for row in reversed(plane.written())
                  if row["tool"] == f"{module.NAME}.{tool}")
    served = mirror.call(tool, args)
    assert served["ok"], served["error"]
    mcp = read_rows(mirror.config.audit_dir / "actions.jsonl")[-1]

    assert native["targets"] == mcp["targets"], f"{native['tool']} vs {tool}"
    assert native["op_class"] == mcp["op_class"]
    assert native["read_targets"] == mcp["read_targets"]
    # `actor:` is the native transport's own flag (9.2.2 has no actor key).
    assert [flag for flag in native["flags"]
            if not flag.startswith("actor:")] == mcp["flags"]
    assert native["transport"] == "native"
    assert native["vts"] == mcp["vts"]
    # The vendor endpoint lives in `args`, never in the tool name.
    assert native["args"]["method"] == method and native["args"]["path"] == path


async def test_an_endpoint_that_ran_no_module_call_cannot_ship_a_row(plane):
    """The guard that replaces "remember to add it to EQUIVALENT".

    The 16 pairs above prove the endpoints that are in the table. What used to
    be unguarded was everything that is not: an endpoint added with a hand-named
    `op_class` and `targets` shipped a plausible row, and nothing failed. Now
    the classification comes off the observation an `Operations` method filled
    in, so an endpoint that ran no such method has nothing to write and says so
    - at the first request, naming the tool, with no row on file.

    The throwaway route below is what that mistake looks like: a span opened,
    something rendered, no module call made.
    """
    def forgotten(request: Request) -> dict:
        with Ledger(request, jira_service.NAME).span("issues_get", key="DATA-141"):
            return {"key": "DATA-141", "fields": {}}

    jira_service.app.add_api_route("/rest/api/3/forgotten", forgotten, methods=["GET"])
    try:
        async with client(jira_service) as http:
            with pytest.raises(UnclassifiedObservation) as refused:
                await http.get("/rest/api/3/forgotten")
    finally:
        jira_service.app.router.routes.pop()
    assert "jira.issues_get" in str(refused.value)
    assert plane.written() == []


# --------------------------------------------------------------------------
# The log, and what must never reach the agent
# --------------------------------------------------------------------------


async def test_every_request_writes_one_row_including_the_refusals(plane):
    """A refused request is still a request served, and leaves a row.

    An unaudited refusal is the hole the action log exists to close: the
    world-diff rule can only call a change unaudited if every attempt is on file.
    """
    async with client(jira_service) as http:
        await http.get("/rest/api/3/issue/DATA-141")
        await http.get("/rest/api/3/issue/DATA-000")
        await http.get("/rest/api/3/issue/DATA-141", auth=(EMAIL, "not-the-token"))
    async with client(slack_service) as http:
        await http.post("/api/conversations.history", params={"channel": "nope"})
        await http.post("/api/conversations.list",
                        headers={"Authorization": "Bearer xoxb-wrong"})

    written = plane.written()
    assert len(written) == 5
    assert [row["exit"] for row in written] == ["ok", "error", "error", "error", "error"]
    assert [row["tool"] for row in written] == [
        "jira.issues_get", "jira.issues_get", "jira.issues_get",
        "slack.chat_history", "slack.chat_channels"]
    assert [row["error"] and row["error"]["code"] for row in written] == [
        None, "issue_not_found", "unauthenticated", "channel_not_found", "invalid_auth"]
    # The caller travels as a flag, because 9.2.2 has no `actor` key.
    assert f"actor:{EMAIL}" in written[0]["flags"]
    assert not [flag for flag in written[2]["flags"] if flag.startswith("actor:")]
    for row in written:
        assert set(row) == set(native_audit.ROW_KEYS)
        assert row["op_class"] in ("read", "write")


async def test_no_author_side_annotation_reaches_any_native_response(plane):
    """`authoring` is stripped by `workflow.py`; going round it would leak the key.

    The same assertion `test_workflow.py` makes for the MCP surface, because the
    measurement it protects (F202) does not care which transport leaked.
    """
    bodies = []
    async with client(jira_service) as http:
        bodies.append((await http.get("/rest/api/3/search",
                                      params={"jql": ""})).text)
        bodies.append((await http.get("/rest/api/3/issue/DATA-141")).text)
    async with client(slack_service) as http:
        bodies.append((await http.post("/api/conversations.history",
                                       params={"channel": "data-eng"})).text)
        bodies.append((await http.post("/api/conversations.replies",
                                       params={"channel": "data-eng",
                                               "ts": THREAD_TS})).text)
    async with client(confluence_service) as http:
        bodies.append((await http.get("/wiki/rest/api/content",
                                      params={"expand": "body.storage"})).text)
        bodies.append((await http.get(f"/wiki/rest/api/content/{RUNBOOK_ID}",
                                      params={"expand": "body.storage"})).text)

    served = "\n".join(bodies)
    assert "authoring" not in served
    for note in authoring_notes():
        assert note not in served
    # And the same has to hold of the rows, which carry the workflow result.
    log = plane.rows.read_text(encoding="utf-8")
    assert "authoring" not in log
    for note in authoring_notes():
        assert note not in log


async def test_a_composed_read_costs_what_an_mcp_agent_would_have_paid(plane):
    """One row per MCP-equivalent sub-call, named after that sub-call.

    A JQL search with a text clause is an `issues_list`, an `issues_search` and
    one `issues_get` per row it returns - which is exactly the sequence an agent
    holding the tool list would have had to run to read the same tickets. A
    header read (the project, the statuses, the user directory) is not a
    question of its own and costs nothing, or `GET /issue/KEY` below would cost
    two rows where `issues_get` costs one.
    """
    async with client(jira_service) as http:
        found = (await http.get("/rest/api/3/search",
                                params={"jql": 'text ~ "revenue"'})).json()
    keys = [issue["key"] for issue in found["issues"]]
    written = plane.written()
    assert keys == ["DATA-141", "DATA-140"]
    assert [row["tool"] for row in written] == (
        ["jira.issues_list", "jira.issues_search"] + ["jira.issues_get"] * len(keys))
    assert [row["targets"] for row in written[2:]] == [[f"issue:{key}"] for key in keys]
    assert all(row["args"]["path"] == "/rest/api/3/search" for row in written)


async def test_an_expanded_listing_pays_per_page_and_a_plain_one_does_not(plane):
    """The same rule on the wiki: bodies cost what `kb_get` costs, or nothing."""
    async with client(confluence_service) as http:
        await http.get("/wiki/rest/api/content", params={"limit": 3})
        await http.get("/wiki/rest/api/content",
                       params={"limit": 3, "expand": "body.storage"})
    assert [row["tool"] for row in plane.written()] == [
        "confluence.kb_list", "confluence.kb_list",
        "confluence.kb_get", "confluence.kb_get", "confluence.kb_get"]


async def test_a_transition_carrying_a_note_is_two_actions(plane):
    """`update.comment` on a transition is a second action and a second row."""
    async with client(jira_service) as http:
        moved = await http.post("/rest/api/3/issue/DATA-141/transitions",
                                json={"transition": {"id": "3"},
                                      "update": {"comment": [{"add": {"body": "On it."}}]}})
    assert moved.status_code == 204
    assert [row["tool"] for row in plane.written()] == [
        "jira.issues_update", "jira.issues_comment"]
    assert all(row["targets"] == ["issue:DATA-141"] for row in plane.written())


def test_the_registry_contract_holds_for_all_three(plane):
    """`serve.py` mounts what `NAME`/`HOST`/`PORT`/`app` describe, on distinct ports."""
    modules = (jira_service, slack_service, confluence_service)
    assert [module.NAME for module in modules] == ["jira", "slack", "confluence"]
    assert [module.HOST for module in modules] == [
        "jira.internal", "slack.internal", "confluence.internal"]
    assert [module.PORT for module in modules] == [8080, 8081, 8090]
    for module in modules:
        assert "alkera-local-dev-token" in module.TOKEN


@pytest.mark.parametrize("module,items,target", [(jira_service, 8, "issues"),
                                                 (slack_service, 3, "chat"),
                                                 (confluence_service, 9, "kb")])
async def test_health_counts_the_world_rather_than_a_constant(plane, module, items, target):
    """The surface gate reads `items`, so it has to come out of the document.

    Counted here against the reference corpus: eight tickets, three channels,
    nine wiki pages. A service whose document went missing cannot answer this.
    """
    async with client(module) as http:
        health = await http.get("/health")
    assert health.json() == {"ok": True, "service": module.NAME, "items": items}
    row = plane.written()[-1]
    # No MCP tool asks this question, so the row keeps the vendor name.
    assert row["tool"] == f"{module.NAME}.health"
    # A probe must not put the world into the episode's target set.
    assert row["targets"] == [f"health:{module.NAME}"]
    assert row["read_targets"] == [target]


def test_the_unreachable_world_is_reported_rather_than_faked(tmp_path, monkeypatch):
    """A service over a world with no chat document says so, in Slack's spelling."""
    settings = tmp_path / "gateway.json"
    empty = tmp_path / "empty"
    (empty / "workspace").mkdir(parents=True)
    settings.write_text(json.dumps({"workspace": str(empty / "workspace"),
                                    "workflow_dir": str(empty / "var" / "workflow"),
                                    "audit_dir": str(tmp_path / "audit")}), encoding="utf-8")
    monkeypatch.setenv(native_context.CONFIG_ENV, str(settings))
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(tmp_path / "audit" / "actions.jsonl"))
    with serving(slack_service.app) as base:
        response = httpx.post(f"{base}/api/conversations.list",
                              headers={"Authorization": f"Bearer {slack_service.TOKEN}"})
        health = httpx.get(f"{base}/health")
    assert response.json() == {"ok": False, "error": "team_not_found"}
    # 503 and the refusal's code, never a zero that reads as wired-and-empty.
    assert health.status_code == 503
    assert health.json() == {"ok": False, "service": "slack", "items": 0,
                             "error": "no_chat"}


def test_confluence_hides_a_body_until_it_is_expanded(confluence_client):
    """Real Confluence gates the body behind `expand`, and so does this."""
    with pytest.raises((KeyError, HTTPError)):
        confluence_client.get_page_by_id(RUNBOOK_ID)["body"]["storage"]["value"]
    assert confluence_client.get_page_by_id(
        RUNBOOK_ID, expand="body.storage")["body"]["storage"]["value"]
