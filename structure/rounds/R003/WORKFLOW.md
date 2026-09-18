# WORKFLOW — issue tracker, comms and knowledge base

**R003. Agent: WORKFLOW.** Built in `gateway/src/alkera_gateway/` and
`gateway/tests/`. `world/` and `observability.py` untouched (FAULTS and
OBS-HISTORY own those); `tasks/harness/` untouched.

---

## 1. What shipped

**Fifteen tools across three surfaces. 48 → 63.**

| Surface | Tools | Store |
|---|---|---|
| Issue tracker | `issues_list` `issues_get` `issues_search` `issues_comment` `issues_update` | `var/workflow/issues.json` |
| Chat | `chat_channels` `chat_history` `chat_thread` `chat_search` `chat_post` | `var/workflow/chat.json` |
| Knowledge base | `kb_list` `kb_get` `kb_search` `kb_edit` `kb_create` | `var/workflow/kb.json` |

Five of the fifteen are writes: `issues_comment`, `issues_update`, `chat_post`,
`kb_edit`, `kb_create`.

| File | What |
|---|---|
| `gateway/src/alkera_gateway/workflow.py` | the three stores (new, 559 lines) |
| `gateway/WORKFLOW_CONTRACT.md` | the contract, the JSON shapes, the imperfection catalogue (new) |
| `gateway/tests/workflow_seed.py` | the reference corpus: 8 issues, 3 channels / 16 messages, 9 wiki pages (new) |
| `gateway/tests/test_workflow.py` | 28 tests (new) |
| `tools.py`, `operations.py`, `config.py`, `conftest.py`, `config.example.json`, `README.md` | wiring |

Config adds `workflow_dir` (default `/var/workflow`) and `workflow_actor`
(default `"agent"`, the identity writes are attributed to).

**Tests: 115 passed, 0 failed, 0 skipped** (`uv run pytest -q`, 135 s). Baseline
on this machine before the work was **87 passed** — the brief's "78 passed, 9
skipped" is the same suite with the optional `dbt`/`snowflake` extras missing.
`ruff check src tests` clean, `ruff format` clean.

### The distinction from `docs_*`, since that was the point

`docs_*` reads markdown files under `/workspace/docs` — the repository's own
documentation, versioned with the code. The KB is the *wiki*: page hierarchy,
`parent`/`children`, revision history, an editor and an edit date per page, and a
derived `age_days` against the world clock. Different store, different
provenance, different failure mode. The two coexist in the fixture world and
never read each other.

---

## 2. Writes persist — the evidence

The failure mode named in the brief ("a read-only mock of a ticketing system
cannot represent the work") is closed structurally: every write rewrites the same
document it was read from, atomically (`canonical()` to `.pending`, then
`replace`), and is proved by reading it back **through a different `Gateway`
instance over the same world tree** — not through the object that wrote it.

| Test | What it proves |
|---|---|
| `test_a_comment_persists_and_a_second_gateway_reads_it_back` | Gateway A appends `DATA-139#3`; Gateway B (fresh process state, separate audit dir) sees three comments, the new body text, `updated_at` moved to the world clock, the history row, and `comment_count: 3` in the *list* view too |
| `test_status_and_assignee_changes_persist_with_a_history_trail` | status/assignee/labels change, the history row appears, and `issues_list(status="in_progress")` now contains the ticket while `issues_list(status="open")` does not — the filter reads the new value, there is no cached index |
| `test_a_posted_reply_lands_in_the_thread_a_later_read_returns` | a reply posted by A appears in B's `chat_thread` with `reply_count: 1`, and B's `chat_channels` shows `message_count: 9` and the new `last_message_at` |
| `test_editing_a_page_keeps_the_revision_it_replaced` | body replaced, `version` 7→8, the replaced revision (v7, `2025-11-04`, `kliu`, its comment and its byte count) is still in `history`, and `kb_list` reports `age_days: 0` |
| `test_creating_a_page_needs_a_parent_that_exists_and_a_free_id` | a created page shows up in B's `children` index under its parent |

**Assertions are on content, never on `ok`.** `assert_ok` unwraps and returns the
result; every test then names a sentence, a date, a reply count or a byte.

**A write that changed nothing says so.** `issues_update` with the status the
ticket already holds returns `changes: []`, leaves `updated_at` untouched, writes
no bytes, and flags the action row `no_change`
(`test_an_update_that_changes_nothing_says_so_rather_than_claiming_success`).
That was the one place this surface could have produced an empty success.

---

## 3. Determinism

Three mechanisms, each structural rather than conventional.

**1. Seeded content is static bytes.** The corpus is a literal Python document
dumped with `sort_keys=True` and no whitespace. Two builds into different
directories:

```
issues.json  2a247d54f741a1659750d3bbf42eb2156b0ad18ff1bddfd6477a6fad7486a0ad  7 513 B
chat.json    4ca66631f9e2ef335b3b382106405507ceaf02aec42da317432259d30ca409f7  5 578 B
kb.json      35b9110faa7633f4d1d9845f8a0398a6465819097b28ad91f58e3fd393528ba9  8 150 B
identical across two builds: True
```

**2. No wall clock anywhere.** `workflow.py` contains no `time.time()`, no
`datetime.now()`, no `utcnow()`; `grep` over the module returns only
`datetime.fromisoformat`. Every generated timestamp is `self.clock()`, which
reads `vnow` from the scheduler state.
`test_every_workflow_timestamp_comes_from_the_virtual_clock` writes a comment at
`2026-03-05T09:00:00Z`, moves the world's `vnow` to `2026-03-06T02:30:00Z`, and
shows the next comment, the next chat message and the next page edit all stamped
with the new virtual value — and `age_days` measured against it, not against
today.

**3. Generated ids are pure functions of the document.** `DATA-141#3`,
`data-eng/0009`, `version+1`. No UUIDs, no external counters.

**The gate that actually settles it** —
`test_a_write_session_replays_onto_a_clone_byte_for_byte`: run all fifteen tools
(ten reads, five writes) against a world, clone the pristine world beforehand,
replay the trace onto the clone through `replay()`, and require

- every response digest to match (`responses_match`), and
- `world_hash(original) == world_hash(clone)` afterwards, having first asserted
  `world_hash(original) != initial_fingerprint` so the equality is not vacuous.

Byte-identical worlds after five writes each, including ids and timestamps.

---

## 4. What is deliberately stale, wrong or contradictory

Recorded author-side in an `authoring` block on each record, **stripped from
every tool response** — the same treatment `known_imprecision` gets on the
observability surface. 16 annotations across the corpus. The vocabulary is
tabulated in `WORKFLOW_CONTRACT.md`.

The corpus is anchored on the incident the observability fixture already models
(`daily_load` fails 03-04 with "connection reset by peer"; backfills two days and
goes green 03-05; 03-03 and 03-04 at a quarter volume; every amount on 03-04
null; world clock `2026-03-05T09:00:00Z`), so the surfaces tell one story rather
than three.

### Issue tracker (8 tickets)

| Ticket | Kind | Why it is there |
|---|---|---|
| DATA-141 | `incident_report_lay` | the incident as the finance analyst experienced it: a symptom, a Friday deadline, no schema. Carries a `relates_to` link to DATA-136 that **a human got wrong** — the tracker itself points at the decoy |
| DATA-140 | `duplicate` | filed by the same person seven minutes later, and **deliberately not linked** |
| DATA-139 | `partial_conclusion` | "last night's run finished green" — true, and not the same claim as "the data is correct". It went green *because* it backfilled |
| DATA-132 | `near_duplicate_trap` | same symptom (null amounts), different cause, already diagnosed in January. The first hit for the obvious search |
| DATA-136 | `true_and_irrelevant` | every word verifiable through `ingest_get`; lands `raw.vendor_returns`, nothing to do with orders amounts |
| DATA-127 | `stale` | open 56 days, assigned to somebody on leave, asking for a page that now exists |
| DATA-143 | `constraint` | quarter-close freeze to 2026-03-10 — a real limit on an acceptable fix |
| DATA-118 | `noise` | closed, irrelevant, so the queue is not eight tickets that all matter |

### Chat (3 channels, 16 messages)

- **`undocumented_decision`** — `data-eng/0003`, 2026-02-18: Acme overwrite the
  same filename on redelivery, so **backfill one day at a time**. Agreed in a
  thread reply, written down nowhere else; the author went on leave two days
  later. `kb_search` for it returns nothing.
- **`unanswered_mention`** — `data-eng/0006`, 2026-03-04: vendor ops says the
  03-03 drop landed late and the loader read it *while it was still uploading*.
  Addressed to the person on leave. `reply_count: 0`. This is the actual cause,
  and nothing else in the world states it.
- **`confident_wrong`** — `incidents/0003`: "This is the Globex connector again…
  that is where the missing amounts are coming from," two 👍 reactions.
  Disprovable in one `ingest_get` call.

### Knowledge base (9 pages)

- **`stale`** — `de/runbooks/nightly-orders-load`, last edited 2025-11-04,
  **120 days old**: wrong vendor, wrong folder, wrong column name, and a
  "safe to backfill up to seven days" instruction that is precisely what the
  February decision reversed.
- **`current_and_contradicting`** — `de/runbooks/vendor-feed-sla`, **13 days
  old**, states the Acme handover, the redelivery rule and the one-day backfill,
  and opens by saying it supersedes the runbook. The disagreement resolves on
  `age_days`, which both `kb_list` and `kb_search` serve.
- **`unactioned_remediation`** — the 2026-01-14 postmortem's action 3 ("refuse to
  read a file that is still growing") is marked **Open**, owner on leave. The
  control that would have prevented today's incident.
- **`accurate_but_partial`** — `de/architecture/warehouse-overview` is entirely
  true and never mentions the vendor drop, the loader or the scheduler.
- **`accurate`** — `de/policies/change-freeze` restates DATA-143 correctly.

**Every planted contradiction is resolvable from evidence the surfaces hand
over.** That was the constraint I held myself to: a trap whose resolution is not
reachable is not a difficulty knob, it is a coin flip.

### Keeping it author-side

`test_author_side_annotation_never_reaches_the_agent` calls all fifteen tools,
concatenates the responses, and asserts the key `authoring`, the substring `WF-`,
and each of the 16 note strings are absent — while asserting that corpus content
*is* present, so the check cannot pass vacuously.
`test_the_annotated_source_document_is_out_of_the_agents_reach` shows
`fs_read`/`docs_get` cannot walk from `/workspace` to `var/workflow/*.json`
(`path_denied`), and that the file on disk does carry the annotation for whoever
mounts the world afterwards.

---

## 5. Action-log conformance

`test_every_workflow_call_lands_a_well_formed_action_row` runs all fifteen tools
and checks, per §9.2.2: contiguous `seq`; `args_hash == digest(args)` over the
*normalised* arguments; `result_hash` matching; `exit: "ok"`; `target ==
targets[0]`; and **`op_class` present in the row and absent from the response**.
The five writes classify `write`, the other ten `read`. Targets are
`issue:<id>`, `chat:<channel>`, `thread:<id>`, `kb:<page>`, and the collection
names `issues`, `chat`, `kb`. `kb_edit` flags `overwrite`.

`issues`, `chat` and `kb` are distinct prefixes under `signals.py`'s
`tool.split("_")[0]`, so consulting the tracker, the channel and the wiki before
writing now raises `surfaces_before_write` — asserted in the same test.

---

## 6. What Track A needs to do (FAULTS / world)

I did not touch `world/`. Two things are owed there:

**(a) Seed the directory.** A theme should write three JSON documents into
`<world>/var/workflow/`. The schemas are in `WORKFLOW_CONTRACT.md` §Issues,
§Chat, §Knowledge base; the working reference implementation is
`gateway/tests/workflow_seed.py`, which imports nothing from the gateway and can
be ported verbatim without reversing the §10.2 dependency arrow. Dump with
`json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)` so
the bytes are stable. The gateway's tests do not block on this — the fixture
seeds it directly.

**(b) Fingerprint it.** `world/world/fingerprint.py` has no `workflow` section,
exactly as it has none for `obs`:

```python
workflow = root / "var" / "workflow"
if workflow.is_dir():
    sections["workflow"] = json_hash(
        {p.name: json.loads(p.read_text(encoding="utf-8"))
         for p in sorted(workflow.glob("*.json"))}
    )
```

Worlds without the directory keep their recorded hashes. Until that lands, a task
built on this surface is not fingerprint-covered for its seeded content. Note
this is world state on purpose (unlike `obs_state_dir`): the initial backlog is
part of what makes two builds the same world, and an agent's comment is a real
change to it.

---

## 7. The single biggest weakness in my own output

**The corpus is one story, written by one author, in one sitting — and every
difficulty claim in it is my own judgement, unmeasured.**

Concretely: I decided that `data-eng/0006` is findable, that
`de/runbooks/nightly-orders-load` is a convincing trap, and that an agent
comparing `age_days: 120` against `age_days: 13` will prefer the fresher page. I
have no evidence for any of it. This is precisely the failure §5 of `3_track.md`
names — *"every difficulty claim in the repo is derived from archetype text, not
measured"* — and I have added fifteen tools' worth of new surface area to it. It
is entirely possible that a frontier model runs `chat_search "amount"` once,
finds the unanswered mention in the first three results, and the whole
carefully-layered decoy structure collapses to a two-call task. It is equally
possible nothing ever searches chat at all and the cause is simply unreachable.
Both outcomes look identical from where I am standing, and a rollout would
separate them in about ten minutes.

Three narrower consequences of the same root cause:

1. **The trap density is probably too high.** Eight tickets, three of which point
   at the wrong thing, plus a wrong human-made `relates_to` link, plus a
   confidently wrong chat message with two agreeing reactions. Real backlogs are
   mostly *boring*, not mostly adversarial. I wrote one `noise` ticket and it is
   not enough ballast. If the measured Pass@1 comes out near zero, the first
   thing to try is deleting two traps, not adding a fourth.

2. **`world_hash` equality in the replay test is weaker evidence than it looks
   for the seeded half.** It proves my *writes* are deterministic. It does not
   prove the seeded content survives a real two-host world build, because the
   real build does not exist yet — Track A has not seeded the directory, and my
   determinism evidence is for a static fixture on one machine. The cross-host
   half of A4 is still unproven for this surface, as it is for every other.

3. **An agent cannot file a ticket.** `issues_create` is absent. "Open a ticket
   for the unactioned postmortem item" is one of the most natural things this
   world asks for, `kb_create` exists as its wiki analogue, and I left the
   tracker one write short. That was a scope call about the argument surface
   (id allocation, required fields, project key) and it was probably the wrong
   one.
