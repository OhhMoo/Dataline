# Workflow surface contract v1

The human-context axis: the ticket the work arrives in, the channel the context
is scattered across, and the wiki that is right in some places and out of date in
others. Fifteen tools over three seeded JSON documents. Frozen for R003; amending
it means re-running `gateway/tests` and telling Tracks A and C.

Distinct from `docs_*`, which reads markdown files in `/workspace/docs`. That is
the repository's documentation. This is the *organisation* around it.

## The rule this contract exists to keep

**A write must be readable by the next process that opens the world.** A
ticketing system that accepts a comment and forgets it cannot represent the work,
and a read-only mock is indistinguishable from a dead tool as soon as an
archetype asks the agent to hand something back. So every write lands in the same
document it was read from, atomically, and `test_workflow.py` proves it by
writing through one `Gateway` and reading through a *different* one over the same
world tree.

## Where things live

| | Path | In the world tree? |
|---|---|---|
| Tickets | `workflow_dir/issues.json` (default `/var/workflow/issues.json`) | **yes** |
| Chat | `workflow_dir/chat.json` | **yes** |
| Wiki | `workflow_dir/kb.json` | **yes** |

All three are world state on the same footing as `var/sched/state.json` and
`var/ingest/state.json`: a theme seeds them at build time, a task author plants in
them, the agent changes them during the episode, and the grader reads the result
after teardown. They are inside the fingerprinted tree on purpose — the initial
backlog is part of what makes two builds the same world.

`workflow_dir` is a sibling of `/workspace`, so `fs_read` and `docs_get` cannot
walk to the raw documents; the only way in is through these tools.

**Open item for Track A.** `world/world/fingerprint.py` has no `workflow`
section, exactly as it has none for `obs`. The fix is one conditional block:

```python
workflow = root / "var" / "workflow"
if workflow.is_dir():
    sections["workflow"] = json_hash(
        {p.name: json.loads(p.read_text(encoding="utf-8")) for p in sorted(workflow.glob("*.json"))}
    )
```

Worlds without the directory keep their recorded hashes. Until that lands, a task
built on this surface is not fingerprint-covered for its seeded content.

## Determinism

Three requirements, all structural rather than conventional:

1. **Seeded content is static bytes.** The reference corpus
   (`gateway/tests/workflow_seed.py`) is a literal document dumped with sorted
   keys and no separators-whitespace. Two builds produce identical SHA-256s.
2. **Every generated timestamp is the virtual clock.** `workflow.py` has no
   `time.time()`, no `datetime.now()`, no `utcnow()`. The single clock is the one
   the gateway was constructed with, which reads `vnow` from the scheduler state.
3. **Every generated id is a pure function of the document.** A comment is
   `<issue>#<len(comments)+1>`, a message is `<channel>/<len(messages)+1:04d>`, a
   page revision is `version+1`. No UUIDs, no counters held outside the file.

Together those make a write session replayable: `test_workflow.py` runs all
fifteen tools against a world, replays the trace onto a clone, and requires
`world_hash` of the two worlds to be **equal** afterwards.

## Issues — `issues.json`

```json
{"version": 1,
 "project": {"key": "DATA", "name": "Data platform"},
 "statuses": ["open", "triage", "in_progress", "blocked", "done", "wontfix"],
 "users": [{"id": "dpatel", "name": "Divya Patel", "role": "Analytics engineer"}],
 "issues": [
   {"id": "DATA-141", "title": "...", "body": "...", "status": "open",
    "reporter": "mrivera", "assignee": null, "labels": ["revenue"],
    "created_at": "2026-03-05T08:12:00Z", "updated_at": "2026-03-05T08:26:00Z",
    "comments": [{"id": "DATA-141#1", "author": "dpatel", "at": "...", "body": "..."}],
    "history":  [{"at": "...", "actor": "...", "field": "status", "from": "open", "to": "done"}],
    "links":    [{"type": "relates_to", "issue": "DATA-136"}],
    "authoring": {"kind": "...", "note": "..."}}]}
```

`statuses` is declared by the world, not hardcoded, so a theme can run a Jira
workflow or a two-state GitHub one. `issues_update` refuses a status outside the
list and names the legal set in the error.

| tool | op_class | notes |
|---|---|---|
| `issues_list` | read | newest-updated first; filters on status, assignee, label; carries `statuses` and `users` so the agent can discover the vocabulary |
| `issues_get` | read | body, comments, history, links |
| `issues_search` | read | literal, case-insensitive, over title, body and every comment; each hit names `field`, `ref` and `line` |
| `issues_comment` | **write** | appends on the virtual clock, bumps `updated_at`, appends a history row |
| `issues_update` | **write** | status, assignee, `unassign`, `add_labels`, `remove_labels`; one history row per field that actually changed |

**A write that changes nothing says so.** `issues_update` with the status the
ticket already has returns `changes: []`, leaves `updated_at` alone, writes
nothing to disk, and flags the action row `no_change`. Reporting `ok` over an
unchanged ticket is the empty-success failure this project keeps finding.

`unassign` is a separate boolean because `assignee: null` cannot be told apart
from "leave the assignee alone" once the field is optional, and handing a ticket
back to triage is a real operation.

## Chat — `chat.json`

```json
{"version": 1, "workspace": "acme-data", "users": [...],
 "channels": [
   {"id": "data-eng", "topic": "...", "purpose": "...", "members": ["dpatel"],
    "messages": [
      {"id": "data-eng/0003", "author": "kliu", "at": "2026-02-18T10:11:00Z",
       "thread_id": "data-eng/0001", "text": "...", "reactions": {"+1": 2},
       "authoring": {"kind": "...", "note": "..."}}]}]}
```

One flat message list per channel, in chronological order. A message with
`thread_id: null` is a thread root; anything else is a reply to that root.
Replying to a reply is refused (`invalid_thread`) — a thread has one root.

`mentions` is **derived at read time** from the text (`@handle`), never stored, so
a seeded message and one the agent posts a moment ago are treated identically.
Handles are not validated: people mistype them, and a mention of somebody who has
left is a real artefact.

| tool | op_class | notes |
|---|---|---|
| `chat_channels` | read | topic, purpose, members, message and thread counts, `last_message_at` |
| `chat_history` | read | thread roots only, oldest-first, each with `reply_count` and `last_reply_at`; `before` pages backwards, `has_more` says whether it truncated |
| `chat_thread` | read | root plus every reply in order; takes a root id or any reply's id |
| `chat_search` | read | literal, case-insensitive, all channels or one |
| `chat_post` | **write** | appends on the virtual clock, optionally into a thread |

## Knowledge base — `kb.json`

```json
{"version": 1, "space": {"key": "DE", "name": "Data Engineering"},
 "pages": [
   {"id": "de/runbooks/nightly-orders-load", "title": "...", "parent": "de/runbooks",
    "labels": ["runbook"], "body": "...markdown...", "version": 7,
    "author": "kliu", "last_edited_by": "kliu", "last_edited_at": "2025-11-04T10:12:00Z",
    "edit_comment": "...", "history": [{"version": 6, "at": "...", "by": "...", "comment": "..."}],
    "authoring": {"kind": "stale", "contradicts": ["de/runbooks/vendor-feed-sla"], "note": "..."}}]}
```

Page ids are slash-separated paths; `parent` builds the hierarchy and `kb_list`
returns a `children` index alongside the flat page list.

**`age_days` is the whole point.** It is computed on every call as whole days
between `last_edited_at` and the world clock. A wiki where two pages disagree is
only navigable if the reader can tell which one is older, and a date alone does
not say that without knowing what today is. `kb_search` carries each hit's
`last_edited_at` for the same reason.

| tool | op_class | notes |
|---|---|---|
| `kb_list` | read | every page with parent, version, editor, `last_edited_at`, `age_days`, `bytes`, plus the `children` index and `as_of` |
| `kb_get` | read | body, numbered evidence lines, revision history |
| `kb_search` | read | literal, case-insensitive, over bodies; each hit carries its page's edit date |
| `kb_edit` | **write** | replaces the body, pushes the replaced revision onto `history`, bumps `version`, stamps the actor and the clock; flagged `overwrite` |
| `kb_create` | **write** | new page under an existing parent; refuses a taken id, an unknown parent, or an id outside `[a-z0-9_-./]` |

## Authorship

Writes are attributed to `workflow_actor` (default `"agent"`), so a world can
name the persona the agent is playing and seed that id into `users`. Nothing here
lets the agent write as somebody else.

## Deliberate imperfection — the documented catalogue

A backlog where every ticket matters, a channel where every statement is true and
a wiki where every page is current are not a backlog, a channel and a wiki. They
are a solution manual. So the reference corpus plants the normal state of a real
team, and **records author-side which is which** in an `authoring` block:

| `kind` | what it marks |
|---|---|
| `noise` | true, closed, irrelevant |
| `stale` | open or published and untouched long enough to be wrong |
| `near_duplicate_trap` | the same symptom as the live incident, a different cause |
| `duplicate` | two tickets for one thing, with `duplicate_of` and `linked` |
| `true_and_irrelevant` | verifiable through another surface, unrelated to the incident |
| `partial_conclusion` | a true statement that is not the claim it is being used as |
| `incident_report_lay` | the incident as a non-engineer described it |
| `constraint` | a real limit on the shape of an acceptable solution |
| `undocumented_decision` | a decision made in a thread and written down nowhere else |
| `unanswered_mention` | the useful observation, addressed to nobody who answered |
| `confident_wrong` | stated flatly, agreed with, and wrong |
| `accurate_but_partial` | true, and silent about the half that matters |
| `unactioned_remediation` | the control that would have prevented this, still open |
| `current_and_contradicting` | the page that supersedes another and says so |
| `accurate` | current and correct |

**Every `authoring` block is stripped from every tool response**, the same
treatment `known_imprecision` gets on the observability surface. Telling the
agent which ticket is the decoy destroys the measurement (F202).
`test_workflow.py` calls all fifteen tools and asserts that neither the key nor
any note text appears in any response, and that the raw documents are unreachable
from `fs_read` and `docs_get`.

A planted contradiction is only fair if the evidence that resolves it is
reachable through the same surfaces. In the reference corpus every one is:
the stale runbook is 120 days old against the superseding page's 13 and
`kb_list` serves both numbers; the confident wrong guess in `#incidents` names a
connector whose `ingest_get` record shows it lands a different table entirely;
the "it went green" comment is checkable against `sched_state` and
`metrics_series`.

## Not done

- No fingerprint section for `var/workflow/` (Track A — see above).
- No attachments, no rich text, no reactions the agent can add, no issue
  creation. An agent can comment on and re-triage a ticket but cannot file one.
- `issues_search` and `kb_search` have no `skipped` accounting, because a JSON
  document is either loaded or the whole call fails; there is no per-record
  unreadability the way `docs_search` has per-file unreadability.
- Nothing here is wired into any task's official score. Shipping the surface and
  scoring on it are separate decisions.
