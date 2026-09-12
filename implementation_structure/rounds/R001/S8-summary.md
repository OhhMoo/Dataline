# S8 — Initial Conditions (RQ5) — Phase 3 handoff

Wrote 6 claim artifacts to `state/claims/` (C019–C024), all `rq: RQ5`,
`proposed_by: S8-initial-conditions`, `status: proposed`. Verified: all six parse under
`yaml.safe_load` (UTF-8), id matches filename, evidence lists are non-empty `list[str]`,
`attacks: []`. No archetypes written (this role produces claims only).

## What was derived (one line each)

- **C019** — Class (a) generation mechanism: seeded parametric generators (per-column
  distributions, correlations, volume, virtual-clock-aligned time range) + a
  distributional-equivalence calibration gate reusing MLE-bench's reconstructed-test-set
  validation (baseline solution must score within tolerance across sibling seeds).
  Evidence: S102, S109.
- **C020** — The E4 leakage argument as three build-time checks: construction (no
  memorizable answer key, S103), provenance-obfuscation score-stability audit (S109), and
  generator audit for accidental structure (row order, ID monotonicity, encoded lags) since
  Santander's leak beat the best honest model by ~2,178 leaderboard places (S124).
- **C021** — DS leak placement as an explicit generator parameter from the S111 8-mechanism
  taxonomy plus S120's three production-only variants (shared embedding split, serving
  imbalance, label lag); leaks must live in pipeline STATE so trajectory-blind state checks
  can grade detection/quarantine.
- **C022** — Class (c) docs as a contradiction graph with one machine-readable ground-truth
  doc, independent staleness ages, wiki/repo/ticket placement, and a coverage knob — the
  Airbnb/Uber/LinkedIn pattern (S200/S201/S202).
- **C023** — Class (a/c) realism test: brokenness is realistic iff it maps to a documented
  incident AND is silent (green pipeline, wrong state — S002/S004/S005/S022); loud crashes
  are contrived unless sourced from the scheduler/config corpus (S008, S015); justified by
  S003 (freshness monitored, correctness not).
- **C024** — Class (b) fault-injection schedule weighted by surveyed postmortem frequencies
  (schema drift 38% S004, load-spike 24% S006, silent loss 19% S005, cascades 14% S007),
  scheduler-time faults from S008, events as (fault id, virtual fire time, surface, payload)
  in a replayable log, interaction limited to the documented shared-state cascade mechanism.

## Richest evidence

- The 50-postmortem survey (S004–S007) — the only source with *quantified* fault-family
  frequencies; it directly fixes the class (b) distribution weights instead of leaving them
  as taste.
- S111/S120/S124 — leakage is the best-evidenced DS failure family and S124 uniquely
  quantifies what an undetected generator artifact does to task validity.
- S102/S103/S109 (MLE-bench) — ready-made, already-peer-reviewed validation machinery for
  the synthetic-data leakage argument; nothing else in the corpus offers checkable
  anti-contamination procedures.

## Least confident artifact

**C024.** The postmortem percentages come from one blog survey of 50 incidents (single
author, self-selected public postmortems) — treating them as target distribution weights is
a defensible prior, not a measured population parameter. Also, my restriction of fault
interaction to the shared-state cascade mechanism is a design judgment made for determinism
(D3), not something any source prescribes. Second-least-confident: C022's "exactly one
designated ground-truth doc" — S200–S202 show contradiction is realistic but none of them
say the ground truth was discoverable from docs at all (in practice it was resolved by
building a platform); a red-teamer could argue coverage should sometimes be zero.

## Gaps flagged for a future pass

- No corpus evidence on how to synthesize realistic *text* (doc prose, ticket threads) —
  S200–S202 evidence the contradiction structure, not the authoring method.
- No evidence grounding volume/scale knobs for class (a) (how many rows make a world feel
  real); C019 is silent on this by design.
