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
import re
from datetime import UTC, datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ...records import ToolError
from ..context import DEV_TOKEN, Ledger, Span, service_health

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


@app.exception_handler(Refused)
async def _refused(request: Request, exc: Refused) -> JSONResponse:
    headers = {"WWW-Authenticate": 'Basic realm="alkera"'} if exc.status == 401 else {}
    return JSONResponse({"statusCode": exc.status, "message": exc.message,
                         "reason": exc.code}, status_code=exc.status, headers=headers)


@app.exception_handler(ToolError)
async def _tool_error(request: Request, exc: ToolError) -> JSONResponse:
    status = 404 if exc.code.endswith("_not_found") or exc.code.startswith("no_") else 400
    if exc.code == "page_exists":
        status = 409
    return JSONResponse({"statusCode": status, "message": str(exc), "reason": exc.code},
                        status_code=status)


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
    """One `kb_list` result -> the pages by id and the space header."""
    return ({page["id"]: page for page in listing["pages"]},
            listing.get("space") or {"key": "KB", "name": "Knowledge base"})


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


def space_object(base: str, space: dict) -> dict:
    return {
        "id": int(content_id(space["key"])) % 1_000_000,
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
