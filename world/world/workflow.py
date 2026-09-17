"""The workflow plane as world state: the ticket, the channel and the wiki.

`gateway/SURFACES.md#workflow` defines three JSON documents under
`var/workflow/` — `issues.json`, `chat.json`, `kb.json` — seeded at build time,
written to by the agent during the episode, and read by the grader after
teardown. Until now no theme seeded them, so in every world we could actually
build, the ticket the work arrives in did not exist.

This module is the assembly line for that surface, on the same footing as
`observe.py` is for the observability store. A theme authors its backlog as
*data* (`themes/<name>/workflow/{users,issues,chat,kb}.yaml`) and declares the
surface in its manifest's `workflow:` block; `gen.write_workflow` calls `seed`.

Three determinism rules, all structural (gateway/SURFACES.md#workflow, "Determinism"):

1. **Static bytes.** The documents are dumped with `json_canon` — sorted keys,
   tight separators — so two builds of one seed produce identical SHA-256s.
   `_check_ascii` additionally refuses non-ASCII content, because the gateway
   rewrites these files with `ensure_ascii=False`: an em dash seeded here would
   land as `\\u2014` at build time and as two raw bytes after the agent's first
   comment, and the file would change under a world that nothing had changed.
2. **Every timestamp is the virtual clock.** Nothing here reads a wall clock.
   Corpus timestamps are written as offsets from the world clock (`-12d 09:15`)
   and resolved against `vnow`, which `build.py` takes from the scheduler state
   it just wrote. A literal date would be a lie the moment a seed file moved
   `vtime_start`, and a ticket filed after `vnow` is a tell; `_check_past`
   makes that unrepresentable rather than merely discouraged.
3. **Every generated id is a pure function of the document.** The gateway
   derives the next comment id as `<issue>#<len(comments)+1>` and the next
   message id as `<channel>/<len(messages)+1:04d>`. Seeded ids that do not
   follow that rule collide with the agent's first write, so `_check_issues`
   and `_check_chat` require them to be exactly what the position implies.

Everything `validate` rejects was chosen the same way: it is a mistake an
author can make in YAML that no test downstream would attribute back here.
"""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from .hashing import json_canon

WORKFLOW = Path("var") / "workflow"
CORPUS = ("users", "issues", "chat", "kb")

# "-12d 09:15" -> a timestamp 12 days before the world clock's day at 09:15Z.
# "-12d" / "+20d" -> a bare date, which is what `away_until` and a freeze
# calendar actually are. Anchored, so no prose line can match by accident.
OFFSET = re.compile(r"^([+-])(\d+)d(?: (\d{2}):(\d{2}))?$")
ID_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_-./")  # kb.py's rule


class SeedError(SystemExit):
    """A corpus the contract would not accept. Fails the build, loudly."""


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve(node, day0: datetime):
    """Replace every offset token with an absolute value, recursively."""
    if isinstance(node, dict):
        return {key: resolve(value, day0) for key, value in node.items()}
    if isinstance(node, list):
        return [resolve(item, day0) for item in node]
    if isinstance(node, str):
        match = OFFSET.match(node)
        if match is None:
            return node
        sign, days, hh, mm = match.groups()
        moment = day0 + timedelta(days=int(days) * (1 if sign == "+" else -1))
        if hh is None:
            return moment.date().isoformat()
        return iso(moment + timedelta(hours=int(hh), minutes=int(mm)))
    return node


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def _fail(message: str):
    raise SeedError(f"workflow seed: {message}")


def _stamps(node, path="") -> list[tuple[str, str]]:
    """Every (json-path, value) that looks like an absolute timestamp."""
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            found += _stamps(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found += _stamps(item, f"{path}[{index}]")
    elif isinstance(node, str) and re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$", node):
        found.append((path, node))
    return found


def _check_past(document: dict, name: str, vnow: str):
    """No timestamp may sit at or after the world clock.

    A ticket filed in the future is the single most legible tell that a backlog
    was pasted in rather than lived through, and `kb_get`'s `age_days` goes
    negative on one, which no wiki has ever shown a reader.
    """
    for path, value in _stamps(document, name):
        if value >= vnow:
            _fail(f"{path} is {value}, at or after the world clock {vnow}")


def _check_ascii(documents: dict):
    for name, document in documents.items():
        if not json.dumps(document, ensure_ascii=False).isascii():
            _fail(f"{name} carries non-ASCII text; seeded bytes must survive "
                  "the gateway rewriting the file with ensure_ascii=False")


def _check_issues(document: dict, actor: str):
    users = {person["id"] for person in document["users"]}
    if actor not in users:
        _fail(f"issues.json has no user {actor!r} for the agent to write as")
    statuses, seen = set(document["statuses"]), set()
    ids = {record["id"] for record in document["issues"]}
    for record in document["issues"]:
        where = f"issue {record['id']}"
        if record["id"] in seen:
            _fail(f"duplicate {where}")
        seen.add(record["id"])
        if record["status"] not in statuses:
            _fail(f"{where} has status {record['status']!r}, not in {sorted(statuses)}")
        for field in ("reporter", "assignee"):
            who = record.get(field)
            if who is not None and who not in users:
                _fail(f"{where} names an unknown {field}: {who}")
        if record["created_at"] > record["updated_at"]:
            _fail(f"{where} was updated before it was created")
        for index, comment in enumerate(record.get("comments", []), 1):
            # The gateway appends `<issue>#<len(comments)+1>`; a seeded id that
            # does not follow suit collides with the agent's first comment.
            expected = f"{record['id']}#{index}"
            if comment["id"] != expected:
                _fail(f"{where} comment {index} is {comment['id']!r}, must be {expected!r}")
            if comment["author"] not in users:
                _fail(f"{expected} is authored by an unknown user: {comment['author']}")
            if comment["at"] > record["updated_at"]:
                _fail(f"{expected} is newer than {where}'s updated_at")
        for link in record.get("links", []):
            if link["issue"] not in ids:
                _fail(f"{where} links to a missing issue: {link['issue']}")


def _check_chat(document: dict, actor: str):
    users = {person["id"] for person in document["users"]}
    if actor not in users:
        _fail(f"chat.json has no user {actor!r} for the agent to post as")
    for channel in document["channels"]:
        roots, last = set(), ""
        for member in channel.get("members", []):
            if member not in users:
                _fail(f"channel {channel['id']} has an unknown member: {member}")
        for index, message in enumerate(channel.get("messages", []), 1):
            expected = f"{channel['id']}/{index:04d}"
            if message["id"] != expected:
                _fail(f"{channel['id']} message {index} is {message['id']!r}, "
                      f"must be {expected!r}")
            if message["author"] not in users:
                _fail(f"{expected} is authored by an unknown user: {message['author']}")
            if message["at"] < last:
                _fail(f"{expected} is older than the message before it")
            last = message["at"]
            parent = message.get("thread_id")
            if parent is None:
                roots.add(message["id"])
            elif parent not in roots:
                # chat_post refuses a reply to a reply; a seeded one would be a
                # thread the tools cannot represent and cannot be added to.
                _fail(f"{expected} replies to {parent!r}, which is not an "
                      "earlier thread root in this channel")


def _check_kb(document: dict):
    pages = {}
    for record in document["pages"]:
        page_id = record["id"]
        if page_id in pages:
            _fail(f"duplicate page {page_id}")
        if not page_id or any(char not in ID_CHARS for char in page_id):
            _fail(f"page id {page_id!r} is outside [a-z0-9_-./]")
        pages[page_id] = record
    for page_id, record in pages.items():
        parent = record.get("parent")
        if parent is not None and parent not in pages:
            _fail(f"page {page_id} hangs off a missing parent: {parent}")
        version = record.get("version", 1)
        for revision in record.get("history", []):
            if revision["version"] >= version:
                _fail(f"page {page_id} keeps a history revision "
                      f"{revision['version']} at or above its own version {version}")


def validate(documents: dict, actor: str, vnow: str):
    _check_issues(documents["issues.json"], actor)
    _check_chat(documents["chat.json"], actor)
    _check_kb(documents["kb.json"])
    _check_ascii(documents)
    for name, document in documents.items():
        _check_past(document, name, vnow)


# --------------------------------------------------------------------------
# assembly
# --------------------------------------------------------------------------

def documents(theme_dir: Path, spec: dict, vnow: str) -> dict:
    """The three contract documents, resolved against the world clock.

    The manifest's `workflow:` block owns the *vocabulary* — the project key,
    the status set (a theme may run a Jira workflow or a two-state one), the
    wiki space, the chat workspace, the id the agent writes as — because that
    is world shape. The corpus files own the content. The roster is authored
    once in `users.yaml` and copied into both documents the contract gives a
    `users` section, so a person cannot exist in chat and not in the tracker.
    """
    corpus_dir = Path(theme_dir) / spec.get("dir", "workflow")
    day0 = parse(vnow).replace(hour=0, minute=0, second=0, microsecond=0)
    parts = {}
    for name in CORPUS:
        path = corpus_dir / f"{name}.yaml"
        if not path.is_file():
            _fail(f"{path} is missing")
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            _fail(f"{path} must hold a mapping")
        parts[name] = resolve(loaded, day0)
    users = parts["users"]["users"]
    built = {
        "issues.json": {
            "version": 1,
            "project": spec["project"],
            "statuses": list(spec["statuses"]),
            "users": users,
            "issues": parts["issues"]["issues"],
        },
        "chat.json": {
            "version": 1,
            "workspace": spec["workspace"],
            "users": users,
            "channels": parts["chat"]["channels"],
        },
        "kb.json": {
            "version": 1,
            "space": spec["space"],
            "pages": parts["kb"]["pages"],
        },
    }
    validate(built, spec.get("actor", "agent"), vnow)
    return built


def seed(root: Path, theme_dir: Path, spec: dict, vnow: str) -> Path:
    """Write var/workflow/{issues,chat,kb}.json. Byte-stable for one seed."""
    directory = Path(root) / WORKFLOW
    directory.mkdir(parents=True, exist_ok=True)
    for name, document in documents(theme_dir, spec, vnow).items():
        (directory / name).write_bytes(json_canon(document) + b"\n")
    return directory


def authoring_notes(documents_by_name: dict) -> list[str]:
    """Every author-side string in a built corpus, for the leak tests. Walks
    the documents rather than enumerating them, so a note added to a theme is
    covered the day it is written."""
    notes: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            block = node.get("authoring")
            if isinstance(block, dict) and isinstance(block.get("note"), str):
                notes.append(block["note"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(documents_by_name)
    return notes
