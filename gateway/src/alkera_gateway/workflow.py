"""Issue tracker, team chat and the wiki (see WORKFLOW_CONTRACT.md).

The three surfaces a data engineer's day actually starts in, and the half of the
job the warehouse tools cannot represent: a request written by somebody who does
not know the schema, a channel where the useful sentence is buried under six
useless ones, and a wiki that is right in some places and out of date in others.

Three rules hold across all three stores:

1. **State is world state.** It lives under `workflow_dir` inside the world tree,
   seeded at build time, and every write lands back in the same file. A comment
   an agent leaves is readable by the next call, and by the grader after
   teardown. There is no in-memory-only path.
2. **Time is virtual.** Every timestamp a write produces comes from the clock the
   gateway was given, which reads `vnow` out of the scheduler state. Nothing here
   ever calls `time.time()`; a wall-clock timestamp would break the
   reproducible-rebuild gate and make replay non-deterministic.
3. **Author-side annotation never reaches the agent.** Seeded records carry an
   `authoring` block saying which page is stale, which ticket duplicates which,
   and which confident message is wrong. It is stripped from every response, the
   same treatment `known_imprecision` gets on the observability surface: telling
   the agent which content was planted crooked destroys the measurement (F202).
"""

import json
import re
from datetime import UTC, datetime

from .records import ToolError, canonical

AUTHOR_SIDE = "authoring"
MENTION = re.compile(r"@([a-z][a-z0-9_.-]*)")
ID_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_-./")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _age_days(at: str, vnow: str) -> int | None:
    """Whole days between an edit and the world clock, on the virtual clock only.

    This is the one derived field the wiki has to offer, because "is this page
    stale?" is the question the surface exists to make answerable, and a reader
    cannot answer it from a date alone without knowing what today is.
    """
    try:
        return int((_parse(vnow) - _parse(at)).total_seconds() // 86400)
    except ValueError:
        return None


def _public(record: dict, *drop: str) -> dict:
    return {key: value for key, value in record.items() if key != AUTHOR_SIDE and key not in drop}


def _mentions(text: str) -> list[str]:
    seen = []
    for name in MENTION.findall(text):
        if name not in seen:
            seen.append(name)
    return seen


def _lines(text: str) -> list[dict]:
    return [{"line": n, "text": line} for n, line in enumerate(text.splitlines(), 1)]


def _hits(text: str, query: str) -> list[dict]:
    needle = query.casefold()
    return [line for line in _lines(text) if needle in line["text"].casefold()]


class _JsonStore:
    """Load-validate-save over one seeded JSON document in the world tree."""

    filename = ""
    missing_code = ""
    missing_message = ""
    invalid_code = ""
    required: tuple[str, ...] = ()

    def __init__(self, directory, clock, actor: str):
        self.path = directory / self.filename
        self.clock = clock
        self.actor = actor

    def load(self) -> dict:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ToolError(self.missing_code, self.missing_message) from None
        except (ValueError, UnicodeError) as exc:
            raise ToolError(self.invalid_code, f"{self.filename} is not readable: {exc}") from None
        if not isinstance(state, dict):
            raise ToolError(self.invalid_code, f"{self.filename} must hold an object")
        for key in self.required:
            if key not in state:
                raise ToolError(self.invalid_code, f"{self.filename} needs a {key!r} section")
        return state

    def save(self, state: dict):
        """Atomic, canonical, byte-stable: same call sequence, same bytes."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.path.with_suffix(".pending")
        pending.write_bytes(canonical(state) + b"\n")
        pending.replace(self.path)

    def user(self, state, user_id: str):
        for person in state.get("users", []):
            if person["id"] == user_id:
                return person
        raise ToolError("user_not_found", f"Unknown user: {user_id}")


class IssueTracker(_JsonStore):
    filename = "issues.json"
    missing_code = "no_issue_tracker"
    missing_message = "This world has no issue tracker"
    invalid_code = "invalid_issues"
    required = ("issues", "statuses")

    def issue(self, state, issue_id: str) -> dict:
        for record in state["issues"]:
            if record["id"] == issue_id:
                return record
        raise ToolError("issue_not_found", f"Unknown issue: {issue_id}")

    @staticmethod
    def summary(record: dict) -> dict:
        comments = record.get("comments", [])
        return {
            "id": record["id"],
            "title": record["title"],
            "status": record["status"],
            "reporter": record["reporter"],
            "assignee": record.get("assignee"),
            "labels": list(record.get("labels", [])),
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "comment_count": len(comments),
            "last_comment_at": comments[-1]["at"] if comments else None,
        }

    @staticmethod
    def full(record: dict) -> dict:
        return {
            **_public(record, "comments"),
            "comments": [_public(comment) for comment in record.get("comments", [])],
            "history": list(record.get("history", [])),
            "links": list(record.get("links", [])),
        }

    def list_issues(self, status, assignee, label, limit):
        state = self.load()
        records = state["issues"]
        if status is not None:
            records = [r for r in records if r["status"] == status]
        if assignee is not None:
            records = [r for r in records if r.get("assignee") == assignee]
        if label is not None:
            records = [r for r in records if label in r.get("labels", [])]
        ordered = sorted(records, key=lambda r: (r["updated_at"], r["id"]), reverse=True)
        return {
            "issues": [self.summary(record) for record in ordered[:limit]],
            "total": len(ordered),
            "limit_reached": len(ordered) > limit,
            "statuses": list(state["statuses"]),
            "users": [_public(person) for person in state.get("users", [])],
        }

    def get_issue(self, issue_id):
        return self.full(self.issue(self.load(), issue_id))

    def search(self, query, limit):
        matches = []
        for record in self.load()["issues"]:
            fields = [("title", None, record["title"]), ("body", None, record.get("body", ""))]
            fields += [("comment", c["id"], c["body"]) for c in record.get("comments", [])]
            for field, ref, text in fields:
                for hit in _hits(text, query):
                    matches.append(
                        {
                            "issue": record["id"],
                            "status": record["status"],
                            "field": field,
                            "ref": ref,
                            "line": hit["line"],
                            "text": hit["text"],
                        }
                    )
                    if len(matches) == limit:
                        return {"matches": matches, "limit_reached": True}
        return {"matches": matches, "limit_reached": False}

    def comment(self, issue_id, body):
        state = self.load()
        record = self.issue(state, issue_id)
        now = self.clock()
        comments = record.setdefault("comments", [])
        entry = {
            "id": f"{issue_id}#{len(comments) + 1}",
            "author": self.actor,
            "at": now,
            "body": body,
        }
        comments.append(entry)
        record["updated_at"] = now
        record.setdefault("history", []).append(
            {"at": now, "actor": self.actor, "action": "commented", "ref": entry["id"]}
        )
        self.save(state)
        return {
            "issue": issue_id,
            "comment": entry,
            "comment_count": len(comments),
            "updated_at": now,
        }

    def update(self, issue_id, status, assignee, unassign, add_labels, remove_labels):
        if not any((status, assignee, unassign, add_labels, remove_labels)):
            raise ToolError("invalid_update", "Name a status, an assignee or some labels")
        if assignee is not None and unassign:
            raise ToolError("invalid_update", "Pass either assignee or unassign, not both")
        state = self.load()
        record = self.issue(state, issue_id)
        now = self.clock()
        changes = []
        if status is not None:
            if status not in state["statuses"]:
                raise ToolError(
                    "invalid_status",
                    f"Unknown status {status!r}; this project uses: "
                    + ", ".join(state["statuses"]),
                )
            if status != record["status"]:
                changes.append(
                    {
                        "at": now,
                        "actor": self.actor,
                        "field": "status",
                        "from": record["status"],
                        "to": status,
                    }
                )
                record["status"] = status
        if assignee is not None:
            self.user(state, assignee)
            if assignee != record.get("assignee"):
                changes.append(
                    {
                        "at": now,
                        "actor": self.actor,
                        "field": "assignee",
                        "from": record.get("assignee"),
                        "to": assignee,
                    }
                )
                record["assignee"] = assignee
        if unassign and record.get("assignee") is not None:
            changes.append(
                {
                    "at": now,
                    "actor": self.actor,
                    "field": "assignee",
                    "from": record["assignee"],
                    "to": None,
                }
            )
            record["assignee"] = None
        labels = list(record.get("labels", []))
        for label in add_labels:
            if label not in labels:
                labels.append(label)
        remaining = [label for label in labels if label not in set(remove_labels)]
        if remaining != list(record.get("labels", [])):
            changes.append(
                {
                    "at": now,
                    "actor": self.actor,
                    "field": "labels",
                    "from": list(record.get("labels", [])),
                    "to": remaining,
                }
            )
            record["labels"] = remaining
        if changes:
            record["updated_at"] = now
            record.setdefault("history", []).extend(changes)
            self.save(state)
        return {"issue": issue_id, "changes": changes, **self.summary(record)}


class Chat(_JsonStore):
    filename = "chat.json"
    missing_code = "no_chat"
    missing_message = "This world has no chat workspace"
    invalid_code = "invalid_chat"
    required = ("channels",)

    def channel(self, state, channel_id: str) -> dict:
        for record in state["channels"]:
            if record["id"] == channel_id:
                return record
        raise ToolError("channel_not_found", f"Unknown channel: {channel_id}")

    def message(self, state, message_id: str):
        for channel in state["channels"]:
            for record in channel.get("messages", []):
                if record["id"] == message_id:
                    return channel, record
        raise ToolError("message_not_found", f"Unknown message: {message_id}")

    @staticmethod
    def render(record: dict) -> dict:
        return {
            **_public(record),
            "reactions": dict(record.get("reactions", {})),
            "thread_id": record.get("thread_id"),
            # Derived, not stored: the same rule applies to a message the agent
            # posts a moment ago and one seeded three weeks back.
            "mentions": _mentions(record["text"]),
        }

    def roots(self, channel: dict) -> list[dict]:
        return [m for m in channel.get("messages", []) if m.get("thread_id") is None]

    def replies(self, channel: dict, root_id: str) -> list[dict]:
        return [m for m in channel.get("messages", []) if m.get("thread_id") == root_id]

    def summarise_root(self, channel, record):
        replies = self.replies(channel, record["id"])
        return {
            **self.render(record),
            "reply_count": len(replies),
            "last_reply_at": replies[-1]["at"] if replies else None,
        }

    def list_channels(self):
        state = self.load()
        channels = []
        for record in state["channels"]:
            messages = record.get("messages", [])
            channels.append(
                {
                    "id": record["id"],
                    "topic": record.get("topic"),
                    "purpose": record.get("purpose"),
                    "members": list(record.get("members", [])),
                    "message_count": len(messages),
                    "thread_count": sum(1 for m in messages if m.get("thread_id") is None),
                    "last_message_at": messages[-1]["at"] if messages else None,
                }
            )
        return {
            "channels": channels,
            "users": [_public(person) for person in state.get("users", [])],
        }

    def history(self, channel_id, limit, before):
        state = self.load()
        channel = self.channel(state, channel_id)
        roots = self.roots(channel)
        if before is not None:
            index = next((i for i, m in enumerate(roots) if m["id"] == before), None)
            if index is None:
                raise ToolError("message_not_found", f"Unknown root message: {before}")
            roots = roots[:index]
        window = roots[-limit:] if limit < len(roots) else roots
        return {
            "channel": channel_id,
            "topic": channel.get("topic"),
            "messages": [self.summarise_root(channel, record) for record in window],
            "has_more": len(roots) > len(window),
            "oldest_returned": window[0]["id"] if window else None,
        }

    def thread(self, message_id):
        state = self.load()
        channel, record = self.message(state, message_id)
        root_id = record.get("thread_id") or record["id"]
        _, root = self.message(state, root_id)
        replies = self.replies(channel, root_id)
        return {
            "channel": channel["id"],
            "thread_id": root_id,
            "root": self.render(root),
            "replies": [self.render(reply) for reply in replies],
            "reply_count": len(replies),
        }

    def search(self, query, channel_id, limit):
        state = self.load()
        channels = [self.channel(state, channel_id)] if channel_id else state["channels"]
        matches = []
        for channel in channels:
            for record in channel.get("messages", []):
                if query.casefold() in record["text"].casefold():
                    matches.append({"channel": channel["id"], **self.render(record)})
                    if len(matches) == limit:
                        return {"matches": matches, "limit_reached": True}
        return {"matches": matches, "limit_reached": False}

    def post(self, channel_id, text, thread_id):
        state = self.load()
        channel = self.channel(state, channel_id)
        messages = channel.setdefault("messages", [])
        if thread_id is not None:
            root = next((m for m in messages if m["id"] == thread_id), None)
            if root is None:
                raise ToolError(
                    "thread_not_found", f"Channel {channel_id} has no message {thread_id}"
                )
            if root.get("thread_id") is not None:
                raise ToolError(
                    "invalid_thread", f"{thread_id} is itself a reply; reply to its root"
                )
        record = {
            "id": f"{channel_id}/{len(messages) + 1:04d}",
            "author": self.actor,
            "at": self.clock(),
            "text": text,
            "thread_id": thread_id,
            "reactions": {},
        }
        messages.append(record)
        self.save(state)
        return {"channel": channel_id, "message": self.render(record)}


class KnowledgeBase(_JsonStore):
    filename = "kb.json"
    missing_code = "no_knowledge_base"
    missing_message = "This world has no knowledge base"
    invalid_code = "invalid_kb"
    required = ("pages",)

    def page(self, state, page_id: str) -> dict:
        for record in state["pages"]:
            if record["id"] == page_id:
                return record
        raise ToolError("page_not_found", f"Unknown page: {page_id}")

    def entry(self, record: dict, vnow: str) -> dict:
        return {
            "id": record["id"],
            "title": record["title"],
            "parent": record.get("parent"),
            "labels": list(record.get("labels", [])),
            "version": record.get("version", 1),
            "last_edited_by": record.get("last_edited_by"),
            "last_edited_at": record.get("last_edited_at"),
            "age_days": _age_days(record.get("last_edited_at", ""), vnow),
            "edit_comment": record.get("edit_comment"),
            "bytes": len(record.get("body", "").encode("utf-8")),
        }

    def list_pages(self):
        state = self.load()
        vnow = self.clock()
        pages = sorted(state["pages"], key=lambda record: record["id"])
        return {
            "space": state.get("space"),
            "as_of": vnow,
            "pages": [self.entry(record, vnow) for record in pages],
            "children": {
                record["id"]: sorted(
                    child["id"] for child in state["pages"] if child.get("parent") == record["id"]
                )
                for record in pages
            },
        }

    def get_page(self, page_id):
        state = self.load()
        record = self.page(state, page_id)
        body = record.get("body", "")
        return {
            **self.entry(record, self.clock()),
            "body": body,
            "lines": _lines(body),
            "history": list(record.get("history", [])),
        }

    def search(self, query, limit):
        state = self.load()
        matches = []
        for record in sorted(state["pages"], key=lambda item: item["id"]):
            for hit in _hits(record.get("body", ""), query):
                matches.append(
                    {
                        "page": record["id"],
                        "title": record["title"],
                        "last_edited_at": record.get("last_edited_at"),
                        "line": hit["line"],
                        "text": hit["text"],
                    }
                )
                if len(matches) == limit:
                    return {"matches": matches, "limit_reached": True}
        return {"matches": matches, "limit_reached": False}

    @staticmethod
    def check_id(page_id: str):
        if not page_id or any(char not in ID_CHARS for char in page_id):
            raise ToolError("invalid_page_id", "Use a-z, 0-9, '_', '-', '.' or '/' in a page id")
        if page_id.startswith("/") or page_id.endswith("/") or ".." in page_id:
            raise ToolError("invalid_page_id", "A page id is a slash-separated path, not a file")

    def edit(self, page_id, body, comment):
        state = self.load()
        record = self.page(state, page_id)
        now = self.clock()
        record.setdefault("history", []).append(
            {
                "version": record.get("version", 1),
                "at": record.get("last_edited_at"),
                "by": record.get("last_edited_by"),
                "comment": record.get("edit_comment"),
                "bytes": len(record.get("body", "").encode("utf-8")),
            }
        )
        previous = record.get("version", 1)
        record["body"] = body
        record["version"] = previous + 1
        record["last_edited_at"] = now
        record["last_edited_by"] = self.actor
        record["edit_comment"] = comment
        self.save(state)
        return {
            **self.entry(record, now),
            "previous_version": previous,
            "edit_comment": comment,
        }

    def create(self, page_id, title, body, parent, labels):
        self.check_id(page_id)
        state = self.load()
        if any(record["id"] == page_id for record in state["pages"]):
            raise ToolError("page_exists", f"Page already exists: {page_id}")
        if parent is not None:
            self.page(state, parent)
        now = self.clock()
        record = {
            "id": page_id,
            "title": title,
            "parent": parent,
            "labels": list(labels),
            "body": body,
            "version": 1,
            "author": self.actor,
            "last_edited_by": self.actor,
            "last_edited_at": now,
            "edit_comment": None,
            "history": [],
        }
        state["pages"].append(record)
        self.save(state)
        return self.entry(record, now)
