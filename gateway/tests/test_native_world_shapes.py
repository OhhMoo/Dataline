"""The human plane over the workflow shapes the task corpus actually ships.

`workflow_seed` spells every header and record one way. The tasks do not:
GT002 declares `project: DATA` and `space: de` as bare keys, GT001/GT003/GT005
declare the chat workspace as `{id, name}`, GT002/GT003/GT005/GT006 link issues
with `kind` rather than `type`, and GT005's chat messages carry `body` rather
than `text`. The world build validates none of those spellings, and each one
was a 500 on every read that touched it in the Opus decision runs (the audit
found `jira.issues_get` KeyError 'type', `confluence.kb_list` TypeError and
`slack.chat_history` KeyError 'text' in the action logs). Each test below
rewrites the seeded world into one of those spellings and asserts on content,
not on a status alone.
"""

import base64
import json

import pytest
from conftest import config
from fastapi.testclient import TestClient

from alkera_gateway.native import context as native_context
from alkera_gateway.native.services import confluence as confluence_service
from alkera_gateway.native.services import jira as jira_service
from alkera_gateway.native.services import slack as slack_service

EMAIL = "agent@alkera.local"
RUNBOOK = "de/runbooks/nightly-orders-load"


def rewrite(world, name, change):
    path = world / "var" / "workflow" / name
    document = json.loads(path.read_text(encoding="utf-8"))
    change(document)
    path.write_text(json.dumps(document), encoding="utf-8")


@pytest.fixture
def serve(world, tmp_path):
    """A client for one service app over the (rewritten) fixture world."""
    audit = tmp_path / "audit"
    audit.mkdir()
    context = native_context.Context(config(world, audit))
    apps = []

    def client(module):
        native_context.set_context(module.app, context)
        apps.append(module.app)
        if module is slack_service:
            return TestClient(module.app, headers={"Authorization": f"Bearer {module.TOKEN}"})
        basic = base64.b64encode(f"{EMAIL}:{module.TOKEN}".encode()).decode()
        return TestClient(module.app, headers={"Authorization": f"Basic {basic}"})

    yield client
    for app in apps:
        app.state.alkera_context = None


def test_an_issue_linked_by_kind_renders_and_searches(world, serve):
    def to_kind(document):
        for issue in document["issues"]:
            issue["links"] = [{"issue": link["issue"], "kind": link["type"]}
                              for link in issue.get("links", [])]
    rewrite(world, "issues.json", to_kind)
    jira = serve(jira_service)

    issue = jira.get("/rest/api/3/issue/DATA-141")
    assert issue.status_code == 200, issue.text
    [link] = issue.json()["fields"]["issuelinks"]
    assert link["type"]["name"] == "relates_to"
    assert link["outwardIssue"]["key"] == "DATA-136"

    # The search renders every hit with `issues_get`, so one linked issue in
    # the result set used to fail the whole page.
    found = jira.get("/rest/api/3/search/jql", params={"jql": "project = DATA"})
    assert found.status_code == 200, found.text
    assert "DATA-141" in {hit["key"] for hit in found.json()["issues"]}


def test_a_bare_project_key_is_the_project_the_issues_name(world, serve):
    rewrite(world, "issues.json", lambda document: document.update(project="DATA"))
    issue = serve(jira_service).get("/rest/api/3/issue/DATA-141")
    assert issue.status_code == 200, issue.text
    # Not the `JIRA` fallback: `"key" in "DATA"` is a substring test.
    assert issue.json()["fields"]["project"]["key"] == "DATA"


def test_a_bare_space_key_serves_the_wiki(world, serve):
    rewrite(world, "kb.json", lambda document: document.update(space="de"))
    wiki = serve(confluence_service)

    listing = wiki.get("/wiki/rest/api/content", params={"spaceKey": "de", "limit": 100})
    assert listing.status_code == 200, listing.text
    titles = {page["title"] for page in listing.json()["results"]}
    assert "Runbook: nightly orders load" in titles
    assert {page["space"]["key"] for page in listing.json()["results"]} == {"de"}

    page = wiki.get(f"/wiki/rest/api/content/{confluence_service.content_id(RUNBOOK)}",
                    params={"expand": "body.storage"})
    assert page.status_code == 200, page.text
    assert "nightly orders load" in page.json()["body"]["storage"]["value"].lower()


def test_a_workspace_mapping_is_served_as_its_name(world, serve):
    rewrite(world, "chat.json",
            lambda document: document.update(workspace={"id": "acme", "name": "Acme"}))
    answer = serve(slack_service).post("/api/auth.test").json()
    assert answer["ok"] is True
    assert answer["team"] == "Acme"
    assert answer["team_id"] == slack_service.slack_id("T", "Acme")


def test_chat_messages_authored_with_body_are_read_as_text(world, serve):
    def to_body(document):
        for channel in document["channels"]:
            for message in channel["messages"]:
                message["body"] = message.pop("text")
    rewrite(world, "chat.json", to_body)
    slack = serve(slack_service)

    history = slack.post("/api/conversations.history", data={"channel": "data-eng"}).json()
    assert history["ok"] is True, history
    assert history["messages"] and all(message["text"] for message in history["messages"])

    needle = history["messages"][0]["text"].split()[0]
    search = slack.post("/api/search.messages", data={"query": needle}).json()
    assert search["ok"] is True, search
    assert search["messages"]["matches"]
