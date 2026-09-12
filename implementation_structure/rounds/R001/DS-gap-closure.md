# R001 — data-science coverage gap closure

**Date:** 2026-09-12 · **Trigger:** code-review finding 11 · **Author:** lead agent (not a
dispatched strategy agent — see *Provenance caveat* below)

---

## The gap

The high-effort code review found that data science held **4 of 24** archetype slots, and
that the largest single source cluster in the corpus — MLE-Bench methodology, S100–S110,
10 sources from L2 — had produced **zero** archetypes. Overall only 63 of 162 sources were
cited by any archetype.

The four existing DS archetypes were also tightly clustered. A005, A012, A013 and A020 are
all *diagnose a broken deployed model*: training-serving skew, leakage audit, distribution
shift, demand shift. Nothing covered defining a problem, choosing an operating point,
reproducing a result, validating an evaluation set, or delivering an artefact under
constraint.

## What was added

Five archetypes, A031–A035. Each derives from previously-orphaned sources.

| id | title | pts | hours | derived from |
|---|---|---|---|---|
| A031 | Define the problem, cohort, label and metric from a vague stakeholder ask | 26 | 25–45 | S110, S118, S125, S108 |
| A032 | Operating-point selection under base-rate shift and a fixed alert budget | 24 | 20–40 | S113, S123, S118 |
| A033 | Reproducibility repair of a notebook-driven model result | 22 | 15–30 | S122, S106, S121 |
| A034 | Holdout reconstruction and contamination audit | 25 | 20–40 | S101, S102, S109, S111 |
| A035 | Deliver a gradeable artefact under hard compute and disk ceilings | 20 | 15–30 | S105, S106, S103 |

**Result:** 23 → 28 archetypes; data science 4 → 9; sources cited 63 → 73 of 162.
Discipline split is now 12 DE / 9 DS / 7 analytics.

## Why these five

A031 is the strongest single differentiator available on this axis, because **MLE-Bench names
it as its own gap**: Kaggle hands the agent a clean problem statement, a documented dataset
and a fixed metric, while the real work is deciding what those should be (S110). No surveyed
benchmark grades problem definition.

A034 turns MLE-Bench's own *construction* methodology into a graded task — they had to
reconstruct a holdout and prove distributional equivalence via a known-baseline comparison
(S101, S102); no benchmark asks an agent to do it.

A032 grades the distinction between model quality and deployment usefulness — a model with
acceptable discrimination that is useless at its deployed threshold (S113), compounded by an
alert budget (S123). A single scalar metric on a fixed split, which is all MLE-Bench can
express, cannot represent this constraint at all.

A033 and A035 target the failures that *actually dominate* agent performance rather than the
ones benchmarks are designed around: no gradeable submission was the modal outcome across
every MLE-Bench scaffold — valid-submission rates of 82.8 / 54.9 / 27.3 percent — and all
three scaffolds exhausted container disk or RAM mid-run (S105, S106).

## Red-team lessons applied at authoring time

These were written *after* the phase-5 attacks landed, so the known defects are designed out
rather than discovered later:

- **Every grading reference is harness-side** (F121). Manifests, evaluation labels and
  reference baselines live outside the agent-visible catalog and attach read-only at scoring
  time. No archetype here seeds its answer key into the world.
- **No famous-incident re-enactment** (F101). A032 preserves the operating-point mechanism
  from S113 but uses a synthetic domain with no recognisable fingerprints, because
  re-enacting a widely-published incident is a training-data-leakage failure.
- **Checks that require the artefact to actually run**, not just inspection of final state —
  A033's two clean-room re-executions and A035's input-permutation probe are the same shape
  as the planted-partition idempotency check from F121: prove the thing works, don't accept
  a no-op.
- **Property-based grading where multiple answers are legitimate.** A031 scores the *cohort
  and label properties* against hidden manifests rather than equality to one answer key,
  because several definitions are genuinely defensible.
- **Per-cause partial credit** on A033 and A034, per the dense-reward requirement.

## Provenance caveat — read before treating these as derived

**These five were authored by the lead agent, not by blind-dispatched strategy agents.** The
phase-3 diversity property does not hold for them: they were written with full knowledge of
the existing corpus and of the phase-5 attack records. That makes them *less* likely to
duplicate and *more* likely to share the author's blind spots.

Consequences, all recorded in each file's `notes`:

1. They have **not been red-teamed**. R1 (leakage), R2 (replication) and R3 (difficulty) must
   run against A031–A035 before they count as derived. Every other archetype in the corpus has
   attack records; these have none.
2. **A034 overlaps A012** at the mechanism level — A012 asks whether a *model* is leaking,
   A034 whether the *evaluation* is trustworthy and demands a constructive rebuild plus an
   equivalence proof. R3 should rule on whether that distinction survives a strong model.
3. **A035 sits at the bottom of the E5 band.** 15–30 hours is the lowest of the five and the
   meeting specified "tens of hours"; R3 should rule on whether it clears the bar or merges
   into a longer task.

## The three sources deliberately left uncited

S100, S104 and S107 remain orphaned **by decision, recorded as C500**. They are
benchmark-construction methodology, not practitioner incidents: what MLE-Bench is, its
automated rule-enforcement detectors, and its human-hours annotation scheme. They inform our
process — admissibility screening, difficulty labelling, grader calibration — not any single
task.

S104 carries a finding worth acting on elsewhere: MLE-Bench's GPT-4o log-analysis rule
checker flagged up to 14.1 percent of medal runs and **was wrong every single time**. Our
process/governance axis is a headline differentiator, and that is direct evidence it needs a
published false-positive rate and a human-review escape hatch before it can carry that weight.

## Still open

- **89 of 162 sources remain uncited** by any archetype. Much of that is legitimate — L4's
  prior-art findings and L5's pricing findings were never meant to become tasks — but it has
  not been audited cluster by cluster, so the real coverage number is unknown.
- Corpus is at **28 of the 30 G3 ceiling**. Any further additions need a withdrawal or a
  raised ceiling, which is a deliberate decision rather than a default.
- **R4 has still not run**; semantic citation auditing remains absent from phase 5.
