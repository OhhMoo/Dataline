# R002 · D1 — red team against the eight never-attacked archetypes

**Agent:** D1 (red team) · **Round:** R002 · **Date:** 2026-09-13
**Targets:** A002, A003, A005, A010, A017, A018, A022, A025
**ID block claimed:** F500–F549. Used F500–F514 (15 records). F515–F549 free.
**Gates:** `python implementation_structure/gates/run_gates.py --gate G0 --gate G4` → **ALL GATES PASSED**
(G0 395 checked / 0 failures / 3 warnings — the warnings are pre-existing `composed_of` fields on
A060–A062, not mine. G4 74 checked / 0 failures.)

---

## Headline

**Eight attacked, eight damaged, zero promotable.** Five require redesign, three are repairable
with concrete named repairs. Nothing goes to `state/verified/` this round, and one finding
(F513) means nothing in the *corpus* can go there yet either.

The single most important result is not any individual kill. It is **F513**: not one archetype
in the entire corpus — all 26 active, not just my eight — has an `agent_visible_brief`. The
schema says the field is mandatory before shipping; §9.2.3 says `instruction.md` is generated
*only* from it. With the field empty there is no legal input to that generator, so the only
prose that exists to become the agent's ticket is the prose that enumerates the planted defects
in order. That is F202 again, corpus-wide, filed before any of these eight were written.

The uncomfortable corollary applies to my own work: **no attack in this round, mine included,
has tested the task the agent actually sees**, because that text does not exist. Treat every
record here as provisional against the brief, and solid against the design.

---

## Kills by archetype

| ID | Records | Verdict | One-line reason |
|---|---|---|---|
| A002 | F500, F501, F513, F514 | **redesign_required** | Mechanism impossible on the v1 surface; no-op passes 2/4 checks, full refresh passes 3/4 |
| A003 | F502, F503, F513, F514 | **repairable** | Design survives both baselines; the *evidence* is two unattested essays |
| A005 | F503, F504, F513, F514 | **repairable** | Kitchen-sink attribution scores full credit; two of four "deterministic" checks require retraining a model |
| A010 | F500, F505, F513, F514 | **redesign_required** (recommend withdraw) | A002 + A017 stapled together, same sources, two free static-config checks |
| A017 | F506, F513 | **redesign_required** | Full re-sync is the oracle — and it is the remedy the source's own reporter proposed |
| A018 | F507, F508, F513 | **redesign_required** | Load-bearing surface (Airflow) does not exist in v1; ~25% of the rubric is a grep-and-edit |
| A022 | F509, F510, F513 | **repairable** | Hidden answer key is `WHERE value < eps`; description encodes an arithmetic misread of its own source |
| A025 | F500, F511, F512, F513 | **redesign_required** | Grading reference is a function of the agent-visible landing zone — the full rebuild *is* the reference |

`status` fields updated in place: `redesign_required` for the five, `draft` + a `repair_note`
for the three repairable ones. I did not set `derived` on anything — the schema rule requires an
attack record from each of R1, R2 and R3, and I am one agent.

---

## The four attacks that landed hardest

### 1. The mechanism three archetypes share cannot exist on the v1 surface (F500)

A002, A010 and A025 all plant positional-INSERT column-shift corruption in a dbt incremental
model. I verified against **the dbt actually installed in this repo**
(`gateway/.venv/Lib/site-packages/dbt`):

- `include/global_project/macros/materializations/models/incremental/strategies.sql` →
  `get_insert_into_sql` builds `dest_cols_csv` from `dest_columns | map(attribute='name')` and
  emits `insert into <target> (<csv>) (select <csv> from <tmp>)` — an explicit, **name**-derived
  column list on both sides.
- `include/duckdb/macros/materializations/incremental_strategy/delete_insert.sql` →
  `duckdb__get_delete_insert_merge_sql` does exactly the same.

There is no `SELECT *` path and no positional matching anywhere in dbt Core or dbt-duckdb. The
bug is a dbt-databricks Delta/`replace_where` defect (S013/S014 — which F454 already showed are
closed and fixed in 1.11.7). Ground rule 6 freezes v1 at DuckDB + dbt Core. So planting this
defect requires a hand-written macro override in `/workspace/dbt_project/macros/`, which is
agent-visible — the defect announces itself to `grep -rn "select \*" macros/`. One query, one
surface, and the override is an artifact no real adapter contains.

I also checked the opposite direction and report it as a *negative* result, because it matters
for the redesign: the **loud** sibling leg (S015, dbt-adapters#601 — verified still OPEN via the
GitHub API) *does* reproduce on DuckDB. With `on_schema_change: ignore`, `incremental.sql` falls
through to `dest_columns = adapter.get_columns_in_relation(existing_relation)`, so a removed
column is still named in the INSERT and the warehouse raises a binder error. Only the error text
differs from BigQuery's. That leg is sound and should survive any redesign.

### 2. A017's oracle is the full refresh, in the source's own words (F506)

A017's world is a mock source API + a connector that lost rows + downstream dbt models. Its
stated repair is "re-sync into a side table, validate counts, swap it in" — a full refresh with
a rename. The source of record is intact by construction, so every byte is recoverable by
re-reading it. Verified via the GitHub API 2026-09-13, `airbytehq/airbyte#77627` (**state OPEN**,
6 comments, filed by `larsen` 2026-04-30) says in the reporter's own words: *"I tried re-syncing
from scratch using a separate connection ... the sync is able to fetch all the records I expect"*
and *"I think I can just refresh this individual stream."*

A017 does name a trap — truncate-and-reload breaking downstream incremental state — but **nothing
in its grading detects it**, because check 4 compares downstream models against a *reference
rebuild*, which is precisely what a truncate-and-reload produces. All four deterministic checks
fall to one command. The brief's own line: *"Anything solvable by full-refresh… the task is
dead."*

### 3. A025's grading reference is computable from data the agent can read (F511)

A025 grades against *"an `expected/` reference table **built from the raw landing zone**"* and
tells the agent to *"repair the affected rows **from the raw landing table**."* Per §9.2.1 the
landing zone is `/workspace/data/landing/...` — inside the episode, readable with `fs_*`. So
`reference = f(raw_landing)`, the agent holds `raw_landing`, and `f` is the model's own SELECT,
also agent-visible. A full rebuild reproduces the reference exactly and passes all four checks.

A025 lists "drops and rebuilds the full table, destroying history the task says to preserve" as a
failure mode — but never names any history that is not derivable from the landing zone. There is
nothing for the rebuild to destroy. This is F121 generalized: **a reference does not have to be
materialised to be copyable, only computable.**

### 4. A022's hidden answer key is a threshold query (F509)

A022's headline axis is a per-date broken/valid verdict matched against hidden ground truth. But
A022 *defines* poisoned days as the days the endpoint returned near-zero and the aggregation
coerced to 0.0. So the label is a deterministic function of the value:
`SELECT dt FROM kpi_daily WHERE value < eps` reproduces the hidden key exactly, from one table,
on one surface. No legitimate near-zero days and no non-zero poisoned days are named, so there is
nothing for the threshold to get wrong. Once check 1 falls, check 3 is mechanical on the same
date set and check 2 is the arithmetic over the complement — three of four checks from one WHERE
clause. Same shape as F128's 0.0%-KPI leg, with the threshold now doubling as the answer key.

Its check 4, "docs metric-definition content diff vs expected / stale text absent", is satisfied
by **deleting the doc**, and the presence half is either an unguessable exact string or a judge.
Either ungradeable or free; there is no third option.

---

## Attack-by-attack coverage

| Attack line | Where it landed |
|---|---|
| **Copy (F121)** | A025 fatally (F511 — reference computable from agent-visible landing zone). A002/A003/A005/A010 say "the reference" with no address (F514); A017/A018/A022 correctly say "hidden" |
| **Leak (F202)** | All eight (F513). A002's `why_hard` even hands over the *detection method*: "a column that suddenly carries values from a different domain" |
| **Cheap-win** | A002 (no-op 2/4), A005 (name all three mechanisms, no stated penalty — F128 recurrence), A010 (two static-config checks), A018 (hygiene axis ≈25% by grep-and-edit), A022 (threshold query) |
| **Full-refresh** | A002 (nothing destroyed), A017 (refresh *is* the oracle), A025 (rebuild *is* the reference). **A003 resists it** — removing the rule re-admits the planted corrupt rows and fails two checks |
| **Determinism** | A005 (retrained AUC + float prediction diff are not reproducible across runs), A003/A005 (undefined tolerances — F503), A002 (grading executes agent-authored SQL against a retained snapshot) |
| **Single-surface** | A017 (source `count(*)` vs destination `count(*)`), A022 (one WHERE clause), A002/A010/A025 (one `grep` over the fabricated macro) |
| **Obscurity / tedium** | A018 on both counts (F507/F508): `datetime.now()`-in-a-DAG is Airflow trivia; the "dozens of entities" walk is an argmax over run-history timestamps, which S011 shows Airbnb *automated* |
| **Grounding** | A003 (F502), A022 (F510), A025 (F512). Sources fetched and checked individually — see below |

---

## Grounding work actually performed

Everything below was fetched 2026-09-13; I did not rule on a source I did not load.

| Source | Check | Result |
|---|---|---|
| S022 · airbyte#77627 | GitHub API | **OPEN**, 6 comments, `larsen`, 2026-04-30. Genuine, rebuildable, and it contains the sentence that kills A017 |
| S015 · dbt-adapters#601 | GitHub API | **OPEN** since 2022-09-12, 9 comments. Genuine live trap; reproduces on DuckDB (traced through the installed macros) |
| S013 / S014 | Deferred to F454 (verified closed/fixed there); I verified the *consequence* — the mechanism has no v1 surface |
| S008, S009 | HTTP | 200, airflowsummit.org session pages. Sources fine; the world they describe is not the world v1 builds |
| S207 / S208 · thicket.sh | HTTP | 200. F458's arithmetic misread confirmed as load-bearing for A022 (F510) |
| S115 / S116 · Google Rules of ML | HTTP | 200 |
| S004 / S005 / S006 | Filesystem + F450 | **Still active, never downgraded.** S005 is `confidence: medium` and still the sole source of A003's core mechanism; S001 is still `confidence: high` despite F451 saying to set it to medium |
| S010 | Filesystem | **Now in `knowledge/withdrawn/`** — and A022 still cites it in `derived_from` with every gate green |

Two corpus-hygiene findings fell out of this and are recorded in F502/F510 notes:

1. **The brief told me S001, S005 and S006 were withdrawn. They are not.** F450/F451
   recommended `downgrade_confidence`; the recommendation was never actioned and nothing built
   on them was revisited. An attack that lands and is then not applied leaves the corpus in
   exactly the state the attack described.
2. **No gate checks `derived_from` against the withdrawn set.** A022 cites S010, which is
   withdrawn, and G0/G3/G4 are all green. That is a gate worth adding.

---

## Promotion recommendation

**Promote nothing.** F513 alone is disqualifying corpus-wide: an archetype with no
`agent_visible_brief` has never had the thing the agent sees attacked, so "survived red team"
cannot be asserted about any of them.

**The closest candidate is A003**, and I want to be explicit about why, because it is the only
one of the eight whose *design* I could not break:

- A no-op scores zero — all four checks are equality-against-reference.
- A full refresh does **not** fix it: removing the over-aggressive rule restores the dropped
  legitimate rows but re-admits the planted corrupt ones, failing "dropped-row counts equal
  reference" and "repaired rule still rejects planted corrupt fixture rows". That is a genuine
  something-a-full-refresh-destroys property, which is exactly what A002, A017 and A025 lack.
- The remediation inversion (every filter the agent adds removes more valid data) is a real
  trap, not a decorative one.
- Two defects requiring different evidence — per-source surviving-row analysis vs an hour-of-day
  histogram — so the difficulty is conjunctive.

What stops it is provenance, not design. Its core mechanism and loss magnitude trace only to
S005, an uncited anecdote inside the unauditable dev.to synthesis F450 demolished; its timezone
leg traces to S002, which F451 showed is a pattern sketch rather than an incident. **Four bounded
repairs — re-source (F502), exact per-hour row counts instead of a tolerance (F503), write the
brief (F513), state the reference is harness-side (F514) — and A003 is a legitimate candidate to
be the corpus's first promotion.** I would prioritise it over anything else in this set.

---

## What I wanted to attack and could not

Ranked by how much the answer would change the verdicts.

1. **Run the adversarial baselines.** Every cheap-win number here (no-op 2/4 on A002, full refresh
   3/4, restart-until-green 3/4 on A018's started-late variant, threshold-query verdict on A022)
   is *derived from the archetype text*, not measured. GT001 has a baseline harness and a validity
   gate; these eight have no world, so nothing can be executed against them. Several of my
   estimates would move by a point or two either way. None would move enough to change a verdict —
   but I cannot prove that, and I would not want anyone to cite my fractions as measurements.
2. **Test `/grading` unreachability in a live container.** §10.3 flags this as unproven, and F511
   is the case that makes it urgent: reachability is not only about mounts. A025's reference is
   unreachable *and* fully computable from the episode zone. A build gate that checks paths would
   pass A025 and miss the hole entirely. The gate needs to ask "is the reference a function of
   agent-readable inputs", which no path check can answer.
3. **Attack the actual agent-visible brief.** Blocked by F513 — the text does not exist. This is
   the single biggest gap in my coverage and it applies to every record here.
4. **Confirm A018's scheduler port is feasible.** I claim the ran-long/started-late discrimination
   survives a move to `/var/sched/state.json` because run history lives there. I read the §10.1
   description of `world/world/scheduler.py`; I did not read the state machine's actual record
   shape to confirm it carries both start time and duration per run. If it carries only completion
   timestamps, A018's one salvageable idea does not port and the verdict should harden from
   "redesign" to "withdraw".
5. **Measure whether A005's three skew mechanisms are separable at all.** They are described as
   "independently sufficient". If any one of them alone reproduces the full offline/online gap,
   per-mechanism *contribution* quantification — which is my proposed repair for the kitchen-sink
   hole — may be ill-posed rather than merely hard. That needs a world to answer.
6. **Fetch S508, S511, S512** (the vendor connector docs under A025). I ruled on their
   *evidentiary class* — vendor pages license `world_texture`, not `mechanism`, which is a
   two-axis-model judgement I can make from the record — but I did not load the pages. If someone
   wants F512's vendor-docs leg to stand on a fetch rather than on classification, that is the
   remaining work on it. The other two legs of F512 (S004 via F450, S013/S014 via F454) are
   independently sufficient.

---

## Biggest weakness in my own output

**Zero survivors out of eight looks like an agent that kills everything, and I should be held to
that suspicion.** My defence is specific rather than rhetorical: the three verdicts I could have
inflated, I did not. A003 is called repairable and named the best promotion candidate in the
corpus, with the reason it beats its siblings spelled out. A005 and A022 are called repairable
with bounded repairs rather than redesigned away. A017's source (S022) is called clean and
unusually good — the defect is the archetype's, not the evidence's. S015 is called sound and
verified live, against my own interest in F500, because it is the one leg of A002 that works.

The defect I cannot argue away is the one in F513: I attacked eight `description` / `why_hard`
blocks, and the agent will never read any of them. Until `agent_visible_brief` exists, my
leak-attack coverage is zero by construction and my cheap-win estimates assume the agent knows
what the author-side text says — which is either too generous (if the brief is written well) or
exactly right (if the brief is generated from the description, which is what happens today).
