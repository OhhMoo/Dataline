"""The Confluence REST subset the `kb_*` tools cover (gateway/NATIVE.md).

Same bargain as jira.py and slack.py: the agent is handed a wiki URL and a
token, so the acceptance test is `atlassian.Confluence` driving this app, not
curl. Every read and every write runs `alkera_gateway.operations.Operations` -
the very methods `kb_get` and `kb_edit` are - so `var/workflow/kb.json` stays
the single source of truth, the virtual clock stamps the edits, `version` is
bumped by the same code, the author-side `authoring` blocks are stripped by the
same code path, and each row's `op_class` and `targets` are read off the same
`records.Observation` the MCP gateway hands that method rather than named here.

Two translations are worth naming up front:

- **Ids.** A world page id is a slash-separated path (`de/runbooks/nightly-orders
  -load`) and a Confluence content id is a number. The number here is derived
  from the path, so two builds of the same world mint the same id and a recorded
  trajectory stays replayable; the path travels back in `_links.webui`.
- **Storage format.** Confluence's `storage` representation is XHTML; the world's
  page bodies are markdown, and they are served verbatim under
  `body.storage.value`. Converting would make every round trip lossy against the
  document the grader reads, which is a worse lie than the format label.

Two API versions, one wiki. Confluence Cloud serves REST v1 under
`/wiki/rest/api/...` and REST v2 under `/wiki/api/v2/...` ("Confluence Cloud
REST API v2", OpenAPI https://dac-static.atlassian.com/cloud/confluence/
openapi-v2.v3.json, servers `https://{your-domain}/wiki/api/v2`). Both are
served here over the same pages and the same rows; v2 is the subset an
engineer reads a page with (`GET /pages/{id}?body-format=storage`, `GET
/pages?title=&space-id=`, `GET /spaces`, `GET /pages/{id}/children` and
`/direct-children`) and edits it with (`PUT /pages/{id}` carrying `version`).
Measured 2026-10-04 (BI001 K=8 Opus run): four rollouts asked `$CONFLUENCE_URL/
wiki/api/v2/pages/<id>?body-format=storage`, and it 404'd. What still 404s, as
it does on Cloud, is the same paths without `/wiki` (`/rest/api/content/...`,
`/api/v2/pages/...`): the Cloud REST APIs live under the site's `/wiki`.

Rows are named `confluence.<mcp_tool>` wherever an MCP equivalent exists, so one
suffix-match rule serves graders and `rollout.py --reclassify` across both
native planes; the vendor endpoint stays visible in `args.method` / `args.path`.
`native/context.py`'s `Ledger` states which module calls cost a row and which do
not. HUMAN_PLANE.md carries the table and a test diffs the rows against MCP's.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from datetime import UTC, datetime
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ...operations import named
from ...records import ToolError
from ..context import DEV_TOKEN, Ledger, Span, audit_unrouted, service_health
from .jira import adf, plain

NAME = "confluence"
HOST = "confluence.internal"
PORT = 8090

# Repo rule: a credential in the workspace is obviously fake. Any username is
# accepted; only the token, passed as the Basic password, is checked.
TOKEN = DEV_TOKEN

# `kb_search` takes a limit; CQL paging is applied after the module call, so
# that call has to see the whole space.
ALL = 100_000
DEFAULT_LIMIT = 25

# The CQL subset: `text ~ "needle"`, optionally `AND type = page` and
# `AND space = KEY`. Anything else is refused rather than ignored - a search
# that silently matches everything is how an agent concludes a stale page is the
# only page.
CQL_CLAUSE = re.compile(r"^(?P<field>[a-z]+)\s*(?P<op>=|~)\s*(?P<value>.+)$", re.I)
CQL_AND = re.compile(r"\s+and\s+", re.I)
SLUG = re.compile(r"[^a-z0-9]+")

app = FastAPI(title="Confluence (alkera native surface)", docs_url=None, redoc_url=None)
# One row per request, including the ones no handler ran for (context.py).
audit_unrouted(app, NAME)


# --------------------------------------------------------------------------
# The caller, and the refusals this app makes in Confluence's spelling. The
# world, the rows and the ledger it writes them through are `native/context.py`
# now: item 1 of SERVICES_A_REQUESTS.md asked for one home for the three copies
# of this block, and that is it.
# --------------------------------------------------------------------------


class Refused(Exception):
    """A request this app declines, carrying the status the vendor would use."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def principal(request: Request) -> str:
    """HTTP Basic: any username plus the workspace token. Anything else is 401.

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
        raise Refused(401, "unauthenticated", "Invalid username or password")
    return user


V2 = "/wiki/api/v2"

# REST v2's error codes and titles. The OpenAPI document gives v2's error
# responses no body schema; Cloud answers `{"errors": [{status, code, title,
# detail}]}`, and that is the envelope served on v2 paths, with this app's
# reason in `detail`. v1 paths keep v1's `{statusCode, message, reason}`.
V2_ERRORS = {400: ("INVALID_REQUEST_PARAMETER", "Bad Request"),
             401: ("UNAUTHORIZED", "Unauthorized"), 404: ("NOT_FOUND", "Not Found"),
             409: ("CONFLICT", "Conflict")}


def error_body(request: Request, status: int, code: str, message: str) -> dict:
    if request.url.path.startswith(V2 + "/"):
        name, title = V2_ERRORS.get(status, ("ERROR", "Error"))
        return {"errors": [{"status": status, "code": name, "title": title,
                            "detail": f"{code}: {message}"}]}
    return {"statusCode": status, "message": message, "reason": code}


@app.exception_handler(Refused)
async def _refused(request: Request, exc: Refused) -> JSONResponse:
    headers = {"WWW-Authenticate": 'Basic realm="alkera"'} if exc.status == 401 else {}
    return JSONResponse(error_body(request, exc.status, exc.code, exc.message),
                        status_code=exc.status, headers=headers)


@app.exception_handler(ToolError)
async def _tool_error(request: Request, exc: ToolError) -> JSONResponse:
    status = 404 if exc.code.endswith("_not_found") or exc.code.startswith("no_") else 400
    if exc.code == "page_exists":
        status = 409
    return JSONResponse(error_body(request, status, exc.code, str(exc)), status_code=status)


# --------------------------------------------------------------------------
# World pages -> Confluence content
# --------------------------------------------------------------------------


def content_id(page_id: str) -> str:
    """A world page path -> a stable numeric Confluence content id."""
    return str(int(hashlib.sha256(page_id.encode("utf-8")).hexdigest()[:9], 16))


def stamp(value: str | None) -> str | None:
    """World ISO-8601 (`2025-11-04T10:12:00Z`) -> Confluence's `+0000` form."""
    if not value:
        return None
    moment = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000%z")


def slug(title: str) -> str:
    """A page title -> the last segment of a world page id.

    Confluence mints an opaque id for a new page and the world mints a path, so
    something has to bridge them; deriving the path from the title is what the
    seeded corpus already looks like (`de/postmortems/2026-03-05-flat-revenue`)
    and it keeps a created page addressable by a human afterwards.
    """
    return SLUG.sub("-", title.lower()).strip("-")


def expanded(expand: str | None) -> set[str]:
    return {part.strip() for part in (expand or "").split(",") if part.strip()}


def wants_body(want: set[str]) -> bool:
    return bool(want & {"body", "body.storage"})


def unpack(listing: dict) -> tuple[dict, dict]:
    """One `kb_list` result -> the pages by id and the space header.

    The header goes through `operations.named` because a theme may declare the
    space as the bare key (GT002: `space: de`), and indexing a string by `key`
    took down every page read in that world.
    """
    return ({page["id"]: page for page in listing["pages"]},
            named(listing.get("space"), {"key": "KB", "name": "Knowledge base"}))


def index(row: Span) -> tuple[dict, dict]:
    """The header and resolution read: `kb_list` through `aside`, and no row.

    Everything the rendering below needs except a body comes from here, which is
    also what keeps `age_days` honest - `kb_list` computes it against the world
    clock, not against anything this module knows.
    """
    return unpack(row.aside(lambda ops, obs: ops.kb_list(obs)))


def resolve(pages: dict, wanted: str) -> str:
    """A content id (or the page path itself) -> the world page id."""
    for page_id in pages:
        if wanted in (page_id, content_id(page_id)):
            return page_id
    raise Refused(404, "page_not_found",
                  "There is no content with the given id, or the calling user does "
                  "not have permission to view the content")


def space_number(space: dict) -> int:
    """The space's numeric id, derived from its key like a content id."""
    return int(content_id(space["key"])) % 1_000_000


def space_object(base: str, space: dict) -> dict:
    return {
        "id": space_number(space),
        "key": space["key"],
        "name": space.get("name", space["key"]),
        "type": "global",
        "status": "current",
        "_links": {"webui": f"/spaces/{space['key']}",
                   "self": f"{base}/wiki/rest/api/space/{space['key']}"},
    }


def user_object(user_id: str | None) -> dict | None:
    if not user_id:
        return None
    return {"type": "known", "accountId": user_id, "displayName": user_id,
            "email": f"{user_id}@alkera.local", "accountType": "atlassian"}


def content_object(base: str, entry: dict, space: dict, *, body: str | None = None,
                   parent: str | None = None, want: set[str] | None = None,
                   excerpt: str | None = None) -> dict:
    """One `kb_list` entry (plus an optional body) as a Confluence content object.

    `space`, `version` and the labels are always present, where real Confluence
    gates them behind `expand`; `body` and `ancestors` are served only when
    asked for, because that is the distinction the real client relies on when it
    decides whether a page needs updating.
    """
    want = want or set()
    identifier = content_id(entry["id"])
    labels = [{"prefix": "global", "name": name, "id": content_id(name)}
              for name in entry.get("labels", [])]
    payload = {
        "id": identifier,
        "type": "page",
        "status": "current",
        "title": entry["title"],
        "space": space_object(base, space),
        "version": {
            "by": user_object(entry.get("last_edited_by")),
            "when": stamp(entry.get("last_edited_at")),
            "message": entry.get("edit_comment"),
            "number": entry.get("version", 1),
            "minorEdit": False,
        },
        "metadata": {"labels": {"results": labels, "size": len(labels),
                                "start": 0, "limit": len(labels)}},
        "extensions": {"position": "none"},
        "_links": {
            "self": f"{base}/wiki/rest/api/content/{identifier}",
            "webui": f"/spaces/{space['key']}/pages/{identifier}/{entry['id']}",
            "base": f"{base}/wiki",
        },
    }
    if excerpt is not None:
        payload["excerpt"] = excerpt
    if wants_body(want):
        payload["body"] = {"storage": {"value": body or "", "representation": "storage"}}
    else:
        payload["_expandable"] = {"body": f"/rest/api/content/{identifier}?expand=body.storage"}
    if "ancestors" in want:
        payload["ancestors"] = ([] if not parent else
                                [{"id": content_id(parent), "type": "page", "title": parent}])
    return payload


def base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def bodies(log: Ledger, page_ids: list[str]) -> dict[str, str]:
    """Fetch a body per page, paying one `kb_get` row each.

    An expanded listing or search returns the same bytes an agent would have had
    to call `kb_get` for, so it costs the same rows. An unexpanded one fetches
    nothing and costs none.
    """
    found = {}
    for page_id in page_ids:
        with log.span("kb_get", page=page_id) as row:
            # `page=` binds this iteration's id into the call; a lambda that read
            # the loop variable would be a live reference to it (ruff B023).
            found[page_id] = row.run(
                lambda ops, obs, page=page_id: ops.kb_get(page, obs))["body"]
    return found


# --------------------------------------------------------------------------
# CQL
# --------------------------------------------------------------------------


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_cql(text: str) -> tuple[str, str | None]:
    """Parse the CQL subset this app understands -> (needle, space key or None).

    Supported: `text ~ "needle"`, optionally `AND type = page` and
    `AND space = KEY`. Anything else is refused with 400 rather than quietly
    dropped: a filter that silently does nothing is how an agent concludes that
    the page contradicting the one it just read does not exist.
    """
    needle, space = None, None
    for part in CQL_AND.split((text or "").strip()):
        clause = CQL_CLAUSE.match(part.strip())
        if not clause:
            raise Refused(400, "invalid_cql", f"Cannot parse CQL clause: {part.strip()!r}")
        field, operator = clause.group("field").lower(), clause.group("op")
        value = unquote(clause.group("value"))
        if field in ("text", "title") and operator == "~":
            needle = value
        elif field == "type" and operator == "=" and value == "page":
            continue
        elif field == "space" and operator == "=":
            space = value
        else:
            raise Refused(400, "invalid_cql",
                          f"Unsupported CQL clause {part.strip()!r}; this server understands "
                          'text ~ "needle", type = page and space = KEY')
    if needle is None:
        raise Refused(400, "invalid_cql", 'This server needs a text ~ "needle" clause')
    return needle, space


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@app.get("/health")
def health(request: Request) -> object:
    """Liveness for the surface gate, and the one endpoint an agent never calls.

    `items` is counted out of the world on every request, through the same
    module call the handlers use - a constant here would let a service wired to
    nothing pass the gate, which is the one thing the gate exists to catch. A
    world with no wiki document answers 503 and the refusal's code, never a zero
    that reads as "wired, and empty".

    Unauthenticated, because a gate that fails on a credential has not told
    anyone whether the surface is up. Audited like every other request, but
    under the target `health:confluence` rather than `kb`: a harness that polls
    health must not inject the episode's target set with rows no agent caused.
    `read_targets` still says what was really read. `context.service_health`
    holds that convention for all nine services.
    """
    return service_health(
        request, NAME, lambda ops, obs: len(ops.kb_list(obs)["pages"])
    )


@app.get("/wiki/rest/api/content/search")
def content_search(request: Request, cql: str = "", start: int = 0,
                   limit: int = DEFAULT_LIMIT, expand: str | None = None) -> dict:
    """CQL text search over page bodies. The equivalent of `kb_search`.

    `kb_search` matches per line and carries each hit's edit date, which is the
    evidence that settles a contradiction between two pages; the hits are folded
    into one result per page here, with the first matching line as the excerpt,
    and the edit date survives in `version.when`.
    """
    log = Ledger(request, NAME)
    with log.span("kb_search", cql=cql, start=start, limit=limit) as row:
        log.actor = principal(request)
        needle, space_key = parse_cql(cql)
        pages, space = index(row)
        if space_key and space_key != space["key"]:
            raise Refused(404, "space_not_found", f"No space with key {space_key!r}")
        result = row.run(lambda ops, obs: ops.kb_search(needle, ALL, obs))
    excerpts: dict[str, str] = {}
    for match in result["matches"]:
        excerpts.setdefault(match["page"], match["text"])
    found = list(excerpts)[start : start + limit]
    want = expanded(expand)
    text = bodies(log, found) if wants_body(want) else {}
    results = [content_object(base_url(request), pages[page_id], space, want=want,
                              excerpt=excerpts[page_id], body=text.get(page_id))
               for page_id in found]
    return {"results": results, "start": start, "limit": limit, "size": len(results),
            "totalSize": len(excerpts), "cqlQuery": cql,
            "_links": {"base": f"{base_url(request)}/wiki", "self": str(request.url)}}


@app.get("/wiki/rest/api/content")
def content_list(request: Request, spaceKey: str | None = None, title: str | None = None,
                 type: str = "page", start: int = 0, limit: int = DEFAULT_LIMIT,
                 expand: str | None = None) -> dict:
    """The space's pages, optionally narrowed to one title. The equivalent of `kb_list`.

    `type` exists because the real client always sends it; this wiki has pages
    and nothing else, so anything but `page` returns an empty page of results
    rather than pretending blog posts exist.
    """
    log = Ledger(request, NAME)
    with log.span("kb_list", spaceKey=spaceKey, title=title,
                  type=type, start=start, limit=limit) as row:
        log.actor = principal(request)
        pages, space = unpack(row.run(lambda ops, obs: ops.kb_list(obs)))
        if spaceKey and spaceKey != space["key"]:
            raise Refused(404, "space_not_found", f"No space with key {spaceKey!r}")
    selected = [] if type != "page" else [
        entry for entry in pages.values() if title is None or entry["title"] == title
    ]
    window = selected[start : start + limit]
    want = expanded(expand)
    text = bodies(log, [entry["id"] for entry in window]) if wants_body(want) else {}
    base = base_url(request)
    results = [content_object(base, entry, space, want=want, parent=entry.get("parent"),
                              body=text.get(entry["id"]))
               for entry in window]
    return {"results": results, "start": start, "limit": limit, "size": len(results),
            "totalSize": len(selected),
            "_links": {"base": f"{base}/wiki", "self": str(request.url)}}


@app.get("/wiki/rest/api/content/{identifier}")
def content_get(identifier: str, request: Request, expand: str | None = None) -> dict:
    """One page. The equivalent of `kb_get`, and one row."""
    log = Ledger(request, NAME)
    with log.span("kb_get", id=identifier, expand=expand) as row:
        log.actor = principal(request)
        pages, space = index(row)
        page_id = resolve(pages, identifier)
        page = row.run(lambda ops, obs: ops.kb_get(page_id, obs))
        return content_object(base_url(request), pages[page_id], space, body=page["body"],
                              parent=page.get("parent"), want=expanded(expand))


@app.get("/wiki/rest/api/content/{identifier}/history")
def content_history(identifier: str, request: Request) -> dict:
    """The revision history, which is how the real client learns the version
    number before it writes - `PUT` does not work without it. The question it
    asks of the world is `kb_get`, so that is the row."""
    log = Ledger(request, NAME)
    with log.span("kb_get", id=identifier) as row:
        log.actor = principal(request)
        pages, _ = index(row)
        page_id = resolve(pages, identifier)
        page = row.run(lambda ops, obs: ops.kb_get(page_id, obs))
        return {
            "latest": True,
            "createdBy": user_object(page.get("last_edited_by")),
            "createdDate": stamp(page.get("last_edited_at")),
            "lastUpdated": {
                "by": user_object(page.get("last_edited_by")),
                "when": stamp(page.get("last_edited_at")),
                "message": page.get("edit_comment"),
                "number": page["version"],
                "minorEdit": False,
            },
            "previousVersions": [
                {"number": revision.get("version"), "when": stamp(revision.get("at")),
                 "by": user_object(revision.get("by")), "message": revision.get("comment")}
                for revision in page.get("history", [])
            ],
        }


@app.put("/wiki/rest/api/content/{identifier}")
def content_update(identifier: str, payload: dict, request: Request) -> dict:
    """Replace a page body and bump its version. The equivalent of `kb_edit`.

    Two refusals rather than silent success: a version that is not the current
    one plus one is a lost update and answers 409, and a title or parent change
    answers 400 because `kb_edit` cannot express either and an ignored rename
    would leave the client believing something the world does not say.

    Reading the current page to check all three is a precondition of the write,
    not a question of its own, so it goes through `aside` and this endpoint
    costs exactly one row - what `kb_edit` costs.
    """
    version = (payload.get("version") or {}).get("number")
    body = ((payload.get("body") or {}).get("storage") or {}).get("value")
    log = Ledger(request, NAME)
    with log.span("kb_edit", id=identifier, version=version,
                  title=payload.get("title")) as row:
        log.actor = principal(request)
        pages, _ = index(row)
        page_id = resolve(pages, identifier)
        current = row.aside(lambda ops, obs: ops.kb_get(page_id, obs))
        if body is None:
            raise Refused(400, "invalid_body", "A page update needs body.storage.value")
        if payload.get("title") not in (None, current["title"]):
            raise Refused(400, "unsupported_edit",
                          "This wiki cannot rename a page; the page id is its path")
        ancestors = payload.get("ancestors") or []
        if ancestors and ancestors[0].get("id") not in (
                None, content_id(current.get("parent") or ""), current.get("parent")):
            raise Refused(400, "unsupported_edit", "This wiki cannot re-parent a page")
        if version is not None and int(version) != current["version"] + 1:
            raise Refused(409, "version_conflict",
                          f"Version must be {current['version'] + 1}; the page is at "
                          f"version {current['version']}")
        # `kb_edit` is what flags the row `overwrite`, so this module never
        # spells that word; the two transports agree because one method sets it.
        message = (payload.get("version") or {}).get("message")
        row.run(lambda ops, obs: ops.kb_edit(page_id, body, message, obs))
        pages, space = index(row)
        return content_object(base_url(request), pages[page_id], space, body=body,
                              parent=current.get("parent"), want={"body.storage"})


# Two paths, because `atlassian-python-api` posts to the trailing-slash form and
# a 307 would make the audit row depend on whether the client follows redirects.
@app.post("/wiki/rest/api/content", status_code=200)
@app.post("/wiki/rest/api/content/", status_code=200)
def content_create(payload: dict, request: Request) -> dict:
    """Create a page under an existing parent. The equivalent of `kb_create`.

    Beyond the endpoint list in NATIVE.md, and deliberately: without it
    an agent in native mode can read and edit the wiki but cannot write the
    postmortem an archetype asks for, which would make native mode a strictly
    smaller world than MCP mode rather than the same one.
    """
    title = payload.get("title") or ""
    body = ((payload.get("body") or {}).get("storage") or {}).get("value") or ""
    ancestors = payload.get("ancestors") or []
    log = Ledger(request, NAME)
    with log.span("kb_create", title=title,
                  ancestors=[item.get("id") for item in ancestors]) as row:
        log.actor = principal(request)
        pages, _ = index(row)
        if not title:
            raise Refused(400, "invalid_title", "A page needs a title")
        if payload.get("type", "page") != "page":
            raise Refused(400, "unsupported_type", "This wiki holds pages and nothing else")
        parent = resolve(pages, ancestors[0].get("id", "")) if ancestors else None
        page_id = f"{parent}/{slug(title)}" if parent else slug(title)
        labels = [label.get("name") for label in
                  ((payload.get("metadata") or {}).get("labels") or []) if label.get("name")]
        row.run(lambda ops, obs: ops.kb_create(page_id, title, body, parent, labels, obs))
        pages, space = index(row)
        return content_object(base_url(request), pages[page_id], space, body=body,
                              parent=parent, want={"body.storage"})


# --------------------------------------------------------------------------
# REST v2 (https://dac-static.atlassian.com/cloud/confluence/openapi-v2.v3.json)
#
# The same pages, ids and rows as v1 above, in v2's shapes: `spaceId` and
# `parentId` instead of embedded objects, `body-format` instead of `expand`,
# cursor paging with `_links.next` (and a `Link` header) instead of start/limit.
# Bodies are served for `body-format=storage` (the world's markdown, verbatim,
# as v1 does) and `atlas_doc_format` (that text as ADF, one paragraph a line,
# JSON-encoded in `value` as Cloud sends it); the rendered formats (`view`,
# `export_view`, `styled_view`, `editor`, ...) are a 400 naming the two, never a
# body quietly labelled as something it is not.
# --------------------------------------------------------------------------

V2_BODY_FORMATS = ("storage", "atlas_doc_format")
V2_PAGE_SORTS = {"id", "-id", "created-date", "-created-date", "modified-date",
                 "-modified-date", "title", "-title"}
V2_CHILD_SORTS = {"id", "-id", "created-date", "-created-date", "modified-date",
                  "-modified-date", "child-position", "-child-position", "title", "-title"}
V2_SPACE_SORTS = {"id", "-id", "key", "-key", "name", "-name"}
V2_MAX_LIMIT = 250


def iso(value: str | None) -> str | None:
    """World ISO-8601 -> v2's `2025-11-04T10:12:00.000Z`."""
    if not value:
        return None
    moment = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def listed(request: Request, name: str) -> list[str]:
    """An array query parameter, repeated (`?id=1&id=2`) or comma-separated
    (`?space-id=1,2`, which atlassian-python-api sends)."""
    return [part.strip() for value in request.query_params.getlist(name)
            for part in value.split(",") if part.strip()]


def v2_limit(request: Request) -> int:
    raw = request.query_params.get("limit", "25")
    try:
        limit = int(raw)
    except ValueError:
        limit = 0
    if not 1 <= limit <= V2_MAX_LIMIT:
        raise Refused(400, "invalid_limit", f"limit must be between 1 and {V2_MAX_LIMIT}")
    return limit


def v2_cursor(request: Request) -> int:
    """The cursor is the offset the next page starts at: opaque to the client,
    and no server-side state to keep alive between requests."""
    raw = request.query_params.get("cursor")
    if raw is None:
        return 0
    if not raw.isdigit():
        raise Refused(400, "invalid_cursor", "cursor is not one this server issued")
    return int(raw)


def v2_choice(request: Request, name: str, allowed, default: str | None = None) -> str | None:
    value = request.query_params.get(name, default)
    if value is not None and value not in allowed:
        raise Refused(400, f"invalid_{name.replace('-', '_')}",
                      f"{name} must be one of {', '.join(sorted(allowed))}; got {value!r}")
    return value


def v2_body_format(request: Request) -> str | None:
    value = request.query_params.get("body-format")
    if value is not None and value not in V2_BODY_FORMATS:
        raise Refused(400, "unsupported_body_format",
                      f"This wiki serves body-format storage and atlas_doc_format; "
                      f"{value!r} is a rendering it does not produce")
    return value


def v2_flag(request: Request, name: str, default: bool = False) -> bool:
    value = request.query_params.get(name)
    return default if value is None else value.lower() == "true"


def v2_body(text: str, fmt: str | None) -> dict:
    if fmt == "storage":
        return {"storage": {"representation": "storage", "value": text}}
    if fmt == "atlas_doc_format":
        return {"atlas_doc_format": {"representation": "atlas_doc_format",
                                     "value": json.dumps(adf(text))}}
    return {}


def v2_version(number, at, message, by) -> dict:
    return {"createdAt": iso(at), "message": message or "", "number": number,
            "minorEdit": False, "authorId": by}


def v2_page(base: str, entry: dict, space: dict, pages: dict, history: list, *,
            body: str | None = None, fmt: str | None = None, include_version: bool = True,
            labels: bool = False, versions: bool = False) -> dict:
    """One `kb_list` entry (+ its `kb_get` history) as a v2 page.

    `createdAt` and `authorId` are the first revision's, which `kb_get` keeps in
    `history`; `version` is the current one, which `kb_list` carries. A parent
    the wiki does not hold (a root page's `parent: null`) is `parentId: null`.
    """
    identifier = content_id(entry["id"])
    first = min(history, key=lambda revision: revision.get("version") or 0) if history else {}
    parent = entry.get("parent")
    payload = {
        "id": identifier,
        "status": "current",
        "title": entry["title"],
        "spaceId": str(space_number(space)),
        "parentId": content_id(parent) if parent in pages else None,
        "parentType": "page" if parent in pages else None,
        "authorId": first.get("by") or entry.get("last_edited_by"),
        "ownerId": first.get("by") or entry.get("last_edited_by"),
        "createdAt": iso(first.get("at") or entry.get("last_edited_at")),
        "body": v2_body(body or "", fmt) if fmt else {},
        "_links": {"webui": f"/spaces/{space['key']}/pages/{identifier}/{entry['id']}",
                   "base": f"{base}/wiki"},
    }
    current = v2_version(entry.get("version", 1), entry.get("last_edited_at"),
                         entry.get("edit_comment"), entry.get("last_edited_by"))
    if include_version:
        payload["version"] = current
    meta = {"hasMore": False, "cursor": None}
    if labels:
        payload["labels"] = {
            "results": [{"id": content_id(name), "name": name, "prefix": "global"}
                        for name in entry.get("labels", [])],
            "meta": meta, "_links": {"self": f"{base}{V2}/pages/{identifier}/labels"}}
    if versions:
        older = sorted(history, key=lambda revision: revision.get("version") or 0,
                       reverse=True)
        payload["versions"] = {
            "results": [current] + [v2_version(revision.get("version"), revision.get("at"),
                                               revision.get("comment"), revision.get("by"))
                                    for revision in older],
            "meta": meta, "_links": {"self": f"{base}{V2}/pages/{identifier}/versions"}}
    return payload


def histories(row: Span, page_ids: list[str]) -> dict[str, list]:
    """Each page's revision list, for `createdAt`/`authorId`: header data, read
    through `aside`, so it costs no row (HUMAN_PLANE.md rule 3)."""
    return {page_id: row.aside(lambda ops, obs, page=page_id: ops.kb_get(page, obs))
            .get("history", []) for page_id in page_ids}


def v2_listing(request: Request, results: list, start: int, limit: int,
               total: int) -> JSONResponse:
    """`{results, _links: {next?, base}}`, and the `Link` header Cloud sends
    beside `_links.next` ("additional results ... will be available through
    the `next` URL present in the `Link` response header")."""
    base = base_url(request)
    links = {"base": f"{base}/wiki"}
    headers = {}
    if start + limit < total:
        params = [(k, v) for k, v in request.query_params.multi_items() if k != "cursor"]
        nxt = f"{request.url.path}?{urlencode(params + [('cursor', str(start + limit))])}"
        links["next"] = nxt
        headers["Link"] = f'<{nxt}>; rel="next"'
    return JSONResponse({"results": results, "_links": links}, headers=headers)


def v2_sorted(entries: list, sort: str | None, history: dict) -> list:
    if not sort:
        return entries
    field = sort.lstrip("-")
    keys = {
        "id": lambda e: int(content_id(e["id"])),
        "title": lambda e: e["title"],
        "modified-date": lambda e: e.get("last_edited_at") or "",
        "created-date": lambda e: min((r.get("at") or "" for r in history.get(e["id"], [])),
                                      default=e.get("last_edited_at") or ""),
        "child-position": lambda e: 0,
    }
    return sorted(entries, key=keys[field], reverse=sort.startswith("-"))


@app.get(V2 + "/pages")
def v2_pages(request: Request) -> JSONResponse:
    """`GET /pages`: the space's pages, filtered by `id`, `space-id`, `title`
    (exact, as Cloud matches it), `status` and `subtype`. The equivalent of
    `kb_list`, and with a `body-format` one `kb_get` per page returned - the
    same rule as v1's `expand=body.storage`."""
    log = Ledger(request, NAME)
    params = dict(request.query_params)
    with log.span("kb_list", **params) as row:
        log.actor = principal(request)
        limit, start = v2_limit(request), v2_cursor(request)
        fmt = v2_body_format(request)
        sort = v2_choice(request, "sort", V2_PAGE_SORTS)
        statuses = set(listed(request, "status")) or {"current"}
        unknown = statuses - {"current", "archived", "deleted", "trashed"}
        if unknown:
            raise Refused(400, "invalid_status", f"Unknown status {sorted(unknown)[0]!r}")
        subtype = v2_choice(request, "subtype", {"live", "page"})
        ids, spaces = set(listed(request, "id")), set(listed(request, "space-id"))
        title = request.query_params.get("title")
        pages, space = unpack(row.run(lambda ops, obs: ops.kb_list(obs)))
        selected = [
            entry for entry in pages.values()
            if "current" in statuses and subtype != "live"
            and (not ids or content_id(entry["id"]) in ids)
            and (not spaces or str(space_number(space)) in spaces)
            and (title is None or entry["title"] == title)]
        history = histories(row, [entry["id"] for entry in selected])
        selected = v2_sorted(selected, sort, history)
    window = selected[start:start + limit]
    text = bodies(log, [entry["id"] for entry in window]) if fmt else {}
    base = base_url(request)
    results = [v2_page(base, entry, space, pages, history[entry["id"]],
                       body=text.get(entry["id"]), fmt=fmt) for entry in window]
    return v2_listing(request, results, start, limit, len(selected))


@app.get(V2 + "/pages/{identifier}")
def v2_page_get(identifier: str, request: Request) -> dict:
    """`GET /pages/{id}`: one page. The equivalent of `kb_get`, and one row.

    `include-labels` and `include-versions` are served (the revision list is the
    evidence that settles a contradiction between two pages); `include-version`
    defaults to true, as documented. `version=<n>` other than the current one
    is a 400: the world keeps no historical page bodies to serve.
    """
    log = Ledger(request, NAME)
    with log.span("kb_get", id=identifier, **dict(request.query_params)) as row:
        log.actor = principal(request)
        fmt = v2_body_format(request)
        pages, space = index(row)
        page_id = resolve(pages, identifier)
        page = row.run(lambda ops, obs: ops.kb_get(page_id, obs))
        wanted = request.query_params.get("version")
        if wanted is not None and wanted != str(page["version"]):
            raise Refused(400, "unsupported_version",
                          f"This wiki keeps no historical page bodies; the page is at "
                          f"version {page['version']}")
        return v2_page(base_url(request), pages[page_id], space, pages,
                       page.get("history", []), body=page["body"], fmt=fmt,
                       include_version=v2_flag(request, "include-version", True),
                       labels=v2_flag(request, "include-labels"),
                       versions=v2_flag(request, "include-versions"))


def v2_children(identifier: str, request: Request, *, typed: bool) -> JSONResponse:
    """A page's children, from `kb_list`'s `children` map. One `kb_list` row:
    the listing is the question asked; resolving the id is a header read."""
    log = Ledger(request, NAME)
    with log.span("kb_list", id=identifier, **dict(request.query_params)) as row:
        log.actor = principal(request)
        limit, start = v2_limit(request), v2_cursor(request)
        sort = v2_choice(request, "sort", V2_CHILD_SORTS)
        pages, _ = index(row)
        page_id = resolve(pages, identifier)
        listing = row.run(lambda ops, obs: ops.kb_list(obs))
        _, space = unpack(listing)
        children = [pages[child] for child in listing.get("children", {}).get(page_id, [])
                    if child in pages]
        history = histories(row, [entry["id"] for entry in children]) \
            if sort and "created" in sort else {}
        ordered = v2_sorted(children, sort, history)
    results = []
    for position, entry in enumerate(ordered[start:start + limit], start=start):
        item = {"id": content_id(entry["id"]), "status": "current", "title": entry["title"],
                "spaceId": str(space_number(space)), "childPosition": position}
        if typed:
            item["type"] = "page"
        results.append(item)
    return v2_listing(request, results, start, limit, len(ordered))


@app.get(V2 + "/pages/{identifier}/children")
def v2_page_children(identifier: str, request: Request) -> JSONResponse:
    """`GET /pages/{id}/children` - deprecated in the OpenAPI document in favour
    of `/direct-children`, and still served there, so served here."""
    return v2_children(identifier, request, typed=False)


@app.get(V2 + "/pages/{identifier}/direct-children")
def v2_page_direct_children(identifier: str, request: Request) -> JSONResponse:
    return v2_children(identifier, request, typed=True)


@app.get(V2 + "/spaces")
def v2_spaces(request: Request) -> JSONResponse:
    """`GET /spaces`: the one space this wiki holds, filtered by `ids`, `keys`,
    `type` and `status`. The question it asks is `kb_list`, whose header is the
    space; fields the world does not record (homepage, icon, description,
    creator) are absent rather than invented."""
    log = Ledger(request, NAME)
    with log.span("kb_list", **dict(request.query_params)) as row:
        log.actor = principal(request)
        limit, start = v2_limit(request), v2_cursor(request)
        v2_choice(request, "sort", V2_SPACE_SORTS)
        kind = v2_choice(request, "type", {"global", "collaboration", "knowledge_base",
                                           "personal", "system", "onboarding",
                                           "xflow_sample_space"})
        status = v2_choice(request, "status", {"current", "archived", "trashed"})
        ids, keys = set(listed(request, "ids")), set(listed(request, "keys"))
        _, space = unpack(row.run(lambda ops, obs: ops.kb_list(obs)))
    spaces = [{"id": str(space_number(space)), "key": space["key"],
               "name": space.get("name", space["key"]), "type": "global",
               "status": "current", "_links": {"webui": f"/spaces/{space['key']}"}}]
    spaces = [item for item in spaces
              if (not ids or item["id"] in ids) and (not keys or item["key"] in keys)
              and kind in (None, "global") and status in (None, "current")]
    return v2_listing(request, spaces[start:start + limit], start, limit, len(spaces))


def v2_text(body: object) -> str:
    """A v2 update body -> the text the world stores. Either spelling the
    OpenAPI allows: `{representation, value}` or `{storage: {...}}` /
    `{atlas_doc_format: {...}}`. `storage` is taken verbatim; `atlas_doc_format`
    (a JSON string, or the object) is read back to its lines, which is the
    inverse of what `GET ?body-format=atlas_doc_format` serves. `wiki` markup
    is refused: this wiki would have to convert it, and a lossy write is worse
    than a 400."""
    if not isinstance(body, dict):
        raise Refused(400, "invalid_body", "A page update needs a body")
    if "representation" not in body:
        for name in ("storage", "atlas_doc_format", "wiki"):
            if isinstance(body.get(name), dict):
                body = {"representation": name, **body[name]}
                break
    representation, value = body.get("representation"), body.get("value")
    if representation == "storage" and isinstance(value, str):
        return value
    if representation == "atlas_doc_format" and value is not None:
        try:
            document = json.loads(value) if isinstance(value, str) else value
        except ValueError:
            raise Refused(400, "invalid_body", "atlas_doc_format value is not JSON") from None
        return plain(document)
    if representation == "wiki":
        raise Refused(400, "unsupported_representation",
                      "This wiki stores storage format; send body.representation storage")
    raise Refused(400, "invalid_body",
                  "A page update needs body.representation storage or atlas_doc_format "
                  "and body.value")


@app.put(V2 + "/pages/{identifier}")
def v2_page_update(identifier: str, payload: dict, request: Request) -> dict:
    """`PUT /pages/{id}`: replace a page body and bump its version. The
    equivalent of `kb_edit`, and one row, exactly as v1's `PUT`.

    The OpenAPI document requires `id`, `status`, `title`, `body` and `version`;
    a missing one is a 400. The refusals are v1's: a `version.number` that is
    not current+1 is a lost update (409), and a changed title, parent or space
    is a rename / move `kb_edit` cannot express (400). `status: draft` is a 400:
    this wiki publishes and keeps no drafts.
    """
    version = (payload.get("version") or {}).get("number")
    log = Ledger(request, NAME)
    with log.span("kb_edit", id=identifier, version=version, title=payload.get("title"),
                  status=payload.get("status")) as row:
        log.actor = principal(request)
        pages, space = index(row)
        page_id = resolve(pages, identifier)
        current = row.aside(lambda ops, obs: ops.kb_get(page_id, obs))
        missing = [name for name in ("id", "status", "title", "body", "version")
                   if payload.get(name) in (None, "")]
        if missing:
            raise Refused(400, "invalid_request", f"Missing required field(s): "
                                                  f"{', '.join(missing)}")
        if str(payload["id"]) != content_id(page_id):
            raise Refused(400, "invalid_request",
                          f"Body id {payload['id']!r} is not the page in the path")
        if payload["status"] != "current":
            raise Refused(400, "unsupported_status", "This wiki keeps no drafts; "
                                                     "status must be current")
        text = v2_text(payload["body"])
        if payload["title"] != current["title"]:
            raise Refused(400, "unsupported_edit",
                          "This wiki cannot rename a page; the page id is its path")
        parent = current.get("parent")
        if payload.get("parentId") not in (None, content_id(parent) if parent else None):
            raise Refused(400, "unsupported_edit", "This wiki cannot re-parent a page")
        if payload.get("spaceId") not in (None, str(space_number(space))):
            raise Refused(400, "unsupported_edit", "This wiki cannot move a page "
                                                   "to another space")
        if version is None or str(version) != str(current["version"] + 1):
            raise Refused(409, "version_conflict",
                          f"Version must be {current['version'] + 1}; the page is at "
                          f"version {current['version']}")
        message = (payload.get("version") or {}).get("message")
        # `kb_edit` flags the row `overwrite`, as it does for v1's PUT.
        row.run(lambda ops, obs: ops.kb_edit(page_id, text, message, obs))
        pages, space = index(row)
        history = row.aside(lambda ops, obs: ops.kb_get(page_id, obs)).get("history", [])
        return v2_page(base_url(request), pages[page_id], space, pages, history,
                       body=text, fmt="storage")
