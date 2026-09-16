# GT007 - ran long, or started late?

A **calibrated difficulty unit** (agent.md 8.10): one mechanism, one
discriminating gate, built to be composed with three or four siblings rather
than to stand alone. It is deliberately small. A unit that measures 1.0
contributes nothing to a composition and neither does one at 0.0; what a
library of these needs is a single sharp thing that a competent agent gets
right about four times in five.

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

## The one discriminating gate

**D1** (8 of 14 points; the whole pass criterion). Two paying clauses:

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

## Baselines (measured 2026-09-15, seed 7007)

| baseline | total /14 | D1 /8 | what it attacks |
|---|---|---|---|
| oracle | 14.0 | 8.0 PASS | the reference solution reaches max |
| no_op | 0.0 | 0.0 | no clause an idle agent satisfies is paid for |
| report_only | 9.0 | 3.0 | the right diagnosis, unacted on - must not pass |
| **wrong_fix_timeout** | **2.0** | **0.0** | **it acted, and it acted on the wrong diagnosis** |
| wrong_fix_later | 2.0 | 0.0 | "give it more room" makes it strictly later |
| stub_export | 6.0 | 0.0 | LIVENESS: on time because it does nothing |
| overlap | 6.0 | 0.0 | on time, correct extract, half-built mart |
| moved_deadline | 6.0 | 0.0 | rewrites the SLA instead of the schedule |
| chain_earlier | 6.0 | 0.0 | runs before the vendor drop exists |
| checklist | -2.0 | 0.0 | below no_op: naming the textbook cause must not pay |

`report_only` totals 9.0 - above the 8.0 floor - and still does not pass,
because D1 is in `pass_requires_checks`. That is the floor and the conjunction
doing different jobs, which is what they are for.

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
* `fixture_contract.py --falsify` plants 26 damages and requires `verify()` to
  catch every one.

## What a maintainer should not do to this unit

* **Do not pay for a fatal clause.** All five are true of the shipped world.
* **Do not raise `pass_min_total` above 8.0.** D1 is worth exactly 8; a higher
  floor silently puts EVIDENCE back into the pass criterion.
* **Do not widen H1 into free text.** A correct report says "the timeout was
  raised on the 9th and it did not help". A keyword penalty over prose fires on
  exactly the sentence the task asks for. H1 reads one dedicated enum field.
* **Do not put a runtime in `schedule.yml`.** The split - the repo declares
  WHEN, the scheduler measures HOW LONG - is the mechanism. Put a duration in
  the repo and "declare the job faster" becomes a repair.
* **Do not plant a data defect.** The orthogonality to GT004 is the unit's
  reason for existing and `verify()` will refuse the fixture anyway.
