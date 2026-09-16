# DE Agent Benchmark — Project Guide

*Distilled from `de-agent-benchmark-assessment.md` (2026-09-08). This document explains
what the project is, how we would develop it, and what the system looks like.
Updated 2026-09-09 with the working backlog and v1 deadline from the team meeting (§6).
Updated 2026-09-13: `split_of_work.md` merged in as §9, and §10 added for the as-built
repository architecture.*

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
- A frontier-model baseline with a meaningful score spread and clear headroom.
  **Target set 2026-09-12: Pass@1 below 20%** against frontier models genuinely trying.
  Reference points: Terminal-Bench 3.0 tops out at 42.7%, ade-bench 56–78%, DataClawEval
  74.9% — so sub-20% means harder than anything public. See §8.3 for how (conjunction of
  5+ independently graded sub-results, not obscurity). If top models pass >90% the
  difficulty thesis is wrong — go harder or stop.
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
- **§10 records where the built code has since diverged from this diagram** — the
  orchestrator is a deterministic state machine rather than Airflow/Dagster, the gateway
  exposes a CLI alongside MCP, and two boxes drawn here (the calibration flywheel and the
  product plane) are not built at all.
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

---

## 7. Sandbox build plan

*Added 2026-09-12. Status: **PROPOSED — for review**. Nothing here is built yet. Items marked
VERIFIED rest on R001 research or an executed test; items marked PROPOSED are design decisions
open to challenge.*

### 7.1 Topology — three containers, and the separation is the security model

```
┌─────────────────────────────────────────────────────────┐
│ HARBOR (harness, host side)                             │
│  seeds -> spins world -> runs agent -> tears down -> grades │
└───────────┬──────────────────────────────┬──────────────┘
            │                              │
   ┌────────▼──────────────┐    ┌──────────▼──────────────┐
   │ WORLD  (agent lives)  │    │ GRADER (agent NEVER     │
   │  DuckDB warehouse     │    │         reaches)        │
   │  dbt project          │    │  expected/ references   │
   │  Airflow (virt clock) │    │  grade.py, rubric       │
   │  BI mock              │    │  defect manifests       │
   │  wiki + scattered docs│    │  sage oracle solution   │
   │  MCP server  :8080    │    └─────────────────────────┘
   │  network: none        │      mounts world volume
   └───────────────────────┘      READ-ONLY, post-episode
```

**The grader does not exist during the episode.** It starts after teardown and mounts the
world volume read-only.

This is not a convenience. VERIFIED by our own red team (failure record F121): with the
reference seeded into the agent-visible catalog, `CREATE OR REPLACE TABLE warehouse.fct_revenue
AS SELECT * FROM expected.fct_revenue` scored 11 of 14 deterministic points with zero
diagnosis. Physical separation is the only form of that guarantee that survives contact.

### 7.2 Containment

| Control | Mechanism |
|---|---|
| No internet | `--network none`; internal services on loopback inside the container |
| No answer lookup | falls out of the above - the agent cannot search for the incident |
| No grader access | separate container, separate volume, started post-episode |
| No host escape | read-only rootfs except `/workspace`, dropped caps, non-root uid, seccomp |
| Resource bound | hard cgroup limits on RAM/disk/pids; wall-clock kill |
| No wall-clock leak | virtualized clock, so "what time is it really" cannot orient the agent |

Everything the agent can do goes through the MCP server. There is no second channel.

### 7.3 Toolbelt (PROPOSED - 15 tools)

```
sql_query(sql)                  read-only against the warehouse
sql_execute(sql)                writes; destructive patterns FLAGGED, not blocked
dbt_run(selector) / dbt_test / dbt_compile / dbt_docs
dag_list / dag_trigger(id, exec_date) / dag_state / dag_logs(task_id)
lineage(node, direction, depth)
bi_list / bi_get(dashboard) / bi_render(dashboard)   <- returns COMPUTED VALUES
fs_read / fs_write / fs_list     scoped to /workspace
docs_search(query)               the wiki, deliberately stale in places
git_log / git_diff / git_commit
ticket_read(id) / ticket_list    the ambiguous-requirements surface
```

Two deliberate choices worth challenging in review:

- **Destructive SQL is flagged, not blocked.** Whether the agent gates its own destructive
  actions is a scored governance signal; blocking would destroy the measurement.
- **`bi_render` returns computed values, not an image**, so "the dashboard disagrees with the
  warehouse" becomes a real checkable condition rather than a rendering exercise.

### 7.4 Determinism layer

The first thing a buyer tests. Treat as product, not scaffolding.

- Seeded generation: `seed -> world`, same bytes every time.
- Virtualized clock (shim service). DAG schedules and freshness SLAs are time-dependent;
  fast-forward is how a multi-day breach is tested in seconds.
- Pinned image digests, not tags. Pinned package versions.
- Reset = restore volume from a content-addressed snapshot.

**Clause D6 - VERIFIED EMPIRICALLY (claim C501).** Required before any data-science task ships:

> Pin `OMP_/OPENBLAS_/MKL_NUM_THREADS=1`; pin `PYTHONHASHSEED`; pin exact library versions AND
> the BLAS build with fixed CPU dispatch (`OPENBLAS_CORETYPE`); **ban GPU for graded training**
> (cuDNN autotuning and atomic reduction order are non-reproducible by construction); compare a
> rounded reported metric plus a hash of the **prediction vector**, never of the fitted model.

The evidence: holding seed, data and code fixed and varying only thread env vars, a 1500x1500
float64 GEMM and a `Ridge(solver='cholesky')` fit produced different hashes at 1 thread vs 8;
`PYTHONHASHSEED` changes silently reordered features. At any *fixed* thread count,
LogisticRegression, HistGradientBoosting, LightGBM and a torch SGD loop were byte-identical.
Two folk beliefs were refuted: tree ensembles did NOT diverge under varying `n_jobs`, and the
breakage is narrower than assumed - it is BLAS-backed linear algebra, and pinning fixes it.

**The specific danger:** OpenMP sizes its thread pool from detected cores, and Harbor runs
thousands of environments in parallel on cloud sandboxes - exactly where two clean rooms get
different core counts. Unpinned, a reproducibility check measures the scheduler, not the agent.

**Proof obligation:** build the world 10x, hash the DuckDB file plus project tree, assert
identical. That hash goes in the run record and is the single most important artifact for the
first lab conversation.

### 7.5 Task package (Harbor + Mercor layout)

```
tasks/<task_id>/
  task.toml            metadata, resource ceilings, timeouts, difficulty
  instruction.md       <- THE ONLY TEXT THE AGENT SEES
  environment/
    Dockerfile
    seed.yaml          generator parameters
  solution/
    solution.sh        oracle; the sage agent runs exactly this
  tests/               HIDDEN - ships to the grader container only
    grade.py
    verifiers.json
    expected/
    manifest.yaml      planted defects, for per-cause credit
```

**`instruction.md` is generated ONLY from an explicit `agent_visible_brief` field.**
VERIFIED necessary (failure record F202): author-side prose - `description`, `why_hard`,
`failure_modes_expected` - routinely enumerates the planted defects in order. One of our own
drafts printed all four "hidden manifest" defects in its `why_hard`. The build pipeline must
make that mistake impossible, not merely discouraged.

### 7.6 Trajectory capture and monitoring

One JSONL per episode, one row per tool call:

```json
{"seq":41,"vts":"2026-03-02T04:15:00Z","wts":1757,"tool":"sql_execute",
 "args_hash":"9f2c","sql_class":"DELETE","target":"fct_revenue",
 "rows_affected":18443,"latency_ms":91,"exit":"ok",
 "rss_mb":412,"disk_mb":2201,"world_fp":"a31c"}
```

Plus periodic world-state fingerprints, so a failure can be bisected to *when* it went wrong.

**Derived process/governance signals** - the axis no public DE leaderboard reports, and a
second product later (longitudinal data on agent trustworthiness):

- doom-loop: identical `(tool, args_hash)` repeated n times
- thrash: state oscillating between two fingerprints
- ungated destructive action: `DROP`/`TRUNCATE`/unbounded `DELETE` with no prior read of the
  target and no backup
- read-before-write ratio, and **time-to-first-write** - a direct proxy for "diagnosed before
  acting"
- surface coverage before first write: did it consult lineage, logs, docs - or just start
  typing SQL

**Hard constraint on this axis (VERIFIED, source S104):** MLE-Bench's GPT-4o log-analysis rule
checker flagged up to 14.1% of medal runs and **was wrong every single time**. Process signals
ship with a published false-positive rate and a human-review escape hatch, or they do not ship.

### 7.7 Build order - 10 days to the 2026-09-22 v1 target

| Days | Deliverable | Done when |
|---|---|---|
| 1-2 | World image: DuckDB + dbt + seeded data. MCP server, 4 tools. | `sql_query` works from Harbor |
| 2-3 | **Determinism harness** | 10 builds, identical hash, recorded |
| 3-4 | Full toolbelt + Airflow on virtual clock | fast-forward triggers a freshness breach |
| 4-5 | Grader container, hidden tests, `expected/` | reference provably unreachable in-episode |
| 5-6 | Trajectory logging + process signals | replayable episode file |
| 6-7 | **First task end to end** + sage validation | sage scores 100%, no-op scores 0 |
| 7-8 | Adversarial baselines: reference-copy agent, no-op-guard agent | both score 0 |
| 8-9 | Frontier baseline, n>=5, 2-3 models | a real number |
| 9-10 | Second and third task, harden, document | one-command spin-up |

Days 2-3 and day 7 are the real gates. If determinism does not hold, nothing downstream
matters. If the reference-copy agent scores above zero, the task is broken.

### 7.8 Open questions for review

1. Airflow or something lighter? Airflow's own docs want 4GB+ while the RL reference footprint
   is ~1 CPU / 2GB per session. Dagster? A minimal scheduler shim? This is unresolved and it
   constrains every task that needs an orchestrator.
2. Is `--network none` compatible with Airflow's internal components, or do we need an
   internal-only bridge with no gateway?
3. Does flagging rather than blocking destructive SQL create an unrecoverable-world problem
   often enough to matter?
4. BI mock: one generic mock behind a common API for v1, or per-vendor fidelity? Recommend
   generic; the meeting named five BI tools, which is too much surface for 10 days.

---

## 8. Problem set design

*Added 2026-09-12. Status: **PROPOSED — for review**, except where marked VERIFIED.*

### 8.1 The target

**Pass rate below 20%, against frontier models genuinely trying.**

Reference points (VERIFIED, R001 prior-art research): Terminal-Bench 3.0 tops out at 42.7%;
ade-bench clusters frontier models at 56-78%; DataClawEval's best is 74.9%. Below 20% means
harder than anything public.

### 8.2 What practitioners actually hit

Three lanes researched independently by blind agents. **They converged on one thing**, and it
is the design key:

> **The system reports green while being wrong.**

Not crashes. Not error messages. Silent, plausible, wrong output that someone downstream
notices days later.

**Data engineering** *(23 findings)*. The recurring shape is *"not a job that simply crashes:
a successful-looking pipeline that emitted wrong or incomplete state, followed by a risky
repair that could create a second incident."* Strongest task seeds: non-idempotent backfills,
semantic and schema drift, cross-system orchestration diagnosis, lineage-based SLA triage,
CDC/WAL failures, disaster recovery under destructive ambiguity.

> ⚠️ **This lane took the worst citation-audit damage.** The "50 pipeline postmortems" survey
> we were using to weight task categories names none of its 50 cases and is vendor-authored;
> one cited "incident" is a fictional vignette opening "It's 8:58 AM..."; two GitHub bugs cited
> as live traps were fixed 2026-04-17. The SHAPE of the finding is corroborated by first-party
> sources that passed audit. The FREQUENCY WEIGHTS are not trustworthy and must be re-derived.
> See `implementation_structure/rounds/R001/CITATION-AUDIT-VERDICT.md`.

**Data science** *(26 findings - the cleanest lane, audit-verified)*. Leakage dominates, is
**invisible in the reported metric**, and is found late - typically after wrong predictions
shipped. Kaggle makes the stakes concrete: honest model ranked 2206th, leaked model 28th.
Second pattern: the model is fine, **the data regime broke** - Instacart's availability metric
fell 93% to 61% and the fix was shortening the training window, not touching the model. Third:
agents fail at *artifact hygiene* before they fail at modelling - valid-submission rates of
82.8 / 54.9 / 27.3 percent across scaffolds, with a validation tool available and unused.

**Analytics** *(19 findings)*. Metric reconciliation is the dominant family: definition drift,
grain mismatch, hidden filters, timezone boundaries, fan-out joins, failure-to-zero coercion,
duplicated semantic logic, and upstream renames whose **BI blast radius is invisible from the
warehouse**. Also: the correct first move is requirements and usage discovery, *not* writing
SQL - which is itself a gradeable behaviour.

### 8.3 How to actually reach sub-20%

**Conjunction, not obscurity.** Five independent sub-results each at 70% gives 17% on
all-green. That arithmetic is the whole design.

It also resolves the apparent conflict with RL training: **pass rate and reward density are
different quantities.**

- **Pass@1** = every deterministic check green. Low by construction.
- **Reward** = weighted partial credit across the same checks. Dense, trains well.

Labs get gradient; the leaderboard gets the brutal number. Both from one rubric.

### 8.4 Seven difficulty levers

1. **Causal distance** - symptom several hops from cause, across surfaces.
2. **Confounders that survive superficial checks** - two decoys that look causal and pass a
   shallow test.
3. **Stacked failures** - fixing one does not fix the system.
4. **Repair hazard** - the obvious fix creates a second incident (the core DE finding). Full
   refresh destroys manual adjustments; range-delete removes late-arriving rows.
5. **Verification burden** - the agent must *prove* the fix with a regression test that fails
   pre-fix and passes post-fix, not merely assert it.
6. **Destructive ambiguity** - the safe action is not obvious; the wrong choice is
   unrecoverable.
7. **Commitment under ambiguity** - hedging scores zero. Listing five candidate causes forfeits
   the axis.

### 8.5 Six anti-patterns that collapse difficulty

**VERIFIED the hard way** - our red team used every one of these to destroy our own designs.
Every task ships through this checklist before entering the corpus.

1. **Reference reachable** -> agent copies it instead of diagnosing. (F121)
2. **One query answers it** -> e.g. a single `pg_replication_slots` lookup naming the stalled
   slot. (F120)
3. **Famous incident** -> the model recognises the fingerprint. (F101) **And renaming is not
   enough:** in one of our designs the fingerprint was the *published remedy*, which a model
   emits before reading a row. (F201)
4. **Answer in the brief** -> author-side prose shipped as the task text. (F202)
5. **No false-cause penalty** -> the agent emits a textbook checklist and collects per-cause
   credit for free. (F128, F129, F306)
6. **Closed-form constraint** -> one of our designs reduced to a single quantile per group.
   (F302)

Plus two **adversarial baselines that must both score 0** on every task, run as permanent CI:
a reference-copy agent, and a no-op-guard agent.

### 8.6 Task template

```
SEED INCIDENT     silent wrongness, real mechanism, synthetic surface
OBSERVATION       symptom surfaces at a BI/consumer layer, N hops downstream
DECOYS            2 plausible alternate causes, each surviving a shallow check
REPAIR HAZARD     the naive fix destroys something valuable
REQUIRED OUTPUTS  5+ independently graded: diagnosis, repair, idempotency,
                  regression test, written impact statement
PROCESS SIGNALS   time-to-first-write, surface coverage, destructive gating
```

Five required outputs is where sub-20% comes from.

### 8.7 Where to start

Build the first three tasks from the **intersection of all three lanes** - cross-surface
incidents that begin as an analytics symptom, trace through a DE defect, and require a
DS-flavoured judgement.

That intersection is also the DE lane's single largest stated evidence gap (*"production
examples that cross a warehouse, transformation layer, orchestrator, and BI surface in one
trace"*), which makes it simultaneously our hardest task family and our most defensible one -
nobody else has it either.

**Proposed task 1:** a metric that silently halved, whose cause is a non-idempotent backfill
two hops upstream, with a timezone-boundary decoy and a fan-out-join decoy, where the obvious
full-refresh repair destroys manual adjustments, and the agent must ship a regression test that
fails on the pre-fix world.

### 8.8 Open questions for review

1. Is conjunction-driven difficulty *honest* difficulty, or does it just make a task tedious?
   A strong reviewer should push here - five sub-results could be five easy things rather than
   one hard thing.
2. Where does partial credit stop being dense reward and start being a participation trophy?
3. The DE frequency weights need re-deriving from audit-clean sources before the taxonomy is
   locked. Does that block task authoring, or can the first three tasks proceed on the
   qualitative shape alone?
4. Should process/governance signals affect the score at all in v1, or only be reported?
   Given the 14.1% false-positive precedent, reporting-only is the safer launch posture.
5. How many archetypes does v1 actually need? The corpus currently holds 26 designs but only
   a minority are both red-teamed and resting on audit-clean sources.

### 8.9 What changed on the commercial side

Pricing and per-task commercial positioning are **no longer a design driver** (decided
2026-09-12): this ships as part of Alkera AI's services regardless. Sections 1 and 6 still
carry the earlier framing of the training-substrate business and should be read with that in
mind. The pricing research from R001 is retained as background only - and note it was audited
down: claim E8's three "independent" corroborating sources collapse to one seller-side anchor
with an undisclosed conflict on the byline (claim C502, failure F400). Do not use it as a
market rate.

---

### 8.10 Difficulty by composition — a library of calibrated units

**Recorded 2026-09-15.** The strategy: hold ~20 archetype units each measured at
a single-unit pass rate near **0.8**, then compose 3-4 of them into one world.
`0.8^3 = 0.512`, `0.8^4 = 0.410`, and `C(20,3) = 1140` distinct tasks fall out of
the combinations alone. Difficulty becomes a property of the *library*, not of
any one authored task.

**This is §8.3's thesis with the defect removed, and the defect matters.** §8.3
rested on *"five sub-results at 70% -> 17% all-green."* Measured in R003, those
sub-results were at **100%**, and the conclusion was recorded as falsified:
*conjunction multiplies ones*. The correction is not to abandon composition. It
is that **a unit's rate must be measured before it is stacked, never assumed.**

**What we actually have, measured (GT004, opus, K=5, `vague`, 16 surfaces live):**

| unit | pass rate |
|---|---|
| G1a window diff, G1b duplicate keys, G2 idempotency, G5 collateral, G6 regression | **1.0** |
| G3 late arrivals | **0.8** |
| G1c repair the second defect | **0.0** |

One unit in the usable band. Five ceilings and one wall. **We do not yet have a
library; we have one calibrated unit.** Everything below follows from that.

**Three conditions the plan has to buy.**

1. **Calibration is the bottleneck, not authoring.** Every unit needs its own
   K>=5 measurement against a real model before it can be stacked. Twenty units
   is 100+ episodes of quota before a single combination is built, and a unit
   that drifts to 1.0 after a world change silently contributes nothing.

2. **Independence is not free, and we have already been wrong about it.** G4
   looked like a sixth gate and was not: its only failing tile could not be
   correct until G1c was repaired, so it restated G1c rather than adding to it.
   Two units that share a mechanism do not multiply. **Independence has to be
   measured pairwise** - a unit's rate conditional on another unit passing -
   which is a second calibration cost the raw combinatorics hides.

3. **Composition has to be mechanical.** GT004 took six parallel agents plus
   integration. `C(20,3)` is only cheap if a world builder can take N archetype
   modules and emit one coherent world, grader, oracle and adversary suite
   without an authoring round each time. That builder does not exist and is the
   real deliverable behind this idea.

**MEASURED 2026-09-15, and it corrects two things written above.** Opus 5, K=5,
host topology, live-surface gate clean:

| task | variant | Pass@1 | Pass^5 | verdict |
|---|---|---|---|---|
| GT001 | - | **1.0** | 1.0 | ceiling |
| GT002 | - | **0.8** | 0.0 | **unit** |
| GT004 | vague / guided / specific | **0.0 / 0.0 / 0.0** | 0.0 | wall |
| GT006 | guided / vague | **1.0 / 1.0** | 1.0 | ceiling |
| GT005 | guided | **1.0** | 1.0 | ceiling |
| GT007 | guided | **1.0** | 1.0 | ceiling |

**Correction 3 - we cannot yet build a 0.8 unit on purpose.** GT005, GT006 and
GT007 were authored specifically to land near 0.8, by three agents that each had
the calibration table above in hand and were briefed that the risk was the
ceiling, not the floor. GT005's author went further: it probed its own ceiling,
found that the strongest honest fake scored full marks, and added a sibling chain
that took that fake from 19/19 to 9/19 without making the oracle harder. **All
three measured 1.0, Pass^5 1.0, every check at 1.0.** Five of six calibrated
tasks are ceilings; the single unit (GT002) got there by accident and nobody
knows why.

The tasks are not badly built - between them they caught three rubric defects by
adversary, falsified one of our own sources against the engine, and made a design
choice that saved a sibling task from destruction. The mechanisms are genuinely
well hidden (GT006 loses 6.4% of rows a day inside a 14.75% volume swing).
**Concealment is not difficulty.** It delays a model that queries the data, and
this model always queries the data.

**What actually stopped it, once, is the interesting result.** GT004's second
defect is not hidden at all - the model NAMES it in most rollouts - and it is
never repaired, across 21 episodes and three prompt framings. The obstacle is a
closed `wontfix` ticket plus a changelog page asserting history was already
full-refreshed: **the world supplies a reason not to act, and the model defers to
it over its own reading of the data.** That is the only lever measured to move
this model, it is a world lever, and it suggests the composition thesis needs
restating: difficulty is unlikely to come from stacking obscure mechanisms
(1.0 x 1.0 = 1.0), and more likely from **interference** - a defect paired with a
plausible, authoritative reason to leave it alone.

**Correction 1 - the prompt variant is NOT the difficulty dial.** This section and
three agent briefs asserted that "the variant is how a unit gets tuned toward
0.8", on the strength of ADE-bench reporting prompt vagueness as its biggest
lever. Five variant measurements across two tasks moved the pass rate by
**zero** in either direction. GT004 stayed at 0.0 even under `specific`, which
states the acceptance criteria outright; GT006 stayed at 1.0 even under `vague`.

The explanation that fits: **our gates grade final world state, not the
narrative.** A brief changes what the agent is *told*; a competent agent
investigates the data regardless, and the grader only looks at what it did. So
where the obstacle is a *belief the world plants* - GT004's `wontfix` ticket and
the changelog page asserting history was full-refreshed - telling the agent more
cannot help, because the brief was never what stopped it. **To tune a unit, change
the world, not the prompt.** Keep the variants: they still make a run's
conditions explicit and reproducible. Do not expect them to move a rate.

**Correction 2 - the yield is roughly one unit in four.** Of four calibrated
tasks, two are ceilings, one is a wall, one is a unit. If that rate holds, a
20-unit library needs on the order of **80 authored tasks**, each with its own
calibration run. Combined with the ~64 rollouts needed to pin a unit to +/-0.05
(below), the library is an order of magnitude more expensive than "author 20
tasks and stack them".

Two failure directions, both instructive and opposite. **GT006 is a single clean
mechanism** - a NULL-unsafe `NOT IN` predicate dropping 6.4% of rows a day inside
a 14.75% volume swing - beautifully hidden, and a strong model recognises it the
moment it reads the predicate. Hiding only delays a model that queries the data,
and it always queries the data. **GT004 is a judgment trap**, and it holds: 21
episodes, three framings, zero repairs. Neither is a difficulty *unit*. What sits
at 0.8 is GT002, whose single failing rollout lost three sub-checks *together* -
one mechanism failing occasionally, not three independent gates.

**Sensitivity, because the target band is narrow.** Units at 0.9 give
`0.729 / 0.656` for 3 and 4 stacked; at 0.8, `0.512 / 0.410`; at 0.7,
`0.343 / 0.240`. A library calibrated loosely lands anywhere between "trivial"
and "impossible", so the unit measurement has to be tight and re-checked
whenever the world moves.

**Corollary worth keeping.** A unit at 1.0 is not a difficulty unit, it is a
**prerequisite**. G1a, G1b, G2, G5 and G6 are all at 1.0 and all worth keeping -
they are what makes the world coherent and what stops a shortcut scoring - but
none of them belongs in the difficulty arithmetic. Counting them is how §8.3
went wrong the first time.

---

## 9. v1 build — contracts, ground rules, and track status

*Merged from `split_of_work.md` (created 2026-09-12, retired 2026-09-13 once all three
tracks landed). The three-way split across Claude / Kimi / Astra is now history; what
survives it are the ground rules, the frozen contracts, and the honest flags.*

### 9.1 Ground rules — non-negotiable, all tracks

These are not style preferences. Each was learned by having a red-team agent destroy an
earlier design. Violating one silently invalidates the task.

1. **The grader does not exist during the episode.** It starts after teardown and mounts the
   world read-only. With the reference co-located in the agent's own catalog, an earlier
   design scored 11 of 14 points via `CREATE TABLE x AS SELECT * FROM expected.x` with zero
   diagnosis (F121).
2. **The agent sees only `agent_visible_brief`.** Author-side prose routinely enumerates the
   planted defects in order — one draft printed all four "hidden" defects in its `why_hard`
   (F202). The build pipeline must make shipping that impossible, not merely discouraged.
3. **No network egress from the episode zone.** Containment *and* contamination control — the
   agent must not be able to look the incident up.
4. **The official score is fully deterministic for v1.** The LLM judge runs in **shadow mode**
   and does not affect the score. Process/governance signals are **report-only**.
5. **Destructive operations are flagged, not blocked.** Whether the agent gates its own
   destructive actions is a measurement; blocking destroys the measurement.
6. **Five surfaces only.** Real DuckDB, real dbt Core, real Git workspace, a deterministic
   scheduler state machine, a generic BI-values + docs surface. The 30-app inventory in §6.1
   is a prioritisation map, not a v1 checklist.
7. **Adversarial baselines are CI, not a one-time check.** They run on every change to the
   task, the grader, or the world generator.

### 9.2 Frozen contracts

Tracks meet here and nowhere else. Amending one of these requires re-running every
downstream suite.

#### 9.2.1 World layout

```
AGENT-VISIBLE
/workspace/                          git repo root
  dbt_project/{dbt_project.yml,models/,tests/}
  data/landing/daily_revenue/dt=YYYY-MM-DD/*.csv
  docs/                              scattered markdown, deliberately stale in places
  README.md
/var/warehouse/warehouse.duckdb      reachable ONLY through sql_* tools, not fs_*
/var/sched/state.json                scheduler run history
/var/bi/dashboards/*.yml             BI definitions

GRADING ZONE — never mounted inside the episode
/grading/expected.duckdb
/grading/manifest.yaml               planted defects, for per-cause credit
/grading/grade.py
/grading/solution.sh                 oracle
```

Warehouse schemas: `raw`, `staging`, `warehouse`.

**Mandatory access rule — never open the warehouse file directly.** DuckDB names the catalog
after the file stem, so `warehouse.duckdb` holding a schema named `warehouse` makes catalog
and schema collide, and *every* two-part reference raises
`Binder Error: Ambiguous reference to catalog or schema "warehouse"`. No view formulation
survives it, and `warehouse.kpi_daily` is the symptom surface the agent starts from — so the
collision made GT001 unsolvable end to end while every track's own tests stayed green.
Connect in-memory and attach under an alias:

```python
con = duckdb.connect(":memory:", config={"threads": 1})
con.execute("ATTACH '<path>' AS wh (READ_ONLY)")
con.execute("USE wh.warehouse")
```

This is the most expensive thing integration taught us: it is invisible to any track working
alone, because it only appears when the file is deployed under its contract name.

#### 9.2.2 Action log — one row per tool call, identical from every transport

```json
{"seq": 41, "vts": "2026-03-02T04:15:00Z", "wts": 1757000000000,
 "transport": "mcp|cli", "tool": "sql_execute",
 "args_hash": "9f2c1a...", "args_summary": "DELETE FROM warehouse.fct_revenue WHERE ...",
 "op_class": "read|write|ddl|destructive|exec",
 "target": "warehouse.fct_revenue", "rows_affected": 18443,
 "result_hash": "a31c4d...", "result_bytes": 210,
 "latency_ms": 91, "exit": "ok", "error": null,
 "rss_mb": 412, "disk_mb": 2201, "world_fp": "cc90fe..."}
```

`world_fp` is written at checkpoints only, not on every row. `op_class` lives in the log, not
in the tool response — the log is the artifact the grader and the process signals consume.

#### 9.2.3 Task package

```
tasks/<task_id>/
  task.toml            metadata, resource ceilings, timeouts, validity gates
  instruction.md       <- generated ONLY from agent_visible_brief
  SEED_SPEC.md         frozen world contract for this task
  solution/solution.sh oracle
  tests/               HIDDEN — ships to the grading zone only
    grade.py  manifest.yaml  fixture_contract.py  fixtures/  judge/
```

**Amended 2026-09-13.** The original draft specified `environment/{Dockerfile,seed.yaml}` and
`tests/expected/`. Neither is how GT001 actually builds: the container lives once in
`world/docker/`, and the reference world is *generated* by
`tests/fixtures/make_fixture_world.py` rather than committed, because `*.duckdb` is gitignored
on purpose — committing a world defeats bit-reproducible resets. The empty directories were
removed rather than left as decoration.

#### 9.2.4 `grade.py` interface

**In:** final world (read-only), `/grading`, action-log path, agent's final artifact.
**Out:** `score.json`

```json
{"total": 14.5, "max": 20,
 "checks": [{"id": "D1", "points": 4.5, "max": 5, "passed": false, "detail": "..."}],
 "process_signals": {"time_to_first_write_s": 412, "surfaces_before_write": 3,
                     "doom_loops": 0, "ungated_destructive": 1},
 "shadow_judge": {"J1": 2, "J2": 1, "J3": 0, "advisory_only": true}}
```

#### 9.2.5 Agent's final artifact

```json
{"primary_cause": "...", "damage_window": ["2026-02-01", "2026-03-02"],
 "supporting_evidence": ["query-17", "log-4"],
 "repair_strategy": "...", "remaining_risks": ["..."],
 "additional_causes": []}
```

Working hypotheses are free during investigation. The **final** submission commits to one
`primary_cause`. Unsupported entries in `additional_causes` incur a **false-cause penalty** —
we do not zero the axis for hedging, which would punish sound incident response.

#### 9.2.6 Observability sample store

**Frozen 2026-09-14** (merged here from `OBS_STORE_CONTRACT.md`, 2026-09-15).
The writer is `world/world/` and the reader is
`gateway/src/alkera_gateway/observability.py`; this seam is the only thing they
share, and neither changes it without saying so loudly.

**Why a store at all.** A real observability system **records what happened**;
a recomputing one **computes what is**. Those differ the moment the world
moves: once `advance` has run, the state that produced yesterday's freshness
reading is gone, and the recorded sample is the only evidence it ever held that
value. The defect this fixes was named precisely by its own author -
*"reading the alarms is strictly dominated by reading the data - an agent that
consults the dashboard can be misled, one that ignores it cannot."*

```
/var/obs/samples.jsonl      append-only, one JSON object per line, ordered by vts
/var/obs/definitions.yaml   metric + alarm definitions, seeded by the theme
```

`/var/obs/` sits beside `/var/sched/` and `/var/bi/` in the §9.2.1 layout and is
**agent-readable** - an observability store the agent cannot read is not a
surface.

```json
{"vts": "2026-03-04T05:30:00Z", "metric": "orders_freshness_seconds",
 "value": 32400.0, "source": "advance", "run_id": "sftp_poll-0031",
 "detail": {"watermark": "2026-03-03T20:30:00Z"}}
```

- `vts` - **virtual** time, never wall clock. A wall-clock stamp breaks the
  reproducible-rebuild gate.
- `value` - `null` is legal and means *insufficient data*, which is not `0.0`.
  Writing `0.0` for "could not measure" is the silent-wrong failure this project
  treats as disqualifying.
- `source` - `advance` | `trigger` | `deploy` | `build`.
- `detail` - the components the value came from, so a reader can re-derive it.

**Writer.** During `build` and `advance`, append one sample per defined metric
after each job. Same seed plus same span must produce a byte-identical
`samples.jsonl`. **Appending is the only permitted operation** - a store that
can be edited after the fact is not evidence.

**Reader.** `metrics_series` and `alarms_history` read the file rather than
recomputing; `metrics_get` may still compute live current state. Retention is
**read-side**: `retention: {days: N}` is declared in the definitions file,
nothing is truncated on disk, and the reader reports how many samples it refused
(`aged_out`).

**The invariant that matters.** *A reader must never be able to reconstruct a
past sample from present state.* If it can, the store is decoration. Test it:
advance the world, then assert a sample recorded before the advance is not
derivable from the world as it now stands.

**Settled while building the reader.**
- **One definitions file per world.** `observe.py` and the gateway both read
  `definitions.yaml` or `observability.yml`, and the gateway **refuses a world
  that ships both** (`invalid_observability`) - a writer and a reader describing
  different metric sets is the one way this surface can be wrong without
  anything looking wrong. Every theme ships exactly one, `obs/definitions.yaml`.
- **Definition spelling is accepted both ways.** A job metric may be `metric.job`
  or `metric.source.job`, and `window_runs` may sit at the top level; the gateway
  lifts the top-level spelling rather than rejecting it. Anything genuinely
  unrecognised still fails loudly.
- **`not_yet_observed`.** A `job_overrun` can push a run past the clock, so a
  sample can carry a `vts` after the resulting `vnow`. The reader refuses to
  serve those and reports the count: a reader at `vnow` cannot honestly claim to
  have observed a reading stamped later.
- **A recorded sample is always a scalar**, so `datapoints_to_alarm` counts
  readings rather than days, and a floor over a cumulative `row_count` can never
  fire. Three of four themes needed a per-period `sql_scalar` metric for their
  series alarms.

`fingerprint.py` includes `/var/obs/` in the semantic fingerprint - it is world
state.

### 9.3 Track status as of 2026-09-13

| Track | Scope | State |
|---|---|---|
| **A — World & determinism** | `world/` | seed to world builder, semantic fingerprint, snapshot save/restore/verify, deterministic scheduler, BI values surface, 3-zone Docker topology. **7 tests green.** |
| **B — Gateway & connectors** | `gateway/` | 17 tools, MCP **and** CLI over one audited core, destructive classification, replay, report-only process signals. **11 tests green.** |
| **C — Task, grader, GEPA** | `tasks/GT001-aov-halving/` | seed spec, generator, oracle, 8-check deterministic grader (19 pts), 6 adversarial baselines, fixture-contract guard, shadow judge + GEPA loop. **TASK VALID.** |
| **Integration** | `tasks/.../tests/test_integration.py` | all three tracks in one process against a real world. **12 assertions green.** |

Baseline profile — the task-validity gate. If any lands outside its band the *task* is broken,
not the agent:

| Baseline | Score | Required |
|---|---|---|
| oracle | 19/19 | = max |
| no_op | 0 | ≤ 1 |
| full_refresh | 9 | partial, **not** zero (it destroys the 412 manual adjustments) |
| dedupe_only | 17 | < oracle (data fixed, loader still on append) |
| checklist | 17 | < oracle (the false-cause penalty must bite) |
| reference_copy | 17 | **0 in production** — scores high in fixtures *by design*, proving topology is the only defence |

That last row is the one to read carefully. `reference_copy` scoring 17 is not a failing
grader; it is the grader honestly reporting that when the reference sits in a reachable
directory, copying it works. Track A must prove `/grading` is unreachable **in the real
container** — a build gate, not a test.

### 9.4 Honest flags

**GEPA here is a machinery demo, not a validated judge.** The labelled set is small and
derived from baseline agents rather than human annotation of real trajectories. The loop runs
end to end with a proper held-out split, and on the current data it **overfits** — which is
the held-out split doing its job, and exactly why agreement must never be quoted from data the
search saw. The ≥98.5% agreement bar remains a Phase-1 gate. Presenting it as anything more is
the one thing that would cost us credibility.

**Process signals ship with a published false-positive rate and a human-review path, or they
do not ship.** Precedent: MLE-Bench's LLM log-analysis rule checker flagged up to 14.1% of
medal runs and was wrong every single time. `gateway/src/alkera_gateway/signals.py` currently
self-reports `false_positive_rate: None`, `calibration: unmeasured`,
`human_review_required: True`. That is the correct posture, not a defect.

**Cut order if time runs short:** GEPA first, then the CLI adapter, then scheduler realism.
**Never cut:** determinism, the unreachable grader, adversarial baselines.

### 9.5 Corpus status — read before trusting any count

The research corpus reports 26 active archetypes, 162 sources, 72 tool records, 60 failure
records. Do not read that as validated.

- 3 archetypes are `redesign_required` (A031, A032, A033); 8 have never been attacked
  (A002, A003, A005, A010, A017, A018, A022, A025).
- Only 15 active archetypes carry any red-team record.
- 11 archetypes rest on sources damaged by the citation audit.
- 49 of 68 practitioner sources were never semantically audited; of those sampled, roughly
  **47% failed semantic review despite passing every mechanical check** — each had a perfect
  verbatim quote match.
- **Nothing has been promoted to `state/verified/`.**
- `G4` verifies that attack records are cross-linked — **not** that an archetype survived two
  independent attacks. `G5` confirms a probe was *recorded*, not executed.

Status is **discovery-complete, disposition-incomplete**. See
`implementation_structure/rounds/R001/CITATION-AUDIT-VERDICT.md`.

The recurring lesson across both the corpus and the code: **mechanical verification is not
quality.** Every failed source passed its citation check. Every track passed its own tests
while the composition was broken.

---

## 10. Repository architecture

§3 is the *product* architecture — the four planes, the north star. This section is the
**code** architecture: what actually exists on disk, why it is shaped that way, and what
should change next. Written 2026-09-13 after the three-track merge and cleanup.

### 10.1 Directory map

```
alkera/
├── agent.md                    THIS FILE — the single source of truth
│
├── world/                      TRACK A · the world factory (was track_a/)
│   ├── world/                  importable package
│   │   ├── gen.py              seeded data synthesis
│   │   ├── build.py            a seed file -> materialized world root
│   │   ├── fingerprint.py      SEMANTIC hash (schemas + canonical contents)
│   │   ├── hashing.py          raw byte hash — diagnostic only
│   │   ├── snapshot.py         save / restore / verify
│   │   ├── scheduler.py        deterministic state machine, NOT Airflow
│   │   └── bi.py               dashboards -> computed values
│   ├── docker/                 3-zone topology + egress_test.py
│   ├── grading/                post-episode zone (never mounted in-episode)
│   ├── scripts/acceptance.sh   the determinism gate battery
│   ├── tests/                  7 tests
│   ├── seed.example.yaml       worked seed contract
│   └── requirements.lock       pinned — determinism depends on it
│
├── gateway/                    TRACK B · the only way an agent touches the world
│   ├── src/alkera_gateway/
│   │   ├── core.py             THE audited chokepoint — every call passes here
│   │   ├── tools.py            the 17-tool registry
│   │   ├── operations.py       op_class: read|write|ddl|destructive|exec
│   │   ├── sql.py  files.py  commands.py  surfaces.py     connectors
│   │   ├── audit.py  records.py                           the action log
│   │   ├── server.py  cli.py                              MCP + CLI transports
│   │   ├── replay.py           reconstruct a session from the log alone
│   │   ├── signals.py          process signals — REPORT ONLY, uncalibrated
│   │   └── config.py           enforces audit_dir/warehouse outside workspace
│   └── tests/                  11 tests
│
├── tasks/                      TRACK C · the graded content
│   └── GT001-aov-halving/
│       ├── SEED_SPEC.md        frozen world contract
│       ├── task.toml           limits + machine-readable validity gates
│       ├── instruction.md      all the agent ever sees
│       ├── solution/           the oracle
│       └── tests/              HIDDEN — grade.py, manifest, baselines,
│                               fixture_contract.py, judge/, test_integration.py
│
├── implementation_structure/   the research swarm and its record
│   ├── ARCHITECTURE.md  ROUND_PLAN.md  roles/    how the swarm runs
│   ├── schemas/  gates/                          G0–G5 machine verdict
│   ├── state/                                    THE BLACKBOARD — commit always
│   │   ├── archetypes/  claims/  knowledge/  inventory/
│   │   ├── failures/                             what round N killed, so N+1 need not
│   │   └── verified/                             still empty — see §9.5
│   └── rounds/R001/                              per-round reports incl. the audit verdict
│
├── verifier/                   append-only acceptance history (2026-09-08 assessment)
└── vendor/                     cloned upstreams (gepa) — gitignored, never committed
```

### 10.2 The one dependency rule

```
   tasks/  ──depends on──▶  gateway/  ──depends on──▶  world/
      │                                                   ▲
      └───────────────depends on (layout only)────────────┘

   implementation_structure/  ──feeds task design──▶  tasks/
```

**Dependencies point one way, and the arrows never reverse.** `world/` knows nothing about
the gateway; the gateway knows nothing about any particular task; a task knows the world's
*layout* (§9.2.1) but never imports its internals. `implementation_structure/` is upstream of
everything and imports nothing — it is a corpus, not a library.

The seam where this broke is instructive: all three respected the rule and integration still
failed, because the contract they shared (§9.2.1) was under-specified. A shared *path* is not
a shared *contract* until it says how to open the file.

### 10.3 How the code maps onto the four planes of §3

| §3 plane | Where it lives | Honest state |
|---|---|---|
| Control plane (generator + determinism) | `world/` | built; cross-host fingerprint gate not yet run in CI |
| World instance | `world/docker/` | 3 zones defined; `/grading` unreachability not yet proven in a live container |
| Toolbelt / gateway | `gateway/` | built, both transports, 17 tools |
| Grading plane — deterministic | `tasks/*/tests/grade.py` | built, 8 checks, 19 points |
| Grading plane — LLM judge | `tasks/*/tests/judge/` | **shadow only**, overfits on current labels |
| Grading plane — process signals | `gateway/.../signals.py` | **report only**, uncalibrated by design |
| Calibration flywheel | — | **not built.** Needs run telemetry that does not exist yet |
| Product plane | — | **not built.** Phase 1+ |

Two rows of that table are empty, and they are the two the §3 diagram draws most confidently.
Worth saying out loud when showing the diagram.

### 10.4 Proposed changes

Ordered by what a second task would force us to confront, not by effort.

1. **Extract the task-authoring kit out of GT001.** `fixture_contract.py`,
   `run_baselines.py` and the `judge/` harness are 90% task-agnostic but currently live
   inside one task directory. GT002 would copy-paste them, and the two copies would drift.
   Proposed: `harness/` at the root, with per-task files reduced to `manifest.yaml`,
   `grade.py` checks, and the generator. **Do this before GT002, not after.**
2. **One virtualenv, not two.** `world/.venv` and `gateway/.venv` have diverged —
   `world/.venv` has no pytest, so Track A's tests can only be run through the gateway's
   interpreter. That is a trap for anyone who clones this. Either one root venv, or a
   `make test` that hides the difference.
3. **Promote `test_integration.py` to a root-level suite.** It tests all three tracks, so
   living under `tasks/GT001/tests/` mislabels it as task-specific. It is the suite that
   caught the only defect no other suite could see.
4. **CI that runs the four suites plus `acceptance.sh`.** The baselines are declared CI in
   §9.1 rule 7 and are not actually wired to anything. A rule nothing enforces is a comment.
5. **Fold `verifier/` into `implementation_structure/`.** It is an append-only verification
   record — the same species as `rounds/` — and sits at the root only for historical reasons.
   Low value, do it when touching that tree anyway.

### 10.5 Structural gaps

Listed so they are not rediscovered.

- **`state/verified/` is empty.** Nothing in the research corpus has been promoted. The gates
  check cross-linking and recording, not survival (§9.5).
- **`replay.final_world_verified: False`** — replay confirms responses match but never
  verifies the final world state matches. A replay that does not check the world is a
  transcript, not a reproduction.
- **`/grading` unreachability is unproven in a live container.** `reference_copy` scores 17
  in fixtures by design; only the container topology makes it 0.
- **GEPA has no `reflection_lm` wired**, so real reflective mutation never runs — the loop
  falls back to a local search.
- **Cross-host determinism has never been executed.** `acceptance.sh` gate 1 builds twice on
  *the same* host, which proves far less than the acceptance criterion claims.

---

---

## 11. Three tracks — build and validate

**Merged here from `3_track.md`, 2026-09-15.** Three agents build in parallel:
**Kimi (A)**, **Astra (B)**, **Claude (C)**; each owns the code it built. The
counts below are as of 2026-09-15 and supersede the ones `3_track.md` carried.

### 11.1 Where the project is

| | R002 | R003 | now (2026-09-15) |
|---|---|---|---|
| tasks built | 1 | 3 | **4** - GT001-GT004, all TASK VALID |
| world shapes | 1 | 4 themes | 4 themes + per-task fixture worlds |
| gateway tools | 17 | 48 | **63** |
| gateway tests | 12 | 78 | **143** |
| world tests | 7 | 37 | **120 + 66 subtests** |
| harness selftest | - | 51 | **133** |
| the scheduler | literal `"success"` | executes dbt | **executes, or returns `not_executed` - never a status it did not observe** |
| episode topology | host subprocess | container | container, containment re-proved; `WorldScheduler` now injected |
| cross-host determinism | never executed | never executed | **executed, host == container** |
| frontier rollouts | zero | GT001 Pass@1 = 1.0 | **GT004 Pass@1 = 0.0, Pass^5 = 0.0** |

### 11.2 The rule R003 paid the most to learn

**A check that never ran against the thing it ships against proves nothing.**
GT001 printed TASK VALID for a round while its oracle aborted on its first
statement, because the baselines staged the warehouse under a *convenient*
filename instead of the contract one.

Corollaries, each of which has now cost real time more than once:
- A registered tool with no test is indistinguishable from a dead one.
- A metric that does not read real state is a lie the grader may believe.
- A passing test that never exercised the surface is not honest; a skipped test
  that names why is.
- **A check can be guarding the bug.** Two gateway tests asserted the inert
  scheduler's `"status": "success"` - including the one named for the executed
  path - which is why 14 of 20 job shapes stayed inert for three rounds.
- **A skipped gate needs an owner or it is indistinguishable from a passing
  one.** Three agents each hit the same `acceptance.sh` abort and each correctly
  said "not mine"; two determinism defects rode through on it.

### 11.3 Track A - Worlds

**Done:** theme-lifted builder; four themes; the executing scheduler; faults;
cross-host determinism now executed and passing.

**Open:**
- ~~`late_arrival` is inert~~ — **FIXED 2026-09-15.** `_kind_scan` templated its
  `path`/`pattern` with `{day}`/`{day_compact}`, the scheduler now threads the
  execution day to the executor on all three scheduled branches, and the two
  scan-bearing themes scope their patterns to the night they are polling for.
  Measured before: 60 stale sentinels, `min: 1`, **success**. After: `0 file(s)
  matching */*_20260303.done, expected at least 2`, **failed**, with the fault
  log, the run record and the healed data telling one story. `torn_write`'s own
  docstring claim was inert for the same reason and is now true at `min: 2`.
  Seeded failures went 2 → 8 (sftp_vendor) and 1 → 6 (mainframe_batch).
  **A test was living off the defect** — `test_the_incident_is_reachable_through_
  the_scheduler_surface` asserted `try_number == 1` on the *first* failed run,
  which only held while SSH resets were the only failures the past could
  contain. Third such test found in two days.
- **NEW, introduced by that fix and not yet resolved:** `sftp_vendor`'s ingest
  plane now contradicts its scheduler history. `gen.write_ingest` seeds one green
  sync per connector per day unconditionally at 05:31, so on the six withheld
  nights a connector reports `rows_inserted: 53` from files that were not on disk
  until 07:30. The two planes agreed trivially while the poll could not fail.
- Themes 5+; 19 of 30 have no archetype.

### 11.4 Track B - Gateway and stack

**Done:** warehouse connector (DuckDB + Postgres); 63 tools; multi-container
compose with containment measured per service; workflow and observability
planes; the honest scheduler trigger.

**Open:**
- Postgres is proven for four SQL statements; `render_dashboard`,
  `BIStore.refresh`, watermark queries and dbt are DuckDB-only.
- `core.py:23` guards `from world.fingerprint import semantic_hash` - a Track A
  rename breaks Track B at runtime and **neither suite goes red**.

### 11.5 Track C - Tasks, harness, grading

**Done:** GT001-GT004 TASK VALID; rollout harness with enforced blindness,
failure classification and quarantine; container episodes plus the terminal;
gates G0-G8; **the live-surface gate** (a required surface that is dead is a
harness error, not a score) and **Pass^k**.

**Open:**
- **Engineering-quality axis.** Process signals exist and are report-only.
- **G7: 89 failure records, ~80 with no execution record.** Red on purpose.
- **The F620 comma residual.** A comma-joined checklist naming one real cause
  and five invented ones escapes the per-atom penalty. Hygiene only, excluded
  from `pass_requires_checks`, so it cannot move a verdict. Recorded, not hidden.

### 11.6 What is still not true

- **Nothing has been promoted to `state/verified/`.** Zero of 41.
- **6 of 162 sources are a practitioner describing their own incident** - 3.7%,
  and that is the thing this project sells.
- **The difficulty thesis as written in §8.3 is falsified.** §8.10 records the
  revised form and what it would cost to make true.
- **No container-topology number exists for GT004.** `gateway/src` is baked into
  the image, so a container measurement needs a commit and a rebuild.

---

*Sources: `de-agent-benchmark-assessment.md`, `copy-list-from-existing-sources.md`
(neither currently present in this repo — see §6.6), team meeting 2026-09-09.
Verification history: `verifier/`.*
