# R002 shared brief — read this before anything else

**Round R002 opened 2026-09-13.** Driven by the 2026-09-12 Environments Project meeting.
Every agent in this round reads this file. Nothing else is shared between you — if you are
authoring archetypes you are **blind** to the other authors by construction, and that is
deliberate. Do not go looking for their output.

Repo root is the parent of `implementation_structure/`. Full project context is `agent.md`
(§9 ground rules and frozen contracts, §10 repository architecture).

---

## 1. What this project is, in one paragraph

A reproducible factory for realistic data-agent failures, sold to frontier AI labs as
training and evaluation substrate. Each task runs inside a live containerized data stack
(warehouse, dbt, scheduler, BI, scattered docs). The agent works through a tool gateway;
grading is trajectory-blind and deterministic; the world rebuilds identically from a seed.

**The target that governs every decision: Pass@1 below 20% for frontier models that are
genuinely trying.** Fable 5.1 and GPT-class models are very capable. A task they solve is
not a task, it is a demo.

---

## 2. The two-axis source model — NEW in R002, and the most important rule here

A citation audit of this corpus found **roughly 47% of sampled practitioner sources failed
semantic review while passing every mechanical check.** Every single failure had a perfect
verbatim quote match. The quote proved the words were on the page. It proved nothing about
whether the author witnessed the thing, or whether it happens to anyone else.

So every source record now carries **two independent scores**:

| Field | Question it answers |
|---|---|
| `vividness` | Could a world generator *rebuild this situation* from the description? |
| `attestation` | Did this actually happen, to this person, as described? |

These do not correlate. A Reddit thread is routinely **high vividness, low attestation** —
which makes it excellent raw material and useless as evidence of frequency.

### `attestation` values

- `firsthand_named` — a named author or company describing an incident they were in.
- `firsthand_anonymous` — a practitioner describing their own incident without verifiable
  identity. **The typical Reddit / Stack Overflow / HN post.** Fully legitimate; just
  bounded.
- `secondhand` — reporting someone else's incident (journalism, a survey that names its
  underlying cases).
- `vendor_marketing` — a vendor describing a problem their product solves.
- `unattested` — nobody is claiming it happened; an essay, a tutorial, a hypothetical.

### `usable_for` — what the source is licensed to support

- `world_texture` — error strings, tool versions, table names, org dysfunction, dialogue,
  the shape of a bad on-call night. **Almost any source can license this.**
- `mechanism` — "this failure works like *this*." Needs enough technical detail to be
  falsifiable.
- `frequency` — "this happens often." **Hardest to earn.** Requires
  `attestation ∈ {firsthand_named, secondhand}` AND `source_type ∉ {forum, vendor_page}`.

**Gate G6 enforces this and will reject your record.** An anonymous forum post claiming
`frequency` is an automatic failure. This is precisely the error that produced the 11
damaged archetypes already in the corpus.

> Vendor marketing may license `world_texture` and nothing else. Failure F400: claim E8's
> three "independent" corroborating sources collapsed to a single seller-side anchor with
> an undisclosed conflict on the byline.

### What good and bad look like

Good — `vividness: high`, `attestation: firsthand_anonymous`, `usable_for: [world_texture, mechanism]`:

> "dbt run --full-refresh on our incremental model at 3am, 400M row table, merge strategy,
> and it silently truncated because the `unique_key` was a column that got renamed upstream
> two weeks ago. No test caught it because the schema test ran on the *source*, not the model."

Bad — rejected regardless of how long the quote is:

- "Data engineers spend a lot of time on data quality." (no incident)
- "Teams often struggle with pipeline reliability." (unfalsifiable, and `frequency` unearned)
- "Common tasks include ETL, modeling, and orchestration." (job-description boilerplate)

The test for `vividness: high`: **error strings, tool + version, table/column names,
timings, org context.** "A pipeline broke" is `low` no matter how many words surround it.

---

## 3. Where to look for vivid sources

Weight heavily toward where practitioners describe *failure*, in their own voice:

- `r/dataengineering`, `r/analytics`, `r/MachineLearning`, `r/ExperiencedDevs` — incident
  threads, "what's the worst bug you shipped", on-call horror stories
- Stack Overflow questions with **accepted answers and high view counts** — the question
  body is usually the incident; the answer is usually the mechanism
- dbt Slack / Discourse archives, Airflow and dbt GitHub **issues that are still open**
- Hacker News comment threads on data-infra posts (the comments beat the article)
- Named postmortems and incident reviews (these are where `firsthand_named` lives)
- Conference talks where someone walks through their own outage

An open GitHub issue cited as a live trap **must actually still be open** — check, and record
what you checked.

---

## 4. Record format

Write YAML into `implementation_structure/state/`. Schemas are machine-enforced in
`schemas/schemas.json`; read the one for your artifact type before writing. Validate with:

```
python implementation_structure/gates/run_gates.py --gate G0 --gate G6
```

IDs must not collide. **Claim the next free block and say which block you took in your
report.** Check what exists (`ls state/knowledge/`) before picking.

Sources go in `state/knowledge/S<NNN>.yaml`, archetypes in `state/archetypes/A<NNN>.yaml`,
tools in `state/inventory/T<NNN>.yaml`, failures in `state/failures/F<NNN>.yaml`.

---

## 5. Ground rules that constrain what you may author

These were each learned by having a red-team agent destroy an earlier design. Full list in
`agent.md` §9.1.

1. **The grader does not exist during the episode.** Any archetype whose grading reference
   sits in the agent's own catalog is invalid — an earlier design scored 11 of 14 points by
   `CREATE TABLE x AS SELECT * FROM expected.x` with zero diagnosis (F121). Say explicitly
   in `grading` that the reference is harness-side.
2. **The agent sees only `agent_visible_brief`.** Author-side prose enumerates the planted
   defects; one draft printed all four "hidden" defects in its `why_hard` (F202). Write
   `agent_visible_brief` as the ticket a colleague would actually file — it must not name
   the defect, the mechanism, the window, or the decoys.
3. **No network egress in the episode.** The agent must not be able to look the incident up.
4. **Deterministic grading for v1.** If your archetype can only be graded by an LLM reading
   prose, it is not ready. Name concrete `deterministic_checks`.
5. **Destructive operations are flagged, not blocked.** Whether the agent gates its own
   destructive actions is a measurement.
6. **Five surfaces for v1:** DuckDB, dbt Core, git workspace, a deterministic scheduler
   state machine, a BI-values + docs surface. You may *design* against more, but say so.
7. **A task with no failing baseline is not a task.** For every archetype, name the naive
   approaches that must partially succeed and the ones that must fail.

### The anti-patterns that collapse difficulty

Do not author these. They look hard and are not:

- **One defect, one surface, one query.** Difficulty must come from conjunction.
- **Difficulty by obscurity** — a flag nobody could know. That measures trivia.
- **Difficulty by tedium** — 200 identical edits. That measures patience.
- **A decoy that explains the whole symptom.** Decoys must be real defects that account for
  *part* of the discrepancy, so ruling them out requires arithmetic, not taste.
- **Grading on prose when state would do.**
- **Anything solvable by full-refresh.** If reloading everything fixes it, the task is dead.
  There must be something in the world that a full refresh destroys.

---

## 6. Honest reporting — how this round is judged

Report what you actually found, including what you failed to find. Specifically:

- If a source pool was thin, say so. Do not pad.
- If you could not verify a quote resolves, mark `confidence: low` and say why.
- Do not invent URLs, quote text, view counts, or issue numbers. A fabricated citation is
  the single worst outcome of this round — it is worse than returning three archetypes
  instead of five, because it poisons everything downstream and costs more to detect than
  it saved.
- If your assignment turns out to be the wrong shape for the evidence you found, say that
  in your report rather than forcing records that fit the brief and not the world.

End your report with: what you wrote (IDs), what you rejected and why, what you could not
verify, and the single biggest weakness in your own output.
