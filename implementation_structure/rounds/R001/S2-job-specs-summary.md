# S2-job-specs — Phase 3 derivation summary (R001)

## What I derived

Six archetypes, A009-A014, written to `state/archetypes/`. Derivation path: the
tool stacks that job postings repeatedly demand (S500-S526 tooling-inventory,
used strictly as frequency/priority signal — no jobpost-type sources exist in the
corpus) selected which subcategories to allocate slots to; all task content is
grounded in incident findings per role instructions.

| id | discipline | subcategory | content grounding | frequency signal |
|----|-----------|-------------|-------------------|------------------|
| A009 | data_engineering | DE-1 backfill/idempotency | S001, S003, S006, S023 | warehouse+dbt+Airflow trio (S505/S507/S508/S509) |
| A010 | data_engineering | DE-2 schema drift | S013, S014, S015, S022 | ingestion into Snowflake-class warehouse (S511/S512/S500) |
| A011 | data_engineering | DE-4 orchestration triage | S008, S009, S018 | Airflow ownership + Jira/Confluence ops context (S509/S524/S522) |
| A012 | data_science | DS-1 leakage | S111, S112, S120, S124 | notebook+warehouse+git profile (S521/S503/S523) |
| A013 | data_science | DS-3/DS-4 drift & delayed labels | S114, S117, S119, S123, S125 | production-ML monitoring stack (S521/S526/S515) |
| A014 | data_analytics | DA-2 numbers-don't-match | S203, S205, S206, S208 | BI-over-warehouse fluency (S515-S519/S508) |

Discipline split: 3 DE / 2 DS / 1 DA. I deliberately skipped the evidence-thin
subcategories (DE-6, DS-6, DA-5, DA-6) flagged in F1, and weighted DE-1/DE-2/DA-2
because the incident corpus is densest there.

## Richest evidence

- **S013/S014/S015/S022 (filed bugs)**: exact failure mechanics (positional INSERT,
  connector row loss, shrunken raw layer) translate almost verbatim into broken-world
  initial conditions and deterministic checks. Strongest per-id material I used.
- **S111/S112**: the per-field leakage catalogue made it possible to plant three
  distinct, layered leakage mechanisms with citations rather than inventing them.
- **S205**: a ready-made grading checklist (grain-first row counts, bisect by
  dimension) — the only analytics source that hands you a deterministic rubric.
- **S205/S208/S206 combine well**: three mutually compatible discrepancy causes
  (timezone, grain, valid-day windows) that can co-occur in one world.

## Least confident

- **A013** (model degradation under delayed labels). The deterministic grading is the
  weakest of my six: "attribution output must name distribution shift as primary" is
  checkable only if the world ships a structured attribution artifact, and the
  reference detection timeline requires careful synthetic telemetry design. It also
  leans on S123/S125 (interview studies) rather than first-party incident mechanics,
  unlike A009/A010.
- Secondary concern: **A011**'s static-lint checks (datetime.now detection, retry
  bounds) risk being greppable shortcuts — a frontier model may pass the lint without
  doing the triage. The DST simulation check mitigates but doesn't eliminate this.

## Notes for the coordinator

- No `source_type: jobpost` entries exist in `state/knowledge/`; if future rounds want
  real job-spec evidence (rather than tool-surface frequency), that is a literature
  gap worth a pass.
- All `derived_from` ids verified present in `state/knowledge/` and content-checked
  against their claims before citation. G0-shape self-check passed on all six files.
