# F2 — Environment spec: what a "world" is, exactly

Author: F2 (Phase 2). Inputs: `state/problem.yaml`, `agent.md` §3 (lines 146–268) and
§6.1–6.2, L1–L6 summaries, S300–S318, targeted claims S002/S004/S008 (DE faults),
S111/S120/S124 (DS leakage), S200/S202 (metric drift), S102/S103 (synthetic/anti-memorization).
Research artifact only — no generator design, no environment code (per `problem.yaml` constraints
and anti-goals).

A **world** is the containerized runtime the agent works inside (agent.md §3). This spec fixes:
(1) its surfaces, (2) how a world starts broken or evolves, (3) its determinism contract,
(4) the replication-strategy vocabulary handed to Phase 4 builders.

## 1. World object spec — surfaces

Surfaces are keyed to the T1xx inventory (`state/inventory/T100–T129.yaml`; ranking + evidence
in L6-summary). "REQUIRED for v1" = the world cannot run a task without it; everything else is
OPTIONAL and enters only when a task archetype demands it (scope task-driven, never
coverage-driven — agent.md §6.1).

| # | Surface | Inventory app(s) | v1 status | State that must be deterministic across resets |
|---|---------|------------------|-----------|------------------------------------------------|
| 1 | **Warehouse (execution substrate)** | T107 Snowflake (emulated) over DuckDB; T108 BigQuery OPTIONAL alt persona | REQUIRED | Hermetic warehouse file: identical bytes after reset (DuckDB = file, reset = file copy, §6.1). Full catalog: schemas, tables, row content, views, plus emulated control-plane metadata (grants/RBAC, stages, time-travel/clones, query history). S303 (dual-backend precedent), S316/S317 (emulation options), S318 (transpilation). |
| 2 | **Transformation** | T105 dbt Core | REQUIRED | dbt project directory (models, tests, seeds, snapshots, `target/` wiped or regenerated identically), manifest/lineage graph, run results. dbt is the grading surface for state checks (dbt tests, S305 auto-generated equality tests). |
| 3 | **Orchestration** | T106 Airflow primary; T123 Dagster OPTIONAL alt | REQUIRED | DAG definitions, schedule expressions bound to the **virtual clock**, run/task-instance history, retry/SLA state. S008 catalogs the deterministic-hostile patterns (datetime.now() in DAGs, local-timezone schedules) the world must both exhibit as faults and control as infrastructure. |
| 4 | **BI / dashboard mock** | T113 Tableau / T114 Power BI / T115 Looker / T128 Sigma (mock one; all OPTIONAL persona variants) | REQUIRED (as mock) | Dashboard definitions, embedded metric formulas (the "duplicated semantic logic" that drifts — S200/S202), last-rendered values for deterministic dashboard value checks (agent.md §3 grading plane). |
| 5 | **Docs / wiki** | T110 Confluence-like wiki + scattered files on disk | REQUIRED | The full documentation corpus: file tree, wiki pages, per-doc staleness/contradiction metadata (see initial-condition class (c)). New surface added by §6.2; makes the world an information-retrieval problem. |
| 6 | **Ticketing / comms** | T102 Jira, T101 Slack | OPTIONAL | Ticket backlog (task briefs may arrive as tickets — data-eng-bench ships ticket-style instructions, S302), message history. Deterministic: fixed corpus per seed, append-only event feed driven by the event schedule. |
| 7 | **Version control** | T100 Git/GitHub (T111 GitHub Actions as CI flavor) | REQUIRED | Repo history: branch graph, commits, open PRs, CI check results. Git history is byte-reproducible given fixed author/committer timestamps (must be forced to the virtual epoch). |
| 8 | **Notebook** | T104 Jupyter; T124 Hex OPTIONAL | OPTIONAL | Notebook files incl. stale cell outputs — L2 names stale notebook state as a realistic failure family. |
| 9 | **Operational databases** | T103 PostgreSQL (also the Redshift persona, T119), T127 Oracle legacy, T126 ClickHouse | OPTIONAL | Source-system databases the pipeline ingests from: table content, CDC/WAL position. NOTE: ClickHouse is structurally hostile to bit-reproducible resets (async merges/mutations, §6.1) — excluded as *substrate*; usable only as a mocked source persona if ever demanded. |
| 10 | **Object storage** | T109 S3/MinIO | OPTIONAL | Bucket listing + object bytes; staged files for `COPY INTO` scenarios (§6.1 L4 control plane). |
| 11 | **Ingestion / streaming** | T117 Fivetran, T121 Airbyte, T116 Kafka | OPTIONAL | Connector configs, offset/cursor state, seeded message log. In v1 these are more likely fault *sources* (schema drift arrives "through" them, S004) than live surfaces. |
| 12 | **Catalog / quality / semantic layer** | T125 Unity Catalog/Atlan, T122 Great Expectations, T120 dbt Semantic Layer/LookML | OPTIONAL | Catalog metadata, expectation suites and their last-run results, metric definitions. Feeds lineage-based triage tasks (L1). |
| 13 | **Lakehouse / misc** | T112 Databricks, T118 Spark, T129 Hightouch/Census | OPTIONAL (out of v1) | Cheapest-to-emulate per §6.1 (open Delta spec) but not demanded by any v1 surface; defer. |

**Grading visibility note:** every REQUIRED surface doubles as a grading surface — deterministic
verifiers read warehouse state, dbt tests, output diffs, and dashboard values (agent.md §3;
S302/S305/S315 precedents). OPTIONAL surfaces exist only to host task content and need only
enough determinism for the checks their tasks declare.

## 2. Initial-condition classes

Four distinct generation mechanisms. (a) and (b) must stay separate in the generator — they
produce different task shapes (§6.2 explicitly).

### (a) Pre-existing brokenness in initial state
The world is born corrupted: broken code, half-finished pipelines, misconfigured applications
present at t=0. ade-bench's existing pattern (§6.2, S304/S305).
**Generator must parameterize:** which fault instances are present (drawn from the fault
library), fault location across surfaces (dbt model vs. DAG vs. warehouse data vs. docs),
brokenness severity, and *visibility* (loud error vs. silent wrong-state — L1's dominant
pattern is successful-looking pipelines emitting wrong state, e.g. S002 silent UTC→local
timestamp switch, S004 eleven days of silently dropped records).

### (b) Time-scheduled fault injection
Seeded incidents fire against a live world on the deterministic event schedule: schema drift,
freshness SLA breaches, bad-row batches (agent.md §3 fault injector). The genuinely novel
mechanism (§6.2).
**Generator must parameterize:** the event schedule (fault id, virtual-clock fire time,
target surface, payload), fault family distribution (grounded in L1: non-idempotent backfills,
schema drift — 38% of surveyed postmortems, S004 — CDC/WAL failures, SLA breaches; S008 for
scheduler-time faults), and interaction rules between concurrent faults.

### (c) Scattered / stale / contradictory documentation
Docs split across files and wiki, partially stale, partially contradictory — a legitimate
difficulty knob (§6.2) matching the metric-definition-drift evidence (S200 Airbnb, S202
LinkedIn: same question, different numbers from different documented definitions).
**Generator must parameterize:** doc count and placement (wiki vs. repo vs. tickets),
staleness ages per doc, the contradiction graph (which docs disagree about which fact and
which one is ground truth), and coverage (what fraction of the task-relevant knowledge exists
in docs at all vs. must be discovered from the systems).

### (d) Synthetic data from distributions
World data is generated, never sourced — contamination is a hard constraint (E4). Lawful
patterns: data-eng-bench's synthetic retail dataset (Apache-2.0) and ade-bench's dbt-project
patterns (§6.2). MLE-Bench supplies the cautionary scaffolding: reconstruction validation
(S102) and an explicit anti-memorization rule (S103) — synthetic data defeats both problems
by construction, but the generator must still prove non-leakage.
**Generator must parameterize:** schema/lineage synthesis knobs and org conventions (agent.md
§3 generator), per-column distributions and correlations, volume, time range (aligned to the
virtual clock), and injected data-quality artifacts (dupes, nulls, encoding drift) tied to the
faults selected in (a)/(b). DS/ML worlds additionally parameterize leak placement, since
leakage is pervasive in real work (S111: 8-mechanism taxonomy across 329 papers; S120:
leakage surfaces only after deployment; S124: leak dominated all legitimate modeling).

## 3. Determinism requirements (each checkable)

Grounded in agent.md §3 determinism layer, §6.1, and the Phase-0 bit-reproducible-reset gate.

- **D1 Seeded RNG.** All stochasticity — data synthesis, fault-library draws, doc placement,
  schedule jitter — derives from a single run seed recorded with the world. Check: two
  materializations from the same seed produce byte-identical initial state.
- **D2 Virtualized clock.** No surface reads wall-clock time; DAG schedules, freshness SLAs,
  ticket timestamps, git commit times, and doc "last edited" dates all read a virtual clock
  that supports fast-forward. Check: reset restores the same virtual epoch; `datetime.now()`
  inside the world returns the virtual time (the S008 failure mode becomes controllable).
- **D3 Deterministic event schedule.** Time-scheduled faults (class (b)) and any comms/ticket
  appends are an ordered, replayable event log keyed to virtual time. Check: replaying the log
  from the initial state reproduces the same world state at every checkpoint.
- **D4 Hermetic warehouse file.** The warehouse is a single file (or file set) inside the
  image; reset = file copy; no external service is ever contacted (E2). Check: hash of the
  warehouse file after reset equals the golden hash; run succeeds with networking disabled.
  S303 proves the pattern (data-eng-bench's hermetic DuckDB variant); S317 disqualifies
  universql (needs a live Snowflake account → violates D4/E2).
- **D5 No runtime cloud dependency.** Every surface is local: native OSS, emulated, or mocked.
  Check: the full world spins up and grades offline (one-command spin-up, agent.md §3 product
  plane).

## 4. Mock-ability constraints for Phase 4 builders

Replication strategy per app (fills the `replication_strategy: none_found` placeholders in
T100–T129). Five strategies, in descending order of preference where more than one applies:

1. **native_oss** — run the real open-source thing in the container (PostgreSQL T103, dbt
   T105, Airflow T106, Jupyter T104, MinIO T109, Dagster T123, Great Expectations T122,
   Git T100). Zero fidelity risk; preferred whenever the license permits (AGPL and
   source-available are rejections, not warnings — `problem.yaml`).
2. **oss_substitute** — a permissively-licensed stand-in presenting the same surface (DuckDB
   as the warehouse substrate behind a Snowflake persona, §6.1; S316 fakesnow; S318 sqlglot
   for build-time dialect translation only — never as verification, since successful parse ≠
   executable SQL).
3. **wire_protocol** — speak the vendor's client protocol/SQL API over a local listener
   (fakesnow's standalone HTTP server mode serves any language's connector incl. JDBC and the
   Snowflake CLI, S316; §6.1 emulation layer L2).
4. **http_intercept** — intercept REST calls and answer with API-shaped payloads (E3:
   intercept HTTP, replicate the API format, point the agent at a fake local URL). The route
   for SaaS-only surfaces (Confluence T110, Jira T102, Slack T101, Tableau/Looker/Power BI).
5. **sdk_reshape** — shim the client SDK in-process (fakesnow's connector-patching mode,
   S316; §6.1 layer L1). **Last resort.**

**Rule: wire_protocol and http_intercept beat sdk_reshape.** Agents increasingly drive tools
via CLI and raw REST, which bypass SDK shims entirely — an SDK patch is invisible to a
connector, CLI, or curl call, so the agent sees the real (missing) service. A wire/HTTP
facade sits below all clients and makes the emulation invisible to the agent regardless of
how it connects (§6.1: L2 "is what makes the emulation invisible to the agent"). sdk_reshape
is acceptable only for Python-notebook-bound personas where the SDK is the sole client.

**Fidelity constraint attached to all strategies:** emulator divergence is a silent grading
bug (§6.1). Every emulated surface inherits data-eng-bench's dual-variant pattern as fidelity
CI (S303), with documented caveats handled in grading: fakesnow's non-deterministic row
ordering and liberal dialect (S316) mean ORDER BY-sensitive checks need explicit ordering and
the grader must not rely on Snowflake rejecting malformed SQL.

## Not grounded in evidence (flags)

- No S5xx evidence for Slack, GitHub Actions, Hightouch/Census, Spark (L6 gap) — their
  surface specs above rest on the meeting list, not literature.
- No first-party incident reports from Tableau/Power BI/Looker/Hex/Sigma operators (L3 gap) —
  the BI surface's deterministic-state list is extrapolated from analytics-failure families,
  not observed BI internals.
- No independent third-party reliability reports for fakesnow/universql in agent benchmarks
  (L4 surprise) — S316/S317 are the projects' own READMEs.
- Quality/observability coverage is one finding deep (S526, Great Expectations only) —
  Monte-Carlo-style observability is ungrounded.
