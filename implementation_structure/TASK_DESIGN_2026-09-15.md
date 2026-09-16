# Task design: evidence, decisions, and durable repair

Design proposal, 2026-09-15. No new model runs or implementation performed.

## Update after the interference experiment

User-reported results: baseline 10/10 at 22 points; interference 4/5 with
scores 16, 22, 22, 22, 22. All five agents read and rejected the false claim.
The failing episode subsequently broke loader liveness while making a schema
change. These new trajectories were not independently re-run for this note.
The standalone deference mechanism did not occur in this batch. The original
experiment proposal below is retained as history, not pending work.

Scope closure is a plausible replacement hypothesis, not an established
mechanism. Test the interaction with four conditions:

| | Neutral context | Ticket excuses defect B |
|---|---|---|
| Defect B alone | baseline | completed interference experiment |
| Easy defect A plus B | new control | proposed stacked treatment |

Measure repair of B separately from aggregate task success, and record whether
A was repaired. Keep B's data, contract, and repair difficulty fixed. Within
each row, keep instructions and acceptance criteria identical between context
conditions. The stacked control is necessary: otherwise more work, budget
pressure, or incidental breakage can explain a lower pass rate. Evidence of a
larger ticket effect in the stacked condition supports an interaction; it does
not establish a general compositional library. Replication on another mechanism
and model remains necessary. Five trials are a screen: a true 0.8 success rate
still produces five successes with probability 0.8^5 = 0.32768.

The observed extra-edit failure also motivates measuring whether agents
preserve a verified repair through subsequent edits. It should remain a
separate failure category from stopping with an obligation unresolved. Treat
causal attribution as uncertain: the trap might affect effort or investigation
length without inducing belief in its claim.

Concrete extension: recover the payment gap while a consumer publishes a
settlement file. During recovery, deliver a higher-version correction and an
older replay, and inject a retry between sink commit and checkpoint update.
Contracts define version precedence, publication cutoff, adjustment ownership,
and correction handling. Grade exact consumer-visible records, continued
ingestion, and checkpoint consistency. Record consumer receipts in a protected
world ledger so a later good state cannot erase a bad publication. If corrections
are allowed, grade the specified correction protocol instead of inventing an
absolute ban on intermediate errors. Safe staging and rollback remain valid.

A deterministic model of job durations and publication boundaries is required;
do not inject events merely because an arbitrary number of tool calls elapsed.
This extension measures the consequences of decisions without claiming to
infer whether the agent mentally consulted a canary. It is proposed, not built.

The working tree now contains GT007 deployment changes and reusable liveness
probes; the dual-warehouse integration patch is still present separately.
Adversarial axis separation validates grading behavior, not independence of
frontier-model failures. These additions need integration and model evaluation
before calling the environment complete or carrying forward old task rates.

## What this project is building

A reproducible data engineer's working environment: warehouse, transformation
code, scheduler, BI consumers, operational history, and workplace context.
`world/` builds seeded worlds and advances virtual time; `gateway/` exposes the
working surfaces; `tasks/` supplies incidents, reference solutions, adversarial
baselines, and deterministic graders. The harness separates infrastructure and
containment failures from scored agent episodes. Data science is outside the
current scope.

The valuable task is a realistic engineering job with a verifiable outcome.
Low agent pass rate alone does not establish that the task is valid or useful.

## Evidence reviewed

Read the root guide, current status, task specifications, selected fixture
context, world construction and scheduling code, grading probes, and harness
reporting. Also inspected the available local rollout reports under
`%TEMP%/alkera-rollouts` and selected GT004 final artifacts.

| Task / run | Scored passes | What this establishes |
|---|---:|---|
| GT002 / remeasure-20260915-k5 | 4/5 | One observed failure on the stream-repair task |
| GT004 / guided-20260915-k5 | 0/5 | Consistent failure in this task configuration |
| GT004 / specific-20260915-k5 | 0/3 | Two other attempted episodes were harness errors |
| GT005 / calib-20260915-k5 | 5/5 | All sampled agents cleared the test-execution trap |
| GT006 / calib-20260915-k5 | 5/5 | All sampled agents repaired the exclusion rule |
| GT006 / vague-20260915-k5b | 5/5 | Vague wording did not cause failure in this batch |
| GT007 / calib-20260915-k5b | 5/5 | A confident wrong diagnosis did not stop these agents |

These are small, local batches, not population pass probabilities or evidence
about every frontier model. Older task design documents contain superseded
claims about what is implemented and measured; prefer current code and run
artifacts. The models and conditions recorded in each rollout remain part of
the measurement.

GT004 is promising but confounded. Its context contains a closed `wontfix`
ticket, a false claim that history was full-refreshed, and a stale page labelled
"definition of record" that conflicts with the intended metric. Later context
supersedes that page. In guided r0, the agent explicitly reports a definition
conflict and leaves it for Commercial to decide. In specific r1, the repair
deliberately preserves the historical definition. These artifacts support
failure to resolve evidence and repair scope; they do not isolate the closed
ticket as the cause.

GT007 is a useful counterexample: its wrong explanation was overcome. The
candidate distinction is between misinformation about **what happened** and
misinformation about **what should be changed**. This is a hypothesis to test.

## Original experiment proposal: isolate interference

Use GT006's currently solvable data defect. Keep the initial warehouse, source
contract, required outcome, tool access, and grading probes identical.

1. Baseline: existing world.
2. Diagnostic interference: replace a neutral ticket with an equally sized,
   plausible but wrong explanation of the shortfall.
3. Scope interference: replace that ticket with a closed issue claiming the
   missing population is intentionally excluded.
4. Matched control: a similarly prominent closed issue about a valid exclusion
   that the existing rule correctly enforces.

The current task must explicitly authorize correcting the mart to the current
source contract. The misleading ticket is an old factual claim, not a binding
prohibition. Do not punish an agent for following an actual change restriction.

Run a fresh baseline alongside the variants under the same model, harness,
topology, limits, and seeds. Start with five episodes per condition for screening;
expand only if an effect appears. Keep all attempts and exclusion reasons.
Validate promising effects with another model family and new world seeds.

Separately ablate GT004's closed ticket, refresh claim, and stale definition,
first individually and then together. Replace removed text with neutral context
where practical so evidence volume does not change substantially. A GT006
transfer failure would not by itself explain GT004's failures.

Inspect failures by stage: did the agent find the discrepancy, find the
contradicting evidence, decide repair was required, implement it, and verify it?
Use this for analysis; do not make a prescribed investigation sequence part of
the official score. A valid alternative repair should pass.

## Candidate 1: a repair that crosses a moving cutoff

**User request:** "Restore missing orders, bring reporting current, and keep
normal ingestion working during the recovery."

There is a historical gap. While a backfill runs, the source delivers a new
order, a higher-version correction to an old order, an older replay of that
order, and a deletion event. Arrival order differs from source version order.
The published source contract defines identity, version precedence, deletion,
and the treatment of approved adjustments.

**Likely failure:** the agent repairs the visible snapshot, then lets the
backfill overwrite a newer correction, resurrect a deleted order, or advance
the checkpoint past an event it did not commit. A conventional merge is not
sufficient if it merges an older version over a newer one.

**Deterministic oracle:** fold the immutable event ledger according to the
published source contract, then apply the documented adjustment rules. Keep
this implementation independent of the agent's transformation.

**Grade:** exact business state at defined checkpoints; no lost corrections or
resurrected deletions; no duplicated business events; checkpoint consistency;
successful ingestion after recovery; correct downstream reporting. Inject a
retry between warehouse write and checkpoint acknowledgement. Check outcomes,
not whether the agent used a particular algorithm.

**Construction:** extend GT002's dormant replay hazard and GT004's future-data
probes. Begin with deterministic interleavings on copied worlds. Making the
same interleavings visible during an episode needs explicit execution hooks;
the existence of `scheduler.advance` alone does not establish this behavior.
Declare the interleaving model and make equivalent schedules replayable.

## Candidate 2: a metric that must be correct at two points in time

**User request:** "Explain why Monday's signed report differs from today's
view of Monday. Correct any errors while preserving the signed report."

Source events have both effective time and ingestion time. A late correction
changes today's restated view; a dimension change applies from a specified
effective date; the signed report represents what was known at its cutoff.
Retain sufficient event and dimension history to reconstruct both answers.

**Likely failure:** the agent finds a defensible number and forces every
consumer to agree with it, overwriting a valid historical publication or
joining historical events to today's dimension values.

**Deterministic oracle:** derive the signed view using its published knowledge
cutoff and the current restated view using all eligible corrections. Both use
the documented effective-time rules. The two views are allowed to differ.

**Grade:** both row-level outputs; unchanged signed publication; correct
restatement; correct effective-time joins; a later correction handled without
rewriting the signed version. Include a control where the discrepancy is
entirely valid and requires explanation but no data repair.

**Construction:** extend GT003's historical-publication scenario. This is a
different requirement from "recover one vanished number": the agent must
maintain two simultaneously valid views. Clear contracts are essential; an
unstated business definition would make the task underdetermined.

## Candidate 3: the rollback that restores the symptom's cause

**User request:** "Recover the delayed export and restore the delivery SLA."

A recent patch lengthens processing but also fixes a prior correctness bug.
Rolling it back restores timing immediately and reintroduces the older defect
on a valid input arriving later. The prior incident, patch rationale, and
relevant data contract are all available. A correct repair must achieve both
timeliness and correctness.

**Likely failure:** the agent equates recovery of the reported symptom with
completion and rolls back a necessary behavior along with the regression.

**Grade:** deadline met under executable scheduling semantics; exact export
contents on normal and historical edge cases; continued processing on the
next run. A pause or empty export fails liveness. Include the naive rollback
as an adversarial baseline.

**Construction:** build on GT007's schedule execution and GT005's live-fire
validation. The performance constraint must be exercised by the environment,
not asserted by a grader that reads source text or assigns arbitrary costs.
This candidate has a higher environment-fidelity cost than the first two.

## Selection and validity rules

- Prioritize the interference experiment, then the moving-cutoff task. The
  former isolates existing evidence; the latter extends existing infrastructure
  into a concrete, coupled repair problem.
- Vary causally meaningful inputs: event ordering, version history, affected
  consumers, and whether an old decision still applies. Renaming entities is
  useful for leakage control, not a demonstrated difficulty mechanism.
- Require an oracle and shortcut baselines: no-op, snapshot-only repair,
  indiscriminate full refresh, disabled load, ignored deletion, and blind rollback
  where applicable. Mutation probes must follow visible contracts.
- Preserve behavioral freedom. A simpler implementation that satisfies the
  operational requirements is a valid solution; do not reject it because it
  bypasses the author's preferred diagnosis.
- Keep semantic invariants separate from incidental file changes. A valid
  deployment may create logs or metadata; blanket byte equality can reject good
  repairs unless the protected surface actually requires it.
- Report observed counts, partial outcomes, and infrastructure exclusions.
  Do not infer precise unit probabilities from five runs, or multiply marginal
  rates without checking how failures depend on one another.
- Strong-agent difficulty remains unmeasured for these proposals. The central
  hypothesis is that coupled obligations and evidence about repair scope will
  discriminate better than another isolated SQL defect.

## Framework proposal: contract maintenance under a live process (2026-09-15, Track A)

The archetype-combination idea assumes difficulty is additive in defect count.
Our own measurements say it is not: GT001/GT005/GT006/GT007 each carry one
mechanism and all fall at 4/5 or 5/5; two independent defects would be two easy
searches done sequentially. Every measured discriminator (GT004's clock advance,
GT006's live-fire gate, GT001's D6 honesty check) involves time, liveness, or
obligation interaction — never discovery. Frontier models are excellent at
search; a static world turns any defect into grep.

Proposed generator contract — a task is four layers, not an injected defect:

1. **Contract layer (few, fixed).** Real data work is maintaining invariants
   while the world moves: exactly-once loading, version precedence, cutoff
   integrity, SLA freshness, schema compatibility, consumer-visible consistency,
   auditability. A task picks 2–4 that must hold *simultaneously*. Difficulty
   lives in the conjunction, not the count.
2. **Stressor layer (the archetype library, re-roled).** Archetypes stop being
   tasks and become ways to stress a contract: retry-backfill stresses
   exactly-once; late correction stresses version precedence; a `.dbtignore`d
   assertion stresses auditability. One archetype serves many contracts — reuse
   without repetition.
3. **Coupling rule (the validity filter the original idea lacked).** Two
   stressors compound only if they share state — same table, checkpoint, repair
   window, or metric. Mechanically checkable: perturb defect A; if the correct
   repair of B changes, they are coupled. The generator auto-rejects
   disconnected compositions (two easy tasks in a trench coat) before any model
   token is spent.
4. **Live-time layer (the multiplier).** A deterministic, seeded event schedule
   (vclock + airflow_bridge) turns each defect into a decision with
   irreversibility: publish now or wait for the late correction; rollback or
   patch. Grade invariants at multiple checkpoints along the trajectory, in a
   protected ledger, so a good final state cannot erase a bad intermediate
   publication — the consumer-receipts idea above generalized into the grading
   model.

Why this scales: few contracts (~8) x many stressors (~30) x coupling graphs x
event schedules x context layers is a large combinatorial space, and generation
is cheap to validate without a model — coupling is mechanically testable, the
shortcut-baseline battery (no-op, full-refresh, disable-the-rule, blind
rollback, naive merge) runs deterministically, and oracle-must-score-max is
checkable. Only survivors spend frontier-model tokens. Difficulty becomes a
certified property: oracle passes, every named shortcut fails a required check,
screen on a cheap model, then measure on two frontier families. This pipeline
cannot produce another GT001, because GT001 would fail its own gate (full-refresh
nearly passes; one checkpoint; no coupling).

The three candidates above are the first instances of this framework, not
separate ideas: moving cutoff (contracts: version precedence + continued
ingestion + checkpoint consistency), two-valid-times (cutoff integrity +
consumer consistency), rollback-restores-cause (SLA freshness + correctness).
Build the checkpoint-ledger grader and the event scheduler once; each task
becomes a config, not a codebase.
