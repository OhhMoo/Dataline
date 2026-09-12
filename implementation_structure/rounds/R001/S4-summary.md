# S4 summary — archetypes derived from tool documentation

Role: S4 (derivation path: what dbt/Airflow/BI/warehouse docs describe as the workflow,
S500-S526, combined with incident findings for difficulty). Producer id: S4-tool-docs.

## What I derived

Six archetypes, A025-A030, all passing G0/G3 gates:

- **A025** (data_engineering, DE-2): silent column-misalignment repair after an
  ingestion-connector schema change — positional-INSERT corruption mechanics from filed
  dbt-databricks bugs S013/S014, connector surface from S511/S512 docs, dbt surface S508.
- **A026** (data_engineering, DE-1): duplicate-partition cleanup + idempotent backfill
  after a wrong-mode re-run — S001/S003 incidents over Airflow (S509) and dbt (S508)
  documented workflow surfaces. Graded by an idempotency probe (agent's own re-run).
- **A027** (data_engineering, DE-4): green-DAG triage of a stalled Kafka consumer behind
  the orchestrator — Pinterest cross-system finding S009 over Airflow/Dagster/Kafka docs
  (S509/S510/S513), with a live decoy asset job.
- **A028** (data_analytics, DA-4): legacy SQL-to-dbt migration with audit-style row-level
  validation — the full documented dbt migration loop S209-S212 plus Oracle-legacy (S504)
  and Superset (S519) surfaces. 20-40 human hours, the largest task in the batch.
- **A029** (data_analytics, DA-2): two-dashboards-one-metric reconciliation with three
  simultaneous planted causes — S205's documented bisect method as a gradeable artifact,
  S002 timezone drift and S208 varying-valid-day window as the mechanics.
- **A030** (data_analytics, DA-1): consolidating near-duplicate metric definitions into a
  semantic layer — Airbnb/Uber/LinkedIn findings (S200/S201/S204) plus dbt semantic-layer
  migration guidance S218 and Looker surface S516.

Discipline split: 3 DE, 3 DA. No DS — the tool-docs evidence (S500-S526) covers
warehouse/orchestration/BI/docs surfaces, and the DS workflow evidence lives in the
MLE-Bench set that other derivation paths own; I did not want to duplicate their slots.

## Richest evidence

- **S209-S212** (dbt change/migration workflow docs) — directly translatable into both
  initial conditions and deterministic checks (dbt list blast radius, MINUS system test,
  audit_helper row diff, duplicate-the-BI-report). A028 is the most doc-faithful artifact.
- **S013/S014** — filed bugs with exact positional-INSERT mechanics; ideal broken-world
  seeds because the corruption is invisible to schema checks.
- **S205** — a ready-made grading rubric (grain-first row counts, bisect by dimension).
- The S500-S526 doc findings themselves are thin per-id (one surface sentence each), so
  they work best as *surface justification* in derived_from, paired with an incident id
  that carries the difficulty. Every archetype cites both kinds.

## Least confident

- **A030**: grading risks arbitrariness unless the world pins the "correct" canonical
  metric definition (I specified a docs_wiki governance page as the pin). Also leans on
  the DA-6-adjacent deprecation-blast-radius mechanic where L3 flags thin first-party
  evidence. Flagged in the artifact's notes for the verifier pass.
- **A027**: the stalled-consumer mechanic borrows from the evidence-thin DE-6 cluster
  (S016-S021); I grounded it in S009 + orchestrator/streaming docs instead, but a
  verifier could argue it double-counts DE-6's narrow base.

## Verification

`python gates/run_gates.py` → G0 PASS, G3 PASS, G5 PASS (0 failures; the 12 G0 warnings
are pre-existing `repair_note` fields on knowledge files, not my artifacts).
