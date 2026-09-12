# S6-oss-substitution — Phase 3 summary

## What I derived

Six RQ3 claim artifacts (C007-C012) arguing mock-ability via OSS substitution, tier by tier
over the T100-T129 inventory:

- **C007** — Warehouse tier substitutes directly: fakesnow/DuckDB for Snowflake, Postgres or
  DuckDB + sqlglot build-time transpile for Redshift/BigQuery, ClickHouse native OSS.
  data-eng-bench's dual-backend (DuckDB default, Snowflake as fidelity axis) validates it.
- **C008** — Transformation/orchestration tier (dbt Core, Airflow, Dagster) is native OSS and
  needs no mock; ade-bench proves the containerized dbt-task packaging and auto-generated
  row-equality grading. Cheapest tier to build and grade.
- **C009** — Storage/streaming tier fully local: MinIO for S3, Kafka native.
- **C010** — BI/catalog tier splits: Superset and OpenMetadata are drop-in OSS stand-ins;
  Slack/Jira/Confluence/GitHub have no OSS equivalent and need E3-style HTTP-intercept mocks —
  the actual mock-ability bottleneck.
- **C011** — Ingestion tier: Airbyte substitutes Fivetran; reverse ETL (T129) has no evidenced
  substitute — defer or drop from v1.
- **C012** — Fidelity caveat: substitutes don't transfer correctness (fakesnow liberal
  dialect + non-deterministic row order; sqlglot "transpiler, not validator"); grading must
  execute against the substitute's real state and use dual-backend dialect spot-checks.

## Richest evidence

S316-S318 (fakesnow/universql/sqlglot with verbatim license and caveat quotes) and S303-S305
(data-eng-bench dual-backend, ade-bench sandbox + auto-grader patterns) — these carry the whole
derivation. The S5xx app findings are thin one-liners (docs landing pages), adequate only as
surface-existence support, not as substitution proof.

## Least confident

- **C010** — Superset-as-Tableau/Looker/Power BI stand-in is asserted from Superset being OSS
  and BI-shaped (S519), but no evidence in the corpus tests fidelity gaps (semantic layers,
  extract engines, Tableau-specific interactions). The "four collaboration apps are the
  bottleneck" half is solid; the Superset coverage half is the weak link.
- **C009** — MinIO-as-S3 rests on T109's name and S514's S3 surface description; no probe or
  compatibility evidence exists in the corpus yet.
