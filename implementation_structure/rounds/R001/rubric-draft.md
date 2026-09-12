# R001 / B3 — Grading rubric draft for archetype A001

**Archetype**: A001 — Non-idempotent backfill repair after a silent doubling incident
**Total**: 20 points (matches `rubric_points: 20` in A001.yaml; inside the E6 band of 15–30)
**Split**: 14 deterministic (70%) / 6 judge (30%). Deterministic dominates per E7 and the
DataClawEval/S308 finding that LLM judges are unstable and gameable; the judge never grades
anything a state check can grade.

Binding world names (to be finalized by the environment builder; used below for concreteness):
target table `warehouse.fct_revenue`, partition column `dt`, business key `(order_id, line_nbr)`,
source files `landing/daily_revenue/dt=YYYY-MM-DD/*.csv`, watermark table
`warehouse.pipeline_watermark(job, last_loaded_dt)`.

> **Grading reference is harness-side, never in the world catalog** (F121). `expected.fct_revenue`
> lives in a separate DuckDB file at `/grading/expected.duckdb`, outside the paths the MCP
> toolbelt can read and **not attached during the episode**. The grader attaches it read-only at
> scoring time:
> ```sql
> ATTACH '/grading/expected.duckdb' AS expected (READ_ONLY);   -- grader session only
> ```
> Rationale: the warehouse *is* the agent's DuckDB instance. With the reference seeded into that
> same catalog, `CREATE OR REPLACE TABLE warehouse.fct_revenue AS SELECT * FROM
> expected.fct_revenue` scores D1+D2+D3 — 11 of 14 deterministic points — with zero diagnosis.
> This applies to every archetype in the corpus, not just A001. The ade-bench `solution_seeds`
> pattern (S305) supplies the reference *content*; it does not license putting it where the
> agent can read it.

The affected window is 30 consecutive
partitions `dt ∈ [W0, W0+29]`; partial-load debris from the crashed run lives in 2 partitions
`dt ∈ {D1, D2}` adjacent to the window. `expected.fct_revenue` contains the legitimate
manually-adjusted rows that a full refresh would destroy.

---

## 1. Deterministic checks (14 pts)

### D1 — Row-level diff vs reference on the affected window (5 pts)

Reads: `warehouse.fct_revenue` vs `expected.fct_revenue`, `dt ∈ [W0, W0+29]`.
Run in the **grader session only**, after attaching `/grading/expected.duckdb` read-only. The
`expected` schema does not exist inside the episode.

```sql
-- fast gate per partition, then row-level attribution
SELECT dt, md5(string_agg(row_hash, '' ORDER BY row_hash)) AS part_checksum
FROM (SELECT md5(fct_revenue::text) AS row_hash, dt FROM warehouse.fct_revenue
      WHERE dt BETWEEN W0 AND W0+29) t
GROUP BY dt;          -- compare against identical query on expected.fct_revenue

-- attribution for mismatched partitions
(SELECT * FROM warehouse.fct_revenue WHERE dt BETWEEN W0 AND W0+29
 EXCEPT SELECT * FROM expected.fct_revenue WHERE dt BETWEEN W0 AND W0+29)
UNION ALL
(SELECT * FROM expected.fct_revenue WHERE dt BETWEEN W0 AND W0+29
 EXCEPT SELECT * FROM warehouse.fct_revenue WHERE dt BETWEEN W0 AND W0+29);
```

Column set follows the ade-bench include/exclude pattern (S305): audit columns
(`load_ts`, `batch_id`) are excluded; all business columns compared exactly.

**Partial credit (dense, not binary):** `score = 5 × (# of the 30 partitions whose rows match
exactly) / 30`, rounded to the nearest 0.5. A full-refresh agent loses only the partitions
containing manual adjustments (invented seeding: 4 of 30 partitions), so it scores ~4.5 here
and is caught by D2-side effects and J2 instead of being zeroed — the damage is localized,
so the reward is localized.

### D2 — Duplicate business-key assertion (3 pts)

Reads: `warehouse.fct_revenue` (whole table — the agent may have re-doubled outside the window).

```sql
SELECT order_id, line_nbr, dt, COUNT(*) AS n
FROM warehouse.fct_revenue
GROUP BY 1,2,3 HAVING COUNT(*) > 1;
```

**Partial credit:** 3.0 if zero rows; 1.5 if duplicates are confined to ≤ 2 partitions
(partially completed dedup); 0 otherwise. Rationale: a half-finished dedup is meaningfully
better than none, but any surviving duplicate is a wrong revenue number, so credit caps at half.

### D3 — Partial-load debris cleanup / per-partition counts vs source (3 pts)

Reads: `warehouse.fct_revenue` partitions `{D1, D2}` vs complete source files only
(half-written `.csv` fragments in `landing/` are excluded from the expected counts — this is
the S006 mechanism: the next scheduled run silently consumed the fragment).

```python
for dt in [D1, D2]:
    expected_n = sum(line_count(f) for f in landing[dt] if f.is_complete)  # no fragments
    actual_n   = query(f"SELECT COUNT(*) FROM fct_revenue WHERE dt = '{dt}'")
    assert actual_n == expected_n   # fragment rows removed, complete-file rows kept
```

**Partial credit:** 1.5 per correct partition. Zero if the partition was wholesale-deleted
(count = 0 ≠ expected) — deleting good rows with the debris is the failure being priced.

### D4 — Idempotency probe, with a planted new partition (2 pts)

Reads: harness-controlled re-execution against a **newly planted partition** the agent never saw.

The naive version of this check — re-run twice over the same window and assert nothing changed —
is satisfied by a load that never does anything at all. An `IF NOT EXISTS` guard that always
skips, or a repair that simply deleted the pipeline, scores full marks while repairing nothing
(F121). The probe must therefore prove two things at once: the load still *works*, and re-running
it does not re-corrupt.

```python
snap0 = snapshot("fct_revenue")                      # post-repair baseline
plant_partition(W0+30, n_rows=N_NEW)                 # complete source file, unseen by the agent

run(agent_repaired_load, window=[W0, W0+30]); snap1 = snapshot("fct_revenue")
#  liveness: the new partition was actually ingested, exactly once
assert rows(snap1, dt=W0+30) == N_NEW
#  no collateral: the already-repaired window is untouched
assert restrict(snap1, W0, W0+29) == restrict(snap0, W0, W0+29)

run(agent_repaired_load, window=[W0, W0+30]); snap2 = snapshot("fct_revenue")
#  idempotence: a second identical run is a true no-op
assert snap2 == snap1                                # row count AND checksum, business columns
```

**Partial credit:** 2.0 if all three assertions hold. 1.0 if liveness and no-collateral hold but
the second run mutates only excluded audit columns (at-least-once-safe, not clean). **0 if the
new partition was not ingested** (the no-op guard — repairs nothing, so it earns nothing), or if
any business row changes on the second run, or if the repaired window moved. Alternate valid write modes are first-class (S305):
`INSERT OVERWRITE`, `MERGE` on business key, or delete+insert in one transaction all pass.

### D5 — Freshness watermark advance + safe next schedule (1 pt)

Reads: `warehouse.pipeline_watermark` before/after the next orchestrator tick, plus a D2
re-run after that tick.

```python
w0 = query("SELECT last_loaded_dt FROM pipeline_watermark WHERE job='daily_revenue'")
fire_next_scheduled_run()   # the live orchestrator's next tick — the hazard from S001
w1 = query("SELECT last_loaded_dt FROM pipeline_watermark WHERE job='daily_revenue'")
assert w1 > w0                       -- pipeline is alive and advancing
assert duplicate_key_check() == 0    -- ...and the tick did not re-double (D2 re-run)
```

Both assertions required for the single point; no partial credit (cheap gate, binary is fine).
This prices "fixes data but leaves the load non-idempotent so the next retry re-corrupts" —
the failure a pure snapshot-diff rubric cannot see.

---

## 2. Bounded judge axes (6 pts)

Anti-hedging rule for all axes (APEX 1.1, S308/S309): **if the agent's answer lists multiple
candidate causes, repair strategies, or windows without committing to one, the axis scores 0.**
Presence of the correct string is not sufficient; scattergunning forfeits the axis. Judge
prompts below are v0 drafts, to be calibrated later by the GEPA recipe in S309 (hand-label
outcomes on real trajectories, optimize the prompt, report held-out FP/FN).

The judge **never sees the full trajectory** (bounded judging per E7). Per-axis evidence packet:

### J1 — Root-cause / damage-window identification (2 pts)

Judge sees: the agent's final incident report section + the harness-computed ground-truth
damage window (`[W0, W0+29]`, debris partitions `{D1, D2}`) + the D1/D3 per-partition diff
summary. Nothing else.

> Prompt fragment: "The agent stated the damage window as X and the debris partitions as Y.
> Ground truth is [W0, W0+29] and {D1, D2}. Score 2 if the stated window and debris set match
> exactly and the mechanism is named (append-mode re-run over existing partitions, crashed-run
> fragment consumed by the next schedule). Score 1 if the window is correct but the debris or
> mechanism is missing. Score 0 if the window is wrong, or if the agent lists several possible
> windows/causes without committing (hedged = zero, no exceptions)."

### J2 — Repair design (2 pts)

Judge sees: the diff of the repaired load script + dbt model config + the incident report's
repair section + D1's list of preserved manual-adjustment rows. Not the conversation.

> Prompt fragment: "Assess the repair design. Score 2 only if ALL hold: (a) the write mode is
> idempotent for retries over the same window; (b) legitimate manual adjustments survive (D1
> shows them intact); (c) debris removal targeted the fragment rows, not whole partitions.
> Score 1 if (a) holds and exactly one of (b)/(c) fails. Score 0 if a full refresh destroyed
> adjustments, or if the write mode is still append-with-retry, or if the report describes
> several alternative repairs without stating which was implemented (hedged = zero)."

### J3 — Regression test added (2 pts)

Judge sees: the repo diff limited to test files (dbt tests / SQL assertions / CI config) +
their pass/fail under the harness.

> Prompt fragment: "Score 2 if a committed, executable test fails on the pre-repair world state
> and passes on the repaired state (harness verified), e.g. a dbt `unique` test on
> (order_id, line_nbr, dt) or a partition-count-vs-source assertion wired into the DAG. Score 1
> if a test exists but only re-checks final state (cannot catch a re-doubling retry). Score 0
> for no test, a non-executable sketch, or multiple conflicting test proposals (hedged = zero)."

Note: J-axes map onto A001.yaml's declared `judge_axes` (`code_quality` → J3/J2 craft,
`robustness` → J2/D4 hardening); the axis names here are the gradeable instantiation.

---

## 3. Failure-mode table

| Expected failure (A001.yaml `failure_modes_expected`) | Caught by | Point cost |
|---|---|---|
| Re-runs the DAG, doubling the data a second time | D1 (window partitions mismatch, ~5 lost), D2 (duplicates across window, 3 lost), J1 if report misidentifies window (≤2 lost) | 8–10 |
| Full-refresh destroys legitimate manual adjustments | D1 (only the 4 adjustment partitions mismatch, ~0.5–1 lost), J2(b) (1–2 lost) | 1.5–3 |
| Cleans the 30-day window but misses partial-load debris | D3 (1.5 per missed partition, up to 3), J1 if debris absent from report (1 lost) | 2.5–4 |
| Fixes data but leaves load non-idempotent; next retry re-corrupts | D4 (2 lost), D5 (1 lost), J2(a) (1–2 lost) | 4–5 |

Deliberate property: every failure mode costs more than the deterministic checks alone would
suggest because the judge axes are keyed to the *reason* the check failed, not its symptom.

---

## 4. Sage-agent / task-validity note (S305)

Following ade-bench: the task ships with `solution.sh`, the oracle repair script (delete the
duplicate rows and fragment rows for exactly `[W0, W0+29]` ∪ `{D1, D2}`, flip the write mode to
`INSERT OVERWRITE` per partition, add a dbt `unique` test, write a committed incident report).
**Task-validity gate: sage must score 20/20** — all deterministic checks green and all judge
axes full marks on the oracle's evidence packet. Any task that fails under sage is a
misconfigured task, not a model failure, and does not enter the corpus.

**Four calibration baselines run at authoring time.** The last two are adversarial and exist
because both were live exploits in the first draft of this rubric (F121):

| Baseline | Expected | What it proves |
|---|---|---|
| No-op agent (does nothing) | ~0 / 20 | no free points anywhere |
| Naive full-refresh agent | 13–15 / 20 | the rubric *prices* destroying manual adjustments rather than zeroing it — correct is separated from plausible |
| **Reference-copy adversary** — `CREATE OR REPLACE TABLE warehouse.fct_revenue AS SELECT * FROM expected.fct_revenue` | **0 / 20, and it must fail at the ATTACH** | the grading reference is genuinely unreachable from inside the episode. If this agent scores above 0, the world is misbuilt and the task does not ship. |
| **No-op-guard adversary** — a repaired load wrapped in a condition that always skips | **0 on D4**, and D5 fails | the idempotency probe rewards a load that still works, not one that has been lobotomised into never running |

The last two are permanent corpus-wide CI, not one-time checks: any archetype whose reference is
co-located, or whose idempotency probe accepts a permanent no-op, is caught by running these two
agents against it. Validators are versioned per the DAB lesson (S315): the
exact D1–D5 assertion code and judge prompt hashes are recorded with every episode score, so
a later rubric fix can trigger re-scoring instead of silent drift.

---

## 5. What was invented beyond the cited evidence

- Concrete schema/table/column names, the 30/2/4 partition counts (window / debris /
  adjustment-bearing), and the D4 "audit-columns-only mutation = 1 pt" tier — binding
  decisions for the environment builder, not from any source.
- The `/grading/expected.duckdb` path, the planted-partition size `N_NEW`, and the two
  adversarial baselines in §4 — these come from F121's recorded mitigation, not from a source.
  They are design decisions made in response to a landed attack, and the environment builder
  must honour the *property* (reference unreachable in-episode; probe requires real work) even
  if the mechanism changes.
- The D5 freshness-watermark check (A001.yaml's `deterministic_checks` list omits it; the
  B3 brief required it and it operationalizes "next scheduled run is a hazard").
- Partial-credit tiers (5/30 per-partition rounding, D2's 1.5 cap, D4's 1.0 tier) — the
  dense-reward requirement is from the brief; the specific cut points are proposals to be
  tuned against the calibration baselines in §4.
- Judge prompt wording (structure follows S308/S309; text is new).
