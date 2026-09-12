# DE Agent Benchmark — Project Guide

*Distilled from `de-agent-benchmark-assessment.md` (2026-09-08). This document explains
what the project is, how we would develop it, and what the system looks like.
Updated 2026-09-09 with the working backlog and v1 deadline from the team meeting (§6).*

---

## 1. The idea

**Build the standard training and evaluation substrate for data-engineering agents.**

Frontier AI labs need realistic environments to train and grade models on real data
engineering work — building pipelines, debugging DAGs, reconciling metrics, handling
incidents. Today that substrate barely exists: existing DE benchmarks are static,
saturating, product-oriented leaderboards (e.g. UC Berkeley's DataAgentBench, where top
submissions already sit at ~0.84–0.88 Pass@1).

The core bets of the project:

1. **Live, stateful environments over static fixtures.** Each task runs inside a real
   containerized DE stack: a warehouse (DuckDB/Postgres), dbt project, orchestrator
   (Airflow/Dagster), and a BI/dashboard mock. The agent works against a running system,
   not a file.
2. **A generator, not a hand-authored world list.** A parameterized engine synthesizes
   thousands of coherent world variants — schemas, lineage depth, org conventions, data
   distributions, seeded faults — so the asset compounds instead of leaking and
   saturating like a static benchmark.
3. **MCP everywhere.** Agents interact through an MCP gateway with a realistic DE
   toolbelt, matching how agent harnesses actually connect today.
4. **Trajectory-blind, deterministic grading.** Correctness is checked against the world
   state (dbt tests, output diffs, dashboard values) — pipelines either produce the right
   rows or they don't. Judgment axes (metric reconciliation, convention adherence, design
   quality, code quality, robustness) use a validated LLM judge held to a human-agreement
   bar. A third axis — **process/governance
   signals** (doom-loops, ungated destructive actions) — is a differentiator no public DE
   leaderboard reports today.
5. **Sell training substrate to labs, not evals to enterprises.** Two products on one
   engine: a recurring training-environment license (private, rotating variants) and a
   public benchmark + leaderboard as inbound marketing. Enterprises buying DE-agent
   *products* (Alkera, Recce, Actioneer et al.) are a different market.

### Why it's hard to replicate

- **The generator is the asset.** A static benchmark is a file; files leak. A world
  synthesis engine plus fault/event library plus realism heuristics is a compounding
  system.
- **Deterministic-live infrastructure.** Bit-reproducible resets despite a "live" stack
  (seeded data, virtualized time, deterministic event scheduling, replay) is unglamorous
  systems work most vendors get subtly wrong — and the first thing buyers test.
- **Proprietary grading corpus.** Gold outputs, rubrics, and the human-labeled judge
  ground-truth set stay private forever; the public leaderboard shows prompts and scores
  only.
- **The calibration flywheel.** Every customer run reveals which variants discriminate
  between models; feeding that back into the generator means the inventory improves with
  usage in a way a new entrant cannot bootstrap.
- **Neutrality.** Snowflake/Databricks self-graded evals carry a conflict-of-interest
  discount; an independent standard is a structural position incumbents can't take.
- **Not moats:** the MCP gateway, container stack, harness, taxonomy, scoring weights —
  all commoditized or trivially copyable. Don't defend them.

---

## 2. Development roadmap

### Phase 0 — Proof (≈ 1 quarter)

One live world, end to end: containerized stack, seeded fault injector, MCP gateway,
deterministic verifiers, one validated LLM-judge dimension, 10–20 authored tasks across
3–4 taxonomy categories (weighted toward ingestion, orchestration, and BI-propagation —
the layers existing benchmarks under-cover).

Go/no-go gates:

- Bit-reproducible resets (same seed → same world, every time).
- A frontier-model baseline with a meaningful score spread and clear headroom. If top
  models pass >90% Pass@1, the difficulty thesis is wrong — go harder or stop.
- LLM-judge agreement ≥ 98.5% against a human-labeled ground-truth set.

This phase falsifies the thesis cheaply — by design. A failing prototype is a feature,
not a bug.

Parallel workstreams to start **now** (longest lead items):

- **Judge ground-truth program** — human-labeled grading set; gates launch credibility.
- **Expert author bench** — vetted senior DEs authoring against the running world
  (author-against-running-world, adversarial frontier-model testing, 15–20% independent
  re-solve QA). The practitioner survey doubles as sourcing and calibrates task-category
  weights.
- **Early lab conversations** — 2–3 labs, with prototype baseline numbers. "Would you
  license this at scale?" reshapes the generator's priorities long before it exists.

### Phase 1 — Product (≈ 2–3 quarters)

- **Generator v1**: parameterized world variants (expect 2–3 iterations before variants
  stop feeling procedurally samey to a strong model).
- **300–500-task held-out benchmark**, with paper + public leaderboard reporting
  Pass@1 / Pass@k / Pass^k *and* the governance/process-signal axis as headline
  differentiators.
- **One-command spin-up** delivery (API / container), never downloadable artifacts —
  license access, not artifacts.
- **First 1–2 paying lab pilots** on the training-environment API.
- Hard gate: no public launch below the judge-agreement bar.

### Phase 2 — Flywheel (ongoing)

- Custom environment commissions; private rotating training sets per customer so no
  leaked set stays valid.
- Calibration loop compounding: usage data → which variants discriminate → generator
  improves.
- Process-signal telemetry becomes a second product: a unique longitudinal dataset on
  agent trustworthiness in DE contexts.
- End state: the benchmark name becomes the category shorthand — "what's your score on
  ___?" for DE agents, the way SWE-bench is for coding.

### Non-goals

- Not a product for data teams (that's Alkera/Recce/Actioneer's market).
- Not a static dataset.
- Not an academic one-off paper.

### Top risks to manage throughout

1. **Platform incumbent moves first** (Snowflake/Databricks extend synthetic-SQL envs
   into a full DE product) — existential. Mitigate with speed, depth beyond SQL
   (orchestration + BI + governance), and vendor neutrality.
2. **Generator produces low-variance worlds** → RL customers saturate and churn.
3. **Determinism bugs destroy trust** — one non-reproducible leaderboard number and the
   lab channel is gone. Treat determinism infra as a first-class product, not scaffolding.
4. **Judge validation slips** — hard-gate the launch on the agreement bar.
5. **Task-category weights guessed wrong** — run the practitioner survey before locking
   the taxonomy.
6. **Neutrality firewall toward Alkera** — if Alkera is a customer or sponsor, decide the
   firewall posture *before* launch; retrofitting it is nearly impossible.
7. **Emulator fidelity drift becomes a silent grading bug** (added 2026-09-09, from the
   "replicate cloud services" workstream — §6.1). An emulated vendor API that diverges
   from the real platform can punish a correct agent or reward a wrong one. Same trust
   surface as risk 3. Mitigate with conformance CI against a real account and by gating
   task authoring to conformance-covered surface.
8. **Deadline compression** (added 2026-09-09) — a three-week v1 against a one-quarter
   Phase 0 forces a scope cut. If the cut is implicit rather than written down, the
   Phase-0 gates get quietly dropped instead of deliberately deferred. See §6.6.

---

## 3. Architecture

The system has four planes: the **world** (what the agent works inside), the **control
plane** (what makes worlds reproducible and varied), the **grading plane** (what scores
the run), and the **product plane** (what customers touch). One engine powers both the
public benchmark and the private training product.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           CONTROL PLANE                                     │
│                                                                             │
│  ┌──────────────────────┐   seeds/params   ┌─────────────────────────────┐  │
│  │  WORLD GENERATOR     │ ───────────────► │  DETERMINISM LAYER          │  │
│  │  · schema/lineage    │                  │  · seeded DB + RNG          │  │
│  │    synthesis         │                  │  · virtualized time (clock  │  │
│  │  · org conventions   │                  │    shim / fast-forward)     │  │
│  │  · data distributions│                  │  · deterministic event      │  │
│  │  · fault/event       │                  │    schedule + replay        │  │
│  │    library           │                  └─────────────────────────────┘  │
│  │  · difficulty knobs  │                              │                    │
│  └──────────────────────┘                              │ spawn              │
│            ▲                                           ▼                    │
│            │ calibration feedback          ┌─────────────────────────────┐  │
│            │ (which variants discriminate) │  WORLD INSTANCE (container) │  │
│            │                               └─────────────────────────────┘  │
└────────────┼───────────────────────────────────────────────┬────────────────┘
             │                                               │
             │            ┌──────────────────────────────────┘
             │            ▼
             │   ┌──────────────────────────────────────────────────────────┐
             │   │              WORLD (agent's runtime environment)          │
             │   │                                                          │
             │   │  ┌────────────┐  ┌────────────┐  ┌───────────────────┐  │
             │   │  │ Warehouse  │  │ dbt project│  │ Orchestrator      │  │
             │   │  │ DuckDB /   │  │ (models,   │  │ Airflow / Dagster │  │
             │   │  │ Postgres   │  │ tests,     │  │ (DAGs on a        │  │
             │   │  │            │  │ lineage)   │  │ virtual clock)    │  │
             │   │  └────────────┘  └────────────┘  └───────────────────┘  │
             │   │  ┌────────────┐  ┌────────────────────────────────────┐ │
             │   │  │ BI /       │  │ Fault injector                     │ │
             │   │  │ dashboard  │  │ (seeded incidents: schema drift,   │ │
             │   │  │ mock       │  │ freshness SLA breaches, bad rows)  │ │
             │   │  └────────────┘  └────────────────────────────────────┘ │
             │   └──────────────────────────┬───────────────────────────────┘
             │                              │ MCP tool calls
             │                              ▼
             │   ┌──────────────────────────────────────────────────────────┐
             │   │  MCP GATEWAY + TOOLBELT  (commodity plumbing)             │
             │   │  query / run DAG / inspect lineage / read dashboards      │
             │   └──────────────────────────┬───────────────────────────────┘
             │                              │
             │                              ▼
             │                     ┌─────────────────┐
             │                     │   AGENT UNDER    │  (frontier model in
             │                     │   EVALUATION     │   customer harness)
             │                     └────────┬────────┘
             │                              │ full action log + final world state
             │                              ▼
             │   ┌──────────────────────────────────────────────────────────┐
             │   │                    GRADING PLANE                          │
             │   │                                                          │
             │   │  ┌──────────────────┐  ┌──────────────────────────────┐ │
             │   │  │ Deterministic    │  │ LLM judge (bounded)          │ │
             │   │  │ verifiers        │  │ · metric reconciliation      │ │
             │   │  │ · dbt tests      │  │ · convention adherence       │ │
             │   │  │ · output diffs   │  │ · gated on ≥98.5% agreement  │ │
             │   │  │ · dashboard      │  │   vs human ground truth      │ │
             │   │  │   value checks   │  └──────────────────────────────┘ │
             │   │  └──────────────────┘  ┌──────────────────────────────┐ │
             │   │  ┌──────────────────┐  │ Process / governance signals │ │
             │   │  │ PRIVATE grading  │  │ · doom-loop detection        │ │
             │   │  │ corpus: gold     │  │ · ungated destructive        │ │
             │   │  │ outputs, rubrics,│  │   actions                    │ │
             │   │  │ judge ground     │  │ (headline differentiator)    │ │
             │   │  │ truth — never    │  └──────────────────────────────┘ │
             │   │  │ published        │                                   │
             │   │  └──────────────────┘                                   │
             │   └──────────────────────────┬───────────────────────────────┘
             │                              │ scores + run telemetry
             │                              ▼
             │   ┌──────────────────────────────────────────────────────────┐
             └───┤  CALIBRATION FLYWHEEL (the compounding moat)              │
                 │  which tasks saturate · which faults are diagnosable ·    │
                 │  which difficulty knobs matter → feeds generator          │
                 └──────────────────────────┬───────────────────────────────┘
                                            │
┌───────────────────────────────────────────▼─────────────────────────────────┐
│                            PRODUCT PLANE                                    │
│                                                                             │
│  ┌─────────────────────────────┐        ┌────────────────────────────────┐  │
│  │ PUBLIC BENCHMARK            │        │ TRAINING PRODUCT (revenue)      │  │
│  │ · 300–500 held-out tasks    │        │ · environment API license       │  │
│  │ · leaderboard + paper       │        │ · private rotating variants     │  │
│  │ · Pass@1 / Pass@k / Pass^k  │        │ · custom world commissions      │  │
│  │   + governance axis         │        │ · per-customer task rotation    │  │
│  │ · inbound marketing only    │        │   (leaked sets stay invalid)    │  │
│  └─────────────────────────────┘        └────────────────────────────────┘  │
│                                                                             │
│  Delivery rule: license access, never artifacts. One-command spin-up;       │
│  prompts and scores public — gold patches, rubrics, ground truth never.     │
└─────────────────────────────────────────────────────────────────────────────┘
```

### How to read the diagram

- **A run starts at the top:** the generator emits seeds/parameters; the determinism
  layer materializes a containerized world that is bit-reproducible across resets —
  including virtualized time, since DAG schedules and freshness SLAs are time-dependent.
- **The agent sees only the MCP gateway.** Everything it can do (query, run DAGs, inspect
  lineage, read dashboards) goes through the toolbelt, and every action is logged.
- **Grading is trajectory-blind and three-axis:** deterministic state checks for
  correctness, a bounded LLM judge for judgment, and action-log analysis for governance.
  The private grading corpus is what makes scores meaningful and is never exposed.
- **The feedback loop is the business:** run telemetry flows into the calibration
  flywheel, which tunes the generator — so the environment inventory improves with usage.
- **One engine, two products:** the public leaderboard is a strict subset view (prompts
  and scores out, nothing private in), while labs license private, rotating variants for
  RL training.
- **The 2026-09-09 meeting extends this diagram** in three places not yet redrawn above:
  the world gains a *documentation surface* (scattered files + a Confluence-like wiki) and
  *emulated vendor APIs* in front of the warehouse; the world's initial state includes
  *pre-existing brokenness* alongside time-scheduled fault injection; and the judge picks
  up design-quality, code-quality and robustness axes. See §6.

---

## 4. Relationship to Alkera

- **If Alkera sponsors:** this is a credibility-and-data flywheel — Alkera defines the
  eval it markets against (instead of renting credibility from saturating third-party
  leaderboards), gets proprietary DE failure-mode telemetry, and gains a lab-channel
  revenue line uncorrelated with enterprise sales.
- **If separate:** complementary, not competitive — Alkera sells DE-agent product to
  data teams; this venture sells training/eval substrate to labs. Alkera is a natural
  launch design-partner, with an explicit neutrality firewall so leaderboard influence is
  never compromised by favoritism.

---

## 5. Resources — what we can copy from the existing ecosystem

*Distilled from `copy-list-from-existing-sources.md` (researched 2026-09-08). Bottom
line: far more is copyable than the planning doc assumed. Harness, leaderboard infra, RL
plumbing, and task packaging are adoptable open source — the build scope shrinks to our
world images, tasks/rubrics/goldens, grading verifiers, and the generator.*

### Harness — adopt Harbor, don't build one

- **[Harbor](https://github.com/laude-institute/harbor)** (Laude Institute / Terminal-Bench
  team, Apache-2.0) is the de-facto standard for containerized agent eval and RL rollouts.
  Mercor's APEX training recipe and Snowflake's data-eng-bench both run on it, so any
  Harbor-supported agent (Claude Code, Codex, Cortex Code, Terminus…) evaluates against
  our environments with one command — itself a sales feature for labs.
- **Leaderboard infra is copyable too:** Snowflake's leaderboard + submission CI was
  adapted from [Terminal-Bench](https://github.com/laude-institute/terminal-bench)'s
  Apache-2.0 repo (run k=3 trials → upload → PR → CI validation → maintainer review).

### Task packaging and world images — copy Mercor's open recipe

- **[ApexAgents-SkyRL-Recipe](https://github.com/Mercor-Intelligence/ApexAgents-SkyRL-Recipe)**
  documents the per-task layout (`instruction.md` + `task.toml` + `environment/Dockerfile`
  + hidden `tests/` with `grade.py`, `verifiers.json`, `golden_responses.json`). It
  directly implements our non-negotiables: hidden-from-agent grading criteria, verifier
  decoupled from trajectory, dense per-episode reward. Copy the layout wholesale.
- **One Docker image per world, not per task** — the world image holds the filesystem
  *and* runs the MCP servers (theirs: Docs/Email/Chat; ours: warehouse SQL, dbt,
  orchestrator, lineage, BI). This answers "where do the MCP servers live."
- **[Mercor's SkyRL training guide](https://www.mercor.com/blog/training-frontier-knowledge-work-agents-a-397b-rl-training-guide-with-skyrl/)**
  (+ [SkyRL](https://github.com/NovaSky-AI/SkyRL)) — end-to-end recipe for RL on
  Harbor-format worlds, token accounting included. Saves weeks on the training product.

### Deterministic grading — three templates

- **[Snowflake data-eng-bench](https://github.com/Snowflake-Labs/data-eng-bench)**
  (Apache-2.0, 103 tasks): hidden pytest verifier checking materialized tables row-by-row
  against a reference; hermetic DuckDB variant plus real-Snowflake variant; its
  `migrate_duckdb.py` (upload an entire DuckDB database into Snowflake) answers "author
  once on DuckDB, migrate for realism."
- **[dbt-labs/ade-bench](https://github.com/dbt-labs/ade-bench)**: SQL validators
  comparing agent output vs `expected/` references with **approximate-equality testing**
  that absorbs database quirks — copy this to avoid a whole class of false-negative
  grading bugs.
- **[DataClawEval](https://arxiv.org/abs/2607.28033)**
  ([repo](https://github.com/Dicemy/DataClawEval)): ready-made artifact/process score
  split `Score = α·S_artifact + (1−α)·S_process`, and evidence that LLM judges show
  "large errors and instability" vs deterministic graders — reinforces our
  bound-the-judge rule.

### Task QA without a deep bench of senior DEs

- **ade-bench's "sage agent"**: a deliberately dumb agent that runs `bash solution.sh`
  and exits — if `--agent sage` doesn't pass all tests, the task is broken. Every task
  ships a solution script = free automated task-validity check. **Copy day one.**
- **Terminal-Bench oracle solutions + per-task verify.sh** — same pattern, ecosystem
  standard.
- **DataClawEval's differential validation**: experts perturb synthesized inputs to
  confirm tasks discriminate correct from semantically-wrong solutions — a documented
  pipeline for authoring tasks from real code when senior authors are scarce.

### Starting content and calibration references

- **ade-bench** — 43+ tasks across 8 domains, each a full dbt project + DuckDB database
  ("hundreds of tables", vague prompts like "it's broken"). Difficulty calibration
  reference and a source of base dbt-project patterns (check licenses per asset).
- **data-eng-bench** — 103 tasks + synthetic retail dataset, Apache-2.0. A lawful base
  world dataset; its category split (65 analytics / 16 bug-fix / 9 dimensional modeling /
  13 DE) confirms ingestion/orchestration/BI-propagation are thin even in the best
  existing DE benchmark.
- **Fault-injection precedent** — ade-bench already does setup-time corruption (delete a
  comma, drop a model, alter source data). We extend it to *time-scheduled* injection in a
  live world — still genuinely novel.
- **Difficulty calibration for the Phase-0 gate:** DataClawEval's best frontier model
  scores 74.9 with a 60.3–74.9 spread across 16 agents; ade-bench clusters frontier
  models at ~56–78% (with large harness/skills effects — why trajectory-blind,
  harness-agnostic grading matters); Terminal-Bench 3.0 tops out at 42.7% — proof that
  fresh, hard, contamination-free sets still crush frontier models.
- ade-bench has also been packaged as an RL environment (binary 0/1 reward, ~1 CPU/2GB
  per session) — proof the eval→RL-env packaging path is standardized, and a spec for
  resource footprints.

### What we CANNOT copy

- **APEX-Agents' world images** — the recipe is open; the 33 hand-built worlds are paid
  data deliveries.
- **Horizon-SWE** — no public repo; Polymath keeps the environment closed. Copy the
  pattern from their blog, not code.
- **The generator** — nobody has open-sourced a parameterized DE-world generator. Still
  the moat. Everything above just gets us to the starting line faster.

---

---

## 6. Working backlog (team meeting, 2026-09-09)

**Done:** high-level inspiration taken from APEX Agents and Polymath (see §5 for what is
copyable from each and what is not).

**Timeline: v1 target 2026-09-22. Hard deadline 2026-09-30.**

> ⚠️ This compresses §2's "Phase 0 ≈ 1 quarter" into roughly three weeks. The two are not
> reconcilable at face value, so **v1 must mean something narrower than "Phase 0
> complete."** Working assumption, to confirm with the team: *v1 = one demo-able vertical
> slice* — a single live world, a handful of authored tasks, the MCP gateway, deterministic
> grading, and a reproducible reset. The ≥98.5% judge-agreement gate and the 300–500-task
> held-out set stay in Phase 1. The judge gate in particular is blocked on a human-labeled
> ground-truth set that cannot plausibly exist by 2026-09-30. Tracked in §6.6.

### 6.1 Applications — simulation inventory

**Task: inventory 20–30 common data applications the environment must simulate or run.**
Named in the meeting:

| Layer | Named | Notes |
|---|---|---|
| Warehouse / lakehouse / DB | Snowflake, Databricks, Redshift (implied: BigQuery) | see "replicating cloud services" below |
| Transformation | dbt | already in §3's world stack |
| Orchestration | Apache Airflow | already in §3 (meeting listed it under transformation) |
| BI / dashboards | Tableau, Hex, Looker, Microsoft Power BI, Sigma | §3 currently says only "BI/dashboard mock" — a real scope increase |
| Documentation | Confluence-like wiki + scattered files | new surface, see §6.2 |

Candidates to fill out the 20–30 (**draft — not yet agreed**): object storage (S3/MinIO),
ingestion (Fivetran, Airbyte), streaming (Kafka), Spark, Dagster or Prefect as an alternate
orchestrator, data catalog / governance (Unity Catalog, Atlan, Alation), data quality
(Great Expectations, dbt tests, Monte Carlo-style observability), notebooks (Jupyter — Hex
doubles here), version control and review (Git/GitHub), ticketing and comms (Jira, Slack),
reverse ETL (Hightouch, Census), semantic layer (LookML, dbt Semantic Layer), CI (GitHub
Actions).

**Replicating cloud services.** The meeting's "replicate cloud services" item is the same
problem raised in the 2026-09-09 architecture discussion: real DE work happens on
proprietary platforms, but real accounts break every determinism gate — no bit-reproducible
reset, network nondeterminism, version drift, rate limits, and per-rollout cost that makes
RL impossible. Emulation is therefore the only route to platform realism. **Proposed
decision, pending confirmation:**

- **DuckDB as execution substrate, not ClickHouse.** ClickHouse is asynchronous by design
  (background merges, async mutations, part compaction), which is structurally hostile to
  bit-reproducible resets — a Phase-0 gate. It is also semantically distant from Snowflake:
  no multi-statement ACID, no true `MERGE`, case-sensitive identifiers where Snowflake
  upper-folds unquoted ones. DuckDB is hermetic (world = file, reset = file copy), has MVCC,
  `QUALIFY`, and nested types that map onto VARIANT. Note that Snowflake's own
  data-eng-bench ships a hermetic DuckDB variant — they ran this decision already (§5).
- **Emulate in four layers**, and note that most of the *value* sits in the last one, which
  is also the cheapest: L1 SDK shim → L2 REST/wire facade (SQL API v2, Statement Execution
  API — this is what makes the emulation invisible to the agent) → L3 SQL dialect
  translation (SQLGlot) → **L4 control plane** (warehouses, credits, RBAC/grants, time
  travel, zero-copy clone, stages + `COPY INTO`, query history). L4 is mostly metadata
  bookkeeping, and it is where the high-judgment governance tasks live.
- **Emulation difficulty is uneven — exploit it.** Databricks is cheapest (open Delta spec,
  `delta-rs`, OSS Unity Catalog, OSS Spark — emulate only the control plane); Redshift is
  Postgres-derived; Snowflake is hardest and probably most demanded. Resolve the ordering
  with the practitioner survey (§2), not by assumption.
- **Scope task-driven, never coverage-driven.** Emulate what Phase-0 tasks need. Explicitly
  out of scope until a task demands it: Snowpark, Cortex, Iceberg tables, multi-cluster
  scaling behaviour.
- **Check prior art before building:** `fakesnow` (Snowflake connector API over DuckDB),
  `universql` (Snowflake wire protocol over DuckDB), and SQLGlot's Snowflake↔DuckDB
  transpilation. *Unverified — scan these before writing any emulator code.*
- **New risk this introduces:** fidelity drift becomes a *silent grading bug*. An emulator
  that diverges from real Snowflake can punish a correct agent or reward a wrong one, which
  attacks the same trust surface as a determinism bug. Mitigate by stealing data-eng-bench's
  dual-variant pattern as **fidelity CI** — a conformance suite run periodically against a
  real account and diffed, task authoring gated to conformance-covered surface, and every
  SQLGlot translation miss logged as fidelity backlog.
- **Legal check before any of this goes public:** cloud warehouse ToS often carry DeWitt
  clauses restricting published benchmark results. Private conformance testing is fine;
  published vendor performance comparisons may not be. Also avoid vendor trademarks in
  product naming and verbatim reuse of their error text.

### 6.2 Initial conditions — what else is in the world

**Task: inventory the non-stack elements of the environment.**

- **Scattered documentation** — across files and a Confluence-like wiki. This makes the
  world an *information-retrieval* problem, not only an execution problem, which matches how
  real DE work actually starts ("where is this documented, and is it stale?"). Stale,
  partial and contradictory docs are a legitimate difficulty knob.
- **Synthetic data sourcing** — generate rather than source, primarily to **avoid leakage**.
  Lawful bases already identified in §5: data-eng-bench's synthetic retail dataset
  (Apache-2.0) and ade-bench's dbt-project patterns (check licence per asset).
- **Pre-existing brokenness** — broken code, half-finished pipelines and misconfigured
  applications present in the world's *initial state*. This is a **second, distinct
  mechanism** from §3's time-scheduled fault injection: initial-state corruption is
  ade-bench's existing pattern (§5), while time-scheduled injection into a live world is the
  genuinely novel part. Keep them separate in the generator — they produce different task
  shapes.

### 6.3 Tasks

**Task: 10–30 high-level task archetypes across data engineering, data science
(MLE-Bench-inspired) and data analytics.** Plus: research what these practitioners actually
do day-to-day — this is the practitioner survey already on §2's critical path, which doubles
as author sourcing and as the input that sets task-category weights.

> ⚠️ **Scope expansion.** This document is currently DE-only, down to its title. Adding data
> science and analytics widens the taxonomy, the toolbelt (notebooks, training runs, feature
> stores), *and* the grading surface — model metrics are not row diffs, so deterministic
> verification works differently for DS tasks. Two consequences: the project probably needs
> a new name, and the DS axis likely needs its own verifier design. Flagged in §6.6.

Seed archetype list (**draft — to be validated against the practitioner survey**):

*Data engineering* — (1) ingestion repair after source schema drift; (2) backfill a date
range without double-counting; (3) incremental-model correctness (dupes/gaps); (4)
freshness-SLA breach triage; (5) intermittent DAG failure triage; (6) pipeline or model
migration across layers/platforms; (7) cost or performance regression hunt; (8) author the
data-quality tests that would have caught a past incident; (9) lineage-driven impact
analysis ("what breaks if we drop this column"); (10) grant access without over-granting;
(11) idempotency bug — rerun yields different results; (12) late-arriving data; (13) dedup
and SCD Type 2 modelling.

*Analytics* — (14) metric reconciliation, dashboard vs warehouse; (15) ad-hoc question →
correct SQL plus caveats; (16) dashboard repaired after an upstream rename; (17) propagate a
metric-definition change; (18) semantic-layer / LookML modelling change.

*Data science* — (19) build a feature pipeline from raw tables; (20) train and evaluate to a
target metric; (21) detect leakage in an existing feature set; (22) triage a model
performance regression (data drift vs code change).

### 6.4 Evaluation

- **Deterministic evaluation (correctness)** — as §3.
- **LLM-as-judge axes now include design quality, code quality and robustness**, on top of
  the metric-reconciliation and convention-adherence axes already in §3.
- **GEPA to improve judge quality.** Reflective prompt optimisation with Pareto-frontier
  candidate selection, used to tune the judge rubric prompts toward the agreement bar. Two
  caveats worth writing down now: (a) GEPA **does not remove the ground-truth critical
  path** — it optimises *against* a labelled set, so the human-labelling programme (§2) is
  still the long pole; and (b) optimising the judge against the same set we then quote
  agreement from will inflate the number, so **hold out a split** and report agreement on
  data GEPA never saw. The ≥98.5% gate is only meaningful measured out-of-sample.

### 6.5 How we compete

- **Quality** — highest bar in the category.
- **Complex environments → complex long-horizon tasks (hundreds of steps).** The sharpest
  differentiator on the list: Terminal-Bench 3.0 tops out at 42.7% (§5) largely because
  horizon length is punishing. Consequences to design for now rather than later — dense
  per-episode reward and partial credit (binary pass/fail is near-useless at 300 steps);
  determinism has to hold over long horizons, which is strictly harder; cost per rollout
  scales with horizon and hits the RL product directly; and judge cost scales with
  trajectory length.
- **Random initialisation / synthetic tasks** — the generator (§1, §2). Design constraint
  established 2026-09-09: variance must be *structural* (where the fault sits relative to
  the observable, how many confounders compete, whether the symptom is ambiguous between two
  causes), not *cosmetic* (table names, row counts, naming conventions). Cosmetic knobs fight
  contamination; only structural knobs create difficulty. Every generated instance must ship
  its own gold solution and be auto-validated at generation time by an ade-bench-style "sage
  agent" run (§5), so bad draws never reach a customer.
- **Competitive pricing** — open; no anchor established yet (§6.6).

**HOW BIG?** — open. The dimensions that need numbers before pricing or capacity planning is
possible: tasks per world; worlds in the v1 set; steps per task; wall-clock and compute per
rollout (ade-bench's RL packaging is ~1 CPU / 2 GB per session, §5 — a useful starting spec);
parallel rollouts a customer would want; and total corpus size at launch versus the 300–500
in §2.

### 6.6 Open questions and flags

1. **What does "v1" mean by 2026-09-22?** Three weeks against §2's one-quarter Phase 0.
   Needs an explicit scope cut, in writing. Proposed reading in the timeline note above.
2. **Which Phase-0 gates survive the compression?** Bit-reproducible reset is achievable in
   three weeks. The ≥98.5% judge gate is not, because the human-labelled ground-truth set
   does not exist yet. Confirm the judge ships post-v1.
3. **Project scope and name.** DE-only → DE + DS + analytics. The DS axis needs its own
   verifier thinking; the name needs revisiting.
4. **BI tooling depth.** Five named BI tools is a large surface. Does v1 need one BI mock
   behind a generic API, or genuine per-vendor fidelity? Recommend one, generically, for v1.
5. **Emulation substrate and ordering** — §6.1 proposes DuckDB, and Databricks before
   Snowflake. Both need a decision.
6. **Pricing anchor** — no comparable public price point identified yet.
7. **Missing source documents.** `de-agent-benchmark-assessment.md` and
   `copy-list-from-existing-sources.md` are cited throughout but are not present in this
   repo. This file is currently the only record of that work.

*Sources: `de-agent-benchmark-assessment.md`, `copy-list-from-existing-sources.md`
(neither currently present in this repo — see §6.6), team meeting 2026-09-09.
Verification history: `verifier/`.*
