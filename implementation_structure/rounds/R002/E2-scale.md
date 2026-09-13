# R002 / E2 — Scale and needle-in-a-haystack failures

Agent: **E2**. Assignment: archetypes whose difficulty is **search under realistic clutter**, not
puzzle-solving. Blind to the other authors by construction; I read `A001.yaml` for format and
nothing else in `state/archetypes/`.

**ID blocks claimed: S650–S664 (sources), A050–A054 (archetypes).**

Gate status from repo root:

```
python implementation_structure/gates/run_gates.py --gate G0 --gate G6
[PASS] G0 schema  (475 checked, 0 failures, 5 warnings)
[PASS] G6 source quality (vividness / attestation split)  (259 checked, 0 failures, 7 warnings)
ALL GATES PASSED
```

Zero failures and zero warnings attributable to my files. The 5 G0 warnings are `composed_of` on
A060–A064 (another author's block); the 7 G6 warnings are pre-existing S008/S014/S015/S022/S122/
S208/S214. Note for the round lead: the brief said 162 pre-existing sources fail G6; by the time I
ran it, G6 reported 0 failures across 259 records, so somebody repaired them earlier in the round.

---

## 1. Source harvest — what I actually found, and what I could not

**Reddit was unavailable to me.** `reddit.com` and `old.reddit.com` return a block page to both
`curl` and the JSON endpoints from this environment. I did not fake around it. Every forum source
below is from **dbt Discourse**, fetched through its public JSON API, which returns post bodies and
view counts verbatim. That is a real gap in the harvest: `r/dataengineering` is where the brief
says the vivid incident prose lives, and I have none of it.

What I used instead, and why it is defensible for *this* assignment specifically: scale failures
leave artifacts. A 1,599-model project leaves a `Found 1599 models, 694 snapshots, 5000 data tests`
log line in a bug report. A nested-loop plan leaves `BLOCKWISE_NL_JOIN` in an issue body. A
1,507-model repo leaves a measurable prefix distribution. So I weighted toward **currently-open
GitHub issues** and **one measured public repo**, which are verifiable and re-checkable, rather
than toward recollected prose.

**Every GitHub issue cited as a live trap was checked open on 2026-09-13 via the REST API**, and
each record states what I checked. One thing worth flagging repo-wide: `dbt-labs/dbt-core` now
**redirects to `dbt-labs/dbt`** — GitHub's search API 422s on the old name. Any older S-record in
this corpus citing `dbt-labs/dbt-core` in a search context will need updating.

| ID | What it is | vividness / attestation | usable_for |
|---|---|---|---|
| S650 | dbt issue 11421 — 1599 models, 694 snapshots, 5000 tests, 1457 sources; ~3 min parse | high / firsthand_anonymous | world_texture, mechanism |
| S651 | dbt issue 9524 — `dbt seed` at 2Gb RSS for 125Mb of CSV; breaks k8s task limits | high / firsthand_anonymous | world_texture, mechanism |
| S652 | dbt issue 12975 — generic test with an invalid `ref()` is **silently dropped at parse time** | medium / firsthand_anonymous | world_texture, mechanism |
| S653 | dbt issue 2793 — `ref()` inside `set_sql_header` resolves to the current model; open since 2020 | high / firsthand_anonymous | world_texture, mechanism |
| S654 | dbt issue 15231 — `state:modified+` over-selects on ~409 models with 0 checksum diffs | high / firsthand_anonymous | world_texture, mechanism |
| S655 | dbt issue 15396 — `get_column_lineage` returns `[]`, `isError: false`, on confirmed lineage | medium / firsthand_anonymous | world_texture, mechanism |
| S656 | dbt issue 15213 — layer filter hardcodes `stg_`/`int_`/`fct_`; non-conforming models vanish | high / firsthand_anonymous | world_texture, mechanism |
| S657 | dbt docs — CLL "doesn't reflect other usage like joins and filters" | medium / unattested | world_texture, mechanism |
| S658 | dbt docs — hardcoded table names instead of `ref` produce unknown lineage | medium / unattested | world_texture, mechanism |
| S659 | duckdb issue 20756 — ~300k tables → 20+ GB on connect; `LoadFromStorage` eager catalog load | high / firsthand_anonymous | world_texture, mechanism |
| S660 | duckdb issue 17392 — `ON ... OR ...` → `BLOCKWISE_NL_JOIN`, 1m42s vs 144ms rewritten | high / firsthand_anonymous | world_texture, mechanism |
| S661 | Discourse 647 — duplicate model-name compile error + `source__object` convention | high / firsthand_named | world_texture, mechanism |
| S662 | Discourse 6835 — multi-tenant dbt project maintained by **Python code generation** of models + YAML | high / firsthand_anonymous | world_texture, mechanism |
| S663 | Discourse 12925 — org changed column naming convention, no way to rename across a project | medium / firsthand_anonymous | world_texture |
| S664 | `TEAMSchools/teamster` — **measured**: 1990 `.sql`, 1518 `.yml`, 10 dbt projects; prefixes `stg_` 787, `int_` 330, `rpt_` 265, `dim_` 52, `fct_` 28, `base_` 6 | medium / unattested | world_texture |

**No record in this block claims `usable_for: [frequency]`, and that is deliberate.** Thirteen of
fifteen are anonymous forum/repo sources or vendor documentation, which G6 forbids from licensing
frequency. S661 is `firsthand_named` (Claire Carroll, dbt Labs) but `source_type: forum`, which G6
forbids regardless of attestation — I could have argued around that and did not. **I have no
evidence that scale-driven failures are common.** I have evidence that they are real, mechanically
specific, and currently unfixed. That is what the archetypes rest on.

Two records are marked `vividness: medium` (S652, S655) purely because the *quoted span* is a bare
behaviour statement even though the issue *body* is fully rebuildable. I chose the accurate score
over the flattering one and explained the gap in `notes`.

**S664 is the record I am most pleased with and it is the least conventional.** It is not an
incident; it is a measurement. I pulled the recursive git tree for a live production dbt monorepo
and counted. The result is the thing the brief warns is usually hand-waved: what a real large dbt
project's naming conventions actually look like. `rpt_` outnumbers `fct_` and `dim_` combined by
more than three to one, `base_` coexists with `stg_`, and a handful of files carry no recognised
prefix at all — none of which matches the canonical staging/intermediate/marts model that S656 says
the tooling hardcodes. That is "conventions inconsistently applied anyway," measured rather than
assumed, and it is what calibrates the convention noise in my generator.

---

## 2. Archetypes

| ID | Title | Discipline | Hours | Pts |
|---|---|---|---|---|
| A050 | The assertion that never ran, inside a suite of four thousand green tests | data_engineering | 6–16 | 22 |
| A051 | A predicate the lineage tool cannot see, on a column that appears in forty models | data_engineering | 8–20 | 20 |
| A052 | A dependency that is in the warehouse and not in the graph | data_engineering | 10–24 | 24 |
| A053 | Three written definitions of one metric, five hundred models, and a frozen number that settles it | data_analytics | 10–24 | 22 |
| A054 | A reconciliation whose obvious query is correct and never finishes | data_engineering | 6–14 | 18 |

All five state harness-side grading explicitly (F121), carry an `agent_visible_brief` that names the
symptom and the deliverable but not the defect, the mechanism, the window or the decoys (F202), and
name something a full refresh destroys.

### The distinguishing test, applied honestly

The brief's test is: *does a better search strategy find it substantially faster than a worse one?*
Here is the strategy gap for each, stated so it can be argued with:

- **A050** — Winner: set-difference `dbt ls --resource-type test` against a parse of the schema YAML.
  Seconds. Loser: read 4,100 tests, or reread the green log. The gap is enormous because nothing in
  the product surfaces the difference between "declared" and "in the manifest."
- **A051** — Winner: classify the 40 usages by *position* (projection vs predicate vs join key) over
  compiled SQL — 40 candidates to 5 in one pass — then per-hop `count(*)` vs
  `count(*) filter (where status is null)` to binary-search 8 hops. Loser: open 40 models, or trust
  column-level lineage, which by documented design cannot see the defect.
- **A052** — Winner: subtract `manifest.json` `depends_on` from the relation set actually referenced
  in `target/compiled`. One pass over artifacts; every out-of-graph read falls out at once. Loser:
  traverse the DAG, `grep ref(`. Both fail by construction, and fail *confidently*.
- **A053** — Winner: compute all three candidate definitions against the frozen published extract,
  then group 60 models by the hash of their normalised window expression — 60 collapses to 3 groups,
  outliers exposed. Loser: read the wiki; read 60 descriptions (all identical, which is the trap).
- **A054** — Winner: `EXPLAIN`, see `BLOCKWISE_NL_JOIN`, rewrite. Loser: raise `memory_limit`
  (capped), add threads (useless on a nested loop), sample (rejected by exactness), or wait.

### What a full refresh destroys, per archetype

- A050 — `snp_dim_source_system`; a snapshot full-refresh erases the `valid_from` that dates the rename.
- A051 — pre-cutover rows survive only in an unloaded `raw_` landing table; a rebuild pulls them from
  the post-cutover source and the NULLs vanish with the evidence.
- A052 — the frozen legacy relation is produced by no model; dropping it turns a quiet wrong number
  into a loud error and makes the historical damage unquantifiable.
- A053 — `snp_metric_definitions` is the only record of whether the doc or the SQL changed first,
  which the grader asks for by name.
- A054 — `crosswalk_override` is hand-curated, produced by no model, regenerable from nothing.

---

## 3. How the world generator produces the clutter deterministically

This is the part the assignment said is most likely to be hand-waved, so it is stated once here and
repeated per-archetype in each `notes` field.

**The mechanism.** `world/gen_dbt.py --seed N` emits the project from a declarative fixture spec
(`world/spec/org.yaml`). Determinism: a single `random.Random(seed)` drawn in a fixed order, sorted
iteration over every collection, no wall-clock and no uuid input. Two runs at the same seed are
byte-identical.

**The shape, and why it is not invented.** 12 source systems × 24–32 tables → ~340 `stg_` models
(one per source table); ~140 `int_` models; ~90 marts; ≈570 models and ~4,100 generic tests. Those
ratios are a **scaled copy of S664's measurement** of a live 1,507-model project (787 `stg_` : 330
`int_` : 265 `rpt_` : 52 `dim_` : 28 `fct_`), and the test count brackets the 5,000 measured in
S650. I did not pick round numbers; I scaled down numbers I counted.

**Why generated bulk is realistic rather than filler.** Two separate arguments, and the second is
the important one.

1. Every generated model does real work. Each `stg_` model is a real projection with real renames
   from a per-source column dictionary. Each `int_` model performs one of six real patterns
   (`row_number()` dedupe, pivot to a new grain, date-spine join, bridge fan-out, SCD2 flatten,
   tenant union). Each mart aggregates a real grain. A model opened at random reads like a model.
2. **Real projects of this size are themselves machine-generated.** S662 is a practitioner
   describing exactly this: metadata-driven Python code generation emitting models, seeds and their
   YAML into the right directories, because hand-maintaining a multi-tenant project at that scale is
   not viable. So generation is not a shortcut around realism — at 500 models it *is* the realistic
   provenance. A053 leans on this directly: its defect is two business units pinned to an older
   template revision, which is a failure mode that only exists because the project was generated.

**Convention noise is seeded, not uniform.** Three of the twelve sources are marked `acquired` in
the spec and emit `rpt_`/`base_` prefixes under `models/gold/` instead of the canonical layout. That
reproduces both S664's measured distribution and S656's documented consequence (non-conforming
models fall out of layer filters entirely). This is what stops grep-by-convention being a one-shot
win while keeping the answer findable — the difference between clutter and obfuscation.

**Bulk data never ships as dbt seeds.** Volume is built in-engine with
`INSERT ... SELECT ... FROM range(...)` plus seeded hash expressions. This is a direct consequence of
S651: `dbt seed` measured at up to 2Gb RSS for 125Mb of CSV would consume the entire episode ceiling
before the task began.

### Resource ceiling — where this fits and where it does not

- **Catalog load is not the risk.** S659 measures DuckDB at 20+ GB opening ~300k tables, linear in
  table count. At ~570 tables we are at ~0.2% of that. The DuckDB file ships prebuilt in the image
  and is *attached*, not ingested, so boot is an attach.
- **dbt parse is the risk, and I am not going to pretend otherwise.** S650 measures ~3 minutes of
  cold parse at 1,599 models + 5,000 tests. Linear extrapolation to ~570 models gives ~40–70s, which
  is at or over the "boots in under a minute" budget. Mitigation: the image ships a prebuilt
  `target/partial_parse.msgpack` so episode start is a *warm* partial parse of a few seconds, and the
  image build — not the episode — pays the cold-parse cost.
- **The caveat that mitigation does not remove.** Editing a test or a model invalidates partial
  parsing. A050 (edits tests) and A052 (edits models) therefore each pay a full reparse per edit
  cycle, and the wall clock must allow at least eight of them. **My extrapolation from S650 is
  linear and dbt parse is probably superlinear in test count, so the true number is likely worse
  than I have estimated.** If piloting shows it does not fit, A050 should be cut to ~300 models
  rather than shipped against a budget it does not meet. I have not built or measured any of this.
- **A054's sizing is the least defensible number I have written.** 38M narrow rows ≈ 1.2GB on disk,
  streaming, with a ~400k-row build side — that should fit a hash-join plan under 2GB, and S660's
  reporter observed "full throttle on the CPU, low memory usage" on the nested-loop plan, so the
  failure mode is the wall clock rather than an OOM kill. But S660's 1m42s was on ~20MB of parquet.
  The row count must be tuned empirically so the correct plan finishes comfortably and the nested
  loop does not finish at all. If the correct plan also strains the ceiling, **reduce the fact table,
  do not raise the memory limit** — raising it deletes the task.

### Surfaces

All five sit inside the five v1 surfaces (DuckDB, dbt Core, git workspace, deterministic scheduler
state machine, BI-values + docs). A052 uses the scheduler as part of the defect rather than as
scenery: a task recreates the frozen relation on its next tick, so a repair that is correct at the
SQL layer and blind to orchestration passes a one-shot check and fails the tick probe. A053 uses the
docs/wiki surface and a frozen BI extract as the tiebreaker.

---

## 4. What I rejected, and why

- **A "500 identical tenant models, find the one that differs" archetype.** This was my first idea
  and it is tedium wearing a scale costume. Every search strategy is the same strategy (diff them),
  so a better strategy is not faster — it is the only strategy. Cut. The surviving trace of the idea
  is A053, where the outliers are found by *hashing normalised expressions* rather than by reading,
  and where the harder half of the task is deciding which of three definitions is authoritative.
- **A "200 models need the same edit" archetype.** Explicitly named as an anti-pattern in the brief.
  Cut without argument.
- **Medium articles on join-grain disasters** surfaced by web search
  ("SQL That Runs but Is Wrong: The Duplicate Join Disaster", "5 Data Pipeline Mistakes That Cost Me
  Weeks of Debugging", "What I Learned From Reading 50 Data Pipeline Postmortems"). I did not cite
  any of them. They are content-farm-shaped, the incidents are not attributable, and at least one
  claims to synthesise 50 postmortems it does not individually cite — which is exactly the collapse
  pattern F400 describes. A fabricated-feeling citation that passes a quote match is the failure this
  round exists to prevent.
- **An HN Show HN comment describing a "$50k Snowflake bill" from repeated full refreshes** (HN
  45931803). Vivid, and it is a founder pitching a product in the same comment, which makes it
  `vendor_marketing` and therefore `world_texture` only. It was not load-bearing for anything, so
  rather than carry a record whose only job is atmosphere, I dropped it.
- **A sixth archetype on `state:modified` over-selection (S654).** Real, open, and genuinely a
  scale problem — but as a task it reduces to "the selector lied, diff the manifests," which is one
  defect on one surface. I folded S654 in as supporting evidence for the manifest-diffing strategy in
  A050 and A052 instead of forcing it into an archetype it cannot carry.

## 5. What I could not verify

- **Reddit, at all.** Blocked from this environment. The `r/dataengineering` incident prose the brief
  points at is absent from my harvest and I did not substitute anything for it.
- **Whether any of this is common.** No `frequency` license anywhere in S650–S664, for the reasons in
  §1. If the round needs a frequency claim about scale failures, it does not come from me.
- **S664's quote span against rendered HTML.** The claim's *numbers* are verified (I measured them and
  recorded the exact API call), but the quoted README sentence spans a line wrap in the raw file, so a
  naive G1 substring match against rendered HTML could behave differently than against the raw file. I
  chose a single-source-line span to minimise this and flagged it. Same class of risk noted in S658,
  where I deliberately truncated the quote before the words "ref statements" because the page wraps
  `ref` in a `<code>` element that would break a naive match.
- **Every performance and boot-time number in §3.** Extrapolated from S650/S651/S659/S660, not
  measured. Nothing here has been built.
- **dbt Slack archives.** Referenced from S652 as an internal `dbt-labs.slack.com` link; not publicly
  fetchable, not cited.

## 6. The single biggest weakness in my own output

**A051 is closest to the tedium line and I am not confident it is on the right side of it.**

Forty models is a haystack a patient agent can simply read. The archetype survives only on the claim
that classifying usages by *position* cuts 40 to 5 in one pass, and that per-hop count reconciliation
localises the drop in a handful of queries. Both are real. But "40 candidates" is small enough that a
frontier model with a long context may just read all forty and win — in which case the task measured
patience and context length, not strategy, and I will have shipped exactly the thing the assignment
warned me about. The right fix if piloting confirms it is to **raise the spread to ~120 models and add
a second conformed column sharing the name in a different source** — enlarging the haystack, not
making the needle subtler. I have written that remediation into A051's `notes` so it is not lost.

A second, structural weakness: **four of my five archetypes are single-defect-plus-decoys rather than
genuine multi-defect conjunctions.** A052 is the only one with real conjunction across surfaces (SQL
layer + orchestration layer, where fixing one without the other fails the tick probe). A050, A051 and
A053 rely on decoys for their conjunction, and decoys are a weaker form of it — an agent that gets
lucky on the primary defect can ignore the decoys and still pass most checks. This is a consequence
of my assignment: **scale pushes toward one needle in a large haystack, which is structurally the
opposite of "difficulty from conjunction."** If the round wants both properties, scale archetypes
probably need to be *composed* with a second archetype's defect rather than deepened on their own.
I would rather name that tension than paper over it.

Third: **A053 carries a `communication` judge axis**, and the memo it grades is not deterministically
checkable. Everything that decides pass/fail is deterministic and I kept the rubric weight on that
half, but by the strict reading of ground rule 4 the memo portion is not v1-ready.
