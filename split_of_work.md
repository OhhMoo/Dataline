# Split of work — v1 golden-task sprint

**Created 2026-09-12. Status: FOR REVIEW — do not start building yet.**

Three parties build in parallel: **Claude**, **Kimi**, **Astra**. This document is the shared
contract. Read all of §0–§2 before touching your own track.

**Deadline:** demo to Andrew **2026-09-14**. v1 target 2026-09-22, hard stop 2026-09-30.

---

## 0. What we are building, in one paragraph

A reproducible factory for realistic data-agent failures — sold to frontier AI labs as training
and evaluation substrate. Each task runs inside a live containerized data stack (warehouse, dbt,
scheduler, BI, scattered docs). The agent works through a tool gateway; grading is
trajectory-blind and deterministic; the world rebuilds identically from a seed. Full background
in `agent.md`; §7 and §8 there carry the sandbox and task-design detail this plan implements.

**The product is not "30 mocked data tools."** It is coherent world state, hidden causal
structure, safe reset, non-gameable grading, and useful failure traces.

**What Monday's demo must show:** a world that rebuilds identically from a seed; an agent working
a real incident through the toolbelt; a grader it cannot reach; four adversarial baselines scoring
exactly what they should; and a frontier model's actual trajectory and score.

---

## 1. Ground rules — all three tracks

These are not style preferences. Each one was learned by having a red-team agent destroy an
earlier design. Violating them silently invalidates the task.

1. **The grader does not exist during the episode.** It starts after teardown and mounts the
   world read-only. With the reference co-located in the agent's own catalog, an earlier design
   scored 11 of 14 points via `CREATE TABLE x AS SELECT * FROM expected.x` with zero diagnosis.
2. **The agent sees only `agent_visible_brief`.** Author-side prose routinely enumerates the
   planted defects in order — one draft printed all four "hidden" defects in its `why_hard`.
   The build pipeline must make shipping that impossible, not merely discouraged.
3. **No network egress from the episode zone.** Containment *and* contamination control — the
   agent must not be able to look the incident up.
4. **Official score is fully deterministic for v1.** The LLM judge runs in **shadow mode** and
   does not affect the score. Process/governance signals are **report-only**.
5. **Destructive operations are flagged, not blocked.** Whether the agent gates its own
   destructive actions is a measurement; blocking destroys it.
6. **Five surfaces only.** Real DuckDB, real dbt Core, real Git workspace, a deterministic
   scheduler state machine, a generic BI-values + docs surface. The 30-app inventory is a
   prioritisation map, not a v1 checklist.
7. **Adversarial baselines are CI, not a one-time check.** No-op, reference-copy, no-op-guard
   must score 0; sage must score 100%; full-refresh must score a predictable partial.

---

## 2. Phase 0 — frozen contracts (BLOCKING)

Drafted below. **Confirm or amend these before anyone writes code.** Parallel builds across three
model families fail at integration if these drift.

### 2.1 World layout

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

Warehouse schemas: `raw`, `staging`, `warehouse`. Concrete table and column names are Track C's
seed spec and land in this file once frozen.

### 2.2 Action log — one row per tool call, identical from every transport

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

`world_fp` is written at checkpoints only, not every row.

### 2.3 Task package (Harbor + Mercor layout)

```
tasks/<task_id>/
  task.toml            metadata, resource ceilings, timeouts
  instruction.md       <- generated ONLY from agent_visible_brief
  environment/{Dockerfile, seed.yaml}
  solution/solution.sh
  tests/               HIDDEN — ships to grading zone only
    grade.py  verifiers.json  expected/  manifest.yaml
```

### 2.4 `grade.py` interface

**In:** final world (read-only), `/grading`, action-log path, agent's final artifact.
**Out:** `score.json`

```json
{"total": 14.5, "max": 20,
 "checks": [{"id": "D1", "points": 4.5, "max": 5, "passed": false, "detail": "..."}],
 "process_signals": {"time_to_first_write_s": 412, "surfaces_before_write": 3,
                     "doom_loops": 0, "ungated_destructive": 1},
 "shadow_judge": {"J1": 2, "J2": 1, "J3": 0, "advisory_only": true}}
```

### 2.5 Agent's final artifact

```json
{"primary_cause": "...", "damage_window": ["2026-02-01", "2026-03-02"],
 "supporting_evidence": ["query-17", "log-4"],
 "repair_strategy": "...", "remaining_risks": ["..."],
 "additional_causes": []}
```

Working hypotheses are free during investigation. The **final** submission commits to one
`primary_cause`. Unsupported entries in `additional_causes` incur a **false-cause penalty** —
we do not zero the axis for hedging, which would punish sound incident response.

---

## 3. Track A — World and determinism

### Deliverables
- Three trust zones: harness / episode (internal network, no egress) / post-episode grading.
- The five surfaces from §1.6. **Deterministic scheduler state machine, not Airflow** — a
  backfill task does not need 4GB of orchestrator just to expose run history and trigger a run.
- `seed → world` builder and snapshot restore.
- **Two separate determinism gates:**
  - *Build provenance:* image digest, dependency locks, generator version, seed, parameters.
  - *Semantic world fingerprint:* schemas, canonically-ordered table contents, scheduler state,
    BI definitions, docs, virtual time.
  - *Raw byte hash:* retained as a **diagnostic**, not the definition of correctness.
- Virtualized clock so a multi-day freshness breach is testable in seconds.

### Acceptance
- Same seed, built from scratch twice **on different hosts** → identical semantic fingerprint.
  *(This proves the generator. Restoring one snapshot only proves snapshot copying — separate gate.)*
- Snapshot restore → identical raw byte hash.
- Egress from inside the episode zone fails.
- Boot < 60s, footprint ≈ 2GB, so RL rollouts stay affordable.

### Out of scope
Airflow, Superset, BookStack, vendor BI replicas, Snowflake fidelity, the world generator.

---

## 4. Track B — Tool gateway and simple connectors

### Deliverables
- MCP server exposing: `sql_query`, `sql_execute`, `dbt_run`, `dbt_test`, `dbt_compile`,
  `fs_read`, `fs_write`, `fs_list`, `git_log`, `git_diff`, `git_commit`, `docs_search`,
  `bi_render`, `sched_list`, `sched_trigger`, `sched_state`, `sched_logs`.
- **A CLI adapter over the same audited operations.** Both transports emit identical action-log
  rows. Rationale: MCP-only risks measuring fluency in our custom tool vocabulary rather than
  data competence.
- `bi_render` returns **computed values**, not an image, so "the dashboard disagrees with the
  warehouse" is a checkable condition.
- Destructive-op classification (flag, don't block).
- Process-signal extraction: doom-loop, thrash, ungated destructive action, read-before-write
  ratio, time-to-first-write, surface coverage before first write. **Report-only.**
- Session replay script.

### Acceptance
- Every tool callable from Harbor.
- The same operation through MCP and through CLI produces byte-identical action-log rows.
- Replay reconstructs a full session from the log alone.

### Note
Process signals ship with a published false-positive rate and a human-review path, or they do not
ship. Precedent: MLE-Bench's LLM log-analysis rule checker flagged up to 14.1% of medal runs and
was wrong every single time.

---

## 5. Track C — Golden task, grader, GEPA

### Deliverables
- **Re-ground A001 first.** Its current derivation cites S005/S006, which are in the
  vendor-survey cluster that failed the citation audit. Semantically audit only the sources this
  task needs — not all 49 unaudited ones — and re-derive.
- Corrected causal arithmetic: a **duplicated denominator fact** during a retry makes a **ratio
  KPI fall by roughly half**. Timezone-boundary and fan-out-join decoys produce smaller, plausible
  discrepancies that **do not explain the full change**. A full refresh would destroy legitimate
  manual adjustments. The agent must locate the exact window, repair it, preserve adjustments,
  make the load idempotent, and add a regression test.
- `instruction.md` from `agent_visible_brief` only.
- `solution.sh` oracle, `grade.py`, hidden `expected/` and `manifest.yaml`.
- **Adversarial baselines:** no-op, reference-copy, full-refresh, no-op-guard.
- **Shadow judge + GEPA** optimising the judge rubric prompt against labels harvested from the
  baseline and frontier runs, with a held-out split GEPA never sees.

### Acceptance profile
| Agent | Required |
|---|---|
| Sage / oracle | 100% |
| No-op | 0 |
| Reference-copy | 0, and must fail at the `ATTACH` |
| No-op-guard | 0 on the idempotency probe |
| Full-refresh | predictable partial, **not** zero |
| Frontier smoke test | low all-green Pass@1, target band 5–20% |

Plus: **every failed run in the smoke test is classified by cause.** A 12% pass rate where a
third of failures are timeouts or grader defects is not a 12% task. Failures must be attributable
to reasoning, not to missing tools.

### On difficulty
"Five checks at 70% → 17% all-green" assumes independence, and these checks are strongly
correlated — a wrong diagnosis fails the repair, the idempotency probe and the regression test
together. Treat the band as an **empirical calibration target**, not a derivation.

---

## 6. Integration

Tracks meet at the contracts in §2 and nowhere else.

- **B depends on A** for surface layout — unblocked by §2.1.
- **C depends on A** for world schema and **on B** for the action-log format — unblocked by §2.1
  and §2.2.
- C can build `grade.py` against a **hand-made fixture world** before A is finished. Build the
  grader before expanding the toolbelt.

Integration checkpoint **2026-09-13 evening**: one end-to-end run, oracle only.

---

## 7. Honest flags

**GEPA here is a machinery demo, not a validated judge.** Two days yields perhaps 20–40 labeled
judgments — enough to show the optimisation loop with a proper held-out split, *not* enough to
claim judge agreement. The ≥98.5% bar remains a Phase-1 gate. Present it to Andrew exactly that
way; claiming more is the one thing that would cost us credibility.

**Cut order if time runs short:** GEPA first (demo the loop on synthetic labels), then the CLI
adapter (design it, ship MCP only), then scheduler realism.

**Never cut:** determinism, the unreachable grader, adversarial baselines scoring zero. A task
whose score cannot be trusted is worse than no task.

---

## 8. Repository status — read before trusting any count

The corpus reports 26 active archetypes, 162 sources, 72 tool records, 60 failure records. Do not
read that as validated.

- 3 archetypes are `redesign_required`; 8 have never been attacked.
- Only 15 active archetypes carry any red-team record.
- 11 archetypes rest on sources damaged by the citation audit.
- 49 of 68 practitioner sources were never semantically audited.
- **Nothing has been promoted to `state/verified/`.**
- `G4` verifies that attack records are cross-linked — **not** that an archetype survived two
  independent attacks. `G5` confirms a probe was *recorded*, not executed.

Status is **discovery-complete, disposition-incomplete**. See
`implementation_structure/rounds/R001/CITATION-AUDIT-VERDICT.md`.

---

## 9. Suggested assignment

| Track | Suggested owner | Why |
|---|---|---|
| A — World & determinism | **Kimi** | already holds repo context and built most of the artifact corpus |
| B — Gateway & connectors | **Astra** | most self-contained and cleanly specified; testable in isolation; benefits from a fresh implementation view |
| C — Task, grader, GEPA | **Claude** | context-heavy — depends on knowing why F121, F128/F129 and F302 killed earlier designs, and on the audit history already in the repo |

Reassign freely; this is the split with the fewest cross-dependencies.
