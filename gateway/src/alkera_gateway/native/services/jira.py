"""Jira Cloud REST v3 - the subset a data engineer's morning needs.

Why a fake Jira rather than the `issues_*` tools (gateway/NATIVE_SURFACES.md):
in native mode nobody hands the agent a tool list. It gets `JIRA_URL` and a
token in `.envrc` and reaches for the client library it already knows. So the
acceptance test for this module is the python `jira` package driving it
unmodified, not curl.

What this is *not* is a second copy of the backlog. Every read and every write
runs `alkera_gateway.operations.Operations` - the very methods `issues_get` and
`issues_comment` are - so `var/workflow/issues.json` stays the single source of
truth, writes are stamped by the virtual clock, and the author-side `authoring`
blocks are stripped by the same code path that strips them for MCP. The vendor
shape adds a wire format and nothing else.

That choice is also where every row's `op_class` and `targets` come from: the
`records.Observation` a handler hands `Operations` is the one the MCP gateway
hands the same method, and the row is written off it afterwards. No endpoint
here names a target. Rows are named `jira.<mcp_tool>` wherever an MCP
equivalent exists, so one suffix-match rule serves graders and
`rollout.py --reclassify` across both native planes; the vendor endpoint stays
visible in `args.method` / `args.path`. `native/context.py`'s `Ledger` states
which module calls cost a row and which do not. HUMAN_PLANE.md carries the
table and a test diffs the rows against MCP's.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from datetime import UTC, datetime

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from ...records import ToolError
from ..context import DEV_TOKEN, Ledger, context_of, note, service_health

NAME = "jira"
HOST = "jira.internal"
PORT = 8080

# Repo rule: a credential in the workspace is obviously fake, never a realistic
# key format. Any email is accepted as the username; only the token is checked.
TOKEN = DEV_TOKEN

# `issues_list` and `issues_search` both take a limit. JQL paging is applied
# after the filter, so the module call has to see the whole backlog.
ALL = 100_000

# Jira status categories. The world declares its own `statuses` vocabulary
# (WORKFLOW_CONTRACT.md), so the category is inferred from the name rather than
# looked up: "done"-like names close, "progress"/"review"/"blocked"/"triage" are
# in flight, everything else is new. Wrong only for a vocabulary that names a
# terminal state something unrecognisable, which no theme does today.
DONE_STATUSES = ("done", "closed", "resolved", "wontfix", "rejected")
BUSY_STATUSES = ("progress", "review", "blocked", "triage")
CATEGORIES = {
    "done": {"id": 3, "key": "done", "colorName": "green", "name": "Done"},
    "indeterminate": {"id": 4, "key": "indeterminate", "colorName": "yellow",
                      "name": "In Progress"},
    "new": {"id": 2, "key": "new", "colorName": "blue-gray", "name": "To Do"},
}

ORDER_FIELDS = {"updated": "updated_at", "created": "created_at", "key": "id", "status": "status"}
ORDER_BY = re.compile(r"\border\s+by\s+(?P<field>[a-z]+)(?:\s+(?P<direction>asc|desc))?\s*$", re.I)
CLAUSE = re.compile(r"^(?P<field>[a-z]+)\s*(?P<op>!=|=|~|\bin\b)\s*(?P<value>.+)$", re.I)
AND = re.compile(r"\s+and\s+", re.I)

# The fields JQL and `fields=` may name. The python `jira` client fetches this
# list to translate clause names to ids before it searches, so serving it is
# what makes the real client work, not decoration.
FIELDS = (
    ("summary", "Summary", ["summary"]),
    ("description", "Description", ["description"]),
    ("status", "Status", ["status"]),
    ("assignee", "Assignee", ["assignee"]),
    ("reporter", "Reporter", ["reporter"]),
    ("labels", "Labels", ["labels"]),
    ("created", "Created", ["created", "createdDate"]),
    ("updated", "Updated", ["updated", "updatedDate"]),
    ("comment", "Comment", ["comment"]),
    ("project", "Project", ["project"]),
    ("issuetype", "Issue Type", ["issuetype", "type"]),
)

app = FastAPI(title="Jira Cloud (alkera native surface)", docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------
# The caller, and the refusals this app makes in Jira's spelling. The world,
# the rows and the ledger it writes them through are `native/context.py` now:
# item 1 of SERVICES_A_REQUESTS.md asked for one home for the three copies of
# this block, and that is it.
# --------------------------------------------------------------------------


class Refused(Exception):
    """A request this app declines, carrying the status the vendor would use."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def principal(request: Request) -> str:
    """HTTP Basic: any email plus the workspace token. Anything else is 401.

    Returned rather than discarded because it becomes the `actor` on every row
    the request writes: in native mode the transport knows who called, and the
    log should say so.
    """
    header = request.headers.get("authorization", "")
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic":
        raise Refused(401, "unauthenticated", "Basic authentication is required")
    try:
        user, _, secret = base64.b64decode(encoded, validate=True).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise Refused(401, "unauthenticated", "Malformed Basic credentials") from None
    if secret != TOKEN or not user:
        raise Refused(401, "unauthenticated",
                      "Client must be authenticated to access this resource.")
    return user


@app.exception_handler(Refused)
async def _refused(request: Request, exc: Refused) -> JSONResponse:
    headers = {"WWW-Authenticate": 'Basic realm="alkera"'} if exc.status == 401 else {}
    return JSONResponse({"errorMessages": [exc.message], "errors": {}},
                        status_code=exc.status, headers=headers)


@app.exception_handler(ToolError)
async def _tool_error(request: Request, exc: ToolError) -> JSONResponse:
    status = 404 if exc.code.endswith("_not_found") or exc.code.startswith("no_") else 400
    return JSONResponse({"errorMessages": [str(exc)], "errors": {}}, status_code=status)


# --------------------------------------------------------------------------
# World records -> Jira resources
# --------------------------------------------------------------------------


def numeric_id(text: str) -> str:
    """A stable numeric id for a world key, because Jira ids are numbers.

    Derived from the key, so two builds of the same world mint the same id and a
    recorded trajectory stays replayable.
    """
    return str(int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:9], 16))


def stamp(value: str | None) -> str | None:
    """World ISO-8601 (`2026-03-05T08:12:00Z`) -> Jira's `+0000` form."""
    if not value:
        return None
    moment = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def adf(text: str) -> dict:
    """Plain text -> Atlassian Document Format, which is what v3 serves."""
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": line}] if line else []}
            for line in text.split("\n")
        ],
    }


def plain(body: object) -> str:
    """ADF (or a bare string) -> the text the world stores.

    The python `jira` client posts a bare string to the v3 comment endpoint even
    though real Jira Cloud requires ADF there; both are accepted, and
    HUMAN_PLANE.md records the discrepancy rather than hiding it.
    """
    if isinstance(body, str):
        return body
    if isinstance(body, dict):
        if body.get("type") == "text":
            return body.get("text", "")
        return "\n".join(plain(node) for node in body.get("content", []))
    return ""


def category(status: str) -> dict:
    lowered = status.lower()
    if lowered in DONE_STATUSES:
        return CATEGORIES["done"]
    if any(word in lowered for word in BUSY_STATUSES):
        return CATEGORIES["indeterminate"]
    return CATEGORIES["new"]


def status_resource(base: str, status: str) -> dict:
    return {
        "self": f"{base}/rest/api/3/status/{numeric_id(status)}",
        "id": numeric_id(status),
        "name": status,
        "statusCategory": category(status),
    }


def user_resource(base: str, users: dict, user_id: str | None) -> dict | None:
    if user_id is None:
        return None
    person = users.get(user_id, {"id": user_id, "name": user_id})
    return {
        "self": f"{base}/rest/api/3/user?accountId={user_id}",
        "accountId": user_id,
        "displayName": person.get("name", user_id),
        "emailAddress": f"{user_id}@alkera.local",
        "active": True,
        "accountType": "atlassian",
    }


def project_resource(base: str, project: dict) -> dict:
    return {
        "self": f"{base}/rest/api/3/project/{numeric_id(project['key'])}",
        "id": numeric_id(project["key"]),
        "key": project["key"],
        "name": project.get("name", project["key"]),
        "projectTypeKey": "software",
    }


def comment_resource(base: str, issue: str, entry: dict, users: dict) -> dict:
    author = user_resource(base, users, entry["author"])
    return {
        "self": f"{base}/rest/api/3/issue/{issue}/comment/{numeric_id(entry['id'])}",
        "id": numeric_id(entry["id"]),
        "author": author,
        "updateAuthor": author,
        "body": adf(entry["body"]),
        "created": stamp(entry["at"]),
        "updated": stamp(entry["at"]),
        "jsdPublic": True,
    }


def issue_resource(base: str, full: dict, project: dict, users: dict) -> dict:
    """One `issues_get` result rendered as a v3 issue."""
    key = full["id"]
    comments = [comment_resource(base, key, entry, users) for entry in full.get("comments", [])]
    return {
        "expand": "renderedFields,names,schema,operations,editmeta,changelog",
        "id": numeric_id(key),
        "self": f"{base}/rest/api/3/issue/{key}",
        "key": key,
        "fields": {
            "summary": full["title"],
            "description": adf(full.get("body", "")),
            "issuetype": {"id": "10001", "name": "Task", "subtask": False},
            "project": project_resource(base, project),
            "status": status_resource(base, full["status"]),
            "reporter": user_resource(base, users, full.get("reporter")),
            "assignee": user_resource(base, users, full.get("assignee")),
            "labels": list(full.get("labels", [])),
            "created": stamp(full.get("created_at")),
            "updated": stamp(full.get("updated_at")),
            "comment": {"comments": comments, "maxResults": len(comments),
                        "total": len(comments), "startAt": 0},
            "issuelinks": [
                {"id": numeric_id(f"{key}:{link['type']}:{link['issue']}"),
                 "type": {"name": link["type"], "inward": link["type"],
                          "outward": link["type"]},
                 "outwardIssue": {"key": link["issue"],
                                  "self": f"{base}/rest/api/3/issue/{link['issue']}"}}
                for link in full.get("links", [])
            ],
        },
    }


def unpack(listing: dict) -> tuple[dict, list[str], dict]:
    """One `issues_list` result -> (project, status vocabulary, users by id).

    All three of the headers a Jira resource has to name, out of one module
    result. `project` arrives there because SERVICES_A_REQUESTS.md item 3 was
    taken up in `operations.py`; before that it was the one thing this module
    read out of a raw workflow document.
    """
    return (listing["project"], listing["statuses"],
            {person["id"]: person for person in listing["users"]})


def directory(operations, observation) -> tuple[dict, list[str], dict]:
    """The header read, spelled as a call `Span.aside` can run: costs no row."""
    return unpack(operations.issues_list(None, None, None, 0, observation))


def base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


# --------------------------------------------------------------------------
# JQL
# --------------------------------------------------------------------------


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_jql(text: str) -> dict:
    """Parse the JQL subset this app understands.

    Supported: `project = K`, `status = S`, `status in (A, B)`, `assignee = U`,
    `labels = L`, `text ~ "words"`, joined by `AND`, optionally followed by
    `ORDER BY <updated|created|key|status> [ASC|DESC]`.

    Anything else is refused with 400 rather than quietly ignored, because a
    filter that silently does nothing is how an agent concludes that a backlog
    it cannot search is empty.
    """
    query = {"project": None, "statuses": None, "assignee": None, "label": None,
             "text": None, "order": "updated_at", "descending": True}
    text = (text or "").strip()
    match = ORDER_BY.search(text)
    if match:
        field = match.group("field").lower()
        if field not in ORDER_FIELDS:
            raise Refused(400, "invalid_jql",
                          f"Cannot order by {field!r}; this server orders by "
                          + ", ".join(sorted(ORDER_FIELDS)))
        query["order"] = ORDER_FIELDS[field]
        query["descending"] = (match.group("direction") or "desc").lower() == "desc"
        text = text[: match.start()].strip()
    if not text:
        return query
    for part in AND.split(text):
        clause = CLAUSE.match(part.strip())
        if not clause:
            raise Refused(400, "invalid_jql", f"Cannot parse JQL clause: {part.strip()!r}")
        field, operator = clause.group("field").lower(), clause.group("op").lower()
        value = clause.group("value").strip()
        if field == "project" and operator == "=":
            query["project"] = unquote(value)
        elif field == "status" and operator == "=":
            query["statuses"] = [unquote(value)]
        elif field == "status" and operator == "in":
            query["statuses"] = [unquote(item) for item in value.strip("()").split(",")]
        elif field == "assignee" and operator == "=":
            query["assignee"] = unquote(value)
        elif field == "labels" and operator == "=":
            query["label"] = unquote(value)
        elif field in ("text", "summary", "description") and operator == "~":
            query["text"] = unquote(value)
        else:
            raise Refused(400, "invalid_jql",
                          f"Unsupported JQL clause {part.strip()!r}; this server understands "
                          "project =, status = / in, assignee =, labels = and text ~")
    return query


def query_page(log: Ledger, jql: str, start: int, size: int) -> tuple[list, int]:
    """Run a JQL query and render one page of it, paying a row per sub-call.

    `issues_list` does the status/assignee/label filtering and `issues_search`
    does the text match, so the semantics an agent sees over JQL are the
    semantics those two tools already have, case folding included. Each
    returned row is then fetched with `issues_get`, which is both what the
    vendor shape needs (a v3 search result carries full fields) and what an
    agent reading the same tickets over MCP would have paid.
    """
    request = log.request
    with log.span("issues_list", jql=jql, startAt=start, maxResults=size) as row:
        log.actor = principal(request)
        query = parse_jql(jql)
        single = (query["statuses"][0] if query["statuses"] and len(query["statuses"]) == 1
                  else None)
        listing = row.run(lambda ops, obs: ops.issues_list(
            single, query["assignee"], query["label"], ALL, obs))

    issues = listing["issues"]
    if query["statuses"] and single is None:
        wanted = set(query["statuses"])
        issues = [issue for issue in issues if issue["status"] in wanted]
    if query["project"]:
        prefix = f"{query['project']}-"
        issues = [issue for issue in issues if issue["id"].startswith(prefix)]
    if query["text"]:
        with log.span("issues_search", query=query["text"]) as row:
            hits = row.run(lambda ops, obs: ops.issues_search(query["text"], ALL, obs))
        matched = {match["issue"] for match in hits["matches"]}
        issues = [issue for issue in issues if issue["id"] in matched]

    ordered = sorted(issues, key=lambda issue: (issue[query["order"]], issue["id"]),
                     reverse=query["descending"])
    project, _, users = unpack(listing)
    base, rendered = base_url(request), []
    for summary in ordered[start : start + size]:
        with log.span("issues_get", key=summary["id"]) as row:
            # `key=` binds this iteration's id into the call. A lambda that read
            # the loop variable would be a live reference to it (ruff B023).
            full = row.run(lambda ops, obs, key=summary["id"]: ops.issues_get(key, obs))
        rendered.append(issue_resource(base, full, project, users))
    return rendered, len(ordered)


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@app.get("/health")
def health(request: Request) -> object:
    """Liveness for the surface gate, and the one endpoint an agent never calls.

    `items` is counted out of the world on every request, through the same
    module call the handlers use - a constant here would let a service wired to
    nothing pass the gate, which is the one thing the gate exists to catch. A
    world with no issue document answers 503 and the refusal's code, never a
    zero that reads as "wired, and empty".

    Unauthenticated, because a gate that fails on a credential has not told
    anyone whether the surface is up. Audited like every other request, but
    under the target `health:jira` rather than `issues`: a harness that polls
    health must not inject the episode's target set with rows no agent caused.
    `read_targets` still says what was really read. `context.service_health`
    holds that convention for all nine services.
    """
    return service_health(
        request, NAME, lambda ops, obs: ops.issues_list(None, None, None, 0, obs)["total"]
    )


@app.get("/rest/api/3/serverInfo")
def server_info(request: Request) -> dict:
    """What the python `jira` client fetches before anything else.

    `deploymentType: Cloud` is the truth about which API shape this is, and it
    is what routes that client to `/rest/api/3/search/jql`. No MCP tool asks
    this question, so the row keeps the vendor name; the question it does ask of
    the world is `issues_list`, for the project title, and that is what
    classifies the row.
    """
    log = Ledger(request, NAME)
    with log.span("server_info") as row:
        log.actor = principal(request)
        project, _, _ = unpack(row.run(
            lambda ops, obs: ops.issues_list(None, None, None, 0, obs)))
        return {
            "baseUrl": base_url(request),
            "version": "1001.0.0-SNAPSHOT",
            "versionNumbers": [1001, 0, 0],
            "deploymentType": "Cloud",
            "buildNumber": 100_285,
            "serverTitle": project.get("name", project["key"]),
            "serverTime": stamp(context_of(request.app).clock()),
        }


@app.get("/rest/api/3/myself")
def myself(request: Request) -> dict:
    """The account the workspace's credentials belong to.

    That is `workflow_actor` - the persona a write is attributed to - not the
    Basic-auth email, so the agent can discover the name its comments will carry.
    """
    log = Ledger(request, NAME)
    with log.span("myself") as row:
        log.actor = principal(request)
        _, _, users = unpack(row.run(
            lambda ops, obs: ops.issues_list(None, None, None, 0, obs)))
        return user_resource(base_url(request), users,
                             context_of(request.app).config.workflow_actor)


@app.get("/rest/api/3/field")
def fields(request: Request) -> list[dict]:
    """The field directory the real client reads before it searches.

    The one endpoint in this module that asks the world nothing: the vocabulary
    is this file's `FIELDS`, so no module classifies it and `Ledger` refuses to
    write a row for an action no module performed. It goes through
    `context.note` instead - the same escape hatch `metabase.session_create`
    uses - under the `issues` target, because a field list is metadata about
    that surface and an unaudited request is the hole native mode exists to
    close.
    """
    try:
        principal(request)
    except Refused as exc:
        # A refused request is still a request served, so it leaves a row here
        # exactly as it would inside a span.
        note(request, NAME, "fields", op_class="read", targets=["issues"],
             error={"code": exc.code, "message": exc.message})
        raise
    note(request, NAME, "fields", op_class="read", targets=["issues"],
         result=[field for field, _, _ in FIELDS])
    return [
        {"id": field, "key": field, "name": label, "custom": False, "navigable": True,
         "searchable": True, "orderable": field in ORDER_FIELDS, "clauseNames": clauses}
        for field, label, clauses in FIELDS
    ]


@app.get("/rest/api/3/issue/{key}")
def issue_get(key: str, request: Request) -> dict:
    """GET one issue. The equivalent of `issues_get`, and one row."""
    log = Ledger(request, NAME)
    with log.span("issues_get", key=key) as row:
        log.actor = principal(request)
        full = row.run(lambda ops, obs: ops.issues_get(key, obs))
        project, _, users = row.aside(directory)
        return issue_resource(base_url(request), full, project, users)


@app.get("/rest/api/3/search")
def search(request: Request, jql: str = "", startAt: int = 0, maxResults: int = 50) -> dict:
    """The offset-paged search. See `query_page` for what it costs in rows."""
    log = Ledger(request, NAME)
    size = max(0, min(maxResults, ALL))
    issues, total = query_page(log, jql, startAt, size)
    return {"expand": "names,schema", "startAt": startAt, "maxResults": size,
            "total": total, "issues": issues, "isLast": startAt + len(issues) >= total}


@app.get("/rest/api/3/search/jql")
def search_jql(request: Request, jql: str = "", maxResults: int = 50,
               nextPageToken: str | None = None) -> dict:
    """The token-paged Cloud search, which is where the real client goes.

    The token is the offset it resumes from: opaque to the client, stable across
    calls, and no server-side cursor state to keep alive between requests.
    """
    log = Ledger(request, NAME)
    try:
        start = int(nextPageToken) if nextPageToken else 0
    except ValueError:
        start = -1
    if start < 0:
        with log.span("issues_list", jql=jql, nextPageToken=nextPageToken):
            log.actor = principal(request)
            raise Refused(400, "invalid_token", "nextPageToken is not a page offset")
    size = max(0, min(maxResults, ALL))
    issues, total = query_page(log, jql, start, size)
    last = start + len(issues) >= total
    return {"issues": issues, "isLast": last,
            "nextPageToken": None if last else str(start + len(issues))}


@app.post("/rest/api/3/issue/{key}/comment", status_code=201)
def issue_comment(key: str, payload: dict, request: Request) -> dict:
    """POST a comment. The equivalent of `issues_comment`: same file, same clock."""
    body = plain(payload.get("body"))
    log = Ledger(request, NAME)
    with log.span("issues_comment", key=key, body=body) as row:
        log.actor = principal(request)
        if not body:
            raise Refused(400, "invalid_body", "A comment needs a body")
        result = row.run(lambda ops, obs: ops.issues_comment(key, body, obs))
        _, _, users = row.aside(directory)
        return comment_resource(base_url(request), key, result["comment"], users)


@app.get("/rest/api/3/issue/{key}/transitions")
def transitions_get(key: str, request: Request) -> dict:
    """The statuses this ticket can move to - every other status the world declares.

    The world's `statuses` list *is* the workflow (WORKFLOW_CONTRACT.md), so
    there is no transition graph to invent; an id is a position in that list,
    which makes it stable for a given world. The one question this asks of the
    world is "what status is this ticket in", so the row is an `issues_get`.
    """
    log = Ledger(request, NAME)
    with log.span("issues_get", key=key) as row:
        log.actor = principal(request)
        full = row.run(lambda ops, obs: ops.issues_get(key, obs))
        _, statuses, _ = row.aside(directory)
        base = base_url(request)
        return {"expand": "transitions", "transitions": [
            {"id": str(index + 1), "name": status, "hasScreen": False, "isGlobal": True,
             "isInitial": False, "isAvailable": True, "isConditional": False,
             "to": status_resource(base, status)}
            for index, status in enumerate(statuses) if status != full["status"]
        ]}


@app.post("/rest/api/3/issue/{key}/transitions", status_code=204)
def issue_transition(key: str, payload: dict, request: Request) -> Response:
    """Move a ticket. The equivalent of `issues_update` with a status.

    A transition may also carry `update.comment`, which is how the client library
    attaches a note to a re-triage. That is a second action and it costs a second
    row, `issues_comment`, exactly as it would over MCP.
    """
    transition = (payload.get("transition") or {}).get("id")
    supplied = payload.get("fields") or {}
    annotation = comment_add(payload.get("update") or {})
    log = Ledger(request, NAME)
    with log.span("issues_update", key=key, transition=transition, fields=supplied) as row:
        log.actor = principal(request)
        # Resolving a transition id against the world's status vocabulary is a
        # header read: part of the write, and no row of its own.
        _, statuses, _ = row.aside(directory)
        if transition is None:
            raise Refused(400, "invalid_transition", "Name the transition to perform")
        try:
            status = statuses[int(transition) - 1]
        except (ValueError, IndexError):
            raise Refused(404, "transition_not_found",
                          f"Unknown transition {transition!r}; ask "
                          f"/rest/api/3/issue/{key}/transitions") from None
        assignee, unassign = assignee_of(supplied)
        # `issues_update` is what flags a write that changed nothing, so the
        # native row carries `no_change` without this module knowing the word.
        row.run(lambda ops, obs: ops.issues_update(
            key, status, assignee, unassign, list(supplied.get("labels") or []), [], obs))
    if annotation:
        with log.span("issues_comment", key=key, body=annotation) as row:
            row.run(lambda ops, obs: ops.issues_comment(key, annotation, obs))
    return Response(status_code=204)


@app.put("/rest/api/3/issue/{key}", status_code=204)
def issue_update(key: str, payload: dict, request: Request) -> Response:
    """Edit a ticket's assignee or labels. The equivalent of `issues_update`.

    `fields.labels` replaces the set and `update.labels` adds and removes items;
    both are expressed as the add/remove pair `issues_update` takes, so the
    history rows the world records are the ones that tool would have written.
    Reading the current labels to compute that pair is part of the write, not a
    question of its own, so this endpoint costs exactly one row.
    """
    supplied = payload.get("fields") or {}
    update = payload.get("update") or {}
    log = Ledger(request, NAME)
    with log.span("issues_update", key=key, fields=supplied, update=update) as row:
        log.actor = principal(request)
        assignee, unassign = assignee_of(supplied)
        add, remove = label_edits(row, key, supplied, update)
        if assignee is None and not unassign and not add and not remove:
            raise Refused(400, "invalid_update",
                          "Name an assignee or some labels; a status moves through "
                          f"/rest/api/3/issue/{key}/transitions")
        row.run(lambda ops, obs: ops.issues_update(
            key, None, assignee, unassign, add, remove, obs))
        return Response(status_code=204)


def comment_add(update: dict) -> str:
    """`{"comment": [{"add": {"body": ...}}]}` -> the text, or the empty string."""
    for operation in update.get("comment") or []:
        added = operation.get("add") or {}
        if added:
            return plain(added.get("body"))
    return ""


def assignee_of(supplied: dict) -> tuple[str | None, bool]:
    """A v3 assignee field -> the `(assignee, unassign)` pair `issues_update` takes.

    `assignee: null` is Jira's unassign, and it is exactly why `issues_update`
    carries a separate boolean: once the field is optional, a null cannot be
    told apart from an absent one (WORKFLOW_CONTRACT.md).
    """
    if "assignee" not in supplied:
        return None, False
    value = supplied["assignee"]
    if value is None:
        return None, True
    if isinstance(value, dict):
        for field in ("accountId", "id", "name"):
            if value.get(field):
                return value[field], False
        return None, True
    return str(value), False


def label_edits(row, key: str, supplied: dict, update: dict) -> tuple[list, list]:
    """Either v3 label spelling -> the add/remove pair `issues_update` takes.

    `fields.labels` replaces the set, so the current labels have to be read to
    diff against. That read goes through `aside`, because it is a precondition
    of the write rather than a question of its own: charging it a row would make
    a native `PUT` cost two where `issues_update` costs one.
    """
    add, remove = [], []
    for operation in update.get("labels") or []:
        if "add" in operation:
            add.append(operation["add"])
        if "remove" in operation:
            remove.append(operation["remove"])
    if "labels" in supplied:
        wanted = list(supplied["labels"] or [])
        current = row.aside(lambda ops, obs: ops.issues_get(key, obs)).get("labels", [])
        add += [label for label in wanted if label not in current]
        remove += [label for label in current if label not in wanted]
    return add, remove
