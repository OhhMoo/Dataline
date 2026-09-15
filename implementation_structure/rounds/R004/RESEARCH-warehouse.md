# RESEARCH-warehouse — can we fake Snowflake / BigQuery / Databricks convincingly?

**Round R004 · agent RESEARCH-WAREHOUSE · 2026-09-14**

Answering the user's question: *"if we cannot directly call them, can we create a fake api or
mcp that let agent believe they are using those corresponding tools?"*

**Evidence legend, used on every non-obvious line.**
**[X]** = I executed it on this machine today and the output is in this document.
**[R]** = I read it in a primary source, cited by URL, and did not execute it.
**[P]** = prior round's claim (`R002/T2`, `R002/T1`, `R003/A5`) that I did **not** re-run.
**[J]** = my judgement, labelled as such, not a measurement.

Environment for every **[X]**: Windows 11, Python 3.11.15, Docker 29.5.2, `duckdb` 1.5.5,
`sqlglot` 30.15.0 (30.18.0 in the repo venv), `fakesnow` 0.11.16, `snowflake-connector-python`
4.7.3, `dbt-core` 1.12.4, `dbt-snowflake` 1.12.0, `deltalake` 1.6.3,
`ghcr.io/goccy/bigquery-emulator:latest` (422 MB, digest `sha256:f4e428d2…`).

---

## 0. The answer, up front

**Yes — but the MCP layer is the wrong seam for the warehouse itself, and only one of the three
platforms is worth the money.**

1. **Rank: Snowflake ≫ Databricks/Delta > BigQuery.** A convincing Snowflake is worth more than
   ten shallow platforms, and the evidence supports that framing more strongly than I expected
   going in. Snowflake is the only one of the three where a mature, permissively-licensed face
   already exists over the exact engine our world layer is frozen on.

2. **The MCP-tool fake cannot carry our primary workload, and that is decisive.** dbt is named by
   **30 of 41 archetypes** **[X]**, and dbt does not speak MCP — it speaks the Snowflake connector
   protocol. An MCP-only "Snowflake" surface leaves the highest-demand surface in the product
   running on plain `dbt-duckdb`, i.e. not faking Snowflake at all. The seam that carries dbt is
   the wire protocol, and `fakesnow` already implements it in **320 lines over 5 routes** **[X]**.

3. **The MCP layer is the right seam for exactly one thing: the platform metadata no engine
   provides** — warehouses, credits, `QUERY_HISTORY`, `ACCOUNT_USAGE`. Those are tables and
   arithmetic, not query execution. But **our own corpus demand for every one of them is
   currently zero** **[X], §1.3** — so that half should be built when a task needs it, not now.

4. **The divergence that should decide funding is not in Snowflake at all.** The BigQuery
   emulator returns a `totalBytesBilled` number that is **the size of the result set**, which
   inverts the single most important cost instinct on the platform. Measured, §3.1. That is the
   textbook "wrong fake," and it is the reason BigQuery ranks last despite being the most
   *finished* emulator of the three.

**What I would fund:** ~8–12 engineer-days to make `fakesnow` honest and put it behind the
existing `SnowflakeWarehouse` connector (§6). Nothing on BigQuery. Databricks/Delta only if a
lakehouse archetype family is authored first.

---

## 1. What actually makes a warehouse feel like itself

### 1.1 The distinction that matters

A data engineer's week is not evenly spread across a platform's feature list. Ranked below by
**how often a working data engineer touches it** — this ordering is **[J]**, my judgement — and
cross-checked in §1.3 against demand measured in our own archetype corpus **[X]**, which is the
only hard evidence available offline.

| Rank | Surface | Why it's there | Notice its absence? |
|---|---|---|---|
| 1 | **SQL dialect + semantics** | Every statement, every day. `IFF`, `QUALIFY`, `:` paths, `FLATTEN`, `TRY_*`, `NUMBER` arithmetic | **Immediately.** First statement. |
| 2 | **dbt adapter behaviour** | The transformation layer. Incremental strategies, `unique_key`, temp-relation handling | **Within one run.** |
| 3 | **`SHOW` / `DESCRIBE` / `INFORMATION_SCHEMA`** | How an engineer orients in an unfamiliar account | Within minutes. |
| 4 | **Session identity** — `CURRENT_ROLE/WAREHOUSE/ACCOUNT/VERSION`, `LAST_QUERY_ID` | Typed constantly; the "where am I" reflex | Minutes. |
| 5 | **Warehouse suspend/resume + sizing** | `USE WAREHOUSE` heads most sessions; sizing is a weekly cost conversation | Same day. |
| 6 | **`QUERY_HISTORY` / `ACCOUNT_USAGE`** | Triage: "what ran, how long, who paid" | Same day, *when triaging*. |
| 7 | **Stages + `COPY INTO`** | Ingestion work; bursty — heavy weeks then none | Only in ingest work. |
| 8 | **Time Travel / `UNDROP` / clone** | Reached for in an incident, and in the "let me snapshot before I break it" reflex | Only under incident. |
| 9 | **RBAC / `GRANT`** | Mostly someone else's job until it blocks you | Only when blocked. |
| 10 | **Result cache** | Invisible until a number won't change | Rarely, confusingly. |
| 11 | **Credits / billing views** | Monthly, or during a spike | Rarely. |
| 12 | **Micro-partitions / clustering / query profile** | Tuning work; a minority of engineers, a minority of weeks | Rarely. |

**The load is front-loaded.** Ranks 1–4 are hit *every session* and are exactly what a fake
over an SQL engine can do well. Ranks 8–12 are the famous Snowflake features and are hit
*rarely* — which is the opposite of how they get prioritised in design discussions.

### 1.2 Verified operational facts a fake must respect

All **[R]**, primary docs, fetched today:

- `ACCOUNT_USAGE.QUERY_HISTORY` lags **45 minutes**; data retained 1 year. `INFORMATION_SCHEMA`
  twin has **no lag but only 7 days**. ("QUERY_HISTORY | Historical | 45 minutes | … Data retained
  for 1 year." — https://docs.snowflake.com/en/sql-reference/account-usage; "QUERY_HISTORY ,
  QUERY_HISTORY_BY_* | 7 days" — https://docs.snowflake.com/en/sql-reference/info-schema)
- Default `DATA_RETENTION_TIME_IN_DAYS` is **1 day**; Standard edition can only be 0 or 1;
  Enterprise+ permits **0–90** for permanent objects.
  (https://docs.snowflake.com/en/user-guide/data-time-travel)
- Credits/hour (Gen1): X-Small **1**, Small **2**, Medium **4**, Large **8**, X-Large **16**,
  doubling to 5X-Large **256**. (https://docs.snowflake.com/en/user-guide/warehouses-overview)
- "Warehouses are required for queries, as well as all DML operations, including loading data
  into tables." — *but* "SHOW TABLES does not require a warehouse to retrieve data, so no charges
  apply" (https://docs.snowflake.com/en/user-guide/warehouses-considerations), while
  `INFORMATION_SCHEMA` views *do* require a running warehouse
  (https://docs.snowflake.com/en/sql-reference/info-schema). **That asymmetry — `SHOW` is free,
  `INFORMATION_SCHEMA` is not — is a detail every fake I looked at gets wrong, and it is a real
  discriminator.**
- Result cache expires after **24 hours**, refreshed on reuse **up to 31 days** from first
  execution; reuse requires exact query-text match and unchanged micro-partitions.
  (https://docs.snowflake.com/en/user-guide/querying-persisted-results)

### 1.3 Corpus demand, measured — the finding that should reset priorities

Counted by `grep -rliE` over the **41 non-withdrawn archetype YAMLs** in
`implementation_structure/state/archetypes/` **[X]**:

| Surface | Archetypes (of 41) |
|---|---|
| dbt | **30** |
| incremental models | **13** |
| backfill | **9** |
| schema drift | **5** |
| `unique_key` | **4** |
| RBAC / grants | **2** (A021, A062) |
| clustering | **1** |
| **credits / cost / billing** | **0** |
| **warehouse sizing** | **0** |
| **`QUERY_HISTORY` / `ACCOUNT_USAGE`** | **0** |
| **Time Travel / `UNDROP` / clone** | **0** |
| **`COPY INTO` / stage / Snowpipe** | **0** |
| **result cache** | **0** |

**Two corrections to my own first pass, recorded because they change the conclusion.** A naive
pattern gave "credits/cost = 10" and "COPY INTO/stage = 4". Both were false positives: `credit`
matched grading prose ("per-cause credit", "zero credit for a manifest"), `billing` matched
business data in a task premise, and `stage` matched "staging". Re-run strictly, both are **0**
**[X]**. Likewise "merge = 16" was `merge` in prose; `merge into|incremental_strategy|is_incremental`
matches **0** files — the incremental demand is carried by the word `incremental` (13), not by
explicit merge strategy.

**What this says.** The entire Snowflake *platform* surface — everything that distinguishes
Snowflake from a competent SQL engine — has **zero authored demand**. What the corpus actually
wants is dbt semantics, incremental correctness, freshness and backfills. That is **dialect and
adapter fidelity**, not platform emulation.

R002/T2 proposed 6 weeks on S2/S3 (warehouses, credits, `ACCOUNT_USAGE`, Time Travel, streams)
and named this exact risk as its own strongest counter-argument: *"author one archetype in each
family on paper before funding S3."* **A round has passed and nobody authored one.** This
measurement is the evidence that settles T2's open question, and it settles it against S2/S3.

---

## 2. What exists to build on

| Project | Licence | Stars | Last push | Open issues | Runs offline? | Verdict |
|---|---|---|---|---|---|---|
| `tekumara/fakesnow` | Apache-2.0 | 208 | 2026-09-13 | 26 | yes | **Adopt** |
| `goccy/bigquery-emulator` | MIT | 1144 | 2026-07-24 | 86 | yes | Works; don't use — §3.1 |
| `duckdb/ducklake` | MIT | 2983 | 2026-09-14 | 177 | yes | Defer with S3 |
| `delta-io/delta-rs` | Apache-2.0 | 3302 | 2026-09-14 | 187 | yes | **Strong, conditional** |
| `unitycatalog/unitycatalog` | Apache-2.0 | 3522 | 2026-09-14 | 427 | yes | APIs "should not be assumed to be stable" |
| `nnnkkk7/snowflake-emulator` | MIT | 48 | 2026-08-17 | 10 | — | Narrower than fakesnow **[P]** |
| `calvinchengx/databricks-emulator` | Apache-2.0 | **1** | 2026-09-10 | 0 | — | Created 2026-08-15. Unadoptable — §2.4 |

All rows **[R]**, GitHub REST API, fetched 2026-09-14. LocalStack (commercial/archived) and
Airbyte (ELv2) remain licence-blocked per R002/T1 **[P]**; I did not re-check them.

### 2.1 fakesnow — measured, not quoted

**[X] Structure.** 6,544 LOC total; `server.py` **320 lines** implementing **5 routes**
(`/session/v1/login-request`, `/session`, `/queries/v1/query-request`,
`/queries/v1/abort-request`, `/monitoring/queries/{sfqid}`); the `transforms/` package is
**3,153 lines**. The rewrite pipeline is a flat chain of ~50 `.transform(fn)` calls on a sqlglot
expression in `cursor.py:298`, so **the transform layer is usable as a library, in-process,
without running the HTTP server**. That fact shapes the proposal in §6.

**[X] Dialect coverage — the number that justifies adoption.** I wrote 30 Snowflake statements a
data engineer would plausibly type (`PARSE_JSON(x):a:b::int`, `QUALIFY`, `LATERAL FLATTEN`, `IFF`,
`NVL`/`ZEROIFNULL`, `DATEADD`/`DATEDIFF`, `LISTAGG … WITHIN GROUP`, `TRY_*`, `OBJECT_CONSTRUCT`,
`MERGE`, `INSERT OVERWRITE`, `CONVERT_TIMEZONE`, `TIMESTAMP_NTZ`, `NUMBER(38,2)`, …) and ran the
identical list three ways:

| Path | Passed | Notes |
|---|---|---|
| **Raw DuckDB** (what `sql_query` does today) | **8 / 30 (27%)** | Everything Snowflake-shaped fails |
| **`sqlglot` transpile snowflake→duckdb, then execute** | **25 / 30 (83%)** | 0 transpile errors, 5 execution errors |
| **fakesnow end-to-end** | **28 / 30 (93%)** | Fails only `INSERT OVERWRITE`, `IS_NULL_VALUE` |

**The gap between 83% and 93% is not convenience — it is semantics.** `TRY_TO_NUMBER` is the
clearest case **[X]**. On a column `('1','2','abc',NULL,'3.5')`:

- fakesnow → `[('1',1),('2',2),('3.5',4),('abc',None),(None,None)]`; `SUM(TRY_TO_NUMBER(v))` = 7;
  `COUNT(TRY_TO_NUMBER(v))` = 3. Correct `TRY_` behaviour: bad input becomes NULL.
- plain sqlglot → transpiles to `CAST(v AS DECIMAL(38,0))` and **raises**
  `Conversion Error: Could not convert string "abc" to DECIMAL(38,0)`. It **inverts the defining
  property of the `TRY_` family**.

For a benchmark whose whole subject is dirty data, quarantining bad rows with `TRY_*` is the most
load-bearing idiom there is. The cheap path breaks it. This is the single strongest argument
against "just add a transpile call to `sql.py`" and for taking fakesnow's transforms.

### 2.2 The one blocking defect — reproduced and isolated

R002/T2 reported that dbt incremental rerun fails on fakesnow **[P]**. **I reproduced it and
isolated the cause** **[X]**, with `dbt-snowflake` 1.12.0 against fakesnow 0.11.16 in server mode:

```
dbt run  #1 → Completed successfully.  PASS=2
dbt run  #2 → Database Error in model inc (models/inc.sql)
              001003 (42000): Parser Error: TEMPORARY table names can *only* use the "temp" catalog
```

Isolated to a single statement shape **[X]**:

```
OK    CREATE TEMPORARY TABLE tmp_unqualified          AS SELECT 1 AS a
ERR   CREATE TEMPORARY TABLE DB1.SCH1.tmp_qualified   AS SELECT 1 AS a
        → Parser Error: TEMPORARY table names can *only* use the "temp" catalog
ERR   CREATE TEMPORARY TABLE SCH1.tmp_two_part        AS SELECT 1 AS a
        → Catalog Error: Schema with name SCH1 does not exist!
```

fakesnow passes the fully-qualified name through to DuckDB, which forbids any qualification on a
`TEMPORARY` table. **The fix is one transform: strip the catalog/schema from the target of
`CREATE TEMPORARY TABLE`.** It is a good upstream PR. This matters out of proportion to its size:
it breaks **13 of 41** archetypes' core mechanism on the second run, 100% of the time.

### 2.3 The operational landmine — worse than reported

**[X]** In server mode, three statements produce HTTP 500, and the Snowflake connector treats 500
as retryable. With `network_timeout=25` set defensively, each burned its full budget before
failing:

```
ERR  'USE WAREHOUSE wh_etl'            28.0s  → InternalServerError: 290500: HTTP 500
ERR  'CREATE FILE FORMAT ff TYPE=CSV'  30.1s  → InternalServerError: 290500: HTTP 500
ERR  'SELECT $1 FROM VALUES (1),(2)'   30.1s  → InternalServerError: 290500: HTTP 500
```

T2 found this with `SELECT $1 FROM VALUES` **[P]**. **The sharper finding is that `USE WAREHOUSE`
does it** — the single most idiomatic statement in any Snowflake session, and the first thing an
engineer types. Without an explicit `network_timeout` this is the ~10-minute hang T2 reported. In
an episode that is **silent consumption of the agent's wall-clock budget with no error it can
attribute**. Must be fixed before this touches an episode: a catch-all in `query_request` mapping
unhandled exceptions to a Snowflake-shaped 4xx.

### 2.4 Databricks — the honest picture

- `databricks-connect` is **proprietary** ("Databricks Proprietary License",
  https://pypi.org/project/databricks-connect/) **[R]**. Spark Connect (Apache-2.0) is the OSS
  protocol underneath (https://spark.apache.org/docs/latest/spark-connect-overview.html) **[R]**.
- `databricks-sql-python` is **Apache-2.0** and is "a Thrift-based client with no dependencies on
  ODBC or JDBC" (https://raw.githubusercontent.com/databricks/databricks-sql-python/main/README.md)
  **[R]** — so a HiveServer2 Thrift endpoint would satisfy the real client.
- Unity Catalog OSS **never claims Databricks-UC REST parity**; its README claims Hive metastore
  and Iceberg REST compatibility and states "**The APIs are currently evolving and should not be
  assumed to be stable**" **[R]**.
- The only OSS Databricks workspace emulator found is `calvinchengx/databricks-emulator` —
  Apache-2.0, **1 star, created 2026-08-15**, single author, conformance claims self-reported
  **[R]**. Real, but not something to build a product on.

**What is genuinely strong is the table format, not the platform.** `deltalake` 1.6.3 **[X]**,
offline, gave me on a 4-commit table:

- a full `DESCRIBE HISTORY` equivalent with **real** `operationMetrics` —
  `{'num_updated_rows': 1, 'num_copied_rows': 2, 'scan_time_ms': 16}` for the UPDATE,
  `{'num_target_files_scanned': 2, 'num_target_files_skipped_during_scan': 0}` for a MERGE;
- time travel by version (`DeltaTable(p, version=1)`), `MERGE`, `OPTIMIZE` compaction
  (`numFilesRemoved: 2 → numFilesAdded: 1`), `Z-ORDER`, and `RESTORE` (which correctly lands as a
  *new* commit, version 7, matching Delta semantics);
- Change Data Feed fails **loudly** when not enabled:
  `DeltaError … Reading a table version: 0 that does not have change data enabled`;
- and DuckDB's `delta` extension reads the same table, **including `delta_scan(path, version=1)`
  time travel** **[X]** — so this runs on the engine we are already frozen on.

The point: **these numbers are not synthesized. Delta's history is the actual mechanism**, which
is a property neither a faked `QUERY_HISTORY` nor a faked `totalBytesBilled` can claim.

---

## 3. Divergence analysis — where each fake is *wrong*, and what an agent learns that is false

This section is the deliverable. The governing rule, inherited from R002/T2 and which I endorse:
**loud unsupported is fine; silent wrong is disqualifying.**

### 3.1 BigQuery emulator — `totalBytesBilled` is the size of the result set

**The worst divergence I found in any option, and the reason BigQuery ranks last.**

**[X] Measured.** Table `raw.big`: 20,000 rows, columns `a INT64`, `b STRING`, `pad` (200 chars).
Numbers read back from `jobs.get` `statistics.query`:

| Query | Emulator `totalBytesBilled` | Real BigQuery |
|---|---|---|
| `SELECT a FROM raw.big` | 88,894 | bytes of column `a`, **min 10 MB** |
| `SELECT * FROM raw.big` | 4,177,788 | all columns, **min 10 MB** |
| `SELECT a FROM raw.big WHERE a = 1` | **1** | **same as `SELECT a`** — no reduction |
| `SELECT COUNT(*) FROM raw.big` | **5** (the string `"20000"`) | **min 10 MB per query** |
| `SELECT SUM(a) FROM raw.big` | 9 | bytes of column `a`, min 10 MB |
| `SELECT 1` | 1 | min 10 MB per query |

The model is unmistakably `len(serialized result)`.

**[R] What real BigQuery documents:**
- "you're charged according to the data processed in the columns you select, **even if you set an
  explicit LIMIT on the results**" — https://cloud.google.com/bigquery/pricing
- "For non-clustered tables, applying a LIMIT clause to a query doesn't affect the amount of data
  that is read. You are billed for reading all bytes in the entire table as indicated by the
  query, even though the query returns only a subset."
  — https://cloud.google.com/bigquery/docs/best-practices-costs
- "Charges are rounded up to the nearest MB, with a **minimum 10 MB data processed per table
  referenced** by the query, and with a **minimum 10 MB data processed per query**."
  — https://cloud.google.com/bigquery/pricing
- Only **partition pruning** and **clustering** are documented as reducing scanned bytes.
  — https://cloud.google.com/bigquery/docs/querying-partitioned-tables

**What an agent would learn that is false:**

1. **"Adding a `WHERE` clause reduces cost."** It does not, on a non-partitioned, non-clustered
   table. The emulator reports a 88,894× reduction for one predicate.
2. **"`LIMIT` is a cost control."** Explicitly contradicted by the docs; it is named as an
   anti-pattern.
3. **"`COUNT(*)` is nearly free."** The emulator says 5 bytes; the documented floor is 10 MB per
   query — wrong by a factor of ~2,000,000.
4. **"Cost scales with rows returned."** The correct instinct is *cost scales with bytes scanned
   in the columns referenced; you reduce it by selecting fewer columns and by filtering on the
   partition key.* The emulator teaches the opposite axis.

Every number in that table is also below the documented 10 MB floor, so **not one is right even
in magnitude**.

**Fairness to the project, which matters for how we write this up.** This is *not* a
goccy/bigquery-emulator bug. Its README never mentions billing — "bytes" and "billing" each occur
**0 times** **[R]**, and the README describes the project as "**beta**" and "a personal project …
no support of any kind from Google". It never claims to model cost. **The defect would be ours,
for grading against a number the upstream never promised.** That is precisely the failure the
user is worried about: a fake that is *wrong* rather than merely incomplete, propagating a false
behaviour into a trained model.

**[X] Second divergence, same family:** `dryRun: true` is **ignored** — the emulator executed the
query and returned rows. Real BigQuery: "If set to true, BigQuery **doesn't run the job**.
Instead, if the query is valid, BigQuery returns statistics about the job such as how many bytes
would be processed." (https://cloud.google.com/bigquery/docs/reference/rest/v2/jobs/query) **[R]**.
Dry run is *the* cost-check reflex on BigQuery. An agent learns dry run is a free preview that
returns data; on a real system it returns none, and the agent would read that as an empty table.

**[X] Loud absences (safe):** `INFORMATION_SCHEMA.JOBS`, `.PARTITIONS`, `.TABLE_STORAGE` and
`__TABLES__` all return `Table not found`; `FOR SYSTEM_TIME AS OF` returns
`FOR SYSTEM_TIME AS OF is not supported`. These are fine — but note the consequence: **there is no
`JOBS` view to correct the wrong billing number from.** The only cost signal available is the
wrong one.

**[X] A workload-blocking limit:** `MERGE` from a subquery fails —
`MERGE: source must be a single-table reference, got *googlesql.ResolvedProjectScan` — while
`MERGE` from a plain table works. That is the shape dbt generates for merge-strategy incrementals.

### 3.2 fakesnow — the silent set

**[X] Confirmed on this machine** (several first reported by T2 **[P]**; re-run here):

| Statement | fakesnow returns | Real Snowflake | Class |
|---|---|---|---|
| `SELECT 1/0` | `inf` | division-by-zero error | **Silent wrong** |
| `SELECT 1.0::NUMBER(38,10)/3` | float `0.3333333333333333` | fixed-point NUMBER | **Silent wrong** |
| `ALTER TABLE t1 CLUSTER BY (id)` | `Statement executed successfully.` | sets a clustering key | **Silent wrong** |
| `CREATE DYNAMIC TABLE dt1 TARGET_LAG='1 minute' …` | `Table DT1 successfully created.`; `SHOW TABLES` then lists `DT1` as type `'TABLE'` | a table that auto-refreshes | **Silent wrong** |
| `SELECT CURRENT_ROLE()` | `'duckdb'` | the session role | **Silent wrong** (identity) |
| `SELECT CURRENT_VERSION()` | `'0.0.0'` | e.g. `8.x.x` | Loud-ish tell |
| `EXPLAIN SELECT * FROM t1` | DuckDB box-drawing plan, **plus** a sqlglot `unsupported syntax … Command` warning to stderr only | Snowflake plan | **Silent wrong** |

The clustering no-op is **deliberate and documented upstream** — `alter_table_strip_cluster_by`'s
own docstring reads *"Turn Snowflake clustering operations into a no-op. DuckDB doesn't support
clustering, so the following statements are ignored"* **[X]**. Correct for fakesnow's audience
(make a test suite go green); disqualifying for ours without a fence.

**What an agent would learn that is false, ranked by damage to us:**

1. **`CREATE DYNAMIC TABLE` succeeds and yields an ordinary table.** The agent learns it has
   provisioned a self-refreshing table. It has provisioned a frozen snapshot. **Staleness is our
   single largest theme** (freshness/watermark language appears across the corpus **[X]**), so
   this plants a stale-data defect *in the substrate* that is indistinguishable from the stale-data
   defects we plant *as tasks*. This is the most dangerous single divergence for this product.
2. **`1/0 → inf`.** The agent learns a bad denominator yields a value rather than an error. An
   `inf` then propagates silently into an aggregate and a dashboard. Our stated thesis is "the
   system reports green while being wrong" — here the *substrate* does it, which is
   indistinguishable from the thing we are measuring.
3. **`NUMBER` arithmetic is float.** Financial totals differ in the trailing digits. Our graders
   compare values.
4. **`ALTER TABLE … CLUSTER BY` is a success no-op.** The agent learns clustering is free and
   instantaneous, and there is no `SYSTEM$CLUSTERING_INFORMATION` to contradict it (absent **[X]**).

**[X] Loud absences (safe, and they cost task classes, not validity):** `CREATE WAREHOUSE`,
`ALTER WAREHOUSE SUSPEND/RESUME`, `USE WAREHOUSE`, the entire `SNOWFLAKE` catalog
(`Binder Error: Catalog "SNOWFLAKE" does not exist!`), `INFORMATION_SCHEMA.QUERY_HISTORY()`,
`CREATE ROLE` / `GRANT` / `USE ROLE` / `SHOW GRANTS` / `SHOW ROLES`, `RESULT_SCAN`,
`LAST_QUERY_ID`, `CURRENT_ACCOUNT/REGION/WAREHOUSE/SESSION/CLIENT`, `AT(OFFSET)`,
`BEFORE(STATEMENT)`, `UNDROP`, `CLONE … AT(…)`, `DATA_RETENTION_TIME_IN_DAYS`, `CREATE STREAM`,
`CREATE TASK`, `SHOW TASKS`, `CREATE PIPE`, `SYSTEM$PIPE_STATUS`, `SYSTEM$CLUSTERING_*`,
`GET_DDL`, `ALTER SESSION SET QUERY_TAG`. `SHOW WAREHOUSES` returns exactly one warehouse named
`FAKESNOW_WAREHOUSE`.

**[X] The detector we get for free.** Every silent no-op I found is preceded by sqlglot's
`… contains unsupported syntax. Falling back to parsing as a 'Command'` warning, written to
**stderr only** — the client never sees it. Promoting that warning to a hard error is the
highest-value single change available, and it is cheap because the pipeline is one flat
`.transform()` chain.

### 3.3 Delta / delta-rs — almost no semantic divergence, but a real determinism divergence

**This is the one option whose divergence is not about correctness.** Delta Lake is the actual
open format Databricks uses; `delta-rs` implements it; the history and metrics I read back are
produced by the real transaction log, not synthesized. What is missing is the **control plane**
— jobs, clusters, Photon, autoscaling, SQL warehouses, Unity Catalog governance — and those are
*absent*, not wrong. An agent cannot learn a false lesson from an API that is not there.

**[X] The divergence that does exist is determinism, and I measured it.** Two identical builds of
the same table (overwrite, then an update):

- **Parquet data files are byte-identical across builds** — both runs produced the same two
  content hashes (`367d7ea1…`, `940836ac…`).
- **The `_delta_log/*.json` commits are not**, for three independent reasons:
  1. `commitInfo.timestamp` differs (wall clock) — `1789419383092` vs `1789419383174`;
  2. `operationMetrics.execution_time_ms` differs — `7` vs `4`;
  3. **JSON key order differs** — run 1 emitted `operationMetrics` before `clientVersion`, run 2
     after. That is map-iteration nondeterminism inside delta-rs, and **it survives freezing the
     clock**, which is the non-obvious one.
- Data filenames are random UUIDs (`part-00000-6ff06ded-…` vs `part-00000-ae299a8c-…`) and the
  log references them, so the log differs even with identical content.

**[X] How that lands against our gates.** `world/world/fingerprint.py` documents `semantic_hash`
as **THE GATE** — canonical logical contents, schemas plus canonically ordered rows — with
`raw_byte_hash` as "diagnostic only … used as THE GATE for snapshot restore, where bytes must
match exactly." So Delta is **compatible with the main gate** (read logical contents through
`delta_scan`) and **breaks the snapshot-restore byte gate** unless commit JSON is canonicalised
and file naming is made deterministic. That is real work, and it is the honest cost of this
option — not a fidelity cost, a determinism cost.

### 3.4 The MCP-surface option — its exact fidelity ceiling

This is the user's question, so I want to answer it precisely rather than generally.

**[X] The architectural fact that makes this option better than it sounds.** The gateway exposes
**59 tools** (the brief says 63; the registry has 59 — a small correction), and **there is no
shell, exec, bash, python or terminal tool among them.** `run_command` in `commands.py` is
reachable only through `dbt` and `git` with fixed argv. So today there is *no escape hatch*: an
agent cannot write a Python script using `snowflake.connector`, cannot run `snowsql`, cannot
`curl` a REST endpoint. R002/T1's stated fidelity risk for this row — *"an agent that shells out
to `snowsql` walks around it"* **[P]** — **does not apply to this architecture as it currently
stands.** Tool descriptions are also engine-neutral ("Run one read-only warehouse query; results
include column types") and name no engine anywhere **[X]**.

**[X] And the four places it breaks anyway:**

1. **It does not touch SQL, and SQL is rank 1.** An MCP tool named `warehouse_list` does nothing
   about the fact that `sql_query` on raw DuckDB passes **8 of 30** ordinary Snowflake
   statements. The agent's *first* statement — `PARSE_JSON(payload):a:b::int`, `IFF(...)`,
   `QUALIFY` — fails. **The MCP surface buys the cheap 320 lines of protocol and still owes the
   expensive 3,153 lines of semantics.**

2. **It cannot serve dbt, and dbt is 30 of 41 archetypes.** dbt speaks the Snowflake connector
   protocol over HTTP; it has no MCP client. An MCP-only Snowflake face therefore leaves the
   highest-demand surface running `dbt-duckdb` against DuckDB — which is not a fake Snowflake, it
   is today's system with different tool names. **This is the decisive objection**, and it is
   structural, not a matter of effort.

3. **Synthesized metadata is too clean, and that is itself a divergence.** A `query_history` tool
   backed by our own action log returns complete, consistent, instantly-available rows. Real
   `ACCOUNT_USAGE.QUERY_HISTORY` **lags 45 minutes** and the no-lag `INFORMATION_SCHEMA` twin only
   covers **7 days** **[R]**. An agent that learns observability data is complete and current will,
   on a real system, triage a live incident against a view that does not yet contain the queries
   that caused it. **A fake that is too tidy teaches false confidence.** If we build this surface
   at all, the lag must be modelled — and modelling it is cheap (`WHERE start_time <
   current_vts - interval '45 minutes'` in a view), which is an argument *for* the MCP seam when
   the time comes.

4. **The ceiling is set by the absence of a shell — and the northstar argues for adding one.**
   The user's own framing is "the sandbox must mock a real DATA ENGINEER's working computer as it
   actually is," and a real data engineer's computer has a terminal. The moment a shell tool is
   added, an MCP-only fake becomes bypassable and detectable in one command, whereas a
   connector-protocol fake keeps working because `snowsql`, `dbt` and any Python script all speak
   it. **The MCP option is cheap precisely because of a property we are likely to give up.**

**[J] Honest fidelity ceiling.** For platform *metadata* — warehouses, credits, query history,
grants — the MCP seam has a **high** ceiling, because those objects genuinely are just tables and
arithmetic, and our virtual clock makes the time-dependent parts deterministic. For *query
execution* the ceiling is **low and it is not raisable by effort**: dbt will not come through it.

---

## 4. Ranking, with the reasoning

**1. Snowflake — fund this one.**
It is the only platform where a mature Apache-2.0 face already exists over the exact engine our
world layer is frozen on; where the real adapter (`dbt-snowflake` 1.12.0) connects **[X]**; where
the measured dialect coverage is **93%** **[X]**; and where the corpus's actual demand (dbt,
incremental, freshness, backfill) is *dialect and adapter* demand — precisely what fakesnow
delivers. Its divergences are enumerable, mostly loud, and the silent ones have identified fixes.

**2. Databricks / Delta — conditional, and narrower than it looks.**
Fund the **table format**, never the control plane. `delta-rs` gives real history, real time
travel, real `OPTIMIZE`/`Z-ORDER` metrics, readable by our own DuckDB **[X]**; the workspace
emulator that exists has 1 star and is a month old **[R]**. Gate on a lakehouse archetype family
being authored, and budget the determinism work in §3.3 before anything else.

**3. BigQuery — do not fund.**
The emulator is the most *finished* of the three (MIT, 1,144★, official-client conformance suite
per its README **[R]**) and its SQL engine is strong. But BigQuery's *distinctive* surface —
the only thing a fake BigQuery offers that a fake Snowflake does not — is **cost by bytes
scanned**, and that is exactly the thing it gets wrong by six orders of magnitude, with no
`INFORMATION_SCHEMA.JOBS` to correct it (§3.1). Add zero corpus demand **[X]** and a MERGE limit
that blocks dbt incrementals **[X]**, and there is nothing left to buy.

**On the user's hypothesis — "a convincing Snowflake may be worth ten shallow platforms" — the
evidence supports it, and more strongly than I expected.** Not for the usual reason (Snowflake's
market share) but because the three platforms' *shared* surface is the SQL dialect and the dbt
adapter, which is 80% of the value and is the same work three times; while their *distinctive*
surfaces are, respectively: buildable and unwanted (Snowflake's platform objects, 0 archetypes),
real but determinism-expensive (Delta's log), and actively wrong (BigQuery's billing). Doing one
platform well dominates.

---

## 5. Concrete proposal — Snowflake face, ~8–12 engineer-days

Written for whoever implements it next week.

### 5.1 Where it sits

Nothing new in the topology. `gateway/CONNECTOR_CONTRACT.md` already defines
`warehouse_backend = snowflake` selecting `SnowflakeWarehouse`, which already speaks the real
connector to `acme.snowflake.local` over local TLS. **The proposal is to make what is behind that
hostname trustworthy, not to add a layer.** `gateway/docker/snowflake_start.py` already boots
`fakesnow.server` with `FakeSnow(db_path=DATA)` behind the proxy; the work is on the instance it
boots and on a strict-mode fence, plus the fingerprint.

Two modes, and **pick server mode**:
- **Server mode** (recommended) — what exists. Serves the connector protocol, so `dbt-snowflake`,
  `snowsql` and any Python client work unmodified. Required by the §3.4 dbt argument.
- **Library mode** (keep as an option) — the transform chain is importable
  (`fakesnow.transforms`, chained in `cursor.py:298`) **[X]**, so `sql.py` could rewrite Snowflake
  SQL to DuckDB in-process with no second service. Cheaper and more deterministic, but it cannot
  serve dbt. Useful as a fallback for `sql_query` if the service proves flaky.

### 5.2 Work items, each with the evidence that sized it

| # | Item | Evidence | Est. | Confidence |
|---|---|---|---|---|
| 1 | **Fix qualified `CREATE TEMPORARY TABLE`** — strip catalog/schema from the target in a transform. Unblocks dbt incremental. | §2.2, isolated to one statement shape **[X]** | **0.5 d** | High |
| 2 | **500 → Snowflake-shaped 4xx catch-all** in `query_request`. Kills the retry storm. | §2.3, 28–30 s per statement **[X]**; `server.py` is 320 lines **[X]** | **0.5 d** | High |
| 3 | **Strict mode**: promote sqlglot's `Command` fallback from a stderr warning to a Snowflake-shaped error, behind a world-image env var. | §3.2 — every silent no-op emits it **[X]**; pipeline is one flat chain **[X]** | **1 d** | Medium-high |
| 4 | **Fence the four silent-wrongs** explicitly: reject `CREATE DYNAMIC TABLE`, reject `ALTER TABLE … CLUSTER BY`, error on division by zero. | §3.2 **[X]** | **1 d** | Medium |
| 5 | **`NUMBER` fixed-point arithmetic** — preserve DECIMAL through division. | §3.2 **[X]** | **1–2 d** | **Low — see §7** |
| 6 | **Session identity**: `CURRENT_ACCOUNT/REGION/WAREHOUSE/SESSION/CLIENT`, `LAST_QUERY_ID`, a real-looking `CURRENT_VERSION()`, a non-`FAKESNOW_WAREHOUSE` warehouse name, a `CURRENT_ROLE()` that is not `'duckdb'`. | §3.2 **[X]**; `current_version` transform already exists **[X]** | **1 d** | High |
| 7 | **Theme dialect migration** — 17 theme `.sql` files across 4 themes, plus the DuckDB-catalog workaround macro in `themes/*/workspace/dbt_project/macros/relations.sql`, plus `dbt-duckdb` → `dbt-snowflake` incremental-strategy differences. | **[X]** file counts; strategy risk is R002/T1's flagged quiet failure **[P]** | **2–3 d** | **Medium-low** |
| 8 | **Fingerprint extension** — hash fakesnow's per-database files' logical contents; exclude its timestamped `_fs_global._fs_information_schema` metadata. | `fingerprint.py` gates on `semantic_hash`, byte hash only for snapshot restore **[X]** | **1–2 d** | Medium |

**Total ≈ 8–12 engineer-days** to a Snowflake face that runs our primary workload honestly.
Item 7 is the one most likely to overrun, and item 5 is the one I am least sure of.

### 5.3 Explicitly **not** in scope

Warehouses, credits, `QUERY_HISTORY`/`ACCOUNT_USAGE`, Time Travel, clones, `UNDROP`, streams,
tasks, RBAC, result cache, `COPY INTO` beyond what exists, micro-partitions, query profiles,
Snowpark. **Each has 0 authored archetypes** **[X], §1.3**. R002/T2 costed these at 6 weeks and
named "author one archetype in each family first" as its own control; that has not happened.
When it does, the **MCP seam is the right place to build them** (§3.4) — they are metadata and
arithmetic over our virtual clock, they reuse the `var/ingest/state.json` and `var/obs/`
fingerprint pattern already proven in the tree **[X]**, and the 45-minute lag that makes
`ACCOUNT_USAGE` interesting is a `WHERE` clause. Interfaces, when funded, would be
`warehouse_list`, `warehouse_alter`, `query_history`, `credits_used` — read-mostly, backed by the
existing action log plus a seeded credit curve (X-Small 1, Small 2, Medium 4, Large 8 credits/hour
**[R]**), with credits a function of **virtual** time.

### 5.4 Two fidelity gates worth building

- **A divergence suite.** ~100 statements with expected Snowflake results, run in CI, marking each
  as pass / loud-fail / **silent-divergence**. The third column is the one that matters. The 30
  statements in §2.1 and the table in §3.2 are the seed.
- **Try the `dbt-snowflake` adapter's own integration tests against it.** T2 flagged this as a
  ready-made conformance harness and had no time to evaluate it **[P]**; I did not either. It is
  worth one engineer-day before item 7, because it would price item 7 properly.

---

## 6. Costs at a glance

| Option | Build | Determinism risk | Offline | Maintenance |
|---|---|---|---|---|
| **fakesnow, hardened** (recommended) | **8–12 d** | **Low** — DuckDB files, existing semantic gate; needs §5.2 item 8 | Yes — 5 routes, no egress **[X]** | Medium. One maintainer, 208★, Apache-2.0 so forkable; upstream items 1–2 |
| **MCP platform-metadata tools** (defer) | 3–5 d **[J]**, when demand exists | **Lowest** — JSON under `var/`, pattern already proven | Yes | Low |
| **Delta / delta-rs** (conditional) | 5–8 d **[J]** + determinism work | **High, measured** — §3.3: UUID filenames, `commitInfo.timestamp`, `execution_time_ms`, JSON key order | Yes | Low. 3.3k★, active |
| **BigQuery emulator** (reject) | 2–3 d to wire **[J]** | Medium — separate Go service, own state | Yes, 422 MB **[X]** | Medium. Beta; v0.8.1 2026-06-13 **[R]** |
| **Real Snowflake / BigQuery / Databricks** | — | — | **No** | Blocked, as reported |

Build-day figures for options we have not started are **[J]** and should be treated as such; the
8–12 d for fakesnow is the only one built bottom-up from isolated defects.

---

## 7. My weakest finding, and what I could not determine

**Weakest finding, named as such: work item 5, `NUMBER` fixed-point arithmetic (§5.2), at
"1–2 days."** I measured the symptom — `1.0::NUMBER(38,10)/3` returns a float **[X]** — but I did
**not** look at how fakesnow's `to_decimal` / `integer_precision` transforms handle division, and
I did not check whether DuckDB's DECIMAL division preserves scale the way Snowflake's does. The
estimate is an extrapolation from "the transform layer is well-factored," which is exactly the
kind of reasoning this project's last round found to be wrong by 4–6×. **Treat it as unbounded
until someone spends an hour reading `transforms/`.** Item 7 (theme migration, 2–3 d) is the
second-weakest, for the same reason: I counted files but did not attempt a migration.

**Other things I could not determine:**

- **Whether `SELECT COUNT(*)` is metadata-only/free on BigQuery.** I believed it was; the
  verification found **no primary Google doc saying so**, and the pricing page states the 10 MB
  per-query minimum with no exception. My §3.1 claim is therefore framed against the *documented
  floor*, not against "free". Recording this because I nearly asserted the stronger version.
- **Whether a `WHERE` on a non-partitioned, non-clustered BigQuery column reduces bytes billed.**
  The docs state the positive (partition/clustering pruning reduces bytes) and never state the
  negative explicitly. The `LIMIT` quote is the closest documented analogue. My claim rests on
  that inference plus the LIMIT quote — strong, but an inference.
- **Whether Snowflake serves result-cache hits without a running warehouse**, and whether
  `SELECT COUNT(*)` is metadata-only there. Neither is documented either way.
- **Whether `snowflake-jdbc` and the `snow` CLI work against fakesnow.** Still unverified, as in
  R002 **[P]**. I tested the Python connector and dbt only.
- **How far real-Snowflake divergence extends beyond what I sampled.** My probe was ~100
  statements. The honest characterisation is "I found this many silent divergences without trying
  hard," not "there are this many."
- **Whether `dbt-snowflake`'s integration suite can run against fakesnow** (§5.4). Still the
  highest-value unexplored idea in this area.
- **Whether fixing the identity tells changes any score.** Unmeasured. I agree with T2 that
  detectability is over-weighted **[P]**, and I would fix items 6 for *usability* — an agent that
  burns fifteen turns on `SNOWFLAKE.ACCOUNT_USAGE` not existing is being measured on our bug —
  not for deception.

---

## 8. One-paragraph answer for the user

Yes, we can build a fake a working data engineer would not immediately reject — but not by
putting Snowflake-shaped names on MCP tools. The thing that makes a warehouse feel real on day
one is its SQL dialect and its dbt adapter, not its warehouses and credits: raw DuckDB passes 8
of 30 ordinary Snowflake statements, and `fakesnow` passes 28 **[X]**. That gap is the product,
and it is already written, Apache-2.0, over the engine we are frozen on. MCP tools cannot close
it, and — decisively — dbt cannot come through an MCP tool at all, while dbt is named by 30 of our
41 archetypes **[X]**. So: adopt fakesnow behind the hostname we already have, spend ~8–12 days
fixing the one bug that breaks dbt incremental reruns **[X]**, the 500-retry storm that silently
eats an episode's clock on `USE WAREHOUSE` **[X]**, and the four places it reports success while
doing nothing **[X]**. Do **not** build warehouses, credits, `QUERY_HISTORY` or Time Travel yet —
all four have zero authored archetypes **[X]**, and when they are authored the MCP seam is the
right place for them. Do **not** build BigQuery: its one distinctive surface is cost, and the
emulator computes cost as the size of the result set, so it would teach an agent that adding a
`WHERE` clause makes a query cheaper — which is false on the real system, documented as false,
and exactly the kind of wrong that propagates into a trained model **[X] + [R]**.
