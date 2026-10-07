# Environment tool inventory and plug-in map

Sprint 2026-09-30 → 10-13, week 1 deliverable ("inventory of what our custom environments
need"). Written 2026-10-02 from the repo at `8f77e7b` plus the R001–R004 research record.
Every "today" claim cites the file it was read from; every external claim cites a source at
the bottom. Effort is a rough size (S ≈ days, M ≈ a week, L ≈ multi-week), not an estimate
anyone has measured.

**The idea (Andrew, 9/30):** an environment is not one monolithic box. It is a set of
**slots** (warehouse, notebook, orchestrator, BI, ...). Each environment profile plugs a
concrete local implementation into each slot, or leaves the slot empty. Data engineering,
data science and data analysis get different profiles over the same world factory. All of it
is local, runs on Linux arm64 or x86_64, follows Harbor's task format, and is closed to
reward hacking by construction.

---

## 1. Decisions (final, 2026-10-02)

The 9/30 meeting list is the final tool scope: filesystem, Python/Jupyter, ML compute
nodes, Redshift, Postgres, Databricks, Snowflake, Airflow, dbt, BigQuery, Power BI, Sigma,
Tableau, on Linux arm64/x86_64, for DE, DS and DA tasks. Where an earlier R004 verdict
conflicts with it, the R004 verdict is overruled (§7).

**Scope:**

1. **Spark/Databricks: in.** R004's "Do not run Spark. Ever."
   (`structure/rounds/R004/RESEARCH-streaming.md:22`) is overruled. Both tiers from §3.2
   ship: tier 1, a SQL plane over DuckDB + delta-rs; tier 2, real local PySpark for the DS
   profile. Spark determinism must be measured before any task depends on it.
2. **BigQuery: in, cost fenced.** R004's "works; don't use" is overruled. The emulator now
   ships multi-arch builds. No task grades on `totalBytesBilled` or cost.
3. **Airflow: in, as an orchestrator plug.** A task selects it with
   `orchestrator = airflow` (headless, R004 recipe). `./ops/sched` (`ops_sched`) stays the
   default, because GT001–GT009 were measured on it and it has an exact MCP twin
   (`gateway/NATIVE.md:72-76`).

**Engineering (not scope):**

4. **MinIO → s3mock.** MinIO was rejected on licence (AGPL, archived;
   `structure/state/failures/B1-license-rejections.yaml:3-6`) and has shipped no community
   images since 2025-10. `gateway/docker/compose.native.yml:388` still pulls
   `pgsty/minio`. Swap it for `adobe/s3mock`, which `compose.yml:76` already uses.
5. **`[environment].surfaces` in `task.toml` becomes the profile.** Today the harness
   treats it as descriptive only (`tasks/harness/taskspec.py:832-833` reads just `seed` and
   `virtual_now`). The slot/plug catalog and profile resolver are now implemented in
   `gateway/src/alkera_gateway/plugs.py` (`alkera-gateway plugs matrix`); §5
   lists the enforcement points still to wire.

---

## 1a. Owners and status (2026-10-06)

**done** = built, tested, and run in the container topology unless stated;
**partial** = usable with stated gaps; **open** = not started. Model results are Claude
Opus, native driver, container topology (`docs/implementation_plan/TASK_STATUS.md`).

| item | owner | status | what is still open |
|---|---|---|---|
| Snowflake | Michael | done (plug live) | fakesnow at `snowflake.internal:8443` over trusted TLS for snowflake profiles, seeded from the world's warehouse, every statement audited (`snowflake.statement`); real connector and dbt-snowflake proven inside the container (`tasks/harness/evidence/snowflake_e2e.json`); image `alkera-episode:head-da-snowflake`. Open: Snowflake box's `services.toml` / onboarding / dbt profile still describe no Snowflake warehouse; containment not re-run with the service up; no task runs on `da/snowflake` |
| Databricks (lakehouse) | Andrew | open | everything (§3.2 tier 1 / tier 2) |
| dbt | Andrew | done for duckdb, postgres, snowflake | dbt-bigquery (measured working against the container's BigQuery proxy, not shipped: it pulls pandas/pyarrow/nbformat into a de image, which its toolbelt gate forbids), dbt-redshift, dbt-databricks. dbt-postgres `debug`/`run` live in a container since 2026-10-07 (`tasks/harness/evidence/postgres_e2e.json`). Finding: `dbt show --inline` with `{{ ref() }}` to a `warehouse`-schema model is a DuckDB Binder Error on commerce_analytics (`macros/relations.sql` puts the database back only in tests; the other four themes carry the same macro); fixing it moves world bytes, so it waits on a decision |
| Airflow | Andrew | partial (plug live, 2026-10-07) | Airflow 3 REST v2 fake over the world ledger at `airflow.internal:8100` for `orchestrator = airflow` (not real Airflow): DAGs read with `ast` from the workspace's `dags/`, DAG runs / task instances / logs derived from `var/sched/state.json`, trigger and clear QUEUE runs that the grader's `advance` executes (`world.scheduler.enqueue`; no clock, no backfill, no pause, no conf - each refused by name; at most 16 queued runs per DAG and 256 in the queue); one row per request in the tailer's vocabulary; real `curl` and `airflowctl` proven inside the container (`tasks/harness/evidence/airflow_e2e.json`); image tag suffix `-airflow`. Open: real headless Airflow under the same plug; `LEDGER_IS_NOT_THE_DAG` (editing a DAG's schedule or edges changes the API's view, not what the ledger runs); containment P6 not re-run with it; no task uses it |
| Python / Jupyter | Andrew (built by Michael) | done | audited kernel and jupyter service in da/ds images; MLflow / compute nodes are separate and open |
| Postgres | Andrew | done (live, audited) | real server at `postgres.internal:5432` in every native stack. Statements audited at teardown: `NativeContainerEpisode.down()` folds the role-scoped server log into the action log (`psql.statement`, one row per statement, `seq` contiguous). A fold that fails makes the rollout a `harness_error` (never graded). Seeded with the world's `warehouse.*` only when a profile plugs it (`de/postgres`), in a schema owned by a NOLOGIN role and closed to `episode` (compose's unlogged owner, whose DSN the box carries in `ALKERA_POSTGRES_DSN`); every other box keeps `warehouse.items` (2 rows) as compose made it. psql, psycopg and dbt-postgres proven inside the container (`tasks/harness/evidence/postgres_e2e.json`, 27/27); tour probes `postgres`, `postgres_dbt`. Open: rows land at teardown, not per call; `episode` (unlogged) still owns `raw`/`staging` on every box and `warehouse` on unseeded ones (containment record); a syntax-error statement leaves no row; connections not counted in `destinations.jsonl`; the box's bare `dbt` still targets DuckDB on `de/postgres`; no task uses it |
| BigQuery | Andrew | done (plug live in the container topology, 2026-10-07) | goccy emulator v0.8.1 behind an audited REST proxy at `bigquery.internal:9050` for `de/gcp` (`native/services/bigquery.py`; engine on backend_net only), seeded from the world's warehouse before native-services binds (140,480 rows in ~15 s); `google-cloud-bigquery` and curl proven inside the container, one `bigquery.statement` row per query, 1:1 with the emulator's own job log, remote writes leave the world's fingerprint unchanged (`tasks/harness/evidence/bigquery_e2e.json`, 37/37); refused by name where the emulator answers success and does something else - dry runs, legacy SQL, transactions and BEGIN...END blocks, ALTER, EXECUTE IMMEDIATE, ASSERT, WRITE_TRUNCATE destinations, schema patches, CREATE OR REPLACE reading itself, schema DDL in scripts, load/extract jobs - each refused query still a `bigquery.statement` row flagged `refused:<feature>`; INT64 overflow wraps (not fenced). Open: dbt-bigquery (works through the proxy - debug/run/test/show - but its pandas/nbformat break the de image's role gate, so not shipped; a gate decision); GCS fake / load jobs; arm64 not run (the emulator image is multi-arch); containment P6 not re-run with it; no task |
| Semantic layer / BI-as-code | Michael | done | semantic core + dbt semantic-layer front-end (real MetricFlow agreed 24/24); MetricFlow `mf` CLI in the toolbelt, shimmed, `mf query` equal to the warehouse (`bi_e2e_host.json` M1-M4). Open: a task where the agent fixes a dbt metric - in the 2026-10-06 live BI001 run the agent read `metrics.yml` and reconciled in SQL, never running `mf` |
| Redshift | Andrew | open | Postgres persona (§3.2) |
| Spreadsheets (Excel) | Andrew | done | `openpyxl==3.1.5` in the `ds` extra since 2026-10-07, so the da/ds images' own Python reads and writes `.xlsx` (`pandas.read_excel`, openpyxl); `in2csv` from `/opt/toolbelt` as before; de unchanged. `toolbelt_check.py` proves the openpyxl -> `read_excel` round trip inside the da image; tour probe `excel` |
| Tableau, Sigma | Michael | done (file/API); UI clone started | skins + fake Tableau Server (real tabcmd/TSC) and Sigma API v2 with the real endpoints agents reached for; BI001 Opus 5/5 and 8/8. Tableau UI slice mounted in `tableau.internal` (views, charts, calc editor through the publish path; `docs/environments/UI_CLONE_PLAN.md`). Open: Sigma UI, Playwright in the image, a UI-only task; two Sigma routes the 2026-10-06 live BI001 agent tried and got 404 (`GET /v2/workbooks/{id}/contents`, `GET .../elements/{id}/export`), to check against Sigma's published API; no golden values from the real products |
| Power BI | Michael | done | PBIP + DAX subset + fake Power BI / Fabric REST with import-mode refresh; BI002 Opus 8/8 on both arms with `updateDefinition` + refresh used as required. Open: deployment-pipeline / Fabric `items` / semantic-model GET / My-workspace / group `dashboards` endpoints agents reached for (pipelines, Fabric `items` and group `dashboards` 404ed again in the 2026-10-06 live BI002 run); workspace name mismatch with the runbook; no golden values from Power BI Desktop |
| BI tasks (`commerce_analytics`, BI001, BI002) | Michael | done, saturated | all four arms valid and 100% for Opus (BI001 default 5/5 provisional, decision 8/8; BI002 8/8, 8/8): the stack and grading work, no training signal yet. Next: harder variants (vague brief, subtler faults, red herrings, stacked faults) |
| Better sandboxing | Andrew | partial | subject sandbox and containment P0-P7 pass; vendor state unforgeable (service-state mounted into native-services only); arm64 not measured |

**Power BI difficulty.** File formats are tractable, the engine is the hard part.

- **Project files: moderate.** PBIP (TMDL semantic model + PBIR report JSON) is GA and the
  default since September 2026, text-based and diffable: the agent edits measures,
  relationships and visuals in the repo, and graders read them structurally.
- **The engine: hard.** No production open-source DAX evaluator exists (parsers do; one
  teaching-subset evaluator). Real DAX semantics (filter context, context transition,
  `CALCULATE`, relationship direction, time intelligence) cannot be approximated without
  being silently wrong, so: a narrow DAX subset compiled to SQL on the warehouse slot,
  everything else refused by name. Power Query M: refuse beyond trivial source steps.
- **Calibration: the hidden cost.** Golden values must come from real Power BI (Windows +
  a licence) by differential testing, or tasks teach our engine's semantics, not Power BI's.
- **REST fake** (groups, datasets, refreshes, reports, `executeQueries` over the subset):
  moderate. **A Desktop UI clone:** out of scope for this sprint.
- **Overlap:** TMDL *is* a semantic layer. One measure/relationship -> SQL core can serve
  both Power BI and the semantic-layer item (and later LookML / dbt semantic models).

## 2. The slots

A slot is one kind of system a data worker touches. A profile picks one plug per slot, or
none. Agent interfaces follow the native-mode rule (`gateway/NATIVE.md:89-97`): the real
vendor CLI or SDK must work unmodified, and every action writes one 9.2.2 audit row on the
server side.

| slot | what the agent sees | plugs available today | where it plugs in (code) |
|---|---|---|---|
| `files` | `$HOME` and a git repo: csv, json, parquet, xlsx, fixed-width | csv, xlsx, EBCDIC (themes) | `world/world/themes/<t>/gen.py`, `workspace/` |
| `shell` | bash and the toolbelt | shipped | `gateway/docker/Dockerfile`, `TOOLBELT.md` |
| `notebook` | Jupyter server and kernels | none | new: episode image layer and kernel audit hook |
| `compute` | job or cluster submit, experiment tracking | none | new service |
| `warehouse` (1..n) | SQL over a vendor driver | duckdb, postgres, snowflake (fakesnow) | `warehouse.py:194` `create_warehouse`, `warehouse_spec.py:39` |
| `transform` | `dbt` against the warehouse slot | dbt-core 1.12 with dbt-duckdb only | `warehouses.py:68` `DBT_ADAPTERS` |
| `orchestrator` | scheduler CLI or UI/API | `./ops/sched` over `sched.internal` | `native/sched_plane.py`, `world/world/scheduler.py` |
| `object_store` | `aws s3`, boto3 | s3mock (MCP), MinIO (native, to replace) | `storage.py`, `compose*.yml` |
| `ingest` | connector API | Airbyte-shaped fake | `native/services/airbyte.py` |
| `bi` | BI REST API and/or project files | Metabase-shaped fake, YAML dashboards; Power BI, Tableau, Sigma fakes over `world.semantic` (partial) | `native/services/{metabase,powerbi,tableau,sigma}.py`, `world/world/bi.py`, `world/world/semantic/`, `world/world/bi_projects.py` |
| `observability` | metrics, alerts, logs | Prometheus, Alertmanager, Loki fakes | `native/services/{prometheus,alertmanager,loki}.py` |
| `release` | deploy CLI | GitHub-Deployments-shaped fake | `native/services/deploy.py` |
| `human` | tickets, chat, wiki | Jira, Slack, Confluence fakes | `native/services/{jira,slack,confluence}.py` |

---

## 3. Inventory: one row per tool

Status key: **shipped** means used by a task. **partial** means code exists but no task
uses it or a piece is missing. **designed** means docs only. **none** means nothing exists.

### 3.1 Files, notebooks, compute

| tool | status today | plug (local backing) | agent interface | licence | arm64 | effort | fences and notes |
|---|---|---|---|---|---|---|---|
| user filesystem | **shipped**: csv, xlsx, EBCDIC (themes) and, opt-in per seed, **parquet, json, jsonl** for any theme (`files: {formats: [...]}`, `world/world/files.py`) | theme generators + `world/world/files.py` | bash, python | n/a | yes | – | Each format plants one pathology (parquet: a money column that is decimal in one part and double in the other; json: JSON:API dump with `relationships` missing from ~2% of records; jsonl: a duplicated, out-of-order event and a swapped pair). Byte-identical across builds (DuckDB 1.5.5 parquet footer is pinned). Avro not done. |
| Python env | **shipped as roles, not built**: `ALKERA_TOOLBELT=de\|ds\|da`. ds/da install `requirements-{ds,da}.lock` into the system interpreter (what `python` and the kernel import); `/opt/toolbelt` keeps the CLIs | `build_episode.py --toolbelt` | python, pip (offline) | BSD/MIT | yes (aarch64 wheel hashes checked) | – | Per-arch toolbelt locks `toolbelt{,.arm64}.lock`; `toolbelt_check.py` asserts each role's packages present and other roles' absent. No image built (no Docker on the dev host). The `python` shim records only argv, exit and changed files (`NATIVE.md:48-50`). statsmodels not yet in the `ds` extra. |
| Jupyter | **partial**: audited kernel `alkera-python` (`native/notebook.py`, one 9.2.2 row per cell incl. silent and `user_expressions`; debugger refused; only kernel the server will start), launcher `gateway/docker/jupyter/launch.py`, compose service `jupyter` behind compose profile `notebook` | jupyter-server 2.21 + ipykernel 7 in ds/da images | Jupyter REST/WS (`jupyter.internal:8888`, `JUPYTER_URL`), nbclient | BSD-3 | yes | – | Verified off-container with real kernels; not yet run in a container. A `!duckdb` in a cell writes two rows by design (cell + shim). Not audited: writes by background threads a cell leaves running, and notebook saves via the contents API (same as bash edits). |
| ML compute nodes | **none** | CPU-only worker containers behind a job-submit API in the shape of the Databricks Jobs API (§3.2); **MLflow OSS** tracking and model registry | `mlflow` client, `databricks jobs submit` | Apache-2.0 | yes | M | No GPU. Size tasks for CPU, single-threaded, with seeded randomness (OpenMP thread-pool issue, `agent.md:680`). The resource cap is a cgroup limit per worker. Andrew's open item to watch host CPU applies here first. |

### 3.2 Warehouses and lakehouses

| tool | status today | plug | agent interface | licence | arm64 | effort | fences and notes |
|---|---|---|---|---|---|---|---|
| DuckDB | **shipped** (default, every GT task) | DuckDB file plus `duckdb` CLI shim | `duckdb`, python `duckdb` | MIT | yes | – | Baseline. The "laptop warehouse" persona. |
| Postgres | **partial**: real backend with role-scoped statement-log audit (`NATIVE_STACK.md:124-132`); `dbt-postgres` now in `requirements.lock` and `warehouses.dbt_output` renders its profile; no task uses it and `native_stack.py` starts no Postgres on the host topology (`NATIVE.md:130-131`) | real Postgres 16 in compose | `psql` 17, `psycopg`, `dbt-postgres` | PostgreSQL | yes (multi-arch digest) | S | dbt-postgres `debug`/`run` live in a container since 2026-10-07 (`postgres_e2e.json`). Doubles as the **OLTP source** plug for ingest tasks. |
| Snowflake | **partial**: fakesnow 0.11.18 behind a TLS proxy; `sql_*` incl. **atomic multi-statement** and **dbt-snowflake** (view + incremental, twice) verified in-process; clients in images built with `--warehouse-clients snowflake` | fakesnow server mode | `snowflake-connector-python`, `dbt-snowflake` | Apache-2.0 | yes | S left | Still fenced/open: DDL inside a batch refused (Snowflake auto-commits DDL); `NUMBER` arithmetic is float (typed `FLOAT`, not approximated); metadata lost on restart; no attach/join with other warehouses; TLS route not exercised by the new tests; no task selects it. New fences: division by zero inside writes, DDL inside an open transaction. Tests: `uv run --with-requirements docker/snowflake-dev.in pytest tests/test_snowflake_plug.py`. |
| Redshift | **none** (`oss_equivalent`; `R001/B1-summary.md:37`) | **Postgres persona**: PG 16 on `:5439`, `SET query_group` no-op, a shim schema for Redshift functions (`GETDATE`, `DATEADD`, `LISTAGG`, `NVL`, ...), sqlglot redshift→postgres at the proxy, and the Redshift Data API via a moto-shaped fake | `redshift_connector`, `psql`, `dbt-redshift`, `aws redshift-data` | PostgreSQL / Apache-2.0 | yes | M | Fence what PG cannot imitate: `SUPER`/PartiQL, `DISTKEY`/`SORTKEY` (accept and record, no effect), `UNLOAD`/`COPY FROM s3` (route to the object_store slot). A loud refusal must name the feature. |
| BigQuery | **partial**: `alkera_gateway/bigquery.py` against goccy/bigquery-emulator **v0.8.1** (sha256-pinned both arches; compose service `bigquery`, profile-gated) | emulator binary / multi-arch image | `google-cloud-bigquery` | MIT | yes (arm64 binary hash-checked, not run) | – | Fenced: no cost fields ever, dry runs refused ("bigquery cost estimation is not modelled"); no transactions (a failed batch says how many statements were applied); `rows_affected` counted by the connector since the emulator never reports it; client retries off (emulator 500s). No dbt-bigquery (a bigquery `dbt_target` is refused by name). No task selects it. |
| Databricks | **none** (workspace `none_found`; `B1-summary.md:30`) | **Tier 1, SQL:** a fake Databricks REST plane (SQL Statement Execution, Unity Catalog read APIs, Jobs 2.1 routed to the world scheduler, DBFS/Volumes onto object_store), backed by DuckDB with **delta-rs** tables. UC is seeded as data, three-level namespace. **Tier 2, DS profile only:** real local **PySpark** (`local[1]`) with Delta and UC OSS. | `databricks` CLI, `databricks-sdk`, `databricks-sql-connector`, `dbt-databricks`; tier 2 adds `pyspark` and notebooks | Apache-2.0 (delta-rs, Spark, UC OSS) | yes (JVM and wheels) | **L**: the biggest item on the list | `databricks-connect` is proprietary, so do not ship it. `calvinchengx/databricks-emulator` (Apache-2.0, clean-room, tested against the official SDK, CLI and Terraform) is a **reference to copy routes from, not a dependency** (one maintainer). delta-rs has a determinism problem in `_delta_log` (R004:402-415): normalise commit timestamps the way `world/` already does for dbt. UC OSS APIs "should not be assumed stable". |

### 3.3 Transform and orchestration

| tool | status today | plug | agent interface | licence | arm64 | effort | fences and notes |
|---|---|---|---|---|---|---|---|
| dbt | **shipped** (dbt-core 1.12.4): duckdb, postgres (base image), snowflake (`--warehouse-clients snowflake` images) | dbt-core plus one adapter per warehouse plug | `dbt` CLI, `profiles.yml` | Apache-2.0 | yes | S per adapter | `DBT_ADAPTERS` (`warehouses.py`) is the registry; `warehouses.dbt_output` renders remote outputs with `env_var(...)` so no secret lands in a file. bigquery is declared and refused (no dbt-bigquery); redshift/databricks not yet. |
| scheduler (`./ops/sched`) | **shipped** | world `WorldScheduler` over `sched.internal:8120` | `./ops/sched list\|logs\|trigger` | ours | yes | – | Default orchestrator. It has an exact MCP twin, which matters for comparisons across drivers. |
| Airflow | **partial**: Airflow 3 REST v2 fake over the world ledger (`native/services/airflow.py` over `world/world/airflow_view.py`, `airflow.internal:8100`, 2026-10-07); real headless Airflow is still only *designed* (compose profile `airflow`) and the prototype archived (`_archive/world/`) | the REST surface over `var/sched/state.json`: DAGs from the workspace's `dags/` (`ast`), runs and logs from the ledger, trigger and clear queued (`world.scheduler.enqueue`) for the grader's `advance` | REST `/api/v2` (JWT from `/auth/token`), `airflowctl` (`requirements-airflow.lock`); no `airflow` CLI (no metadata DB) | Apache-2.0 (airflowctl) | yes (pure-Python lock) | done | Orchestrator plug, selected per task with `orchestrator = airflow`; `ops_sched` stays the default (Decision 3). No backfill and no pause (400, named); a clear names one DAG run. A trigger queues: nothing runs and `vnow` does not move until the grader advances, so the ledger and observability stay true. MCP `sched_*` read the same ledger (a queued run is in `sched_state`'s `queue`). |

### 3.4 Storage and ingestion

| tool | status today | plug | agent interface | licence | arm64 | effort | notes |
|---|---|---|---|---|---|---|---|
| S3 | **shipped** (s3mock in MCP compose, MinIO in native) | **adobe/s3mock everywhere** (Decision 4) | `aws s3`, boto3 | Apache-2.0 | verify digest | S | The MinIO audit path is the webhook receiver (`NATIVE_STACK.md:133`); s3mock has none, so add a request-log tailer or a thin proxy that writes the audit rows. |
| GCS / ADLS | **none** | `fake-gcs-server` (BigQuery profile), Azurite (Databricks-on-Azure or Power BI profile) | `gsutil`/`gcloud storage`, `az storage` | MIT / MIT | yes | S each | Only add with a task that needs it. |
| Ingest connectors | **shipped** (Airbyte-shaped fake; only `sftp_vendor` seeds it) | the fake stays, since it is API-shape only and runs no Elastic-2.0 code. Add **dlt** as the real-code loader plug. | Airbyte REST, `dlt` | n/a / Apache-2.0 | yes | S | `revenue_daily` seeds no ingest, so the service answers 503 (`NATIVE_STACK.md:111-117`). A profile that plugs `ingest` must use a theme that seeds it. |
| Kafka / streaming | **none** (GT002 is a simulated stalled consumer) | Apache Kafka (KRaft) if a task needs a live broker | `kafka-console-*`, python client | Apache-2.0 | yes | M | Redpanda is BSL, so excluded (`A5:122`). R004's streaming recommendation names Redpanda; that conflicts with the licence rule. |

### 3.5 BI

Nothing offline exists for any commercial BI vendor (`B1-summary.md:31-33`). The plan is
**file-format first, API second**: the agent edits the vendor's own text project format in
the repo (gradeable, diffable, no running vendor binary), and a fake REST service covers
refresh, publish and query.

| tool | status today | plug | agent interface | effort | fences and notes |
|---|---|---|---|---|---|
| Metabase | **shipped** as a fake (`/api/card/{id}/query`) | keep | Metabase API | – | Licence NOASSERTION, so never ship the real binary. |
| Superset | none | real Superset as the OSS BI "escape hatch" (T1) | Superset UI/API | M | Apache-2.0. Only if a DA task needs a real BI server. |
| Power BI | **partial** (`world/world/semantic/formats/powerbi/`, `native/services/powerbi.py`; uncalibrated; BI001) | **PBIP project** in the repo (TMDL semantic model plus report definition files), plus a fake Power BI REST plane (`groups`, `datasets`, `refreshes`, `reports`, export) | files, `pbi`-style REST via python | M | `executeQueries` (DAX) refuses loudly, or accepts a narrow DAX subset compiled to SQL against the warehouse slot. Never silently approximate DAX. |
| Tableau | **partial** (`formats/tableau/`, `native/services/tableau.py`; uncalibrated; BI001) | `.twb`/`.tds` XML in the repo, plus a fake Tableau REST plane (sites, workbooks, datasources, refresh) | `tableauserverclient` (real client per NATIVE rule) | M | **Do not depend on the Hyper API**: no Linux arm64 wheel and no plans for one. Point extracts at a live connection to the warehouse slot. |
| Sigma | **partial** (`formats/sigma/`, `native/services/sigma.py`; uncalibrated; BI001) | fake Sigma REST v2 (workbooks, datasets, connections, export); workbook elements compile to SQL over the warehouse slot | REST via python | M | Cloud-only product, so a fake is the only option. Sigma's model *is* "SQL on the warehouse", which makes the fake cheap to keep honest: its numbers come from the warehouse, never from a fixture. |

### 3.6 Ops and human planes (shipped, listed for completeness)

Jira, Slack, Confluence, Prometheus, Alertmanager, Loki and deploy are all shipped fakes
(`NATIVE.md:53-69`). Known open item: authoring metadata leaks into
`var/workflow/*.json` and `var/obs/definitions.yaml`, which are readable from bash
(`NATIVE.md:134-136`). That is a reward-hacking surface, so fix it before any new profile
ships.

### 3.7 Content (databases, sizes)

Every theme is small: 30 days, roughly 40–60 rows/day, under 2k fact rows. That is enough
for correctness tasks and far too small for the "realistic" bar. Proposed size tiers, a
seed parameter per theme:

| tier | fact rows | purpose |
|---|---|---|
| `xs` (today) | ~1k–2k | validity ladders, CI |
| `m` | ~1M | query must be selective; `SELECT *` into pandas starts to hurt |
| `l` | ~50M | DE performance and partitioning tasks; DuckDB and Spark both fine on one node |

data-eng-bench's 579-table retail DuckDB is the reference for **schema breadth**: wide
catalogues where finding the right table is part of the task. A size tier must still pass
the fingerprint gate. Hash cost grows with rows, so measure `fingerprint.py` at `l` before
committing to it.

---

## 4. Profiles: what plugs in for each environment

### 4.1 By role

● required · ○ optional (task decides) · ✕ excluded (absent from the box, not just hidden)

| slot | Data engineering | Data science | Data analysis |
|---|---|---|---|
| files | ● csv/json/parquet/landing zones | ● parquet/csv datasets | ● csv/xlsx exports |
| shell / toolbelt | ● DE toolbelt | ● DS toolbelt (pandas, sklearn, ...) | ● DA toolbelt (lighter) |
| notebook | ○ | ● Jupyter | ○ Jupyter |
| compute | ✕ | ● workers + MLflow | ✕ |
| warehouse | ● 1..n (vendor stack, §4.2) | ● 1 (read-mostly) | ● 1 (read-only role) |
| transform (dbt) | ● | ○ | ○ (read models, propose changes) |
| orchestrator | ● | ○ (scheduled retrain) | ✕ |
| object_store | ● | ● (datasets, artifacts) | ○ |
| ingest | ○ | ✕ | ✕ |
| bi | ○ (downstream consumer) | ✕ | ● (vendor stack, §4.2) |
| observability | ● | ○ | ✕ |
| release | ○ | ✕ | ✕ |
| human | ● | ● | ● (stakeholder asks live here) |

The current GT001–GT009 tasks are all **DE on the DuckDB stack**.

### 4.2 By vendor stack (fills the warehouse, transform, object_store and bi slots)

| stack | warehouse | transform | object_store | bi | notes |
|---|---|---|---|---|---|
| `laptop` (today) | DuckDB | dbt-duckdb | s3mock | Metabase fake | what every GT task runs on |
| `snowflake` | fakesnow (+ DuckDB as a legacy peer) | dbt-snowflake | s3mock (stages) | Sigma or Tableau fake | closest to ready (§3.2) |
| `databricks` | Databricks fake, tier 1 (tier 2 for DS) | dbt-databricks | s3mock as DBFS/Volumes, or Azurite | Power BI | the long pole |
| `aws` | Redshift persona (+ Postgres OLTP source) | dbt-redshift | s3mock | Tableau or Superset | |
| `gcp` | BigQuery emulator | dbt-bigquery | fake-gcs-server | Metabase fake or Superset | cost fenced |
| `postgres` | Postgres | dbt-postgres | s3mock | Metabase fake | the cheapest second backend; it proves the plug path works |

A profile is role × stack, for example `de/snowflake` or `ds/databricks`. The same seed
under two stacks should produce the same logical data. data-eng-bench runs the same 103
tasks on DuckDB or Snowflake to isolate dialect failures, and we can get that for free if
`warehouse_seed.py` seeds every warehouse plug from one set-based source.

### 4.3 Declaring a profile

The catalog, roles and stacks live in `gateway/src/alkera_gateway/plugs.py`. A task names
a profile in `task.toml`, and may adjust any slot: name other plugs, add a slot the role
lacks, or remove one with an empty list.

```toml
[environment]
profile  = "de/snowflake"
surfaces = ["snowflake", "dbt", "git", "scheduler", "bi"]   # each must be provided

[environment.plugs]
warehouse    = ["snowflake", "duckdb"]   # primary first; duckdb as a legacy peer
orchestrator = "airflow"                 # a plug instead of the stack default
notebook     = []                        # absent from the box, not just unlisted
```

The gateway config takes the same pair (`"profile"`, `"plugs"`), and native mode takes
`serve --profile de/snowflake --plug orchestrator=airflow --plug notebook=`.
`alkera-gateway plugs show <profile> [--plug SLOT=PLUG]` prints what a profile
plugs in.

### 4.4 Readiness, generated

Output of `alkera-gateway plugs matrix`. A profile is `runnable` when none of
its plugs is `planned`; `partial` plugs (Postgres, Snowflake, BigQuery, Jupyter, Power BI, Tableau,
Sigma) run with the gaps §3 lists. Regenerate this block whenever a plug's status changes
(regenerated 2026-10-04: Sigma, Tableau and Power BI are `partial`, so `*/snowflake` is
runnable and `*/aws`, `*/databricks` wait only on their warehouses).

```
de/laptop        runnable
de/postgres      runnable
de/snowflake     runnable
de/databricks    planned: databricks
de/aws           planned: redshift
de/gcp           runnable
ds/laptop        planned: ml_workers, mlflow
ds/postgres      planned: ml_workers, mlflow
ds/snowflake     planned: ml_workers, mlflow
ds/databricks    planned: ml_workers, mlflow, databricks
ds/aws           planned: ml_workers, mlflow, redshift
ds/gcp           planned: ml_workers, mlflow
da/laptop        runnable
da/postgres      runnable
da/snowflake     runnable
da/databricks    planned: databricks
da/aws           planned: redshift
da/gcp           runnable
```

---

## 5. Making include/exclude real

Before 2026-10-02 there was no tool include/exclude: `build_tools` returned the global
`TOOLS` table by identity, `native-services` always mounted all nine services, and a plane
was "absent" only because the world did not seed it. All five points are now implemented
in code and unit-tested; nothing container-side (compose, images) has been run, because
the dev host has no Docker.

Five enforcement points, each needed:

1. **World build, done.** `profile: {name, slots}` in the seed: an unplugged slot leaves
   no state (no `var/workflow`, `var/ingest`, `var/obs`, dashboards), `.envrc` /
   onboarding / `services.toml` name nothing from it, and the profile is a conditional
   fingerprint section (`var/world/profile.json`), so two profiles over one seed hash
   differently. Unflagged seeds reproduce every recorded hash.
2. **Gateway (MCP), done.** `GatewayConfig.profile` filters `build_tools` by slot, but only when
   a profile is declared, so today's "returns `TOOLS` by identity" invariant, which
   `test_image_parity`, `containment.py` and `register_bash` depend on, still holds for
   unprofiled runs.
3. **Native services, done.** `serve --profile` loads only the profile's services and
   drops the scheduler plane and the object store's audit receiver when their slots are
   unplugged; the per-rollout compose overlay replaces the `*.internal` alias list
   (`!override`), drops `minio.internal` without an object store, `!reset`s the excluded
   services' endpoint variables, and narrows the endpoint map the destination audit and
   the surface gate read. Not run: compose `!override`/`!reset` need Compose >= 2.24.4; the
   bare service name `minio` still resolves (known, `NATIVE_STACK.md` weakest finding).
4. **Episode image, done (not built).** `build_episode.py --toolbelt {de,ds,da}
   --warehouse-clients snowflake` -> build args and tags (`alkera-episode:head-ds`). An
   excluded package is **not installed**, never PATH-masked; `toolbelt_check.py` fails an
   image where another role's package imports.
5. **Harness, done.** `taskspec.py` refuses a task whose `surfaces` the profile does not
   provide (all nine GT tasks fit `de/laptop` unchanged); `rollout.py` refuses a planned
   plug before any world or container exists, then refuses a world whose recorded profile
   disagrees with `task.toml` in either direction; every gateway config the harness writes
   carries `profile`/`plugs`, and native-services gets `--profile`/`--plug`.

**Anti-reward-hack rules that come with plugging:**

- **The grader never reads through a plug.** Grading reads the world's own store (DuckDB
  file, ledger, workflow JSON) in the `grading` zone. A fake service that could be
  talked into lying cannot move the score. This already holds, and every new plug must
  keep it.
- **Every plug writes the 9.2.2 row server-side.** A plug without an audit path (s3mock
  today, Spark) does not ship. The Jupyter kernel now writes one row per cell, and its
  container strips the same paths the episode's does (`launch.py --strip`). Otherwise the world-diff rule turns every
  rollout that uses it into a hard violation, or worse, the rule gets relaxed.
- **Fidelity fences are loud.** An unsupported feature returns an error naming the
  feature, never a plausible wrong answer (T2's rule, extended to every fake).
- **No authoring metadata reachable from any slot** (the §3.6 leak).
- **The profile is part of the fingerprint.** Two profiles over one seed are two worlds.
  `semantic_hash` must differ, or a measurement can be silently compared across stacks.

---

## 6. Linux arm64 and x86_64

The dev host is x86_64 and every evidence file records x86_64. Nothing has been built or
run on arm64 yet, but every input has been checked (2026-10-02, registry API and PyPI):

- **Digest-pinned images are all multi-arch indexes** (python, postgres, s3mock,
  pgsty/minio, airflow, and the new bigquery-emulator 0.8.1): nothing needed re-pinning.
- **Locks:** `toolbelt.arm64.lock` beside `toolbelt.lock`; `requirements-{ds,da}.lock` and
  the gateway locks carry aarch64 wheel hashes (`tests/test_toolbelt_roles.py` checks an
  installable wheel per arch against PyPI). All 18 apt pins exist for arm64 in the pinned
  Debian snapshot. A stale `toolbelt.lock` had only SQLAlchemy's sdist hash (every build
  compiled it from source); re-locked at the same version.
- **The DuckDB `postgres` extension** for 1.5.5 is published for linux_arm64.
- **dbt-core-experimental-parser 2.0.0rc2** is sdist-only and downloads a hash-checked
  wheel from github.com at build time, on both arches.
- **Tableau Hyper API**: x86_64 only on Linux, which is why §3.5 avoids it.
- **Re-measure containment P0–P7 and the fingerprint gate on arm64.** Cross-arch
  `semantic_hash` equality is the claim to prove, since float formatting and sort
  collation are the usual suspects.

---

## 7. Where Andrew's list conflicts with earlier research

| item | earlier verdict | now | decision |
|---|---|---|---|
| Spark | "Do not run Spark. Ever." (R004 streaming:22) | Databricks and ML compute are on the list | in: Databricks tier 1 + tier 2 (DS); Spark `local[1]`, determinism measured before any task depends on it (Decision 1) |
| BigQuery | **partial**: `alkera_gateway/bigquery.py` against goccy/bigquery-emulator **v0.8.1** (sha256-pinned both arches; compose service `bigquery`, profile-gated) | emulator binary / multi-arch image | `google-cloud-bigquery` | MIT | yes (arm64 binary hash-checked, not run) | – | Fenced: no cost fields ever, dry runs refused ("bigquery cost estimation is not modelled"); no transactions (a failed batch says how many statements were applied); `rows_affected` counted by the connector since the emulator never reports it; client retries off (emulator 500s). No dbt-bigquery (a bigquery `dbt_target` is refused by name). No task selects it. |
| Airflow | full deployment "Reject" (R004 orchestration:549); prototype archived | on the list | in, orchestrator plug; ops_sched default (Decision 3) |
| MinIO | licence-rejected (B1) | still in `compose.native.yml` | replace with s3mock (Decision 4) |
| Redpanda | BSL landmine (A5:122), yet recommended in R004 streaming:21 | n/a | Apache Kafka if a live broker is ever needed |
| Harbor | "ADOPT" as harness and format (S300) | built 2026-10-03 (`harbor/`): every arm (28) exports as a Harbor task + `dataset.toml`; accepted by harbor 0.23.0 and by its gate (oracle 1, nop 0 on all 28) | single-container `environment/Dockerfile` (not compose: portable to Dockerfile-only providers, and Harbor's egress sidecar shares one netns), separate verifier `tests/Dockerfile` whose `verify.py` wraps `grade.py`; `reward.json` = the pass bit (pass@k) or `reward.py`'s scalar with `--reward training`. Grader hardening in progress (`harbor/README.md`) |

---

## 8. Build order for the remaining sprint (proposal)

Ordered by "proves the plug mechanism" first, then by how many profiles each unlocks.
Status 2026-10-04: items 1, 2, 4 and 6 are built; 5 is half done (BigQuery partial,
Redshift not started); 3, 7 and 8 are open. 2 is task-ready except the TLS route and a
task. 6 went further than one plug: the semantic core with Power BI, Tableau and Sigma
skins (all `partial`: no golden values from the real products), the `commerce_analytics`
theme and BI001 on `da/laptop`. The first container runs are BI001's native oracle (episode
image `alkera-episode:head-da`).

1. **The plug mechanism itself** (§5 points 1–5) with the existing plugs: DuckDB and
   Postgres as two warehouse plugs, plus include/exclude on the nine native services.
   Proof: GT001 under `de/laptop` and `de/postgres`, the same grader passing the oracle.
2. **Snowflake to task-ready:** connector and `dbt-snowflake` in the image, the
   multi-statement fix, an audit tailer. *Built (task-ready); no task selects it.*
3. **s3mock in native** (MinIO out) and the authoring-metadata leak fixed.
4. **DS toolbelt + Jupyter with the per-cell audit hook.** This unlocks the first
   non-DE profile. *Built.*
5. **Redshift persona and BigQuery emulator** (each M, independent, can run in parallel).
   *BigQuery built (emulator, `partial`); Redshift not started.*
6. **One BI file-format plug end to end** (Power BI PBIP or Tableau `.twb`). This unlocks
   the DA profile. *Built: the semantic core, all three skins (`partial`, uncalibrated),
   `commerce_analytics`, BI001 (`docs/environments/BI_BUILD_PLAN.md`, "Status
   2026-10-04").*
7. **Databricks tier 1**, then MLflow and compute workers, then Databricks tier 2.
8. **arm64 build and re-measure** (§6). Do it as soon as the new Linux machine is up, not
   last.

---

## 9. Benchmarks and which slots they exercise

For the task-sourcing survey (Michael, due Friday). The first five are in the repo's
research record; the last four have **not been surveyed yet**.

| benchmark | role | backing | slots exercised | repo verdict |
|---|---|---|---|---|
| Snowflake data-eng-bench (103 tasks) | DE/DA | DuckDB or real Snowflake (`DB_TYPE`) | warehouse, transform | ADOPT tasks + grading pattern (S302) |
| ade-bench (dbt Labs, 43+ tasks) | DE/DA | DuckDB + dbt | warehouse, transform | COPY-PATTERN (S304) |
| DataAgentBench (54 queries, 4 DBMSes) | DA | multi-DBMS | warehouse (multi), files | COPY-PATTERN (S315) |
| MLE-Bench (75 Kaggle comps) | DS | files + Python | files, notebook, compute | COPY-PATTERN, not a deliverable (S307) |
| Terminal-Bench 2/3 | general | Harbor | shell | COPY-PATTERN, superseded by Harbor (S306) |
| Spider 2.0, DA-Code, DSBench, BIRD | DA/DS | various | – | **not yet surveyed** |

---

## Sources

- Repo research record: `docs/implementation_plan/structure/rounds/R001/B1-summary.md`,
  `R002/T1-mock-priority.md`, `R002/T2-snowflake.md`, `R003/A5-licensed-surfaces.md`,
  `R004/RESEARCH-{warehouse,orchestration,streaming}.md`,
  `structure/state/failures/B1-license-rejections.yaml`,
  `docs/research/llm-judge/r1_data_eng_bench.md`.
- BigQuery emulator, multi-arch releases: https://github.com/goccy/bigquery-emulator/releases
- fakesnow releases (0.11.17/0.11.18, 2026-09): https://github.com/tekumara/fakesnow/blob/main/CHANGELOG.md
- Databricks emulator (reference only): https://github.com/calvinchengx/databricks-emulator
- MinIO community images discontinued 2025-10: https://gigazine.net/gsc_news/en/20251023-minio-stops-distributing-free-docker-images/
- Tableau Hyper API, no Linux aarch64: https://github.com/tableau/hyper-api-samples/issues/100
- Redshift-on-Postgres images (prior art for the persona): https://github.com/HearthSim/docker-pgredshift
