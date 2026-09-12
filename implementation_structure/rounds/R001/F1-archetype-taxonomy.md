# F1 — Archetype taxonomy for Phase 3 strategy agents

Phase 2 output. This is STRUCTURE, not archetypes: Phase 3 derives 10-30 archetypes
inside the subcategories below. Every subcategory cites its supporting knowledge ids;
archetypes must land in exactly one subcategory and cite the same ids in `derived_from`.

Evidence base: L1 (S001-S023, data engineering), L2 (S100-S125, data science/ML),
L3 (S200-S218, analytics), L4 prior art (S300-S318, grading/harness patterns only),
L6 inventory (T100-T129).

## 1. Category tree

### data_engineering (evidence-rich overall)

- **DE-1 backfill & idempotency repair** — re-running jobs over existing partitions,
  duplicate cleanup, materialization-change full refreshes. Support: S001, S003, S006, S023.
- **DE-2 schema & semantic drift** — added/removed/renamed columns breaking positional
  INSERTs, silent type/timezone shifts, ingestion connector row loss. Support: S002,
  S004, S013, S014, S015, S022. (Richest single subcategory in the corpus.)
- **DE-3 silent data loss & correctness-vs-freshness monitoring** — quality rules
  dropping valid rows, green pipelines emitting wrong state. Support: S003, S005, S019.
- **DE-4 cross-system orchestration triage** — failures originating behind the
  orchestrator surfacing inside it; DAG-definition anti-patterns. Support: S008, S009, S018.
- **DE-5 lineage-based SLA / freshness breach triage** — multi-hop lineage walkback,
  ran-long vs started-late disambiguation, rename blast radius into BI. Support: S010,
  S011, S012. (Archetype_EXAMPLE.yaml A004 is the reference instance.)
- **DE-6 CDC/streaming & recovery under destructive ambiguity** — replication-slot WAL
  bloat, stalled consumers, restore-with-no-backups, repair-gone-wrong. Support: S016,
  S017, S020, S021. EVIDENCE-THIN: all four ids cluster on two incidents
  (Debezium/WAL, GitLab). Derivable but narrow; diversify before assigning many slots.

### data_science (concentrated sources; see thinness notes)

- **DS-1 leakage & split integrity** — preprocessing/feature-selection fit on combined
  sets, temporal leakage, duplicates across splits, proxy features, competition leaks.
  Support: S111, S112, S120, S124.
- **DS-2 training-serving skew & feature consistency** — mutable join tables between
  train and serve, copied-pipeline dropped filters, bespoke long-tail pipeline bugs.
  Support: S115, S116, S121.
- **DS-3 drift, delayed labels & model monitoring** — demand-shift retraining,
  no-observability incidents, ~2-week label delay, alert fatigue, data cascades.
  Support: S114, S117, S123, S125.
- **DS-4 offline/online & proxy-metric divergence** — deployed-model external validation
  failure, business-metric vs accuracy gap, hand-built integrity checks. Support: S113,
  S118, S119.
- **DS-5 end-to-end MLE deliverable validity & compute budgeting** — produce a gradeable
  artifact under disk/RAM/time limits; anti-memorization and anti-plagiarism rules;
  admissibility checklist; human-hours calibration. Support: S100-S109 (MLE-Bench set:
  S101, S102, S105, S106, S107, S108, S109; S103, S104 for grading rules).
- **DS-6 notebook / experiment reproducibility** — wrong-input notebook runs, stale
  state, dev/prod divergence. Support: S121, S122. EVIDENCE-THIN: one direct source
  (S122); L2 explicitly flags gaps on feature-store, experiment-tracking, and
  model-registry/rollback incidents — do not over-allocate slots here.

### data_analytics

- **DA-1 metric definition reconciliation** — same question, different numbers across
  teams; near-duplicate metric instances; single-source-of-truth consolidation.
  Support: S200, S201, S202, S204.
- **DA-2 numbers-don't-match triage** — timezone/DST boundaries, grain mismatch,
  fan-out joins, hidden filters, currency/rounding, late data; bisect-by-dimension
  method. Support: S203, S205, S206, S208.
- **DA-3 silent metric breakage** — failure-to-zero coercion, retired-endpoint 404s
  recorded as negatives, varying-valid-day windows. Support: S207, S208.
- **DA-4 migration & refactor validation** — dbt change workflows (dbt list blast
  radius, MINUS system tests), audit_helper-style row-level diffs, legacy-to-dbt
  migrations, semantic-layer consolidation. Support: S209, S210, S211, S218 (+S212).
- **DA-5 requirements & usage discovery** — underspecified requests, ticket-queue
  workflows, "is this query even used" checks before building. Support: S212, S213, S214.
  EVIDENCE-THIN: essay-source only, and hard to grade deterministically — pair with a
  checkable artifact (e.g. usage logs, doc store) if used.
- **DA-6 BI governance: deprecation, renames, semantic-layer drift** — usage-metadata
  deprecation pipelines, column renames with invisible BI blast radius, dbt-model vs
  BI-layer rename boundaries. Support: S215, S216, S217, S218. EVIDENCE-THIN: L3 flags
  zero first-party incident reports from Tableau/Power BI/Looker/Hex/Sigma operators;
  sources are practitioner blogs and community threads.

## 2. Dependency map (subcategory → required app-inventory ids)

Simulation substrates per L6/L4: DuckDB (S507) is the hermetic grading substrate;
fakesnow (S316) emulates Snowflake over DuckDB; sqlglot (S318) transpiles dialects at
task-build time. Superset (S519) and OpenMetadata (S525) are available OSS stand-ins for
BI and catalog surfaces. Cloud-only apps below must go through these mocks (E2/E3).

| Subcategory | Core apps (T-ids) | Supporting surfaces |
|---|---|---|
| DE-1 | T105 dbt, T106 Airflow, T103 PostgreSQL | T107 Snowflake (via fakesnow), T109 S3 (s3mock T154) |
| DE-2 | T105 dbt, T103 PostgreSQL, T117 Fivetran / ~~T121 Airbyte~~ (platform Elastic-2.0, F111 - connector configs only) | T112 Databricks, T108 BigQuery |
| DE-3 | T122 Great Expectations, T105 dbt, T103 PostgreSQL | T107 Snowflake, T109 |
| DE-4 | T106 Airflow, T123 Dagster | T101 Slack, T102 Jira, T110 Confluence |
| DE-5 | T106 Airflow, T107 Snowflake, T113 Tableau / T115 Looker | T125 catalog (or OpenMetadata), T110 |
| DE-6 | T103 PostgreSQL, T116 Kafka, T109 S3 (s3mock T154) | ~~T121 Airbyte~~ (Elastic-2.0, F111), T100 Git |
| DS-1 | T104 Jupyter, T103 PostgreSQL | T118 Spark, T112 Databricks |
| DS-2 | T104 Jupyter, T112 Databricks, T118 Spark | T109, T103 |
| DS-3 | T112 Databricks, T104 Jupyter, T122 Great Expectations | T103, T109 |
| DS-4 | T104 Jupyter, T103/T107 warehouse | T113/T115 BI, T120 semantic layer |
| DS-5 | T104 Jupyter, T100 Git, T111 GitHub Actions | T109, T103 |
| DS-6 | T104 Jupyter, T100 Git | T124 Hex (cloud-only — mock), T111 |
| DA-1 | T120 semantic layer/LookML, T115 Looker, T105 dbt | T107, T110 Confluence |
| DA-2 | T103/T107/T108 warehouse, T113/T114/T115/T128 BI | T105 dbt |
| DA-3 | T105 dbt, T117/T121 ingestion, T103 warehouse | T113 Tableau, T101 Slack |
| DA-4 | T105 dbt, T100 Git, T127 Oracle (legacy source) | T113/T115 BI, T103 |
| DA-5 | T101 Slack, T102 Jira, T110 Confluence | T113/T115 BI, T103 |
| DA-6 | T115 Looker, T114 Power BI, T120 semantic layer | T113 Tableau, T125 catalog |

## 3. Derivation guidance for the 8 strategy agents

Richest raw material per subcategory (mine these first, read full yaml + quotes):

- DE-1: S001-S008 (the 50-postmortem survey S004-S007 is the densest per-id source in
  the whole corpus).
- DE-2: S013-S015, S022 (filed bugs with exact failure mechanics — directly translatable
  to broken-world initial conditions).
- DE-3: S004-S007, S019 (empirical defect taxonomies).
- DE-4/DE-5: S008-S012, S018 (talk + first-party platform accounts + peer-reviewed study).
- DE-6: S016-S021 only — narrow; combine into at most 1-2 archetypes.
- DS-1/DS-2: S111-S112 (8-mechanism leakage taxonomy + per-field catalogue), S115-S116,
  S120-S121, S124.
- DS-3/DS-4: S113-S119, S123, S125.
- DS-5: S100-S110 wholesale (MLE-Bench paper — task admissibility, hours calibration,
  anti-gaming rules are reusable as design constraints, S307/S308/S309 for grading).
- DS-6: S121-S122 only. Thin.
- DA-1/DA-2: S200-S208 (both first-party platform blogs and practitioner triage guides;
  S205 is a ready-made grading checklist).
- DA-3: S207-S208.
- DA-4: S209-S212, S218.
- DA-5: S212-S214. Thin and hard to verify deterministically.
- DA-6: S215-S218. Thin (no first-party BI operator incidents, per L3).

Evidence-thin subcategories (plainly): **DE-6, DS-6, DA-5, DA-6**, and partially
**DS-3/DS-4** (concentrated on S120-S125 interview studies + one platform blog each).
Cross-cutting gaps from L1/L2/L3 that NO subcategory currently covers: access-control/
governance work, warehouse cost incidents, feature-store/experiment-tracking/registry
incidents, and single traces crossing warehouse+dbt+orchestrator+BI. Strategy agents
should not invent archetypes in these gaps; flag them for a future literature pass.

Grading-design raw material (L4, applies to all subcategories): S301 (reward-file
contract), S302/S303 (hidden pytest verifier, dual-backend), S305 (auto-generated
equality tests + oracle agent as task CI), S307 (deterministic grading, ≥3 seeds),
S308/S309 (scattergunning-proof rubrics, GEPA judge calibration), S315 (per-query
validate.py + ground_truth.csv), S316/S318 caveats (fakesnow row-ordering
non-determinism, sqlglot transpiler-not-validator — handle in deterministic checks).

## 4. Constraints Phase 3 must respect (from state/problem.yaml + archetype_EXAMPLE.yaml)

- **Count**: 10-30 archetypes total, all three disciplines covered, G3 dedup passes
  (RQ2). Split budget suggestion, not a rule: weight by evidence richness above.
- **Difficulty**: tens of human-hours per task (E5; MLE-Bench "High" = >10h, S107, is
  the calibration anchor).
- **Rubric**: 15-30 points per task (E6); deterministic state checks for quantitative
  outputs + bounded LLM judge for code quality/design/robustness (E7).
- **Provenance**: every archetype's `derived_from` cites real ids from state/knowledge/
  (S001-S023, S100-S125, S200-S218; S300-S318 for grading/harness patterns). Invented
  archetypes are rejected at G3 — this is the failure mode the field exists to prevent.
- **No cloud dependencies** at agent runtime (E2); mock cloud APIs with OSS equivalents
  or reshaped SDKs (E3) — use the fakesnow/DuckDB/sqlglot stack per S316-S318.
- **No public datasets** as world data (E4) — synthetic from distributions, with a
  leakage argument (S109's obfuscation test is the validation pattern).
- **Licenses**: AGPL and source-available are rejections (constraints).
- **Anti-goals**: do not design the generator, do not build the LLM judge, no
  environment code this round.
- E11 resolved: "Emily Bench" = MLE-Bench (S100).
