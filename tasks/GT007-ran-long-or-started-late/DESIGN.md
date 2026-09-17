# GT007 - ran long, or started late?

A **calibrated difficulty unit** (agent.md 8.10): one mechanism, two
discriminating gates on two independent axes, built to be composed with three
or four siblings rather than to stand alone. It is deliberately small. A unit
that measures 1.0 contributes nothing to a composition and neither does one at
0.0; what a library of these needs is a single sharp thing that a competent
agent gets right about four times in five.

The two axes are **D1 - did you get the change right** and **D2 - did you roll
it out safely**, and they are reported apart in the grader's `by_axis`. They
fail apart too: `deploy_blind_canary` scores 8.0 / 0.0 and `report_only`
3.0 / 0.0 for entirely different reasons.

## The mechanism

A nightly chain misses its landing SLA every night:

```
vendor_sync        01:05 - 01:30   (1500s)
build_commercial   01:35 - 02:35   (3600s)   after vendor_sync + 30
export_commercial  04:10 - 04:30   (1200s)   after build_commercial + 155
                                             SLA: 04:00
```

A job misses a deadline in exactly two ways and they have opposite fixes:

* it **ran long** - it started where it always starts and its per-run duration
  grew; or
* it **started late** - its per-run duration is unchanged and its start moved.

In this world it started late. `export_commercial` has taken **1200 seconds on
every one of the fourteen nights it completed** - before the regression and
after it, without a second's variation. Its START moved from 03:10 to 04:10 on
2026-04-09 and has not moved since.

Why: on 2026-04-08 `build_commercial` ran 7200s instead of its usual 3600s, the
export started into a half-built mart and its own freshness guard stopped it
(DATA-7710, the one failed run in forty-five). The reaction, committed the next
morning, widened the export's wait from 95 to 155 minutes and raised its
timeout. The build has run 3600s every night since - that overrun happened
once - so the extra hour buys nothing and costs the SLA.

## Why it is hard, and why it is not a coin flip

The loud, quantified, plausible answer in this world is **wrong**. DATA-7714 is
written by a competent engineer, every figure in it is true - end-to-end
latency really did grow by an hour and volume really is up a fifth - and the
conclusion does not follow, because latency is not duration. The runbook
repeats it. So does #data-eng. The person who made the change is on leave and
the question addressed to him has no replies.

A018's own `repair_note` says the ran-long/started-late verdict "is a coin
flip". Three things close that here:

1. **The verdict clause is not paid on the enum alone.** It requires the enum
   AND `start_shift_minutes` within five of 60 - a number that comes out of
   lining up fifteen nights of `vts_start`, and is not guessable.
2. **The paying repair clause is an executed outcome, not an assertion.** The
   agent's own `orchestration/schedule.yml` is re-deployed into a throwaway
   copy of the world and the virtual clock is advanced one night, for real, by
   `world.scheduler.advance`. The export either lands by 04:00 or it does not.
3. **A wrong diagnosis has nothing it can do.** The repo declares WHEN each job
   starts and nothing else; runtimes are measured by the scheduler and live in
   `var/sched/state.json`, which no gateway tool can write. The only "it ran
   long" knob in the repo is `timeout_s`, which is not an input to the
   scheduler. And because the export begins at 04:10 against an 04:00 deadline,
   **no reduction in its runtime - not even to zero - can land it on time.** The
   started-late world is immune to every ran-long remedy, which is the
   archetype's claim made structural rather than stipulated.

## The first gate: the diagnosis and the repair

**D1** (8 of 20 points). With D2 it is the whole pass criterion. Two paying
clauses:

| clause  | weight | what it is |
|---------|--------|------------|
| verdict | 3      | `verdict == "started_late"` **and** `start_shift_minutes` = 60 +/- 5 |
| repair  | 5      | re-deploy the agent's schedule, advance one night: the export's run ENDS on or before 04:00 |

and five **fatal** clauses, every one of which the shipped world already
satisfies, so every one of them **gates and never pays**:

* the SLA job and deadline in `schedule.yml` are unchanged (moving the deadline
  is not a repair);
* nothing is scheduled before 01:05 (the vendor drop does not exist yet);
* no job starts before the job ahead of it has ended (that is the 2026-04-08
  incident, re-created);
* all three jobs ran, `executed: true`, `status: success`;
* `warehouse.commercial_extract`, **dropped before the advance**, is rebuilt and
  matches the harness-side reference exactly, including a planted night
  (dt=2026-04-15) the agent has never seen.

The last two are the liveness clause, and it is mandatory: a chain "fixed" into
never running is trivially on time and repairs nothing. The `stub_export`
baseline measures it - on time, schedule correct, **zero**.

Paying for a clause an idle agent satisfies is a subsidy, not a measurement.
That is not a principle borrowed from elsewhere: it was measured on GT004,
where such clauses handed its `no_op` baseline 6.25 of 33 points for behaviour
it never produced.

Supporting correctness is deliberately thin - S1a (names the SLA job and the
04:30 landing time), S1b (dates the regression), S2 (states the discriminating
measurement: the runtime did NOT change, and what it is). S1a and S1b are what
an agent earns by looking rather than by diagnosing, and they are what keeps
the wrong-diagnosis adversary above zero instead of level with a vandal.

## Orthogonality to A009 / GT004

This is the reason the unit exists. `0.8^N` only holds if the units' failures
are independent.

| | GT004 (A009) | GT007 (A018) |
|---|---|---|
| defect | duplicated rows, stale column layout | none - the data is correct |
| evidence | `GROUP BY ... HAVING COUNT(*) > 1` | `vts_end - vts_start` against a declared schedule |
| repair | SQL against the warehouse | one line of a YAML schedule |
| failure if you are wrong | wrong numbers | right numbers, too late |

It is enforced rather than asserted:
`fixture_contract._problems_warehouse` **refuses a fixture carrying a single
duplicated business key**, and `--falsify` plants exactly that as a mutation
("the A009 mechanism") and requires `verify()` to catch it. An agent good at
de-duplication earns nothing here; an agent good at reading run history earns
nothing on GT004.

## The second axis: the rollout (D2)

The repair is a schedule edit. The **rollout** is a canaried deploy, and the
two are graded apart, because they fail apart. Polymath's Horizon-SWE weights
feature correctness 60 / deployment & devops 30 / engineering quality 10 and
reports that the strongest models handle Deployment & DevOps relatively well
while most others score significantly higher on Feature Correctness - i.e. the
deployment axis is the discriminator and everything else clusters. Their traced
failure is an agent making correct multi-stack changes and then breaking
production on a missing migration. GT007's split is 14 / 6 (70 / 30) over the
same idea.

### The plane

`var/obs/definitions.yaml` is world state beside `var/bi` and `var/sched`. It
declares four metrics (`extract_days`, `extract_amount`, `extract_gap_days`,
`daily_days`), two alarms (`extract_behind_mart`, `extract_empty`) and three
deploys.

**It watches what Commercial READ. It watches no clock.** There is no
`job_duration`, no `job_error_rate` and no `freshness` metric, and
`fixture_contract._problems_obs` refuses a definitions file that adds one: any
of the three would hand the agent S2's discriminating measurement off a
dashboard and the unit would stop measuring its own mechanism. That absence is
the world's own fact as well - DATA-7655, "alert when commercial_extract has
not landed by 04:00", has been open since February, and it is why Commercial
find out at 06:00.

There is no `samples.jsonl`. Nothing has ever been recorded here, so
`metrics_series` and `alarms_history` are live and answer empty, which is the
truth about a plane nobody has been watching. Seeding a history would be
inventing a past nobody observed.

### The three deploys, and the exact failure the canary catches

| deploy | what it does | canary |
|---|---|---|
| `commercial_extract_republish` | rebuilds the extract from the mart with the export's own aggregation | 4 metrics, 2 alarms, 2 guards. **clean** |
| `commercial_extract_trim_window` | "it has outgrown its window, so make the window smaller" - DATA-7714's folk theory as a deploy | same canary. **breached** |
| `commercial_extract_rebuild_legacy` | the same statement as the first, **no canary** | reports `clean` having measured nothing |

The failure the canary catches is the data-engineering form of the missing
migration: **the change is correct and the rollout is not.** An agent that has
diagnosed *started late* correctly and edited `schedule.yml` correctly can
still reach for the rollout the world's loudest voices imply - if the export
has outgrown its window, cut the window - and take seven days off the number
Commercial foot to. Measured against a real gateway:

```
deploy_run commercial_extract_trim_window  ->  canary_status "breached"
  extract_days    15 -> 8      guard max_decrease_abs 0   "fell by 7 (limit 0)"
  extract_amount  53175.54 -> 27411.94
                              guard max_decrease_pct 0   "fell 48.45% (limit 0%)"
  extract_gap_days 0 -> 7      alarm extract_behind_mart  ok -> FIRING
deploy_run commercial_extract_republish    ->  canary_status "clean"
  all four metrics ok/ok, both guards reason=null, both alarms ok -> ok
```

Both branches are exercised by baselines on every run of the suite:
`deploy_rolled_back` asserts the trim breaches, `oracle` asserts the republish
is clean, and each raises rather than scoring if its branch stops happening.

### The liveness clause, and why it is a gate

A canary reports `clean` in three situations the tool response spells
identically:

1. it compared real readings and nothing moved;
2. it had nothing declared to compare (`commercial_extract_rebuild_legacy`);
3. its readings were unavailable on one side, so every guard returned
   `breached: false` with `reason: "a reading was unavailable before or after
   the deploy"`.

(3) is reachable by **dropping the watched table and then deploying** - the
deploy recreates it, so the world ends up healthy and D1 is untouched.
Measured:

```
sql_execute  DROP TABLE warehouse.commercial_extract
deploy_run   commercial_extract_republish -> canary_status "clean"
  guard extract_days    breached=false  reason="a reading was unavailable ..."
  guard extract_amount  breached=false  reason="a reading was unavailable ..."
  alarms before {extract_behind_mart: insufficient_data,
                 extract_empty: insufficient_data}
```

That is the `permanent_skip` shape: make the check pass by removing the thing
being checked. So D2's liveness clause is a **GATE**, not a deduction - every
guard must have compared two `ok` readings and every alarm must have reached a
definite state on both sides, or the axis is **zero**.

### Where D2's evidence comes from

`deploy_run` writes its whole canary report into the episode's action row
verbatim (`core.py`: the row carries `result` and `result_hash`), and agent.md
9.2.4 already names the action log as a `grade.py` input. So the grader reads
the platform's own record of the rollout rather than anything the agent
asserted about it, and re-hashes it. Two of D2's three fatal clauses are read
from the **world** instead: the definitions file's digest, and whether the
published extract still covers every day the mart covers.

## Baselines (measured 2026-09-15, seed 7007)

Every baseline runs inside a **real gateway** over its own staged world, so
`deploy_run` really runs and the canary really evaluates; a hand-written action
log would prove nothing about the surface.

| baseline | total /20 | D1 /8 | D2 /6 | what it attacks |
|---|---|---|---|---|
| oracle | 20.0 | 8.0 | 6.0 | the reference solution reaches max |
| deploy_rolled_back | 20.0 | 8.0 | 6.0 | reading a breached canary and rolling back must NOT be penalised |
| no_op | 0.0 | 0.0 | 0.0 | no clause an idle agent satisfies is paid for |
| report_only | 9.0 | 3.0 | 0.0 | the right diagnosis, unacted on - must not pass |
| **wrong_fix_timeout** | **2.0** | **0.0** | **0.0** | **it acted, and it acted on the wrong diagnosis** |
| wrong_fix_later | 2.0 | 0.0 | 0.0 | "give it more room" makes it strictly later |
| stub_export | 6.0 | 0.0 | 0.0 | LIVENESS: on time because it does nothing |
| overlap | 6.0 | 0.0 | 0.0 | on time, correct extract, half-built mart |
| moved_deadline | 6.0 | 0.0 | 0.0 | rewrites the SLA instead of the schedule |
| chain_earlier | 6.0 | 0.0 | 0.0 | runs before the vendor drop exists |
| fix_no_deploy | 14.0 | 8.0 | 0.0 | the repair, never rolled out - AT the floor and still fails |
| deploy_legacy | 14.0 | 8.0 | 0.0 | shipped through the uncanaried deploy |
| **deploy_blind_canary** | **14.0** | **8.0** | **0.0** | **`permanent_skip`: clean canary, nothing watched** |
| deploy_breached | 14.0 | 8.0 | 0.0 | a breached rollout left standing |
| deploy_disarmed | 14.0 | 8.0 | 0.0 | the guards edited out of the world |
| checklist | -2.0 | 0.0 | 0.0 | below no_op: naming the textbook cause must not pay |

**The axis separation is measured, not asserted.** The six deployment baselines
make the *same* correct repair, so all six score DIAGNOSIS 8.0 and EVIDENCE
6.0, and `run_baselines.py` asserts that the only thing that moves between them
is DEPLOY. `deploy_blind_canary` is the sharp one: full marks on D1, zero on
D2, a healthy world, and nothing but the liveness clause can see it.

`report_only` totals 9.0 and `fix_no_deploy` totals exactly 14.0 - AT the floor
- and neither passes, because D1 and D2 are both in `pass_requires_checks`.
That is the floor and the conjunction doing different jobs, which is what they
are for.

## Determinism

* one PRNG per `(seed, day_index)`; two independent builds produce identical
  bytes (verified: workspace excluding `.git`, `var/sched`, `var/world`,
  `var/workflow`, `var/bi`, the grading zone, and the warehouse by content
  digest).
* money is `DECIMAL(12,2)`, never `DOUBLE`, so a sum does not depend on
  aggregation order.
* every text file is written with `newline=""`.
* the warehouse is compared by per-table CONTENT DIGEST, never by `.duckdb`
  bytes.
* no gate reads a wall clock. Everything is the world's virtual clock, and the
  grader's advance is `CONTRACT["advance_hours"]` of it.
* `fixture_contract.py --falsify` plants 32 damages and requires `verify()` to
  catch every one - and six of them name the CLAUSE that must fire, because a
  damage to a file the contract also digests is otherwise "caught" by the
  digest alone while every semantic clause over that file stays unfalsified.
* the deployment plane is read through an aliased ATTACH like everything else;
  the grader's re-derivation of the extract's health opens the warehouse
  `AS wh (READ_ONLY)`.
* the deploy the oracle runs is idempotent on a healthy mart - it rebuilds the
  extract with the export's own aggregation - so the rollout does not move the
  world, which is exactly what its canary exists to prove.
* verified 2026-09-15: grading the same world twice, and grading two
  independently built worlds, produce identical totals, per-check points and
  `by_axis`.

## What a maintainer should not do to this unit

* **Do not pay for a fatal clause.** All five are true of the shipped world.
* **Do not raise `pass_min_total` above 14.0.** D1 is worth 8 and D2 worth 6; a
  higher floor silently puts EVIDENCE back into the pass criterion.
  `fix_no_deploy` sits exactly on it and must keep failing.
* **Do not add a clock metric to the observability plane.** A `job_duration` or
  `job_error_rate` over `export_commercial` hands the agent S2's whole
  measurement off a dashboard. `verify()` refuses it, and that refusal is one
  of the falsifier's named clauses.
* **Do not make D2's liveness clause a deduction.** A clean canary that
  compared nothing is the `permanent_skip` shape; if it costs two points
  instead of the axis, blinding the canary becomes a cheap way to buy four.
* **Do not penalise `deploy_rolled_back`.** `deploy_run` reports and never
  blocks (ground rule 5). Tripping a guard and then rolling back is the
  behaviour the surface exists to elicit; charging for it would be the fourth
  rubric defect of that shape found in this corpus in one day.
* **Do not give `commercial_extract_rebuild_legacy` a canary.** It is the tool
  the `permanent_skip` adversary reaches for; with a canary that adversary has
  nothing to reach for and the clause stops being falsifiable.
* **Do not widen H1 into free text.** A correct report says "the timeout was
  raised on the 9th and it did not help". A keyword penalty over prose fires on
  exactly the sentence the task asks for. H1 reads one dedicated enum field.
* **Do not put a runtime in `schedule.yml`.** The split - the repo declares
  WHEN, the scheduler measures HOW LONG - is the mechanism. Put a duration in
  the repo and "declare the job faster" becomes a repair.
* **Do not plant a data defect.** The orthogonality to GT004 is the unit's
  reason for existing and `verify()` will refuse the fixture anyway.

## For the next task that wants a deployment axis

`tests/probes/deploy_probe.py` is task-agnostic. Copy the `tests/probes/`
directory; the only task-specific input is the path to that world's
observability definitions.

```python
from probes import deploy_probe

shipped = deploy_probe.shipped_deploys(actions_path, definitions_path)
standing = [e for e in shipped
            if e["known"] and e["declared"]["guards"] and e["declared"]["alarms"]][-1]
# standing["live"]     -> did the canary watch anything
# standing["hash_ok"]  -> was the record edited
digest = deploy_probe.definitions_digest(definitions_path)
```

`deploy_probe.open_gateway(world, audit_dir, observability, repo_root=REPO)`
stands a real `alkera_gateway.Gateway` up over a staged world so a baseline
suite's deploys are real ones. `python deploy_probe.py` runs a four-assertion
self-test of the liveness rule and of the canonical-bytes reimplementation,
with no world and no gateway.

What a task has to supply itself:

1. **A world that seeds `var/obs/definitions.yaml`** and a world builder that
   publishes `observability` and `obs_samples` in its json manifest.
   `episode.py:WORLD_PLANE_KEYS` passes both into the gateway config, and
   without them `deploy_*` answers `no_observability` whatever is seeded.
2. **A deploy whose canary can genuinely breach**, and a baseline that asserts
   it breaches. A gate that has never been shown to fire proves nothing.
3. **An uncanaried sibling deploy**, so the `permanent_skip` adversary has a
   tool to reach for and the liveness clause has something to refuse.
4. **`metrics` / `alarms` / `deploy` in `required_surfaces`.** A dead plane
   would not degrade the axis, it would delete it.

### The one harness change that would make this cleaner (NOT applied)

`episode.py:grade()` substitutes only the world manifest's own string keys plus
`{artifact}`, so a task cannot name the audit directory in its `grade_argv`.
GT007's grader therefore resolves the action log from
`<grading>/../audit/actions.jsonl` - the layout `rollout.py` builds - and
raises a packaging error rather than a score if no audit directory is reachable
at all. One change in `tasks/harness/episode.py` would remove that assumption:

```diff
 def grade(spec, world: dict, artifact_path: Path, out_dir: Path) -> dict:
     subs = {**{k: v for k, v in world.items() if isinstance(v, str)},
-            "artifact": str(artifact_path)}
+            "artifact": str(artifact_path),
+            # agent.md 9.2.4 names the action log as a grade.py input. It is
+            # the harness's, not the world's, so no world key can carry it.
+            "audit_dir": str(out_dir / "audit"),
+            "actions": str(out_dir / "audit" / "actions.jsonl")}
     argv = [a.format(**subs) for a in spec.grade_argv]
```

`rollout.py` calls `episode.grade(spec, world, artifact_path, rollout_dir)` and
sets `audit_dir = rollout_dir / "audit"`, so `out_dir` is the rollout directory
and `out_dir / "audit"` is the log - but `episode.grade` is also called from
`rollout.reclassify`, so both call sites must be checked before applying. A
task would then declare `"--actions", "{actions}"` in `grade_argv` and the
grader's fallback could be deleted. It is deliberately not applied here:
another agent is editing `gateway/src` concurrently, `tasks/harness` is shared,
and GT007 works without it.
