# GT004 — design contract

**The first task built for the lifecycle axis.** Everything before it graded code
correctness; measured across every stored rollout, **D1-D5b pass at 1.0 for every
model we have run**, and lifecycle is 9 of 63 points across GT001/2/3 combined.
Code checks are saturated. Difficulty has to come from somewhere else.

Target: **Fable 5 ~40%**, Kimi K3 below it. Six roughly-independent gates at ~0.85
each lands on 0.377; the same six at 0.80 land on 0.262. Composition amplifies a
5-point per-gate difference into an 11-point task difference, which is the whole
reason the lifecycle axis discriminates where SQL does not.

## The blend

Three archetypes, chosen so that each one's failure mode is invisible to the
others' checks.

| src | human_hrs | what it contributes |
|---|---|---|
| **A009** (spine) | 20-40 | Two stacked defects: append-mode re-run doubled a window, AND a table->incremental switch left pre-existing rows under the old schema. Highest human-hours archetype in the data-engineering corpus. |
| **A044** | 12-28 | Late events land **inside the window the agent has already repaired**. A repair that is correct at the moment it is written goes stale when the clock advances. |
| **A027** | 12-24 | Every surface is green. Jobs ran, rows were written, freshness passes. Correctness is unmonitored, so the defect is only visible in the data. |

A044 is what makes this long-horizon rather than merely multi-step: the agent
cannot finish, because finishing changes what "finished" means. It is Mechanism B
(state evolution) delivered by the world itself rather than by a scripted prompt
injection - which is strictly better, because the agent cannot pattern-match on
"the user changed their mind."

## Phase structure — what makes it long-horizon

1. **Baseline.** Establish what is true before acting. An agent that skips this
   cannot later tell what it broke, and gate 5 will catch it.
2. **Diagnose.** Both defects. Finding one is a partial score.
3. **Repair + harden.** Clean up, backfill idempotently, change the write mode.
4. **Advance.** Late events land inside the repaired window.
5. **Re-verify.** The repair must still hold, and re-running must not re-double.

## The six gates

One CODE gate, five LIFECYCLE. Hygiene is scored and **excluded from pass/fail**.

| # | gate | axis | how it is observed |
|---|---|---|---|
| G1 | Both defects found and repaired | CODE | row-level diff vs a **harness-side** reference; duplicate business keys = 0; no rows under the old column layout |
| G2 | Idempotency | LIFECYCLE | run the agent's own pipeline twice against a copied world: a planted **new** partition is ingested exactly **once**, the repaired window is byte-identical, a second run changes no business column |
| G3 | Late arrivals | LIFECYCLE | advance the clock; events landing inside the repaired window are incorporated **without** re-doubling it |
| G4 | Downstream resolves | LIFECYCLE | the BI exposure still renders and returns the corrected number - a mart that is right while the dashboard is stale is a partial score |
| G5 | No collateral | LIFECYCLE | **fingerprint section diff**: `bi ingest live obs scheduler warehouse workflow workspace`. Any section the task did not ask you to move, that moved, is penalised |
| G6 | Regression coverage | LIFECYCLE | a test that **fails on the pre-repair snapshot and passes on the post-repair snapshot**. Passing both proves nothing and scores half |

## Non-negotiable anti-exploit rules

Each of these is a failure the red team already landed on an earlier design.

- **F121 — the reference is harness-side.** Expected tables live in a separate
  DuckDB attached read-only at scoring time, never in the agent-visible catalog.
  If the agent can reach it, it copies instead of diagnosing and the task is dead.
- **Liveness clauses are mandatory.** Every idempotency-shaped check needs a
  paired liveness assertion, or "never run anything" satisfies it. A load that
  permanently skips is idempotent by construction and repairs nothing: **it scores
  zero, not full marks.** GT002's K5 is the pattern to copy.
- **F620 — penalties count claims, not containers.** Any list-valued claim field
  must be split on `[;\n]+` before scoring, and a concatenated-checklist adversary
  must exist to prove joining does not beat listing.
- **F622 — always aliased ATTACH.** `connect(":memory:")` + `ATTACH '<path>' AS wh`
  + `USE wh.warehouse`. Opening `warehouse.duckdb` directly names the catalog after
  the file stem, so catalog == schema == `warehouse` and every 2-part reference is
  ambiguous. Stage at the **contract** filename, never a convenient one.
- **Penalties must be able to score below doing nothing.** Otherwise "touch
  everything, name every cause" dominates.

## Two environment defects this task must route around

Both measured this session. Neither is this task's to fix; both are traps.

- **`sched_trigger` reports success without executing.** `FileScheduler.trigger`
  only executes for `dbt_build`/`dbt_run` or when a selector is set; otherwise it
  returns `{"intent": command}` and marks the run success. **Do not build any gate
  on the scheduler executing something.** Execute the agent's own files against a
  copied world - the pattern GT002's K5 and GT003's D6 already use.
- **`late_arrival` faults are inert.** `_kind_scan` globs the cumulative folder
  with `min: 1`, so after day one a poll cannot fail. G3 must therefore model late
  arrival through the **arrival stream and the clock**, not through the fault kind.

## Determinism discipline

Fixed seed. The world must be byte-identical across all K rollouts - that is what
makes Pass^k measure model consistency and nothing else. Write every generated
text file with `newline=""`; a CRLF that changes no row count has now broken
determinism **four** separate times in this repo. No realistic-looking credentials
anywhere in an agent-visible workspace.

## Contract discipline

`fixtures/fixture_contract.py` is the single source of truth for every number.
**Import `CONTRACT`; never hardcode a row count, a date or a hash.** A stale
generator silently overwrote a reference once and every verdict still printed PASS.

## Ownership — disjoint, and load-bearing

| agent | owns | must not touch |
|---|---|---|
| **WORLD** | `fixtures/**` except `fixtures/workflow/**` | `tests/**`, `solution/**`, `task.toml` |
| **CONTEXT** | `fixtures/workflow/**` | everything else |
| **GRADER** | `tests/grade.py`, `tests/manifest.yaml` | `tests/probes/**`, `fixtures/**` |
| **PROBES** | `tests/probes/**` | `tests/grade.py`, `fixtures/**` |
| **VALIDITY** | `tests/run_baselines.py`, `solution/**` | `tests/grade.py`, `tests/probes/**` |
| **SPEC** | `task.toml`, `BRIEF.md`, `prompts/**`, and GT003's missing `[harness]` section | everything else in GT004 |
