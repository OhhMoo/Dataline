# T1 — Mock priority list

**Round R002 · 2026-09-13 · agent T1 · the "only bottleneck" assignment**

What this answers: of the tools a data agent would touch, **which do we stand up for real,
which do we substitute, which do we actually have to build, and which do we not build at all.**

Three things to know before reading the table.

1. **The default answer is "run the real one."** The meeting agreed: no custom tools where an
   SDK, an API or a terminal will do. Of the 43 unresolved items, **18 are served by something that
   really runs** — a Docker image, a pip install, or an OSS stand-in — **4 need something
   built**, and **21 are dropped** because nothing needs them or the licence forbids them.
2. **Demand comes from the archetypes, not from the product's fame.** The 26 authored archetypes
   name *surfaces* (`warehouse`, `bi_dashboard`, `docs_wiki`), not products. Slack is #2 on the
   meeting's frequency list and is touched by **zero** archetypes. Ranking by fame would have us
   build a Slack mock before a docs surface that twelve tasks read.
3. **"Probed" and "the docs say so" are different claims** and are marked differently below.
   14 artifacts were actually pulled and run on this machine. Where the row says *documented*, nobody
   started the container.

Score = (archetypes needing it × how load-bearing it is, 1–3) ÷ engineer-days.

---

## The list

| # | Tool / surface | Category | Driven by | Verdict | Effort | Fidelity risk |
|---|---|---|---|---|---|---|
| 1 | **Warehouse** (Snowflake-shaped) · T107 | warehouse | all 26 | `sdk_reshape` — **deferred to T2** | T2 owns it | **Highest.** Every task runs through it. `fakesnow` is an SDK shim, so an agent that shells out to `snowsql` walks around it |
| 2 | **dbt Core** · T105 | transformation | A001 A002 A003 A006 A009 A010 A017 A018 A019 A022 A025 A026 A028 A029 A030 (15) | `native_oss` — **probed** | 1 d | **High, and it is the adapter not dbt.** `dbt-duckdb`'s incremental strategies ≠ `dbt-snowflake`'s. A002/A009/A025/A026 are authored on exactly those semantics |
| 3 | **Docs / wiki surface** · T205 T110 | docs | A004 A006 A011 A018 A019 A020 A021 A022 A028 A030 A031 A033 (12) | `custom_mock_required` — a seeded markdown tree | 1 d | Low. No page history or write path. All 12 archetypes *read* docs; none write them |
| 4 | **Jupyter / JupyterLab** · T104 | notebook | A005 A012 A013 A020 A031 A032 A033 (7) | `native_oss` — **probed** | 1 d | Medium. The risk is execution state, not the server: A033 needs out-of-order cells and a stale kernel, which the harness must seed deliberately |
| 5 | **BI values surface** · T204 T113 | bi | A001 A003 A006 A009 A013 A017 A019 A021 A022 A025 A028 A029 A030 A031 A032 (15) | `custom_mock_required` — 4 objects | 3–4 d | Medium **by choice.** We own the refresh/watermark model because "dashboard disagrees with warehouse" is a staleness claim, and borrowing Superset's cache semantics means grading on someone else's detail |
| 6 | **Airflow / scheduler** · T106 | orchestration | A001 A004 A009 A011 A018 A020 A026 A027 (8) | `native_oss` — **probed** | 2 d | **High, and structural.** Real Airflow's scheduling is not seed-reproducible; A018 grades ran-long vs started-late. See the flag below |
| 7 | **PostgreSQL** · T103 | database | A028 + substrate for A004 | `native_oss` — **probed** | 0.5 d | None. Lowest-risk item in the inventory |
| 8 | **Git + forge (Gitea)** · T100 | vcs | A005 A012 A033 (3) | `native_oss` — **probed** | 0.5 d | Low. Gitea's API is not GitHub's — `gh`/octokit will not work unmodified. No archetype needs the forge API; all three need local `git` only |
| 9 | **Ticketing (Gitea issues)** · T102 T149 | ticketing | A011 A029 (2) | `oss_equivalent` — **probed** | 0.5 d | Low. No JQL, sprints, custom fields or workflow transitions. Both archetypes need title + body + comments + a status change |
| 10 | **Object storage (S3)** · T109 T154 T201 | storage | A010 A017 A022 A025 (4) | `http_intercept` — **probed** (round trip) | 1 d | Low-medium. No IAM, so no permissions-misconfiguration task. Staged-file / COPY INTO is well inside scope |
| 11 | **Catalog / lineage** · T125 T200 | catalog | A019 A021 (2) | `native_oss` available; **seed it as data instead** | 1 d seeded / 2 d for the server | Medium. A019 needs column-level lineage, A021 needs a grants table. Both are a table and a graph — a 2.6 GB Java catalog for two tasks is a poor trade |
| 12 | **Data quality** · T122 T207 | quality | A013 A020 A032 (3) | `native_oss` — *documented* | 2 d | **Inverted risk.** A003 is "the quality gate *is* the bug", so the gate's behaviour is the planted defect. Do not let a third-party library own grading-critical behaviour |
| 13 | **Ingestion connector control plane** · T203 T117 | ingestion | A010 A017 A018 A022 A025 (5) | `custom_mock_required` — **the only irreplaceable build** | 4–6 d | **Highest build risk.** Nothing OSS reproduces a sync-history timeline + schema-change policy + green-status-while-rows-missing. Score ranks it 13th; read the callout below before believing that |
| 14 | **Semantic layer (MetricFlow + LookML)** · T120 T115 | other | A019 A030 (2) | `native_oss` — **probed** (`lkml`) | 1–2 d | Medium. `lkml` parses LookML, it does not evaluate it — the task can be about the *model* being wrong, not about generated SQL |
| 15 | **Legacy DB persona (Oracle → Postgres)** · T127 T119 | database | A028 (1) | `oss_equivalent` — **probed** (Postgres) | 0.5 d (reuse) | Medium. No PL/SQL, ROWNUM, CONNECT BY, NUMBER precision. A028 needs legacy-*looking* SQL and stable numbers, not Oracle's runtime |
| 16 | **Kafka** · T116 | streaming | A027 (1) | `native_oss` — **probed** | 2 d | Low for Kafka itself. The risk is licence, not fidelity — see landmines |
| 17 | **Dagster (secondary orchestrator)** · T123 | orchestration | A027 (1) | `native_oss` — *documented* | 2 d | Medium. Cheapest honest read of A027 is a second instance of the deterministic state machine, not a second real product |
| 18 | **BigQuery emulator** · T108 | warehouse | — (0) | `wire_protocol` — **probed** (real query) | 1 d | Low but unneeded. No slots, bytes-billed or JOBS history, so no cost-based task |
| 19 | **ClickHouse** · T126 | database | — (0, strategic) | `native_oss` — **probed** | 1 d standalone | Low standalone. Strategic only: candidate backend for T2's Snowflake-shaped API |
| 20 | **Superset** · T137 | bi | escape hatch for #5 | `oss_equivalent` — **probed** | 2 d | Use only when a task needs a *real* BI tool rather than believable BI values |
| 21 | **Evidence.dev** · T206 | bi | runner-up for #5 | `oss_equivalent` — *documented* | 2 d | BI-as-code: dashboards are git-diffable text, which suits rebuild-from-seed. No refresh model of its own |
| 22 | **moto** · T201 | storage | second option for #10 | `http_intercept` — **probed** | 1 d | Broader AWS coverage than S3Mock, less S3-focused |
| 23 | **Unity Catalog OSS** · T200 | catalog | option for #11 | `native_oss` — *documented* (image verified, not started) | 2 d | New since the last pass: Apache-2.0, speaks Hive metastore + Iceberg REST. 2.6 GB |
| 24 | **Apicurio Registry** · T213 | streaming | contingency for #16 | `oss_equivalent` — *documented* | 1 d | Only if a schema-registry task appears. Not wire-identical to Confluent's client protocol |
| 25 | **Trino** · T211 | warehouse | for T2 | `oss_equivalent` — *documented* | 3 d | Filed for T2's warehouse work, not evaluated here |

### Not built for v1 (`drop`)

| Tool | Why |
|---|---|
| Slack · T101 | 0 archetypes. Comms context already carried by docs + tickets. Ship a static transcript if texture is wanted |
| GitHub Actions · T111 | 0 archetypes. `act` (MIT) is a fast reopen if a CI task is ever authored |
| Databricks · T112 T166 | 0 archetypes. Community Edition is SaaS. Catalog half is now free (T200); control plane is not |
| Power BI · T114 T139 | Proprietary, Windows-only. A name on surface #5 |
| Looker server · T115 T140 | SaaS-only. The useful half — LookML parsing — survives at #14 |
| Sigma · T128 T142 · Hex · T124 T141 | 0 archetypes, SaaS-only, no OSS analogue |
| Spark · T118 | 0 archetypes. Large JVM container earning nothing |
| Reverse ETL · T129 T159 | 0 archetypes, already out of v1, and the OSS field is a licence graveyard |
| Oracle · T127 T167 | Images contain Oracle binaries under Oracle's terms; redistribution not established |
| **Licence rejections** — Airbyte (ELv2), MinIO (AGPL + archived), LocalStack (archived + NOASSERTION), Wiki.js (AGPL), Outline (BSL), Redpanda (BSL), Confluent Schema Registry (Confluent Community), Metabase (NOASSERTION) | See landmines |

---

## Three things worth arguing about tomorrow

**One build serves fifteen tasks, and it is not a Tableau mock.** Four inventory rows —
Tableau, Power BI, Looker, Sigma — were each recorded "none found," and each would be a
hopeless mock. But no archetype needs Tableau. Fifteen need *a number on a dashboard, the
definition behind it, and when it last refreshed.* That is four objects and three or four days,
and it dissolves all four rows. Ground rule 6 already freezes v1 on "a BI-values + docs
surface," so this verdict agrees with a decision the repo has made — it now has a cost attached.

**Rank 13 is the one that will hurt.** The formula ranks the ingestion connector control plane
thirteenth because it costs 4–6 days for 5 archetypes. That is arithmetically right and
practically misleading: it is the **only** item on this list with no alternative at any price.
`dlt` and Meltano *move data*; Fivetran's role in A010/A017/A018/A022/A025 is to be a control
plane that made a bad schema-drift decision and reported success. Airbyte is the one OSS product
with such a control plane and it is licence-blocked. The four objects are written down in T203 —
connector, sync run, table-level schema config, alert; six read endpoints, two actions. **The
risk is not difficulty, it is that "connector control plane" sounds like a product and will grow
endpoints for a fortnight unless that list is treated as a contract.**

**Airflow vs. the frozen scheduler needs a decision, not two builders.** Eight archetypes name
`airflow`. Ground rule 6 freezes v1 on "a deterministic scheduler state machine." Real Airflow's
scheduler does not reproduce byte-identically from a seed, and A018 grades on distinguishing
ran-long from started-late — which only means something if the timings are ours. Suggested
split: the state machine is the **executor of record** (it gets graded), Airflow's **artifacts**
— DAG files, task logs, a run-history table — are the **read surface**. Someone should confirm
this before two agents build two orchestrators.

---

## Licence landmines

Checked against the GitHub licence API on 2026-09-13. Four are new since the last pass.

| What | Licence | Why it bites |
|---|---|---|
| **Confluent Schema Registry** (and all `cp-*` images) | Confluent Community — source-available | **Best-disguised trap here.** Kafka is Apache-2.0, so people assume the ecosystem is. The text explicitly binds anyone *distributing* the software. Use Apicurio (Apache-2.0) |
| **LocalStack** | NOASSERTION + **repo archived 2026-03-23** | *New.* The reflex answer for AWS emulation is gone. Archived fails liveness on its own |
| **MinIO** | AGPL-3.0 + **archived 2026-04-24** | Double failure, re-confirmed. Contradicts an older Phase-3 claim (F110) |
| **Redpanda** | BSL 1.1 | The usual "lighter Kafka" suggestion. The API reports *no detected licence at all* — silence is not permission |
| **Airbyte** | Elastic-2.0 (platform) | Connectors are MIT and may be salvageable; the platform is not |
| **Metabase** | NOASSERTION | The reflex "open-source BI" answer. Needs a human read. Superset (Apache-2.0) and Redash (BSD-2) are clean |
| **Wiki.js / Outline** | AGPL-3.0 / BSL 1.1 | Both already rejected; recorded so nobody re-finds them. BookStack (MIT) is the clean wiki |
| **Mattermost / Rocket.Chat** | NOASSERTION | If comms ever re-enters scope, Zulip (Apache-2.0) is the clean one |
| **Oracle Database images** | UPL-1.0 scripts, **Oracle terms on the binaries inside** | The permissive licence on the build repo does not grant redistribution of the database |
| **git** | GPL-2.0 | Invoked as a subprocess, never linked — the ordinary posture for a CLI, but flag it for the human read |

**One correction in the other direction:** **MetricFlow is Apache-2.0, not BSL.** Verified twice
— the GitHub API and the `LICENSE` file itself. Anyone who remembers the Transform-era licensing
and writes it off without checking will drop a shippable dependency for no reason.

---

## What was actually run

On this machine, 2026-09-13, Docker 29.5.2 / Windows 11, Docker Desktop Linux engine.

**Probed and passed (14):** `postgres:16` → PostgreSQL 16.15 · `adobe/s3mock` → full round trip
(create bucket, PUT object, GET returns body) · `gitea/gitea` → `{"version":"1.27.3"}` ·
`clickhouse/clickhouse-server` → 26.8.2.7 · `apache/kafka` → topic created and listed ·
`ghcr.io/goccy/bigquery-emulator` → real query returned rows and a typed schema ·
`motoserver/moto` → HTTP 200 · `quay.io/jupyter/base-notebook` → `{"version": "2.21.0"}` ·
`apache/superset` → `/health` OK · `apache/airflow:3.0.2` → 3.0.2 · dbt-core 1.12.4 +
dbt-duckdb 1.11.0 + duckdb 1.5.5 · `fakesnow` → query returned 42 through
`snowflake.connector` · `lkml` → parsed a LookML view · `dlt` 1.30.0.

**Image existence verified by registry query but never started (3):**
`unitycatalog/unitycatalog` (2.6 GB), `trinodb/trino` (1.0 GB), `atlassian/confluence` and
`atlassian/jira-software` (proprietary — deliberately not probed).

**Taken on documentation only:** Great Expectations, Elementary, Dagster, Evidence.dev, Zulip,
Apicurio. Each says so in its record.

**Three probes failed first and are worth knowing:** Gitea needs 30–60 s to initialise SQLite and
a retry loop without a sleep will burn 60 attempts in five seconds; current ClickHouse images
require `CLICKHOUSE_PASSWORD` and no longer allow a passwordless default user; adobe/S3Mock is a
JVM service and binds slower than an eager loop expects. Also: the `jupyter/*` images on Docker
Hub still resolve but were last pushed **2023-10-20** — the project moved to `quay.io`.

---

## Where a week could disappear

**The ingestion connector control plane (T203).** Not because it is hard — four objects is a
small build — but because it is the only thing on this list with no upstream to copy, and its
scope has no natural edge. Freeze the four objects in writing before anyone opens an editor.

**Runner-up, and it is the quieter one: the dbt adapter (T105).** dbt installs in sixty seconds
and looks done. But `dbt-duckdb` and `dbt-snowflake` disagree on incremental strategies and
`unique_key` handling, and four archetypes are authored on precisely those semantics. If the
adapter merges where Snowflake would delete-and-insert, the planted defect does not fire, or
fires for the wrong reason — **and the task still grades, just not on the thing we meant.** That
is the most expensive failure mode this project has, because nothing goes red.
