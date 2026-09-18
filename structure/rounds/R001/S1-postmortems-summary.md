# S1-postmortems — Phase 3 summary (R001)

Derivation path: archetypes from **incident post-mortems** (what actually broke in
production). Six artifacts written to `state/archetypes/`, all schema-validated locally
(required fields present, discipline enum, rubric_points 15-30, all `derived_from` ids
exist in `state/knowledge/`).

## Artifacts

- **A001** (data_engineering, DE-1) — Non-idempotent append-mode backfill doubles a month
  of revenue; two-stage incident (original bug + botched repair) plus crashed-run partial
  debris. From S001, S005, S006.
- **A002** (data_engineering, DE-2) — Positional-INSERT silent value corruption after
  `on_schema_change`; mixed-state table with a discoverable corruption boundary, plus the
  complementary loud removed-column failure. From S013, S014, S015.
- **A003** (data_engineering, DE-3) — The quality gate is the bug: a validation rule
  silently drops valid rows while a UTC→local semantic drift hides in identical schema;
  the "add more validation" instinct is anti-correlated with success. From S003, S005,
  S002, S019.
- **A004** (data_engineering, DE-6) — CDC replication-slot WAL bloat; graded traps are
  the destructive escape hatches (slot drop, basebackup on the wrong host) and the
  silently-failing pg_dump backup leg. From S016, S017, S020, S021.
- **A005** (data_science, DS-1/DS-2) — Post-deployment offline/online accuracy collapse
  with three independently seeded skew mechanisms (mutable join table, copied-pipeline
  dropped filter, preprocessing leakage); graded per-mechanism attribution. From S115,
  S116, S121, S112.
- **A006** (data_analytics, DA-1/DA-2/DA-3) — Numbers-don't-match reconciliation: grain /
  fan-out / hidden-filter divergence between two teams, DST timezone drift, and a 0.0%
  KPI that is failure-to-zero coercion surviving on confirmation bias. From S203-S207.

Discipline split: 4 DE, 1 DS, 1 DA — weighted to the evidence richness of the
post-mortem corpus per F1.

## Richest evidence

1. **S013/S014/S015** (filed dbt adapter bugs) — exact failure mechanics with repro
   steps; the most directly translatable-to-initial-conditions sources in my sample.
2. **S001 + S004-S007 cluster** — the two-stage "bug then botched repair" structure and
   the frequency weighting (schema drift 38%, silent loss 19%, load spikes 24%).
3. **S016/S017 + S020/S021** — two incident clusters that combine into the strongest
   "graded destructive trap" design I found.
4. **S203-S208** (analytics triage) — S205's bisect-by-dimension checklist is nearly a
   ready-made deterministic rubric; S207's confirmation-bias survival is the best
   anti-shortcut world mechanic.

## Least confident

- **A004** — depends on simulating real PostgreSQL WAL / replication-slot behaviour
  in-container under the no-cloud constraint; mechanically gradeable but needs a
  builder-phase feasibility probe before slotting into a world.
- **A005** — per-mechanism attribution grading assumes the diagnosis report can be
  matched deterministically against a seed manifest; if report parsing proves brittle,
  attribution credit may need to move into the bounded LLM judge with anchor answers.
- A006's memo-quality points and A003's "monitoring added" points are the rubric lines
  most exposed to judge variance; deterministic legs of both are solid.

## Flags for the coordinator

- DE-6 absorbed into a single archetype (A004) per F1's thinness warning — recommend
  other paths not re-derive it.
- Cross-cutting gap confirmed from this path: no source in S001-S218 supports
  access-control/governance or warehouse-cost archetypes; flag for a future literature
  pass rather than inventing.
