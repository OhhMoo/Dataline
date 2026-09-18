# T2 — Fake Snowflake: decision document

**Round R002 · agent T2 · 2026-09-13 · for the 2026-09-14 comparison with Andrew**

Everything below marked **[V]** I verified by running it on this machine today. Everything
marked **[R]** is a claim I read and attributed but did not execute. The distinction is load
bearing and I have kept it on every line that needs it.

Environment used for all **[V]** results: Windows 11, Python 3.11.15, `fakesnow` 0.11.16,
`duckdb` 1.5.5, `sqlglot` 30.15.0, `snowflake-connector-python` 4.7.3, `dbt-core` 1.12.4,
`dbt-snowflake` 1.12.0, `ducklake` extension for DuckDB 1.5.5.

---

## 0. The four positions

1. **Do not build a Snowflake mock. Adopt `tekumara/fakesnow` (Apache-2.0) and extend it.**
   It already implements the connector wire protocol in 320 lines, runs on DuckDB — the engine
   our world layer is already frozen on — and I got the real `snowflake-connector-python` and
   the real `dbt-snowflake` adapter talking to it over HTTP inside twenty minutes. **[V]**

2. **Andrew's ClickHouse hypothesis is wrong, and not for the reason I expected.** It is not
   too heavy — the `clickhouse-server` image is ~246MB compressed (distroless). **[V, Docker Hub API]** It
   is wrong because (a) `sqlglot`'s Snowflake→ClickHouse path is visibly worse than
   Snowflake→DuckDB on real statements, (b) ClickHouse has no `MERGE INTO` and does `UPDATE`
   as an *asynchronous mutation*, which puts a silent-divergence generator underneath the exact
   dbt-incremental mechanism most of our archetypes are about, and (c) choosing it throws away
   fakesnow, whose ~3,200 lines of Snowflake transforms are hard-wired to DuckDB.

3. **Time Travel and Streams are affordable, and this is the most useful thing I found.**
   DuckDB's `ducklake` extension gives `FROM tbl AT (VERSION => n)` and `AT (TIMESTAMP => ts)`,
   a `snapshots()` version↔timestamp map, and a `table_changes()` change feed emitting
   `insert` / `delete` / `update_preimage` / `update_postimage` — which is precisely Snowflake's
   `METADATA$ACTION` / `METADATA$ISUPDATE` stream contract. All verified by hand. **[V]**
   Nobody has shipped this: not fakesnow, not the Go emulator, not LocalStack's commercial
   product.

4. **The "full one-to-one mock" is not worth doing and I would argue against funding it.**
   The surface is unbounded and still growing (Snowpark, Cortex, Native Apps, Iceberg, Hybrid
   Tables, Streamlit, Notebooks). LocalStack has had a commercial team on this since May 2024
   and their published coverage matrix still has no Time Travel, no `QUERY_HISTORY` and no
   `ACCOUNT_USAGE` row. **[R]** What we want is a *task-driven* mock: roughly 7–9 engineer-weeks
   to something better than anything that exists, staged so each week ships something usable.

---

## 1. Why fidelity matters more for us than for anyone else who has tried this

Every prior emulator was built so a developer's test suite would go green without a Snowflake
bill. For that audience a wrong answer is tolerable, because a human eventually eyeballs it.

Our thesis (agent.md §8.2) is **"the system reports green while being wrong."** We are
building a benchmark whose entire difficulty comes from silent, plausible, wrong state. A
substrate that is *itself* silently wrong does not add noise to that measurement — it is
indistinguishable from the thing we are measuring. This gives a sharp rule that should govern
every scoping decision below:

> **Loud unsupported is fine. Silent wrong is disqualifying.**
> If the agent asks for something we did not build, it must get a Snowflake-shaped error.
> An unsupported feature that quietly returns a plausible number is worse than no feature.

fakesnow today violates this rule in specific, enumerable places. Fixing those is a large
part of the Stage-1 work in §7, and it is cheap because the failure is almost always
"sqlglot parsed it as a no-op Command" rather than a deep semantic problem.

**[V] Silent divergences I found in a single afternoon:**

| Statement | fakesnow returns | Real Snowflake |
|---|---|---|
| `SELECT 1/0` | `inf` | division-by-zero error |
| `SELECT 0/0` | `nan` | division-by-zero error |
| `CREATE DYNAMIC TABLE dt TARGET_LAG=...` | creates a **plain table** | a dynamic table that refreshes |
| `ALTER TABLE t CLUSTER BY (g)` | `Statement executed successfully` (no-op) | sets a clustering key |
| `CREATE FUNCTION f1(x int) ... AS $$ x+1 $$` then `SELECT f1(1)` | `' x+1 '` (the body as a string) | `2` |
| `SELECT 1.0::NUMBER(38,10) / 3` | float `0.3333333333333333` | fixed-point NUMBER |
| `USE SCHEMA db.schema` (server mode) | sets schema, **leaves database NULL** | sets both |

The last one cost me two debugging cycles and it will cost an agent an episode. Every
subsequent statement fails with `090105 (22000): Cannot perform SELECT. This session does not
have a current database.` — a real Snowflake error code attached to a situation real Snowflake
would never be in.

---

## 2. Surface-area decomposition

"Snowflake" is not one thing. Decomposed, with an honest column for whether our archetypes
touch it. Task families referenced are from agent.md §8.2 (DE: backfills, schema/semantic
drift, orchestration, CDC, DR; Analytics: metric reconciliation, BI blast radius).

| # | Surface | What it actually is | Do we need it? | fakesnow today **[V]** |
|---|---|---|---|---|
| 1 | **SQL dialect — expressions** | `VARIANT`/`:` paths, `FLATTEN`, `QUALIFY`, `IFF`/`NVL`/`ZEROIFNULL`, `DATEADD`/`DATEDIFF`, `LISTAGG`, `TRY_*` | **Must have.** This is every task. | Works. All of these passed. |
| 2 | **SQL dialect — DML** | `MERGE`, `INSERT OVERWRITE`, transactions | **Must have.** dbt incremental, backfills, repair hazard. | `MERGE` works; `BEGIN`/`ROLLBACK` work. |
| 3 | **Connector wire protocol** | `/session/v1/login-request`, `/queries/v1/query-request`, Arrow IPC results | **Must have.** It is how anything connects. | Works — 320 lines, 5 routes. |
| 4 | **Public SQL API** `/api/v2/statements` | documented REST API | **Nice to have.** Some Airflow operators, any agent reaching for `curl`. | **Absent — 404s.** ~1 day to add. |
| 5 | **dbt-snowflake adapter** | our primary transformation surface | **Must have.** | `debug`/`run`/`test`/`show` pass; **incremental rerun FAILS.** See §5. |
| 6 | **`SHOW` / `DESCRIBE` / `INFORMATION_SCHEMA`** | discovery — how an agent orients | **Must have.** Agents start by looking around. | `SHOW TABLES/SCHEMAS/DATABASES/COLUMNS`, `DESCRIBE`, `information_schema.tables` all work. |
| 7 | **Stages + `COPY INTO`** | ingestion — landing files into raw | **Must have.** Ingestion archetypes are thin even in the best existing DE benchmark (agent.md §5). | Basic path works; `CREATE FILE FORMAT` **crashes the server**; `FORMAT_NAME`, `NULL_IF`, `EMPTY_FIELD_AS_NULL` unimplemented **[R, issue #403]**. |
| 8 | **Time Travel** (`AT`/`BEFORE`, `UNDROP`) | 1-day default retention, 90 days on Enterprise | **High value.** A whole archetype class: DR under destructive ambiguity, "can we recover the pre-backfill state". | **All three forms fail.** Buildable on DuckLake — §6. |
| 9 | **Zero-copy clone** | `CREATE ... CLONE`, coupled to Time Travel | **High value.** The safe-rollback affordance an agent should reach for. | `CREATE TABLE c CLONE t` and `CREATE DATABASE d2 CLONE d1` both work — but as *copies*, with no `AT(...)` variant. |
| 10 | **Streams** | CDC change table, `METADATA$ACTION`/`$ISUPDATE` | **High value.** CDC failures are a named DE seed. | `CREATE STREAM` → parser error. Buildable on `table_changes()` — §6. |
| 11 | **Tasks** | scheduled DAG of SQL inside the warehouse | **Medium.** We already own a deterministic scheduler (Track A). Snowflake Tasks would be a *second* orchestrator, which is realistic but redundant. | `CREATE TASK` → parser error. Recommend metadata-only: DDL + `SHOW TASKS` + `TASK_HISTORY`, execution driven by our scheduler. |
| 12 | **`QUERY_HISTORY` / `ACCOUNT_USAGE`** | 365-day history with **up to 45 minutes of lag**; `INFORMATION_SCHEMA` twin has no lag but only 7 days | **Highest value-per-line on the platform.** See §6. | **Entirely absent** — `SNOWFLAKE` catalog does not exist. |
| 13 | **Warehouses + credits** | sizes on a doubling credit curve; required for every query and DML | **Medium-high.** Cost-spike triage is a real on-call task and needs no real compute. | `CREATE`/`ALTER`/`USE WAREHOUSE` all fail; `SHOW WAREHOUSES` returns one hard-coded `FAKESNOW_WAREHOUSE`. |
| 14 | **Roles / RBAC / grants** | `USE ROLE`, `GRANT`, `SHOW GRANTS` | **Medium.** "Your role cannot see that table" is a genuinely good decoy and a real failure. | **Nothing.** `CREATE ROLE`, `GRANT`, `USE ROLE`, `SHOW GRANTS TO ROLE` all parser-error. README says access control is not implemented. **[R]** |
| 15 | **Result cache + `RESULT_SCAN`** | 24-hour persisted results | **Low-medium.** One nice confusion; cheap because the proxy sees every statement. | `RESULT_SCAN` → parser error. |
| 16 | **Micro-partitions / pruning / query profile** | 50–500MB partitions, min/max metadata | **Do not build.** Its only observable signature is a query profile and a wall-clock number; a sub-second DuckDB cannot produce either honestly, and a faked profile is a fabricated artifact we would then grade against. **Drop the clustering task class.** | Absent, and should stay absent. |
| 17 | **Snowpark** | DataFrame API + Python UDFs/sprocs | **Do not build.** Snowflake's own local testing framework cannot even run `Session.sql()`. **[R]** If a world needs Python transforms, use Python. | Absent. |
| 18 | **SnowSQL / `snow` CLI** | the terminal an agent would actually type into | **Should have** for the "real work laptop" feel. Both go through the connector protocol, so #3 covers them. | README claims the CLI and JDBC both connect unmodified **[R]**; I verified the Python connector only. |
| 19 | **Stored procedures, masking policies, tags, `GET_DDL`, `SYSTEM$*`** | long tail | **Not for v1.** Each is a loud, obvious gap. | Absent. `CREATE TAG` works; the rest error. |

---

## 3. Prior art — what exists, and where it stops

I searched hard for a project that would let us skip this work. **There is one, and it gets us
most of the way.**

### fakesnow — `tekumara/fakesnow` · Apache-2.0 · 208★ · pushed 2026-09-13 · 25 open issues+PRs (15 issues) **[V, GitHub API]**

**Verdict: adopt as the base.** Two modes: in-process patching of the Python connector, and a
standalone HTTP server. The server mode is the one that matters, because it makes the emulator
a *network service* the world can point any client at.

**[V] What I measured:**

- Boot: **1.3–1.6s** cold. Well inside the sub-60s world budget.
- RSS: **172MB** holding a 1M-row table. Dependency tree on disk ~242MB, of which 88MB is
  `pyarrow` and 36MB is the DuckDB binary. A slim image lands well under 500MB.
- Throughput: 2M-row CTAS in **0.2s**; a 13-group aggregate over it in **0.02s**; fetching
  500k rows over HTTP in **0.62s**.
- Parallel rollouts: `FAKESNOW_DB_PATH=':isolated:'` gives genuinely separate catalogs per
  session — I confirmed session B cannot see session A's database.
- `dbt debug`, `dbt run`, `dbt test`, `dbt show` all pass against it over HTTP.
- Codebase: ~3,400 lines package + ~3,200 lines of sqlglot transforms. `server.py` is 320
  lines. This is a readable, forkable codebase, not a black box.

**Where it stops** — rows 4, 8, 10, 11, 12, 13, 14, 15 of the table in §2, plus the dbt
incremental bug in §5 and the silent divergences in §1.

**One operational landmine that must be fixed before this touches an episode.** An internal
exception in server mode becomes an HTTP 500, and `snowflake-connector-python` treats 500 as
retryable. **[V]** I hit this with `SELECT $1 FROM VALUES (1),(2)`: a DuckDB
`InvalidInputException` became a 500 and the connector reached `retryCount=42` before I killed
it. An independent user reports the same shape from `CREATE FILE FORMAT`: *"returning HTTP 500.
The snowflake connector then retries the 500 in a loop, so the client hangs for ~10 minutes
instead of failing fast."* **[R, issue #403, confirmed still open]**. In our setting that is
not a bug, it is a **silent consumption of the episode's wall-clock budget with no error the
agent can see or the log can attribute**. Fix: a catch-all in `query_request` that maps any
unhandled exception to a Snowflake-shaped 4xx error body. Half a day.

### Everything else I checked, and why it is not the answer

| Project | License / status **[V via GitHub or vendor docs]** | Verdict |
|---|---|---|
| **LocalStack for Snowflake** | Commercial, per-seat tokens, no community edition | **Disqualified on licence,** not merit. Most complete emulator that exists — their matrix claims `STREAM`, `TASK`, `WAREHOUSE`, `STAGE`, `ROLE` DDL. Notably their matrix has **no Time Travel, no `QUERY_HISTORY`, no `ACCOUNT_USAGE`** row at all. Also the precedent for our hostname plan: they serve on `snowflake.localhost.localstack.cloud`, a *real* DNS name resolving to 127.0.0.1, and ship a matching SSL cert. **[R]** |
| **`nnnkkk7/snowflake-emulator`** | MIT · 48★ · pushed 2026-08-17 | **Not adopted.** Go + DuckDB. README explicitly excludes *"Time Travel / Zero-Copy Cloning"* and *"Streams, Tasks, Pipes"*. Strictly narrower than fakesnow. I did not run it. (`OswaldOniSango/snowflake-emulator`, which shows up as a separate search hit, is a **1★ fork** of it — not independent prior art. Flagging because it is exactly the kind of thing that inflates a prior-art list.) |
| **`buremba/universql`** | Apache-2.0 · 206★ · pushed **2025-10-20** | **Dead end, and now stale.** Needs a live Snowflake account for catalog and ACLs — disqualified by the no-egress rule, as R001 already found (C003). Eleven months without a push is new information. Its architecture — sqlglot in front of DuckDB behind the connector protocol — is still the right one; it is also, not coincidentally, fakesnow's. |
| **Snowpark local testing framework** | Snowflake's own, shipped in `snowpark-python` | **Useless for us.** *"`Session.sql(...)` is not supported in the local testing framework."* **[R]** DataFrame API only; query tags, query history and lineage documented as unsupported. Its value is as calibration: the vendor, with source access, did not attempt query history locally. |
| **`Snowflake-Labs/snowflake-vcrpy`** | Apache-2.0 · 30★ · pushed 2025-05-29 | **Rejected.** VCR cassette record/replay of connector HTTP. Only answers requests that were recorded; an agent writes SQL nobody anticipated. Kept as the strongest evidence that the connector's HTTP surface is stable enough to intercept — Snowflake's own engineers ship a tool that does it. |
| **`SpotOnInc/asbestos`** | MIT · 9★ | **Rejected.** Canned-response unit-test mock. No engine. Listed so nobody re-checks it. |
| **`anjeongkyun/testcontainers-fakesnow`** | Apache-2.0 · **2★** · pushed 2026-09-13 | Corroboration only, not a dependency. Two stars is one person. It is the only third-party evidence I found that `snowflake-jdbc` connects to fakesnow unmodified. |

**What I did not find, and looked for:** any emulator implementing Time Travel; any Snowflake
fixture set inside the `dbt-snowflake` adapter test suite that runs without a live account; any
LocalStack-style OSS project other than the two above. If Andrew has found one of these, it
beats my Stage-3 plan and I would drop mine.

---

## 4. The ClickHouse hypothesis, answered directly

Andrew's framing: *"ClickHouse might be something we can wrap around to make a fake Snowflake."*
I think the instinct — put a Snowflake-shaped face on a mature OSS engine — is exactly right,
and it is what fakesnow already does. The choice of ClickHouse as the engine is the part I
disagree with.

**The argument I expected to make, and cannot.** I assumed ClickHouse would lose on container
weight. It does not: `clickhouse-server:latest-distroless` is **245.7MB compressed amd64** and the
`head` tag 284.4MB **[V, Docker Hub tags API, 2026-09-13]**. Comfortably inside a 2GB budget. I am
recording this because it was my prior and it was wrong.

**Three arguments that survive.**

**(a) The transpilation path is measurably worse.** **[V]** I ran 30 representative Snowflake
statements through `sqlglot` 30.15.0 to both targets. Snowflake→DuckDB produced native
equivalents; Snowflake→ClickHouse produced invalid SQL on the semi-structured core:

| Snowflake | → DuckDB | → ClickHouse |
|---|---|---|
| `PARSE_JSON('{"a":1}'):a` | `JSON('{"a":1}') -> '$.a'` | `JSONExtractString(PARSE_JSON(...), 'a')` — **`PARSE_JSON` left untranslated; it does not exist in ClickHouse** |
| `OBJECT_CONSTRUCT('a',1,'b',col)` | `{'a': 1, 'b': col}` | `tuple('a' := 1, 'b' := col)` — **not valid ClickHouse** |
| `LATERAL FLATTEN(input => o.items)` | `CROSS JOIN UNNEST(...)` | `LATERAL arrayJoin(input => o.items)` — **named-arg syntax carried through verbatim** |
| `CREATE OR REPLACE TRANSIENT TABLE ...` | `CREATE OR REPLACE TABLE ...` | passed through **with `TRANSIENT` intact** |
| `PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY v)` | `QUANTILE_CONT(v, 0.5 ORDER BY v)` | untranslated |
| `MERGE INTO ... WHEN MATCHED ...` | passthrough — **and DuckDB has native `MERGE INTO`** | passthrough — **ClickHouse has no `MERGE INTO`** |

This is not a fixable gap by writing more sqlglot rules; it reflects that ClickHouse's data
model genuinely lacks the constructs.

**(b) ClickHouse's DML model is wrong for our task family, in the dangerous direction.**
ClickHouse implements `UPDATE`/`DELETE` as `ALTER TABLE` mutations, documented as *"this is a
heavy operation not designed for frequent use"*, and *"By default, it is asynchronous."* **[R]**
Our archetypes are about non-idempotent backfills, dbt incremental merges, and repairs that
create a second incident. Putting those on an engine where a write's visibility depends on when
a background merge lands means the substrate produces silent, timing-dependent wrongness
*inside the exact mechanism the task is measuring*. That is the one failure mode §1 says we
cannot accept.

**(c) It discards the only working asset.** fakesnow's ~3,200 lines of transforms, its
`_fs_global._fs_information_schema` metadata layer, its `SHOW`/stage/`COPY` implementations and
its Arrow result encoding all assume a DuckDB cursor. Retargeting to ClickHouse is a rewrite of
the whole valuable part while keeping only the 320-line protocol shim — which is the cheap part.

**The best argument for ClickHouse, stated fairly, and why it still fails.** ClickHouse gives
you `system.query_log` for free — durations, memory, bytes read, per query. That is the closest
free analogue to `QUERY_HISTORY`/`ACCOUNT_USAGE` of any candidate engine, and §2 row 12 says
that surface is our highest-value gap. The reason it does not decide the question: **our
architecture already owns the query log**. Every statement passes through our proxy, which can
append to its own history table with exactly the columns, the retention and the *45-minute lag*
we want. ClickHouse would give us a query log with the wrong schema and no lag, which we would
then have to reshape anyway.

**Backend ranking.** DuckDB ≫ Postgres > ClickHouse. Postgres is second on the merits — it is
what LocalStack reportedly uses per account **[R]** — but it loses on analytical scan
performance, no native `VARIANT`, no `QUALIFY`, weaker sqlglot Snowflake coverage, and a second
daemon. DuckDB's decisive advantage over both is not technical elegance: **it is the engine our
world layer is already frozen on** (agent.md §9.2.1). A Snowflake face over DuckDB reuses the
existing seed→world builder, the semantic fingerprint, the snapshot/restore machinery and the
fixture contract. Any other backend forks all of it.

---

## 5. Interception vs. emulation — where the seam falls

**The question is a false binary, and the answer is that the cheap seam is the protocol and the
expensive part is the semantics, so you buy the protocol and build the semantics.**

Snowflake has *two* HTTP surfaces and they should not be conflated:

- **The connector protocol** — what `snowflake-connector-python`, `snowflake-jdbc`, ODBC and
  the `snow` CLI actually speak. Undocumented, but small: `POST /session/v1/login-request`,
  `POST /session`, `POST /queries/v1/query-request`, `POST /queries/v1/abort-request`,
  `GET /monitoring/queries/{sfqid}`. Results are inline base64 Arrow IPC. **fakesnow implements
  all five in 320 lines.** **[V, read the route table]**
- **The public SQL API** (`/api/v2/statements`) — documented, JSON, async statement handles.
  **fakesnow does not implement it at all.** **[V]** Anything reaching for it 404s.

So interception is *already done and it cost 320 lines*. That settles the priority: do not
spend another hour on protocol work beyond adding `/api/v2/statements` (~1 day). Every
remaining hour goes into semantics.

### Where it breaks, and whether it breaks loudly

This taxonomy is the part I would most want Andrew to argue with, because it is the one that
decides what we build.

**Loud (safe — agent sees a real error, we lose a feature but not validity):**
`CREATE STREAM`, `CREATE TASK`, `CREATE WAREHOUSE`, all RBAC statements, `RESULT_SCAN`,
`UNDROP`, `information_schema.query_history()`, `SNOWFLAKE.ACCOUNT_USAGE.*`, `GET_DDL`,
`SYSTEM$CLUSTERING_INFORMATION`, stored procedures. These produce parser or binder errors. They
cost us task classes; they do not corrupt grading. **[V]**

**Silent (dangerous — must be fixed or explicitly fenced):** the table in §1. Add to it every
place `sqlglot` emits *"contains unsupported syntax. Falling back to parsing as a 'Command'"* —
that warning goes to the server's stderr, not to the client, and the statement becomes a no-op
that reports success. **That log line is the single best automated detector we have**, and
Stage 1 should promote it to a hard error in the world container.

**Hanging (worst — no error, no result, budget consumed):** unhandled exception → 500 →
connector retry loop. **[V]** Must be fixed before any episode runs.

**Recommended fence — "strict mode".** A world-image env var that makes the emulator refuse
rather than approximate: any statement that hits the `Command` fallback, any unimplemented
function, any `NUMBER`-precision-losing division, returns a Snowflake-shaped error instead of a
plausible answer. Grading correctness is worth more to us than convenience. Cost: perhaps two
days, most of it choosing the right error codes.

---

## 6. The features that make Snowflake tasks interesting

This is where I disagree most usefully with the received wisdom, which is that a mock without
Time Travel loses a whole class of task and that is just the price. **It is not the price.**

### Time Travel, UNDROP and `CLONE ... AT` — **feasible, and this is the finding**

DuckDB's `ducklake` extension (MIT, `duckdb/ducklake`, 2,981★, pushed 2026-09-10 **[V, GitHub
API]**) is a snapshot-based table format. Verified by running it today **[V]**:

- `SELECT * FROM fct AT (VERSION => 2)` returned the correct historic state after a sequence of
  `INSERT`/`UPDATE`/`DELETE`.
- `SELECT * FROM fct AT (TIMESTAMP => TIMESTAMPTZ '...')` also correct — *the same syntactic
  shape Snowflake uses*.
- `FROM lake.snapshots()` gives the version↔timestamp map, so `AT(TIMESTAMP => ...)` and
  `AT(OFFSET => -n)` are both a lookup plus a rewrite. `AT(STATEMENT => query_id)` becomes a
  lookup once we keep our own query history (below) — the two features compose.
- `ALTER TABLE ADD COLUMN` did not break reads of older versions.
- An out-of-range version failed cleanly: `Invalid Input Error: No snapshot found at version 99`
  — which is exactly the shape needed to emulate *"Time travel data is not available"* when an
  agent reaches past `DATA_RETENTION_TIME_IN_DAYS`.

The integration cost is small and I checked it: fakesnow attaches databases with one line —
`ATTACH DATABASE '{db_file}' AS {self.database}` in `conn.py`. Swapping that to
`ATTACH 'ducklake:{meta}' AS {db} (DATA_PATH ...)` is the hook. **[V]** The extension is 24MB
and must be baked into the image (no egress in-episode).

**Why this is worth real money.** The 1-day default retention **[R]** is itself a task
generator: a plausible incident window is *longer* than the retention the agent actually has,
so "just time-travel back" is a trap, and `DATA_RETENTION_TIME_IN_DAYS = 0` on a critical table
is a plantable defect that a full refresh cannot undo. That satisfies agent.md §8.5's "must not
be solvable by full-refresh" and §8.4's "destructive ambiguity" lever simultaneously.

### Streams — **feasible, same mechanism**

`lake.table_changes('fct', 3, 5)` returned rows tagged `insert`, `delete`, `update_preimage`,
`update_postimage` **[V]**. Snowflake's contract is `METADATA$ACTION` (INSERT/DELETE),
`METADATA$ISUPDATE`, `METADATA$ROW_ID`, with an update represented as a paired delete and insert
**[R]**. These map onto each other almost directly. The part we build ourselves is **offset
state** — a Snowflake stream advances its offset only when consumed inside a DML transaction,
and "the stream was consumed twice and the second read was empty" is a genuinely good incident.
That state is a row in our metadata table, not an engine feature.

### `QUERY_HISTORY` / `ACCOUNT_USAGE` — **feasible, cheap, and the best value on the list**

Nothing implements this. It is also, per line of code, the most task-generative surface
Snowflake has, because of one documented fact: **`ACCOUNT_USAGE.QUERY_HISTORY` lags by up to 45
minutes**, while the no-lag `INFORMATION_SCHEMA.QUERY_HISTORY` table function only covers 7
days. **[R, both from Snowflake docs]**

An agent triaging a live incident therefore faces a real choice with a real trap: the fast view
is too short, and the long view does not yet contain the queries that caused the incident.
Reproducing that costs a `WHERE start_time < current_timestamp - interval '45 minutes'` in a
view definition. Our proxy already sees every statement; fakesnow already maintains its own
metadata tables under `_fs_global._fs_information_schema`, including a `_fs_load_history` for
`COPY` **[V]**. Estimate: **1–2 days** for `query_history`, `warehouse_metering_history` and
`access_history` with a synthetic credits column.

### Warehouse sizing and credit burn — **feasible, no compute needed**

Credits are a doubling curve by size (X-Small 1/hour, Small 2, Medium 4, Large 8 for Gen1) and
a warehouse is required for every query and every DML **[R]**. A cost-spike archetype needs
warehouse size × auto-suspend × virtual elapsed time, all of which we control deterministically.
Nothing here needs real compute; it needs a metadata table and arithmetic a grader can check.
Note the tension with our own determinism layer: credits must be a function of *virtual* time
(agent.md §7.4), never wall-clock, or the task stops being reproducible.

### RBAC — **feasible but deliberately shallow**

Full Snowflake RBAC (role hierarchies, future grants, ownership chains, secondary roles) is a
large project. The *useful* subset is small: roles, grants on schemas and tables, `USE ROLE`,
`SHOW GRANTS`, and a `Insufficient privileges to operate on table 'X'` error with the right
error code. That is enough for the best RBAC decoy — the agent's query returns nothing, or the
scheduled job's role lost a grant, and the data is fine. Ship the subset; loudly reject the
rest.

### What we lose, and I think should lose

- **Micro-partition pruning and query profiles.** Not emulable honestly at DuckDB speed, and a
  faked profile is a fabricated artifact we would then grade against. Drop the class.
- **Snowpark.** Snowflake's own local mode cannot run `Session.sql()`. Not worth it.
- **Concurrency and warehouse queueing.** Real Snowflake behaviour under contention is not
  reproducible on an embedded single-node engine, and faking queue delays is faking a
  performance profile.
- **Fail-safe, replication, data sharing, Cortex, Native Apps.** Out of scope, permanently.

---

## 7. Staged build proposal

Each stage ships something usable. Estimates are engineer-weeks for one competent engineer who
has read fakesnow's transforms package. They assume we fork or vendor fakesnow and upstream
what we can — most of these are fixes the maintainer would plausibly take, which is worth
pursuing because it keeps us on the mainline.

| Stage | Scope | Effort | Ships |
|---|---|---|---|
| **S0 — do nothing** | Keep DuckDB as the frozen v1 warehouse surface (agent.md §9.2.1). Snowflake is *not* on the v1 path. | 0 | The 2026-09-22 v1 target, unendangered |
| **S1 — the Snowflake face** | fakesnow server in the world container behind a fake hostname + local TLS. Fix the three blockers: 500-retry hang, dbt incremental temp-view qualification, `USE SCHEMA db.schema`. Add `CURRENT_ACCOUNT/REGION/ROLE/WAREHOUSE`, a real-looking `CURRENT_VERSION()`. Add `/api/v2/statements`. Turn the sqlglot `Command`-fallback warning into a hard error ("strict mode"). | **1 week** | dbt, the Python connector and `snow` all work against a Snowflake-flavoured world. Existing archetypes port with dialect changes only. |
| **S2 — platform objects** | Warehouses as metadata + synthetic credits. RBAC subset (roles, grants, `USE ROLE`, `SHOW GRANTS`, privilege errors). `QUERY_HISTORY`/`ACCOUNT_USAGE`/`WAREHOUSE_METERING_HISTORY` with the 45-minute lag. `RESULT_SCAN` + 24h result cache. `CREATE FILE FORMAT` and the missing `COPY` options. Stream/Task **DDL and catalog** (no execution). | **2–3 weeks** | Cost-spike, RBAC-decoy and observability-triage archetypes become possible. This is where the interesting tasks start. |
| **S3 — temporal** | DuckLake-backed storage. `AT`/`BEFORE` with TIMESTAMP/OFFSET/STATEMENT, `UNDROP`, `CLONE ... AT(...)`, `DATA_RETENTION_TIME_IN_DAYS` enforcement including the "retention expired" error. Streams over `table_changes()` with our own offset state. Tasks executed by the Track-A deterministic scheduler, not a new one. | **3–4 weeks** | DR-under-destructive-ambiguity and CDC archetype classes. **Capability nobody else has**, which is also a sales line. |
| **S4 — full one-to-one** | Everything else | **Multi-quarter, unbounded** | **Recommend against.** See §0.4. |

**Total to a fake Snowflake better than anything that exists: 6–8 weeks after S1, i.e. ~7–9
engineer-weeks.** Against the "full one-to-one" framing in the meeting notes, that is the
honest number, and the staging means we can stop after any stage with something coherent.

**The S1/S2 boundary is the real decision.** S1 alone buys realism (it *looks* like Snowflake
and everything connects) but buys almost no new task difficulty, because every archetype it
supports is one we can already build on DuckDB. If the goal is "the agent believes it is on a
work laptop," S1 is sufficient and cheap. If the goal is "Snowflake unlocks task families we
cannot otherwise reach," the money is in S2 and S3 and S1 is only the entry fee. **I think S2 is
where the return is, and I would not fund S1 as an end in itself.**

---

## 8. Sandbox detectability — my honest opinion is that this is over-weighted

**What would give it away today**, in descending order of obviousness **[V unless noted]**:

1. `SELECT CURRENT_VERSION()` returns **`'0.0.0'`**. Free to fix; fix it in S1.
2. `CURRENT_ACCOUNT()`, `CURRENT_REGION()`, `CURRENT_ROLE()` don't exist at all — *"Scalar
   Function with name current_account does not exist!"*. Free to fix.
3. Error text is DuckDB's wearing Snowflake's error codes: `100035 (22007): Conversion Error:
   Could not convert string 'abc' to INT64` where Snowflake says *"Numeric value 'abc' is not
   recognized"*. Moderate work to map the common ones; genuinely unbounded to map all.
4. `EXPLAIN` returns a DuckDB box-drawing plan. Loud. Either map it or reject it.
5. `SHOW WAREHOUSES` returns exactly one warehouse literally named `FAKESNOW_WAREHOUSE`.
6. Missing system views: no `SNOWFLAKE` catalog at all, which is *very* visible to anyone who
   knows Snowflake.
7. Latency: **5–20ms** per statement versus Snowflake's typical hundreds of ms to seconds.
8. Account URL shape — solvable with a fake hostname + local CA, which is precedented:
   LocalStack serves on a real DNS name resolving to 127.0.0.1 with a published SSL cert **[R]**.

**And now the honest opinion, which is that items 3 and 7 do not matter much and we should not
spend weeks on them.**

The argument for caring is that a model which concludes it is in a simulation may behave
differently — sandbagging, or treating destructive actions as consequence-free. That is a real
alignment-evaluation concern and I am not dismissing the category. But for *our* grading
validity:

- **Our tasks do not have a "cheat if you know it's fake" move.** The score comes from
  diagnosing a planted defect and repairing state. Knowing the warehouse is DuckDB does not tell
  you which of three decoys accounts for the AOV halving. Contrast with a security or
  deception eval, where sandbox-awareness changes the optimal policy directly. It does not
  here.
- **The agent already knows it is in an eval.** It was handed a ticket, a repo and a tool
  gateway. Believing the *warehouse vendor* is real while the whole situation is obviously
  constructed is not a coherent belief state to engineer for.
- **Names 1, 2, 5, 6 are worth fixing anyway** — not for deception, but because an agent that
  wastes fifteen turns on `SNOWFLAKE.ACCOUNT_USAGE` not existing is being measured on our bug
  rather than on the task. **That** is the real cost of detectability, and it is a *usability*
  argument, not a deception one.

**Position: fix the cheap identity tells (1, 2, 5, 6) in S1 because they're an afternoon and
they stop wasted turns. Map the top ~20 error messages in S2 because good error text is how an
agent navigates. Do not attempt latency injection, do not attempt a full error-message
catalogue, and do not treat "the agent figured out it's DuckDB" as a bug unless we can show it
changed a score.** If someone wants to argue the opposite, the thing that would change my mind
is a measurement: run the same archetype with and without the tells fixed and show a score
delta outside noise. Until that measurement exists, this is aesthetics.

---

## 9. Risks

1. **Silent divergence corrupts a grading reference.** The one that actually matters. Mitigation:
   strict mode (§5); grade values through explicit casts with tolerance, never wire types; and
   keep data-eng-bench's dual-backend discipline — author on DuckDB, spot-check on real
   Snowflake where a paid account allows (agent.md §5).
2. **Single-maintainer dependency.** fakesnow is essentially one person. 208 stars, pushed
   today, so healthy now. Mitigation: Apache-2.0 means we can vendor and fork; the codebase is
   ~6,600 lines and readable; and we should upstream fixes rather than diverge.
3. **The emulator executes agent-authored SQL and has an open SQL-injection issue in its stage
   transforms** (#292, still open) **[R]**. It must sit inside the blast radius of the
   three-zone topology (agent.md §7.1), not be treated as trusted infrastructure. Not a blocker.
4. **DuckLake pins us to a DuckDB version pair.** DuckLake is young. Extension/engine version
   skew would break world rebuilds. Mitigation: pin both in the image, bake the extension, and
   put the pair in the world fingerprint.
5. **Scope creep into S4.** The meeting notes' enthusiasm for a full one-to-one mock is the risk
   this document exists to price. The staging in §7 is the control.
6. **Snowflake tasks are not obviously harder than DuckDB tasks.** If the archetype difficulty
   comes from conjunction (agent.md §8.3), a fancier warehouse may add realism without adding
   difficulty. S2 and S3 are justified by *new task families* (cost, RBAC, temporal, CDC) —
   and if those families do not survive authoring, the investment does not pay. **Author one
   archetype in each family on paper before funding S3.**

---

## 10. Recommendation, the strongest counter-argument, and what I could not determine

**Recommendation.** Keep DuckDB for v1 exactly as frozen — Snowflake is not on the path to
2026-09-22. Immediately after, adopt `tekumara/fakesnow` as the Snowflake face rather than
building a mock, because it already implements the connector wire protocol in 320 lines, runs
on the engine our world layer is frozen on, and I verified today that the real Python connector
and the real `dbt-snowflake` adapter both work against it. Spend one week on S1 to make it
honest — kill the 500-retry hang, fix the dbt incremental break and the `USE SCHEMA` bug, and
turn sqlglot's silent `Command` fallback into a hard error — then spend the real money on S2
(warehouses, credits, RBAC subset, and `ACCOUNT_USAGE` with its 45-minute lag) and S3 (DuckLake
time travel, streams, clones), which together produce a Snowflake emulator with capabilities no
existing project, commercial or open, currently has. Reject the ClickHouse backend: the
transpilation path is measurably worse on semi-structured SQL, its async-mutation DML model puts
silent timing-dependent wrongness underneath the dbt-incremental mechanism our archetypes are
built on, and it discards the only working Apache-2.0 Snowflake front end. Reject the full
one-to-one mock outright.

**The strongest argument against my own recommendation.** It is that S2 and S3 are ~6 weeks of
engineering justified by *hypothesised* task families, and I have not authored a single
archetype to prove those families are real. The counter-case runs: our difficulty comes from
conjunction across surfaces (§8.3), not from warehouse-vendor features; a cost-spike task can be
built from a fake billing CSV and a scheduler log without any Snowflake at all; RBAC failures
can be simulated by a gateway that refuses a tool call; and Time Travel's actual role in an
incident is usually one `CREATE TABLE backup AS SELECT` an agent could do anyway. If that is
right, then S1 — pure costume — is the whole correct investment, and S2/S3 are six weeks buying
realism that does not move Pass@1. I do not believe this, mostly because the
`ACCOUNT_USAGE`-lag-versus-`INFORMATION_SCHEMA`-window trade is a genuine diagnostic trap that
cannot be simulated with a CSV and that no amount of conjunction on DuckDB reproduces. But it is
the argument I would make if I were arguing the other side, and risk 6 in §9 is the cheap test
that settles it: author one archetype in each proposed family on paper first.

**What I could not determine.**

- **Whether `snowflake-jdbc` and the `snow` CLI actually work against fakesnow.** Both are
  claimed by the README and one corroborated by a 2-star Testcontainers module. I verified the
  Python connector only. If a world needs a JVM or CLI client, test this before committing.
- **How far real-Snowflake divergence extends beyond what I sampled.** My probe was ~70
  statements. The honest characterisation is "I found seven silent divergences in an afternoon
  without trying hard," not "there are seven."
- **Whether DuckLake's snapshot machinery survives our reset/fingerprint contract.** Snapshots
  are extra on-disk state; whether `world_fp` and the bit-reproducible rebuild still hold with a
  DuckLake catalog is untested and is the first thing S3 should validate.
- **Real-Snowflake latency distribution.** I quoted "hundreds of ms to seconds" from experience,
  not measurement. If the latency-tell argument ever becomes load-bearing, measure it.
- **LocalStack's backend engine.** Reported as a PostgreSQL backend per account; I saw this
  second-hand in a release blog summary and did not confirm it on a primary page. Treat as
  unconfirmed.
- **Whether the `dbt-snowflake` adapter's own integration test suite could serve as a
  conformance harness for our fork.** Strong idea, no time to evaluate. If it can run against
  fakesnow, it is a ready-made fidelity gate and would be worth a day of someone's time.
