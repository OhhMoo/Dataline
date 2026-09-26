"""The Slack Web API subset the `chat_*` tools cover (gateway/NATIVE.md).

Same bargain as jira.py: the agent is handed a bot token and reaches for
`slack_sdk`, so the acceptance test is `slack_sdk.WebClient` driving this app,
not curl. Every read and every write runs `alkera_gateway.operations.Operations`
- the very methods `chat_history` and `chat_post` are - so
`var/workflow/chat.json` stays the single source of truth, the virtual clock
stamps the writes, the author-side `authoring` blocks are stripped by the same
code path, and each row's `op_class` and `targets` are read off the same
`records.Observation` the MCP gateway hands that method rather than named here.

The one interesting translation is Slack's `ts`, which is both a timestamp and a
message id. The world's ids are positional (`data-eng/0003`) because that is how
`chat_post` derives the next one, so a `ts` here is `<virtual epoch seconds>.<six
digit position>`: it reads as a Slack timestamp, it round-trips to a world id,
and it is a pure function of the document the way every other generated id in
the workflow surface is.

Rows are named `slack.<mcp_tool>` wherever an MCP equivalent exists, so one
suffix-match rule serves graders and `rollout.py --reclassify` across both
native planes; the vendor method stays visible in `args.method` / `args.path`.
`native/context.py`'s `Ledger` states which module calls cost a row and which do
not. HUMAN_PLANE.md carries the table and a test diffs the rows against MCP's.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ...records import ToolError
from ..context import DEV_TOKEN, Ledger, Span, service_health
from ..http import RawBody

NAME = "slack"
HOST = "slack.internal"
PORT = 8081

# Repo rule: obviously fake. It carries the `xoxb-` prefix because slack_sdk and
# every runbook an agent has read expect a bot token to look like one.
TOKEN = f"xoxb-{DEV_TOKEN}"
#: What the workspace exports as SLACK_BOT_TOKEN (the bare shared credential).
#: Accepted beside the `xoxb-` form: an agent that uses the token it was handed
#: must get in, and one that shapes it the way a real bot token looks must too.
ACCEPTED_TOKENS = frozenset({TOKEN, DEV_TOKEN})

# `chat_history` and `chat_search` take a limit; Slack's own paging is applied
# after the module call, so that call has to see the whole channel.
ALL = 100_000
DEFAULT_LIMIT = 100

# Workflow refusals -> the Slack error string for the same condition. An
# unmapped code is passed through verbatim rather than flattened into a
# plausible-looking Slack error: inventing `channel_not_found` for something
# else is how a surface teaches an agent a false fact about the world.
ERRORS = {
    "channel_not_found": "channel_not_found",
    "message_not_found": "thread_not_found",
    "thread_not_found": "thread_not_found",
    "invalid_thread": "thread_not_found",
    "no_chat": "team_not_found",
    "invalid_chat": "fatal_error",
}

app = FastAPI(title="Slack Web API (alkera native surface)", docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------
# The caller, and the refusals this app makes in Slack's spelling. The world,
# the rows and the ledger it writes them through are `native/context.py` now:
# item 1 of SERVICES_A_REQUESTS.md asked for one home for the three copies of
# this block, and that is it.
# --------------------------------------------------------------------------


class Refused(Exception):
    """A request this app declines, carrying the Slack error string for it."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def arguments(request: Request, body: bytes) -> dict:
    """Query string, form body and JSON body, in that order of precedence.

    slack_sdk's urllib transport POSTs form-encoded parameters even for the
    methods Slack documents as GET, and posts JSON for `chat.postMessage`, so an
    app that accepts only one of the three does not work with the real client.
    """
    values = dict(request.query_params)
    if not body:
        return values
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            payload = json.loads(body)
        except ValueError:
            raise Refused("invalid_json") from None
        if not isinstance(payload, dict):
            raise Refused("invalid_arguments")
        values.update(payload)
    else:
        values.update(parse_qsl(body.decode("utf-8", "replace")))
    return values


def principal(request: Request, values: dict) -> str:
    """`Authorization: Bearer <token>`, or a `token` parameter. Else `invalid_auth`.

    Returned rather than discarded because it becomes the `actor` on every row
    the request writes: in native mode the transport knows who called, and the
    log should say so.
    """
    header = request.headers.get("authorization", "")
    scheme, _, supplied = header.partition(" ")
    token = supplied if scheme.lower() == "bearer" else values.get("token", "")
    if not token:
        raise Refused("not_authed")
    if token not in ACCEPTED_TOKENS:
        raise Refused("invalid_auth")
    return token


@app.exception_handler(Refused)
async def _refused(request: Request, exc: Refused) -> JSONResponse:
    """Slack answers an application error with HTTP 200 and `ok: false`."""
    return JSONResponse({"ok": False, "error": exc.code}, status_code=200)


@app.exception_handler(ToolError)
async def _tool_error(request: Request, exc: ToolError) -> JSONResponse:
    return JSONResponse({"ok": False, "error": ERRORS.get(exc.code, exc.code)}, status_code=200)


# --------------------------------------------------------------------------
# World records -> Slack objects
# --------------------------------------------------------------------------


def slack_id(prefix: str, text: str) -> str:
    """A stable Slack-looking id for a world id (`C0A1B2C3D4`, `U9F8E7D6C5`).

    Derived from the world id, so two builds of the same world mint the same id
    and a recorded trajectory stays replayable. Every endpoint also accepts the
    world id itself, and `#name`, because that is what a human types.
    """
    return prefix + hashlib.sha256(text.encode("utf-8")).hexdigest()[:9].upper()


def epoch(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC).timestamp())


def message_ts(entry: dict) -> str:
    """A rendered world message -> its Slack `ts`. See the module docstring."""
    position = int(entry["id"].rsplit("/", 1)[1])
    return f"{epoch(entry['at'])}.{position:06d}"


def message_id(channel_id: str, ts: str) -> str:
    """A Slack `ts` -> the world message id it names."""
    _, _, position = str(ts).partition(".")
    if not position.isdigit():
        raise Refused("invalid_ts")
    return f"{channel_id}/{int(position):04d}"


def message_object(channel_id: str, entry: dict, thread_ts: str | None = None) -> dict:
    """One `Chat.render` result as a Slack message.

    `thread_ts` is set on a reply and on a root that has replies, which is what
    Slack does and what lets a client tell a thread from a standalone message.
    """
    body = {
        "type": "message",
        "user": slack_id("U", entry["author"]),
        "username": entry["author"],
        "text": entry["text"],
        "ts": message_ts(entry),
        "reactions": [{"name": name, "count": count, "users": []}
                      for name, count in sorted(entry.get("reactions", {}).items())],
    }
    if entry.get("reply_count"):
        body["reply_count"] = entry["reply_count"]
    if thread_ts:
        body["thread_ts"] = thread_ts
    return body


def channel_object(record_: dict) -> dict:
    return {
        "id": slack_id("C", record_["id"]),
        "name": record_["id"],
        "name_normalized": record_["id"],
        "is_channel": True,
        "is_private": False,
        "is_archived": False,
        "is_member": True,
        "num_members": len(record_.get("members", [])),
        "created": 0,
        "topic": {"value": record_.get("topic") or "", "creator": "", "last_set": 0},
        "purpose": {"value": record_.get("purpose") or "", "creator": "", "last_set": 0},
    }


def user_object(person: dict) -> dict:
    return {
        "id": slack_id("U", person["id"]),
        "name": person["id"],
        "real_name": person.get("name", person["id"]),
        "deleted": False,
        "is_bot": person.get("role", "").lower() == "automation",
        "profile": {
            "display_name": person["id"],
            "real_name": person.get("name", person["id"]),
            "title": person.get("role", ""),
            "team": person.get("team", ""),
            "email": f"{person['id']}@alkera.local",
        },
    }


def resolve(row: Span, wanted: str) -> dict:
    """A channel id, a `#name` or a world id -> the world channel record.

    The resolution read: it runs `chat_channels`, so an unknown channel raises
    the same refusal an agent would get from the MCP tool, and it goes through
    `aside` so it costs no row - naming a channel is not a question about its
    contents, and charging for it would make `conversations.history` cost two
    rows where `chat_history` costs one.
    """
    if not wanted:
        raise Refused("channel_not_found")
    wanted = wanted.lstrip("#")
    for found in row.aside(lambda ops, obs: ops.chat_channels(obs))["channels"]:
        if wanted in (found["id"], slack_id("C", found["id"])):
            return found
    raise Refused("channel_not_found")


def window(items: list[dict], values: dict) -> tuple[list[dict], str]:
    """Apply Slack's `oldest` / `latest` / `limit` / `cursor` to a ts-sorted page.

    The cursor is the offset the next page resumes from - opaque to the client,
    stable across calls, and no server-side cursor state to keep alive.
    """
    oldest, latest = number(values.get("oldest"), 0.0), number(values.get("latest"), None)
    inside = [item for item in items
              if float(item["ts"]) > oldest and (latest is None or float(item["ts"]) <= latest)]
    try:
        start = int(values.get("cursor") or 0)
        size = int(values.get("limit") or DEFAULT_LIMIT)
    except ValueError:
        raise Refused("invalid_cursor") from None
    page = inside[start : start + max(size, 0)]
    remaining = start + len(page) < len(inside)
    return page, (str(start + len(page)) if remaining else "")


def number(value: object, fallback: float | None) -> float | None:
    if value in (None, ""):
        return fallback
    try:
        return float(value)
    except (TypeError, ValueError):
        raise Refused("invalid_ts_oldest") from None


def ok(payload: dict, cursor: str = "") -> dict:
    return {"ok": True, **payload, "response_metadata": {"next_cursor": cursor}}


def carry(row: Span, values: dict, *names: str):
    """Put the vendor parameters the caller actually sent onto the row."""
    row.args.update({name: values[name] for name in names if name in values})


# --------------------------------------------------------------------------
# Methods
# --------------------------------------------------------------------------


@app.get("/health")
def health(request: Request) -> object:
    """Liveness for the surface gate, and the one endpoint an agent never calls.

    `items` is counted out of the world on every request, through the same
    module call the handlers use - a constant here would let a service wired to
    nothing pass the gate, which is the one thing the gate exists to catch. A
    world with no chat document answers 503 and the refusal's code, never a zero
    that reads as "wired, and empty".

    Unauthenticated, because a gate that fails on a credential has not told
    anyone whether the surface is up. Audited like every other request, but
    under the target `health:slack` rather than `chat`: a harness that polls
    health must not inject the episode's target set with rows no agent caused.
    `read_targets` still says what was really read. `context.service_health`
    holds that convention for all nine services.
    """
    return service_health(
        request, NAME, lambda ops, obs: len(ops.chat_channels(obs)["channels"])
    )


@app.api_route("/api/auth.test", methods=["GET", "POST"])
def auth_test(request: Request, body: RawBody) -> dict:
    """Who this token is. No MCP tool asks it, so the row keeps the vendor name.

    The workspace name and the `user` both come off `chat_channels` - the
    workspace because `operations.py` now serves it there
    (SERVICES_A_REQUESTS.md item 3), the user because `workflow_actor` is the
    persona a post is attributed to, so the agent can discover the name its
    messages will carry.
    """
    log = Ledger(request, NAME)
    with log.span("auth_test") as row:
        values = arguments(request, body)
        log.actor = principal(request, values)
        listing = row.run(lambda ops, obs: ops.chat_channels(obs))
        workspace = listing["workspace"]
        actor = row.operations.config.workflow_actor
        return {
            "ok": True,
            "url": str(request.base_url),
            "team": workspace,
            "user": actor,
            "team_id": slack_id("T", workspace),
            "user_id": slack_id("U", actor),
            "bot_id": slack_id("B", actor),
            "is_enterprise_install": False,
        }


@app.api_route("/api/conversations.list", methods=["GET", "POST"])
def conversations_list(request: Request, body: RawBody) -> dict:
    """The channels. The equivalent of `chat_channels`."""
    log = Ledger(request, NAME)
    with log.span("chat_channels") as row:
        values = arguments(request, body)
        log.actor = principal(request, values)
        listing = row.run(lambda ops, obs: ops.chat_channels(obs))
        return ok({"channels": [channel_object(found) for found in listing["channels"]]})


@app.api_route("/api/users.list", methods=["GET", "POST"])
def users_list(request: Request, body: RawBody) -> dict:
    """The directory. `chat_channels` is the tool that answers it, so that is the
    row; `args.path` is what says the caller asked for people, not channels."""
    log = Ledger(request, NAME)
    with log.span("chat_channels") as row:
        values = arguments(request, body)
        log.actor = principal(request, values)
        listing = row.run(lambda ops, obs: ops.chat_channels(obs))
        return ok({"members": [user_object(person) for person in listing["users"]]})


@app.api_route("/api/conversations.history", methods=["GET", "POST"])
def conversations_history(request: Request, body: RawBody) -> dict:
    """A channel's top-level messages, newest first. The equivalent of `chat_history`.

    Thread replies are excluded, which is both what Slack does and what
    `chat_history` does: it returns thread roots and a reply count, and the
    replies come from `conversations.replies`.
    """
    log = Ledger(request, NAME)
    with log.span("chat_history") as row:
        values = arguments(request, body)
        log.actor = principal(request, values)
        carry(row, values, "channel", "oldest", "latest", "limit", "cursor")
        channel = resolve(row, values.get("channel", ""))
        history = row.run(
            lambda ops, obs: ops.chat_history(channel["id"], ALL, None, obs))
        roots = [message_object(channel["id"], entry,
                                message_ts(entry) if entry.get("reply_count") else None)
                 for entry in reversed(history["messages"])]
        page, cursor = window(roots, values)
        return ok({"messages": page, "has_more": bool(cursor)}, cursor)


@app.api_route("/api/conversations.replies", methods=["GET", "POST"])
def conversations_replies(request: Request, body: RawBody) -> dict:
    """A thread: the root followed by every reply. The equivalent of `chat_thread`.

    `chat_thread` accepts a root id or any reply's id and resolves the root
    itself, so passing either `ts` here behaves the way Slack does.
    """
    log = Ledger(request, NAME)
    with log.span("chat_thread") as row:
        values = arguments(request, body)
        log.actor = principal(request, values)
        carry(row, values, "channel", "ts", "limit", "cursor")
        channel = resolve(row, values.get("channel", ""))
        wanted = message_id(channel["id"], values.get("ts", ""))
        thread = row.run(lambda ops, obs: ops.chat_thread(wanted, obs))
        root_ts = message_ts(thread["root"])
        messages = [message_object(thread["channel"], thread["root"], root_ts)] + [
            message_object(thread["channel"], reply, root_ts) for reply in thread["replies"]
        ]
        messages[0]["reply_count"] = thread["reply_count"]
        page, cursor = window(messages, values)
        return ok({"messages": page, "has_more": bool(cursor)}, cursor)


@app.api_route("/api/search.messages", methods=["GET", "POST"])
def search_messages(request: Request, body: RawBody) -> dict:
    """Literal, case-insensitive message search. The equivalent of `chat_search`.

    Slack's `in:#channel` modifier is the one piece of its query language this
    understands; the rest of the query is the literal needle `chat_search` takes,
    so what matches here is what matches there.
    """
    log = Ledger(request, NAME)
    with log.span("chat_search") as row:
        values = arguments(request, body)
        log.actor = principal(request, values)
        carry(row, values, "query", "count")
        query, channel = split_query(values.get("query", ""))
        found = resolve(row, channel) if channel else None
        limit = int(values.get("count") or DEFAULT_LIMIT)
        result = row.run(lambda ops, obs: ops.chat_search(
            query, found["id"] if found else None, limit, obs))
        matches = [
            {**message_object(match["channel"], match),
             "channel": {"id": slack_id("C", match["channel"]), "name": match["channel"]},
             "permalink": f"{request.base_url}archives/{match['channel']}"}
            for match in result["matches"]
        ]
        return {"ok": True, "query": values.get("query", ""),
                "messages": {"total": len(matches), "matches": matches,
                             "pagination": {"total_count": len(matches)}}}


@app.api_route("/api/chat.postMessage", methods=["POST"])
def chat_post_message(request: Request, body: RawBody) -> dict:
    """Post a message, optionally into a thread. The equivalent of `chat_post`."""
    log = Ledger(request, NAME)
    with log.span("chat_post") as row:
        values = arguments(request, body)
        log.actor = principal(request, values)
        carry(row, values, "channel", "text", "thread_ts")
        channel = resolve(row, values.get("channel", ""))
        text = values.get("text") or ""
        if not text:
            raise Refused("no_text")
        thread_ts = values.get("thread_ts") or None
        parent = message_id(channel["id"], thread_ts) if thread_ts else None
        result = row.run(
            lambda ops, obs: ops.chat_post(channel["id"], text, parent, obs))
        return {"ok": True, "channel": slack_id("C", channel["id"]),
                "ts": message_ts(result["message"]),
                "message": message_object(channel["id"], result["message"], thread_ts)}


def split_query(query: str) -> tuple[str, str]:
    """`in:#data-eng still uploading` -> (`still uploading`, `data-eng`)."""
    words, channel = [], ""
    for word in query.split():
        if word.lower().startswith("in:"):
            channel = word[3:]
        else:
            words.append(word)
    return " ".join(words), channel
