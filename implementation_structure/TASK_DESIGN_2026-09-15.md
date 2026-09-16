# Task design: evidence, decisions, and durable repair

Design proposal, 2026-09-15. No new model runs or implementation performed.

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

## First experiment: isolate interference before making more tasks

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
