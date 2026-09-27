# B1 — Connector scout summary (Phase 4, R001)

Author: B1-connector-scout. Scope: candidate mocks / substitutes / harnesses for all 30
`simulate_target` apps (T100–T129). Artifacts: `state/inventory/T130–T171.yaml` (42 rows),
license rejections in `state/failures/B1-license-rejections.yaml`. Licenses stated here were
read from the GitHub API / raw LICENSE files on 2026-09-12 — `license_verified` stays false,
G2 confirms. Probe fields are deliberately unset (B4 owns them); every ease rating is
`medium` unless the surface is `hard` or `blocked` — no `easy` claims without a probe.

Schema note: the role doc calls the strategy `oss_substitute`, but `schemas.json#tool`
enforces `oss_equivalent`; artifacts use the schema vocabulary (role field still carries
`oss_substitute` where applicable).

## App → best strategy → ease → license → probe

| App (target) | Best candidate (artifact) | Strategy | Ease | License | Probe |
|---|---|---|---|---|---|
| T100 Git / GitHub | git (T148) + Gitea (T146) | native_oss / oss_equivalent | medium | GPL-2.0 (flagged, human read) / MIT | not run (B4) |
| T101 Slack | ygalblum/slack-server-mock (T150) | http_intercept | hard | Apache-2.0 (stale repo) | not run |
| T102 Jira | **none_found** (T149); generic harnesses T151/T152 | — | blocked | — | — |
| T103 PostgreSQL | PostgreSQL (T168) | native_oss | medium | PostgreSQL License (non-GitHub, G2 path) | not run |
| T104 Jupyter | JupyterLab (T171) | native_oss | medium | BSD-3-Clause | not run |
| T105 dbt Core | dbt-labs/dbt (T134) — repo renamed from dbt-core | native_oss | medium | Apache-2.0 | not run |
| T106 Airflow | apache/airflow (T135) | native_oss | medium | Apache-2.0 | not run |
| T107 Snowflake | fakesnow (T130) over DuckDB substrate (T131) + sqlglot (T132) | sdk_reshape (server mode = wire_protocol) / oss_equivalent | medium | Apache-2.0 / MIT / MIT | not run |
| T108 BigQuery | goccy/bigquery-emulator (T133) | wire_protocol | medium | MIT | not run |
| T109 S3 | adobe/s3mock (T154) — **MinIO rejected** (T153) | http_intercept | medium | Apache-2.0 | not run |
| T110 Confluence | BookStack (T143) — Wiki.js & Outline rejected (T144/T145) | oss_equivalent | medium | MIT | not run |
| T111 GitHub Actions | nektos/act (T147) | native_oss | medium | MIT | not run |
| T112 Databricks | Spark+Delta cover compute/storage (T164/T165); workspace **none_found** (T166) | mixed | hard | Apache-2.0 / Apache-2.0 / — | not run |
| T113 Tableau | **none_found** (T138); Superset persona (T137) | — | blocked | — | — |
| T114 Power BI | **none_found** (T139); Superset persona (T137) | — | blocked | — | — |
| T115 Looker | **none_found** (T140); lkml parser only (T163) | — | blocked | — | — |
| T116 Kafka | apache/kafka (T155) | native_oss | medium | Apache-2.0 | not run |
| T117 Fivetran | dlt (T157) + Meltano (T158) — **Airbyte rejected** (T156) | oss_equivalent | medium | Apache-2.0 / MIT | not run |
| T118 Spark | apache/spark (T164) | native_oss | medium | Apache-2.0 | not run |
| T119 Redshift | Postgres persona + sqlglot (T170) | oss_equivalent | medium | (inherits T168) | not run |
| T120 dbt Semantic Layer / LookML | MetricFlow (T162) + lkml (T163) | oss_equivalent | medium / hard | Apache-2.0 / MIT (stale) | not run |
| T121 Airbyte | **rejected** (T156, ELv2) — see T117 substitutes | — | blocked | Elastic-2.0 | — |
| T122 Great Expectations | great_expectations (T160) — repo moved to fivetran/ | native_oss | medium | Apache-2.0 | not run |
| T123 Dagster | dagster-io/dagster (T136) | native_oss | medium | Apache-2.0 | not run |
| T124 Hex | **none_found** (T141); Jupyter covers notebooks | — | blocked | — | — |
| T125 Unity Catalog / Atlan | OpenMetadata (T161) | oss_equivalent | medium | Apache-2.0 | not run |
| T126 ClickHouse | ClickHouse (T169) | native_oss | hard (determinism) | Apache-2.0 | not run |
| T127 Oracle | **none_found** (T167); Postgres persona | — | blocked | — | — |
| T128 Sigma | **none_found** (T142) | — | blocked | — | — |
| T129 Hightouch / Census | **none_found** (T159) | — | blocked | — | — |
| Generic SaaS REST mocks | Mockoon (T151) / MockServer (T152) | http_intercept harness | medium | MIT / Apache-2.0 | not run |

Every F2-REQUIRED v1 surface is covered: warehouse (T130+T131), dbt (T134), Airflow (T135),
BI mock (T137 Superset), docs wiki (T143 BookStack), Git (T146+T148).

## Where the real build cost is

**none_found (8 surfaces — hand-build or change scope):**
- **BI vendors** T113 Tableau, T114 Power BI, T115 Looker, T124 Hex, T128 Sigma — no vendor
  ships a license-clean offline edition and no OSS mock speaks their APIs. The v1 BI mock is
  Superset, a *persona*, not protocol emulation. If a task truly needs Tableau-shaped REST,
  that is a hand-rolled http_intercept fixture set (Mockoon/MockServer) — real build cost.
- **T102 Jira** — no Jira-API-shaped OSS mock. Cheapest honest path: hand-written fixtures
  on Mockoon, or accept Gitea issues as the ticket surface.
- **T112 Databricks control plane** — workspace/jobs/SQL-warehouse UI unmockable; Spark+Delta
  cover only compute and storage.
- **T127 Oracle** — XE/Docker are OTN-proprietary; no wire-protocol substitute. Postgres
  persona + sqlglot flavoring is the ceiling.
- **T129 reverse ETL (Hightouch/Census)** — every OSS entrant is license-blocked or dead:
  Multiwoven AGPL-3.0, RudderStack ELv2, Grouparoo archived 2022, Castled's repo is gone.

**hard:**
- **T150 Slack mock** — the only license-clean option (ygalblum/slack-server-mock) is stale
  (last push 2025-02) and covers a subset of a huge API. Expect fixture-writing on top.
- **T169 ClickHouse** — structurally hostile to bit-reproducible resets (async merges); mocked
  source persona with pinned results only, never grading substrate.
- **T163 LookML** — parser-only (joshtemple/lkml, stale since 2025-01); no Looker execution.

**License surprises that contradict Phase-3 claims (also in `state/failures/`):**
- **MinIO is AGPL-3.0 AND its repo was archived 2026-04** — the C-claim "MinIO for S3" fails
  twice. Replacement: adobe/s3mock (Apache-2.0), thinner S3 semantics.
- **Airbyte's root LICENSE is Elastic-2.0** (connectors MIT, platform ELv2) — the C-claim
  "Airbyte for Fivetran" fails the source-available rule. Substitutes: dlt + Meltano; no
  managed-connector control plane survives the license screen.
- **Wiki.js AGPL-3.0, Outline BSL-1.1** — the two most obvious Confluence substitutes are both
  out; BookStack (MIT) is the pick.
- **Repo moves to expect at G2:** dbt-labs/dbt-core → dbt-labs/dbt (301);
  great-expectations/great_expectations → fivetran/great_expectations (301, Fivetran acquired
  GX); LocalStack/localstack archived 2026-03 (not needed — s3mock covers S3).
- **git is GPL-2.0** — not on the auto-accept list; flagged for a human read (local CLI tool,
  not a re-served network service).

## Probe results (B4)

Phase 4 feasibility probes, run 2026-09-11 on Windows 11 (Python 3.11.15 throwaway venvs;
Docker Desktop 29.5.2). Probe = install + import (pip) or `docker pull` / binary download +
`--version` (non-pip). `easy` below means probe_result: pass recorded in the inventory YAML
(G5-verified: 23 artifacts claim easy, all with pass + recorded command). 26 candidates
probed: 26 pass, 0 fail, 0 not_run. Unprobed = blocked/none_found candidates (T138–T142,
T144, T145, T149, T153, T156, T159, T166, T167) — nothing installable exists.

| Candidate | Probe command (as run) | Result |
|---|---|---|
| T130 fakesnow | `pip install fakesnow` (batched w/ duckdb/sqlglot/lkml, py3.11 venv) → import | pass → **easy** (0.11.16) |
| T131 DuckDB | `pip install duckdb` (same batch) → import | pass → **easy** (1.5.5) |
| T132 sqlglot | `pip install sqlglot` (same batch) → import | pass → **easy** (30.15.0) |
| T133 bigquery-emulator | `docker pull ghcr.io/goccy/bigquery-emulator:latest` | pass → **easy** |
| T134 dbt Core | `pip install dbt-core dbt-duckdb` → import | pass → **easy** (1.12.4 + dbt-duckdb 1.11.0) |
| T135 Airflow | `pip install apache-airflow --constraint constraints-main/constraints-no-providers-3.11.txt` → `PYTHONUTF8=1 python -c "import airflow"` | pass w/ caveats → medium (3.3.1; POSIX-only per upstream warning; plain import on GBK-locale Windows crashes w/o PYTHONUTF8=1) |
| T136 Dagster | `pip install dagster` (batched w/ dlt/metricflow) → import | pass → **easy** (1.13.22) |
| T137 Superset | `pip install apache-superset` (dedicated venv) → import | pass → **easy** (6.1.0, clean pure-pip on Windows) |
| T143 BookStack | `docker pull lscr.io/linuxserver/bookstack:latest` | pass → **easy** |
| T146 Gitea | `docker pull gitea/gitea:latest` | pass → **easy** |
| T147 nektos/act | `curl -LO .../releases/latest/download/act_Windows_x86_64.zip && unzip && act.exe --version` | pass → **easy** (0.2.89) |
| T148 Git | `git --version` | pass → **easy** (2.51.2.windows.1, already on host) |
| T150 slack-server-mock | `git clone --depth 1 … && pip install <clone>` → import | pass w/ caveats → medium (Python/poetry app, no package.json; stale since 2025-02) |
| T151 Mockoon | `docker pull mockoon/cli:latest` | pass → **easy** |
| T152 MockServer | `docker pull mockserver/mockserver:latest` | pass → **easy** |
| T154 s3mock | `docker pull adobe/s3mock:latest` | pass → **easy** |
| T155 Kafka | `docker pull apache/kafka:latest` | pass → **easy** |
| T157 dlt | `pip install dlt` (batched w/ dagster/metricflow) → import | pass → **easy** (1.30.0) |
| T158 Meltano | `pip install meltano` → import | pass w/ caveat → medium (4.2.2; forces pathspec 1.1.1, conflicts with dbt-core's pathspec<1.1 when co-installed — separate env) |
| T160 Great Expectations | `pip install great-expectations` (batched w/ jupyterlab) → import | pass → **easy** (1.23.0) |
| T161 OpenMetadata | `docker pull docker.getcollate.io/openmetadata/server:latest` | pass w/ caveat → medium (pull fine; real deploy needs Java+ES+MySQL compose stack) |
| T162 MetricFlow | `pip install metricflow` (batched w/ dagster/dlt) → import | pass → **easy** (0.213.0) |
| T163 lkml | `pip install lkml` (first batch) → import | pass w/ caveat → medium (1.3.7 installs clean; stale upstream keeps it off easy) |
| T164 Spark | `pip install pyspark` (batched w/ delta-spark) → import | pass → **easy** (4.2.0; running local mode needs a JRE) |
| T165 Delta Lake | `pip install delta-spark` (same batch) → import | pass → **easy** (4.4.0) |
| T168 PostgreSQL | `docker pull postgres:16` | pass → **easy** |
| T169 ClickHouse | `docker pull clickhouse/clickhouse-server:latest` | pass, ease stays **hard** — install is easy; the hard rating is about bit-reproducible resets (async merges), not installation |
| T170 Redshift persona | `docker pull postgres:16` (same image as T168) | pass → **easy** (no separate artifact) |
| T171 JupyterLab | `pip install jupyterlab` (batched w/ great-expectations) → import | pass → **easy** (4.6.3) |

Notable findings: (1) every v1-required surface (warehouse substrate, dbt, Airflow, Superset
BI mock, BookStack wiki, git/Gitea/act) now has a passing probe; (2) Superset's pure-pip
install on Windows is clean — the "needs docker" folklore is stale; (3) Airflow is the only
v1 install with real friction (POSIX-only + locale-sensitive config load); (4) Meltano and
dbt-core cannot share one environment (pathspec pin conflict); (5) ygalblum/slack-server-mock
is Python, not Node as its name suggests. Gates: G5 PASS (23 easy artifacts, 0 failures),
full offline run (G0+G3+G5) ALL GATES PASSED.
