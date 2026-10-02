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

## 1. Decisions needed Friday

1. **Spark is in or out.** R004 says "Do not run Spark. Ever."
   (`structure/rounds/R004/RESEARCH-streaming.md:22`). Andrew's list has Databricks and
   ML compute nodes. §5 recommends a two-tier Databricks plug: SQL-only over DuckDB/Delta
   first, and real local PySpark only for the DS profile.
2. **MinIO has to go.** It was rejected on licence (AGPL, archived;
   `structure/state/failures/B1-license-rejections.yaml:3-6`) and has shipped no community
   images since 2025-10. `gateway/docker/compose.native.yml:388` still pulls
   `pgsty/minio`. Swap it for `adobe/s3mock`, which `compose.yml:76` already uses.
3. **Airflow becomes a native-only plug.** It was kept out so the MCP and native arms
   would match (`gateway/NATIVE.md:72-76`). The recommendation is to keep `./ops/sched`
   as the default orchestrator and offer headless Airflow (R004 recipe) only in native
   profiles, where no MCP twin exists to stay comparable with.
4. **BigQuery goes back in, fenced.** R004 said "works; don't use" because
   `totalBytesBilled` is inverted. The emulator now ships multi-arch builds. Adopt it for
   SQL and API, and never author a task on cost or bytes billed.
5. **`[environment].surfaces` in `task.toml` becomes the profile.** Today it is
   descriptive only (`tasks/harness/taskspec.py:832-833` reads just `seed` and
   `virtual_now`), and no tool include/exclude exists anywhere (§4).

---

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
| `bi` | BI REST API and/or project files | Metabase-shaped fake, YAML dashboards | `native/services/metabase.py`, `world/world/bi.py` |
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
| user filesystem | **shipped** for csv, xlsx and EBCDIC (`themes/{revenue_daily,excel_close,mainframe_batch}`) | theme generators, plus new generators for **parquet, json/jsonl, avro** | bash, python | n/a | yes | S | No pandas or pyarrow in any lock today, so a parquet file is unreadable in the box; ship `pyarrow` and `duckdb` reading in the DS/DA toolbelt. Each format needs a seeded pathology (schema drift, nested json, type coercion) or it fails `TOOLBELT.md`'s "bar 2". |
| Python env | **partial**: `/usr/local/bin/python` plus `/opt/toolbelt` (awscli, sqlfluff, csvkit) | per-profile toolbelt lock: DS adds pandas, polars, pyarrow, scikit-learn, statsmodels, matplotlib | python, pip (offline) | BSD/MIT | yes | S | Locks are compiled `--python-platform x86_64` (`toolbelt.lock:2`); re-compile per arch (§6). The `python` shim records only argv, exit and changed files (`NATIVE.md:48-50`), a known loss of audit resolution. |
| Jupyter | **none** (rated `native_oss`, probed; `R002/T1-mock-priority.md:33`) | real `jupyter-server` with an `ipykernel` on the DS toolbelt; `nbclient`/`papermill` for headless runs | JupyterLab REST/WS, `jupyter nbconvert --execute`, `.ipynb` in the repo | BSD-3 | yes | M | Needs an **IPython `post_run_cell` hook that writes a 9.2.2 row per cell**, or notebook work escapes the audit log, which `NATIVE.md` treats as a hard violation. Seeded stale kernel state is the known risk (T1). Grade the `.ipynb` outputs and the world, never the kernel's memory. |
| ML compute nodes | **none** | CPU-only worker containers behind a job-submit API in the shape of the Databricks Jobs API (§3.2); **MLflow OSS** tracking and model registry | `mlflow` client, `databricks jobs submit` | Apache-2.0 | yes | M | No GPU. Size tasks for CPU, single-threaded, with seeded randomness (OpenMP thread-pool issue, `agent.md:680`). The resource cap is a cgroup limit per worker. Andrew's open item to watch host CPU applies here first. |

### 3.2 Warehouses and lakehouses

| tool | status today | plug | agent interface | licence | arm64 | effort | fences and notes |
|---|---|---|---|---|---|---|---|
| DuckDB | **shipped** (default, every GT task) | DuckDB file plus `duckdb` CLI shim | `duckdb`, python `duckdb` | MIT | yes | – | Baseline. The "laptop warehouse" persona. |
| Postgres | **partial**: real backend with role-scoped statement-log audit (`NATIVE_STACK.md:124-132`), but no task uses it and `native_stack.py` starts no Postgres on the host topology (`NATIVE.md:130-131`) | real Postgres 16 in compose | `psql` 17, `psycopg` | PostgreSQL | yes (verify digest) | S | Needs **`dbt-postgres`**, which is absent from every lock. Doubles as the **OLTP source** plug for ingest tasks. |
| Snowflake | **partial**: fakesnow 0.11.16 behind a TLS proxy, 12/12 socket checks (`evidence/snowflake_socket.json`), fidelity patches (`snowflake_fidelity.py`), but no task selects it | fakesnow server mode (upstream is at 0.11.18 as of 2026-09-22) | `snowflake-connector-python`, `snowsql`-style CLI, `dbt-snowflake` | Apache-2.0 | yes (pure python plus duckdb) | M: R004 sized fakesnow hardening at 8–12 days | Gaps to close first: the connector is **not in the episode image locks** (only the optional `snowflake.lock`); **no `dbt-snowflake`**; multi-statement refused; no attach or join with other warehouses; `NUMBER` arithmetic returns float; metadata lost on restart (`snowflake_fidelity.py:84-141`). The rule stands: "loud unsupported is fine, silent wrong is disqualifying" (T2). |
| Redshift | **none** (`oss_equivalent`; `R001/B1-summary.md:37`) | **Postgres persona**: PG 16 on `:5439`, `SET query_group` no-op, a shim schema for Redshift functions (`GETDATE`, `DATEADD`, `LISTAGG`, `NVL`, ...), sqlglot redshift→postgres at the proxy, and the Redshift Data API via a moto-shaped fake | `redshift_connector`, `psql`, `dbt-redshift`, `aws redshift-data` | PostgreSQL / Apache-2.0 | yes | M | Fence what PG cannot imitate: `SUPER`/PartiQL, `DISTKEY`/`SORTKEY` (accept and record, no effect), `UNLOAD`/`COPY FROM s3` (route to the object_store slot). A loud refusal must name the feature. |
| BigQuery | **none**; R004 tested it and ranked it "do not fund" | `goccy/bigquery-emulator` (v0.8.1, multi-arch) | `bq` CLI, `google-cloud-bigquery`, `dbt-bigquery` | MIT | **yes** (multi-arch image since 2026) | M | **Fence `totalBytesBilled` and dry-run cost** (inverted vs real BQ, `R004/RESEARCH-warehouse.md:280-300`). No task may grade on cost. GCS needs `fake-gcs-server` in the object_store slot. |
| Databricks | **none** (workspace `none_found`; `B1-summary.md:30`) | **Tier 1, SQL:** a fake Databricks REST plane (SQL Statement Execution, Unity Catalog read APIs, Jobs 2.1 routed to the world scheduler, DBFS/Volumes onto object_store), backed by DuckDB with **delta-rs** tables. UC is seeded as data, three-level namespace. **Tier 2, DS profile only:** real local **PySpark** (`local[1]`) with Delta and UC OSS. | `databricks` CLI, `databricks-sdk`, `databricks-sql-connector`, `dbt-databricks`; tier 2 adds `pyspark` and notebooks | Apache-2.0 (delta-rs, Spark, UC OSS) | yes (JVM and wheels) | **L**: the biggest item on the list | `databricks-connect` is proprietary, so do not ship it. `calvinchengx/databricks-emulator` (Apache-2.0, clean-room, tested against the official SDK, CLI and Terraform) is a **reference to copy routes from, not a dependency** (one maintainer). delta-rs has a determinism problem in `_delta_log` (R004:402-415): normalise commit timestamps the way `world/` already does for dbt. UC OSS APIs "should not be assumed stable". |

### 3.3 Transform and orchestration

| tool | status today | plug | agent interface | licence | arm64 | effort | fences and notes |
|---|---|---|---|---|---|---|---|
| dbt | **shipped** (dbt-core 1.12.4, dbt-duckdb 1.11.0) | dbt-core plus **one adapter per warehouse plug**: duckdb (have), postgres, snowflake, redshift, bigquery, databricks | `dbt` CLI, `profiles.yml` | Apache-2.0 | yes | S per adapter | `DBT_ADAPTERS` (`warehouses.py:68`) is the registry. Adapters go in the per-profile lock, not the gateway's own lock (same reasoning as awscli, `TOOLBELT.md:50-55`). |
| scheduler (`./ops/sched`) | **shipped** | world `WorldScheduler` over `sched.internal:8120` | `./ops/sched list\|logs\|trigger` | ours | yes | – | Default orchestrator. It has an exact MCP twin, which matters for comparisons across drivers. |
| Airflow | **designed** (compose profile `airflow`, `compose.native.yml:441`); the headless prototype is archived (`_archive/world/`) | headless Airflow behind the 12-line clock shim (R004: byte-identical `dag_run` rows) | `airflow dags/tasks` CLI, REST API, DAG files in the repo | Apache-2.0 | yes (verify digest) | M (R004 8-step ship list) | Native-only plug (Decision 3). **Never** expose `airflow dags backfill` with real time: the clock is the world's. A trigger must go through the executor so the ledger and observability stay true. |

### 3.4 Storage and ingestion

| tool | status today | plug | agent interface | licence | arm64 | effort | notes |
|---|---|---|---|---|---|---|---|
| S3 | **shipped** (s3mock in MCP compose, MinIO in native) | **adobe/s3mock everywhere** (Decision 2) | `aws s3`, boto3 | Apache-2.0 | verify digest | S | The MinIO audit path is the webhook receiver (`NATIVE_STACK.md:133`); s3mock has none, so add a request-log tailer or a thin proxy that writes the audit rows. |
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
| Power BI | none | **PBIP project** in the repo (TMDL semantic model plus report definition files), plus a fake Power BI REST plane (`groups`, `datasets`, `refreshes`, `reports`, export) | files, `pbi`-style REST via python | M | `executeQueries` (DAX) refuses loudly, or accepts a narrow DAX subset compiled to SQL against the warehouse slot. Never silently approximate DAX. |
| Tableau | none | `.twb`/`.tds` XML in the repo, plus a fake Tableau REST plane (sites, workbooks, datasources, refresh) | `tableauserverclient` (real client per NATIVE rule) | M | **Do not depend on the Hyper API**: no Linux arm64 wheel and no plans for one. Point extracts at a live connection to the warehouse slot. |
| Sigma | none | fake Sigma REST v2 (workbooks, datasets, connections, export); workbook elements compile to SQL over the warehouse slot | REST via python | M | Cloud-only product, so a fake is the only option. Sigma's model *is* "SQL on the warehouse", which makes the fake cheap to keep honest: its numbers come from the warehouse, never from a fixture. |

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

### 4.3 Profile file, sketch

```yaml
# tasks/<task>/task.toml [environment] would name one of these; world/profiles/<name>.yaml
name: de/snowflake
slots:
  warehouse:   [{name: primary, plug: snowflake, dbt_target: true},
                {name: legacy,  plug: duckdb, read_only: true}]
  transform:   dbt
  orchestrator: ops_sched
  object_store: s3mock
  ingest:      airbyte_fake
  bi:          sigma_fake
  observability: [prometheus, alertmanager, loki]
  human:       [jira, slack, confluence]
  notebook:    null          # null = absent from the box, not just unlisted
  compute:     null
toolbelt: de                 # which pinned lock the episode image installs
```

---

## 5. Making include/exclude real

There is no tool include/exclude today. `build_tools` returns the global `TOOLS` table by
identity (`warehouses.py:464-482`), `native-services` always mounts all nine services, and
a plane is "absent" only because the world did not seed it, so its tools return errors.
Five enforcement points, each needed:

1. **World build.** `profile:` in the seed decides which planes `world.build` seeds. An
   unplugged slot leaves no state behind.
2. **Gateway (MCP).** `GatewayConfig.profile` filters `build_tools` by slot, but only when
   a profile is declared, so today's "returns `TOOLS` by identity" invariant, which
   `test_image_parity`, `containment.py` and `register_bash` depend on, still holds for
   unprofiled runs.
3. **Native services.** Filter `services.NAMES` in `native/serve.py` and emit only the
   matching `*.internal` aliases. An excluded service must not resolve, so P6 checks it,
   instead of resolving and answering 404.
4. **Episode image.** One image per toolbelt (`de`, `ds`, `da`), built by
   `build_episode.py`, with each warehouse plug's client and dbt adapter as a layer. An
   excluded binary is **not installed**, never removed by PATH masking: a masked binary is
   one `find /` away.
5. **Harness.** `[environment].surfaces` becomes authoritative. `taskspec.py` checks it
   against `[harness].required_surfaces` and `dead_by_design`, and refuses a task whose
   required surface is in an unplugged slot.

**Anti-reward-hack rules that come with plugging:**

- **The grader never reads through a plug.** Grading reads the world's own store (DuckDB
  file, ledger, workflow JSON) in the `grading` zone. A fake service that could be
  talked into lying cannot move the score. This already holds, and every new plug must
  keep it.
- **Every plug writes the 9.2.2 row server-side.** A plug without an audit path (s3mock
  today, a Jupyter kernel, Spark) does not ship. Otherwise the world-diff rule turns every
  rollout that uses it into a hard violation, or worse, the rule gets relaxed.
- **Fidelity fences are loud.** An unsupported feature returns an error naming the
  feature, never a plausible wrong answer (T2's rule, extended to every fake).
- **No authoring metadata reachable from any slot** (the §3.6 leak).
- **The profile is part of the fingerprint.** Two profiles over one seed are two worlds.
  `semantic_hash` must differ, or a measurement can be silently compared across stacks.

---

## 6. Linux arm64 and x86_64

The host this was written on is x86_64, and every evidence file records x86_64. Nothing
has been built on arm64 yet. Known blockers:

- **Digest-pinned images** (`python@sha256:9534e5a8…`, `postgres@sha256:…`, `s3mock`,
  `airflow`): a digest can name a single-arch manifest. Run
  `docker buildx imagetools inspect` on each and re-pin to the **index digest**.
- **`toolbelt.lock`** was compiled `--python-platform x86_64-unknown-linux-gnu`. Compile
  one lock per arch, or compile with `--universal`.
- **The DuckDB `postgres` extension** is fetched at build time per arch. Pin both.
- **Tableau Hyper API**: x86_64 only on Linux, which is why §3.5 avoids it.
- **Re-measure containment P0–P7 and the fingerprint gate on arm64.** Cross-arch
  `semantic_hash` equality is the claim to prove, since float formatting and sort
  collation are the usual suspects.

---

## 7. Where Andrew's list conflicts with earlier research

| item | earlier verdict | now | recommendation |
|---|---|---|---|
| Spark | "Do not run Spark. Ever." (R004 streaming:22) | Databricks and ML compute are on the list | Tier 1 Databricks with no Spark; Spark only in the DS profile, `local[1]`, with determinism measured before any task depends on it |
| BigQuery | "works; don't use" (R004 warehouse:152) | on the list | adopt with the cost fence (Decision 4) |
| Airflow | full deployment "Reject" (R004 orchestration:549); prototype archived | on the list | headless, native-only plug (Decision 3) |
| MinIO | licence-rejected (B1) | still in `compose.native.yml` | replace with s3mock (Decision 2) |
| Redpanda | BSL landmine (A5:122), yet recommended in R004 streaming:21 | n/a | Apache Kafka if a live broker is ever needed |
| Harbor | "ADOPT" as harness and format (S300) | tasks run on our own harness, `score.json` instead of `reward.json`, no per-task `environment/` | add a Harbor export: per-profile `environment/Dockerfile` + `docker-compose.yaml`, `tests/test.sh` wrapping `grade.py`, `reward.json` from `reward.py`'s scalar |

---

## 8. Build order for the remaining sprint (proposal)

Ordered by "proves the plug mechanism" first, then by how many profiles each unlocks.

1. **The plug mechanism itself** (§5 points 1–5) with the existing plugs: DuckDB and
   Postgres as two warehouse plugs, plus include/exclude on the nine native services.
   Proof: GT001 under `de/laptop` and `de/postgres`, the same grader passing the oracle.
2. **Snowflake to task-ready:** connector and `dbt-snowflake` in the image, the
   multi-statement fix, an audit tailer.
3. **s3mock in native** (MinIO out) and the authoring-metadata leak fixed.
4. **DS toolbelt + Jupyter with the per-cell audit hook.** This unlocks the first
   non-DE profile.
5. **Redshift persona and BigQuery emulator** (each M, independent, can run in parallel).
6. **One BI file-format plug end to end** (Power BI PBIP or Tableau `.twb`). This unlocks
   the DA profile.
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
