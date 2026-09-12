# L6 Tooling Inventory (RQ4)

> **CORRECTION (post red-team, F110):** row 10 lists "Amazon S3 / MinIO". MinIO is AGPL-3.0 and archived 2026-04, and is REJECTED. The S3 substitute is adobe/s3mock (T154). T109 has been renamed to "Amazon S3".


Wrote 30 inventory artifacts (`state/inventory/T100.yaml`–`T129.yaml`), one per data application the environment must simulate, covering every app named in the team meeting list. 24 of 30 are backed by at least one S5xx finding (S500–S526); the remaining 6 (Slack, GitHub Actions, Spark, Hightouch/Census, plus partial backing for the semantic layer and Unity Catalog) are included because the meeting named them explicitly. Categories span the full stack: warehouses (4), databases (3), lakehouse (1), transformation (2), orchestration (2), BI (4), ingestion (2), streaming (1), storage (1), docs (1), notebook (2), vcs (1), ticketing (1), catalog (1), quality (1), other (4). All entries are `role: simulate_target` with `replication_strategy: none_found` as placeholder; license, replication_ease, and probe fields are deliberately left for later phases. `frequency_rank` 1–30 reflects expected prevalence in real data workflows (developer-collaboration surfaces and operational databases rank above cloud-only BI), justified by S5xx evidence where available.

## Ranked inventory

| rank | app | category | sources |
|---|---|---|---|
| 1 | Git / GitHub | vcs | S523 |
| 2 | Slack | other | meeting list |
| 3 | Jira | ticketing | S524 |
| 4 | PostgreSQL | database | S505 |
| 5 | Jupyter / JupyterLab | notebook | S521 |
| 6 | dbt (Core) | transformation | S508 |
| 7 | Apache Airflow | orchestration | S509 |
| 8 | Snowflake | warehouse | S500 |
| 9 | BigQuery | warehouse | S503 |
| 10 | Amazon S3 / MinIO | storage | S514 |
| 11 | Confluence | docs | S522 |
| 12 | GitHub Actions | other | meeting list (cf. S523) |
| 13 | Databricks | lakehouse | S501 |
| 14 | Tableau | bi | S515 |
| 15 | Power BI | bi | S517 |
| 16 | Looker | bi | S516 |
| 17 | Apache Kafka | streaming | S513 |
| 18 | Fivetran | ingestion | S511 |
| 19 | Apache Spark | transformation | meeting list (cf. S501) |
| 20 | Amazon Redshift | warehouse | S502 |
| 21 | dbt Semantic Layer / LookML | other | S516, S508 |
| 22 | Airbyte | ingestion | S512 |
| 23 | Great Expectations | quality | S526 |
| 24 | Dagster | orchestration | S510 |
| 25 | Hex | notebook | S520 |
| 26 | Unity Catalog / Atlan | catalog | S501, S525 |
| 27 | ClickHouse | database | S506 |
| 28 | Oracle Database | database | S504 |
| 29 | Sigma | bi | S518 |
| 30 | Hightouch / Census (reverse ETL) | other | meeting list |

## Gaps noticed

- **No evidence yet for collaboration/activation surfaces**: Slack, GitHub Actions, Hightouch/Census have zero S5xx findings; Spark and Prefect are only implied (S501). A future literature pass should source these.
- **Findings not converted**: S507 (DuckDB), S519 (Apache Superset), S525 (OpenMetadata) support strong OSS apps not on the meeting list; they were excluded to stay at 30 entries but are natural candidates for the RQ3 mock-implementation role (DuckDB as local grading substrate, Superset as native BI surface, OpenMetadata as catalog surface).
- **Quality/observability coverage is thin**: only Great Expectations (S526) supports the quality category; Monte-Carlo-style observability is explicitly not covered end to end by that finding.
- **Cloud-only apps (Snowflake, Hex, Sigma, Tableau, Fivetran, Confluence, Jira) have no local runtime story yet** — expected, since replication strategy belongs to RQ3.
- **Prefect dropped in favor of Dagster** (S510 exists for Dagster; no Prefect finding).
