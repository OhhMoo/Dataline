# R002 / E1 — Concurrency and interleaved-event failures

Agent: E1. Blocks claimed: **S600–S611** (12 sources, 38 free), **A040–A044** (5 archetypes, 5 free).
Gates: `--gate G0 --gate G6` → **ALL GATES PASSED**, zero failures and zero warnings on my files.
`--gate G1` run voluntarily: **11 of my 12 quotes verified verbatim by the gate itself**; the one
miss is a 403, not a mismatch (see "What I could not verify").

---

## 1. Where the evidence actually lives, and where it does not

The assignment assumed a Reddit-heavy harvest. That was not available to me, and I want that on
the record before anything else.

**Reddit is hard-blocked for this agent.** `reddit.com` and `old.reddit.com` return HTML or
nothing to direct fetches, and `WebSearch` refuses the domain outright
(`400 The following domains are not accessible to our user agent: ['reddit.com']`). I therefore
**cited zero Reddit sources**. I could have found thread titles through third-party mirrors and
written quotes I could not see on the page. That is precisely the fabrication the shared brief
calls the worst outcome of the round, so I did not.

**Stack Overflow was genuinely thin for this shape.** I queried the Stack Exchange API directly
across four phrasings (concurrent MERGE duplicates, Airflow backfill vs scheduled run, Snowflake
lost update, Spark speculation duplicate output). Best hit across all four was a 2-score, 562-view
question with no accepted answer. My read: concurrency incidents do not present as answerable
questions. The asker cannot reproduce them, so they get filed as bug reports instead of questions.

**Where the evidence actually was: open GitHub issues.** That is where practitioners write down
millisecond timelines, snapshot ids, and duplicate-row counts, because they are trying to convince
a maintainer. Eight of my twelve sources are GitHub issues and six of those are **still open**,
liveness checked via the GitHub API on 2026-09-13 with state and creation date recorded in each
record's `notes`.

One methodological note worth passing on: **the `gh` API is a much better harvesting tool than
web fetch for this.** It returns the raw markdown body, which let me choose quote spans that
survive G1's tag-stripper. G1 strips `<code>` and `<strong>` to spaces, so a span crossing a
backtick-then-comma boundary silently stops matching. Several of my quotes are shorter than I
would have liked purely for that reason, with the specifics moved into `notes` rather than
smuggled into the quote as if they were one contiguous span.

---

## 2. Sources written — S600–S611

| ID | What it establishes | Type | Vividness | Attestation | State |
|---|---|---|---|---|---|
| S600 | Two concurrent dbt runs race on check-then-ALTER schema evolution | repo | high | firsthand_anon | **open** |
| S601 | Airflow backfill reports COMPLETE while its own DAG runs still execute | repo | high | firsthand_anon | closed |
| S602 | Why S601 is a timing window: ~30s completion sweep vs a two-step commit | repo | high | secondhand | **open** |
| S603 | Coordinator crash between commit and offset-advance re-registers the same file in two snapshots | repo | high | firsthand_named | **open** |
| S604 | Concurrent DML during a backfill overwritten by the versions the backfill had already read | repo | high | firsthand_named | closed |
| S605 | Concurrent reader mid-view-swap sees torn state the transaction was supposed to hide | repo | high | firsthand_anon | **open** |
| S606 | Deterministically named tmp relation is a shared mutable object between concurrent runs | repo | medium | secondhand | **open** |
| S607 | Stale defer-exit SUCCESS fails a resumed attempt that is already RUNNING | repo | high | firsthand_anon | **open** |
| S608 | CircleCI postmortem: race in workflow termination → 33,135 duplicate workflows, 38 projects | other | high | **firsthand_named** | n/a |
| S609 | Two streaming tasks in one job both merging into the same Silver table | forum | medium | firsthand_anon | n/a |
| S610 | dbt's own idempotence guidance: incremental result depends on target state | docs | medium | unattested | n/a |
| S611 | DuckDB: appends never conflict, even on the same table | docs | medium | unattested | n/a |

**No record in my block claims `usable_for: [frequency]`.** Two of them (S603, S608) would have
cleared G6's rule mechanically — `firsthand_named` plus a non-forum `source_type`. I did not claim
it anyway, and said so in each record. S608 is one company's incident report and S603 is two
incidents at one company; both are existence proofs, not base rates. Claiming frequency there is
exactly the move the 47% citation audit was designed to catch, and the gate passing would have
made it worse, not better, by laundering it.

Attestation calls I want reviewed:

- **S603 → `firsthand_named`, with a caveat written into the record.** The author handle is
  `tongwai-wong-appier` and the body says "we have observed this in production". The company
  attribution rests on a handle suffix, not a signed byline. I flagged that inside the record
  rather than quietly upgrading it.
- **S602 and S606 → `secondhand`.** Both are maintainers writing up a code path or a user-facing
  limitation, not an outage they were in. S602 explicitly cites someone else's bug report (S601).
  It would have been easy to call these firsthand because the authors are authoritative. Authority
  is not attestation.

---

## 3. Archetypes written — A040–A044

All five are drafts (no `attacks` records yet — see §5). All five name the grading reference as
harness-side per F121, carry an `agent_visible_brief` that does not name the defect per F202, and
carry something a full refresh destroys.

| ID | Mechanism | The thing full refresh destroys |
|---|---|---|
| **A040** | Two overlapping incremental builds both read the same high-water mark; a second band comes from a shared permanently-named staging relation | An analyst's hand-patch on three refunded orders (only copy) + every row-level invocation stamp |
| **A041** | Backfill reported complete before its runs existed; on-call launched a repair on that false signal; dates fall into four categories | Six weeks of source has aged out of the landing zone — a rebuild **succeeds** and returns an empty window |
| **A042** | Retry started while attempt 1 was still writing; the audit log records attempt 1 as having written nothing | Quarter-end reconciliation adjustment + the task-log ordering that is the only evidence |
| **A043** | BI extract read inside a view-swap window; the wrong number provoked a compensating change that is itself a defect | The leftover `__dbt_backup` relation (only physical evidence) + append-only snapshot history Finance quotes from |
| **A044** | Late-arriving burst landed inside an in-flight repair; repair's read-modify-write lost the concurrent load's increments | `ingested_at` is stamped at ingestion — any reload re-stamps it and makes the incident permanently undiagnosable |

Design commitments I made across all five, which I think matter more than the individual stories:

**1. Nothing races at world-build time.** S611 is why. On DuckDB appends never conflict, so a live
race produces no error and nothing forces a particular winner — the world would not rebuild
identically from the seed, which is the project's core contract. So every interleave is staged as
*committed artifacts*: two invocation ids with overlapping start/end times, per-row load stamps, a
scheduler event log with explicit ordering. The only thing that actually runs concurrently is the
**scoring probe**, at a fixed offset. I put this in each archetype's `notes` because it is the
single easiest way for a world-builder to get these wrong.

**2. Every probe has an anti-degenerate twin.** F121's second half — the idempotency probe
satisfied by making the load a permanent no-op — applies with full force to concurrency hardening,
where "disable the thing" is an even more attractive shortcut. So: A040's probe fails a model that
skips the planted slice; A042 pairs the stale-event probe with a *crashed-attempt-still-retries*
probe, so deleting `retries` scores zero; A043 pairs the read-during-write probe with an
*extract-still-runs-outside-the-window* probe.

**3. Every decoy is a real defect accounting for part of the arithmetic.** A040's vendor resend,
A041's genuine source-outage day, A042's malformed-record quarantine, A043's late currency rate,
A044's timezone boundary. Each is independently fixable and none explains the whole symptom.

**4. Two of the five make the most authoritative surface the liar.** A041's orchestrator status
and A042's load audit table. This is the part I think is most specific to Michael's observation:
the model is not asked to spot a wrong number, it is asked to decide that its own bookkeeping is
wrong. In A042 that is not malice — the audit row is written by the scheduler's failure handler,
which by construction cannot know what the worker had already committed.

**5. A044 pairs errors that cancel.** Fact table too high, aggregate too low, headline moves under
a percent. If the agent can see it in a daily total the task collapses to arithmetic.

Discipline spread: four `data_engineering`, one `data_analytics` (A043 — the deliverable is a
defensible account of a past number, not a clean table). No `data_science`; this shape does not
naturally produce one and I did not force it.

---

## 4. What I rejected, and why

- **All Reddit.** Unfetchable. See §1. Zero records rather than unverifiable ones.
- **Three closed Airflow zombie-task issues** (#42553, #31212, #32289). State-checked, all closed,
  and all about zombie *detection* bookkeeping rather than a second writer. S607 is the same family
  done better and is still open; citing four for one mechanism is padding.
- **dbt-core #10432** ("Duplicate row detected during DML action", Snowflake MERGE). Closed as *not
  planned* with `awaiting_response` and `stale` labels, and the reporter never confirmed the
  duplicate source. Good error string, unresolved diagnosis. Rejected: it would have licensed
  `world_texture` and nothing I needed.
- **dbt Discourse "incremental model + unique constraint still allows duplicates"** (verbatim text
  confirmed). Rejected as off-shape: it is about `unique_key` not being a constraint, which is a
  sequential-re-run issue, not an interleaving one. Including it would have padded the count while
  weakening the block's focus.
- **Four tutorial/essay pages on high-water marks and clock skew** that surfaced repeatedly in
  search. Every one was `unattested` with no incident and no specifics — "clock skew can cause you
  to miss records" is the shared brief's own example of an unfalsifiable claim. **This means I have
  no real-incident source for the clock-skew high-water-mark shape, and therefore authored no
  archetype on it.** That is a genuine gap in my coverage of the assignment, not an omission.
- **Vendor and vendor-adjacent explanations.** The Databricks community manager's reply naming
  `ConcurrentAppendException` in S609's thread is not treated as independent corroboration (F400).
- **An `attacks: [F121, F202]` field I had initially put on all five archetypes.** Wrong semantics
  — `attacks` means "attack records filed against *this* archetype", and F121/F202 target A001 and
  A032. Removed. These are drafts and the G3 warning now says so correctly.

---

## 5. What I could not verify

- **S609 is the only G1 failure in the entire 179-source corpus, and it is mine.**
  `community.databricks.com` returns **403** to the gate's `alkera-research-swarm/1.0` user agent.
  This is a fetch refusal, not a quote mismatch: I re-fetched the page with a browser user agent,
  applied G1's own normalization (strip script/style, strip tags, collapse whitespace, lowercase),
  and the quote matches exactly, with the author handle `khangnguyen164` and date `03-25-2025`
  visible on the page. I wrote a `repair_note` into the record saying all of this and recommending
  withdrawal if a reviewer wants a clean G1 run — it is the least load-bearing source in my block.
- **S603's company attribution.** Inferred from the GitHub handle suffix and first-person plural.
  Flagged inside the record.
- **S608's postmortem timings.** Verified verbatim on the status page. I have no independent
  confirmation of the internal timeline beyond CircleCI's own account of it, which is inherent to
  a self-published postmortem.
- **Whether the six open issues remain open.** Checked 2026-09-13 via the GitHub API; recorded per
  record. They can close at any time.
- **Nothing in my block was verified against a running system.** These are all designs.

One coordination note, not a verification issue: **G3 now fails on `archetype count 36 outside
target 10-30`** (28 shippable after the 8 `redesign_required`). My five contributed to that. G3
also has three pre-existing `derived_from` failures (A004→S017, A019→S010, A022→S010) and G4 has
one pre-existing link failure (F552/A019). None are mine, and I did not touch them.

---

## 6. The single biggest weakness in my own output

**Four of my five archetypes are the same task wearing different clothes: "reconstruct a write
ordering from stamps and logs, then delete or repair the surplus."** A040, A041, A042 and A044 all
resolve to a join between a row-level provenance column and a time-ordered event log. The stories
differ, the surfaces differ, the decoys differ — but a model that learns *"when two writers are
implicated, stop reasoning from the final state and join the provenance stamp to the run log"* has
learned the trick for four fifths of my block in one shot. G3's dedup check does not catch this
because it compares titles and app lists, and mine differ. It is a semantic duplicate, not a
structural one, and I only see it because I wrote all five.

A043 is the one that is actually different, and it is different for an instructive reason: the
corrupted artifact **no longer exists in queryable form**. Nothing in the mart is wrong by the time
the agent looks. The agent has to prove a claim about a state that is gone, from timestamp
alignment and a leftover relation — no provenance column will save it. If I were spending the
budget again I would cut one of A041/A044 and write a second archetype in A043's mode.

The second weakness, which is downstream of the first: **I gave every archetype a row-level
provenance column because it makes the task deterministically gradable, and in doing so I may have
made the tasks easier than the incidents they came from.** The real S604 has no residue at all —
the losing write is simply absent, and that is what makes lost updates the hardest sub-shape in
this family. A044 gestures at this but still hands the agent an `ingested_at` and a `_processed_at`
to triangulate with. A genuinely faithful lost-update task would give the agent *nothing* in the
final state and force the entire argument from the repair job's read timestamp against a load the
aggregate does not reflect. I did not author that, because I could not convince myself it was
deterministically gradable at a rubric granularity finer than pass/fail. That is the honest version
of "concurrency is hard to make deterministic": the problem is not staging the race — staging it as
committed artifacts works fine — it is that **the most realistic concurrency bugs leave evidence so
thin that any artifact I add to make them gradable also makes them solvable.** Someone should attack
A044 specifically on whether its evidence trail is too generous.
