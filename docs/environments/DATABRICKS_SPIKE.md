# Databricks tier 1: transport spike (2026-10-07)

Task 6, step 1 of `docs/superpowers/plans/2026-10-07-remaining-tools-and-harbor.md`,
time-boxed to 2 h. Question: which transport do `databricks-sql-connector` and
`dbt-databricks` speak, so the fake knows what it has to serve; and may the Databricks CLI
be shipped.

## How it was measured

A catch-all HTTPS stub (uvicorn, a certificate from `alkera_gateway.native.tls.mint`,
trusted through `REQUESTS_CA_BUNDLE`) logged every request each client sent, answering
404 `{"error_code": "ENDPOINT_NOT_FOUND"}`. Each client was pointed at it with the
workspace's fake token and `http_path=/sql/1.0/warehouses/alkera`. Two client sets:

- **unpinned** (what `pip install` gave on 2026-10-07): databricks-sdk 0.117.0,
  databricks-sql-connector 4.4.0, dbt-databricks 1.12.6;
- **locked** (`gateway/requirements-databricks.lock`, compiled by
  `lock_warehouse_clients.py databricks` against the base, native and ds locks):
  databricks-sdk **0.47.0**, databricks-sql-connector **4.6.0**, dbt-databricks
  **1.10.9** (dbt-spark 1.11.0). The base lock's `dbt-common==1.39.0` is what holds
  dbt-databricks back: 1.12.x requires `dbt-common<1.39.0`, and 1.10.9 is the newest that
  resolves beside it; it caps the SDK at 0.47.

What each client sent first (both sets agree unless noted):

| client | request | transport |
|---|---|---|
| `databricks.sql.connect(...)` (default) | `POST /sql/1.0/warehouses/alkera`, `Content-Type: application/x-thrift`, body `OpenSession` | **Thrift HS2 over HTTP** |
| `databricks.sql.connect(..., use_sea=True)` | `POST /api/2.0/sql/sessions` `{"warehouse_id": "alkera"}`, then `POST /api/2.0/sql/statements` with `session_id` | **SQL Statement Execution API** (SEA) |
| `WorkspaceClient(...).statement_execution.execute_statement` | `POST /api/2.0/sql/statements` (0.47.0 adds a trailing `/`) | Statement Execution API |
| `WorkspaceClient(...).current_user.me()` | `GET /api/2.0/preview/scim/v2/Me` | REST |
| SDK >= 0.5x only | `GET /.well-known/databricks-config` (unauthenticated) before the first call | REST |

`dbt-databricks` opens its connections with `databricks.sql.connect(**args,
**connection_parameters)` (`handle.py`), so it speaks whatever the connector speaks, and
its profile's `connection_parameters` reach the connector unchanged
(`use_sea: true` is accepted; only `server_hostname`, `http_path`, `access_token`,
`client_id`, `client_secret`, `session_configuration`, `catalog`, `schema` and the user
agent are reserved). Over SEA, dbt's metadata is SQL: `SHOW SCHEMAS IN`, `SHOW TABLES IN`,
`SHOW VIEWS IN`, and on a Unity Catalog catalog `system.information_schema.tables`
(`get_uc_tables`), so the fake answers those statements.

The connector 4.6.0 prints, for `use_sea=True`: "The SEA backend (use_sea=True) is
deprecated and incomplete and should not be used in production; it is slated for removal.
Use the kernel backend instead by passing use_kernel=True". The kernel backend is a Rust
extension that is not in the lock (4.4.0's docstring: "the wheel is not yet published on
PyPI"). Recorded: the day a lock regeneration drops SEA, this plug's connector path goes
with it, and the e2e check is what notices.

## Decision: the Statement Execution API

Of the plan's three outcomes this is the first: **the Statement Execution API, configured
in the box profile and the discovery files.**

- The fake serves the REST routes (statements, sessions, Unity Catalog, warehouses, SCIM
  `Me`, Jobs 2.1) and nothing that speaks Thrift. A Thrift `POST` to the warehouse's HTTP
  path is refused loudly: HTTP 400 with `x-thriftserver-error-message: Thrift HS2 transport
  is not available in this environment: connect with use_sea=True (SQL Statement
  Execution API)`, which the connector puts in the exception it raises, and one
  `databricks.thrift` audit row.
- `databricks-sql-connector`: works with `use_sea=True`, which the onboarding page and
  `services.toml` (`use_sea = "true"`) say.
- `dbt-databricks`: the box profile's databricks target carries
  `connection_parameters: {use_sea: true}`.
- Thrift was not implemented: the minimal TCLIService subset plus Arrow row sets is a second
  protocol to keep faithful, for the same SQL the REST path already serves.

## Two measurements that moved the plan

**The port is 443, not 8098.** The connector cannot take `host:port`: it builds its base
URL as `https://{server_hostname}:{_port}` with `_port` an underscore-private keyword
(default 443), so `server_hostname="databricks.internal:8098"` yields
`https://databricks.internal:8098:443` and fails. dbt-databricks hands the same bare `host`
to the connector and to the SDK's `Config`, and SDK >= 0.5x resolves
`/.well-known/databricks-config` on that host's default port before it authenticates: on
any port but 443 every dbt invocation stalls in retries (measured: `Config(host=
"https://localhost:1", token=...)` returned after **300.51 s**). The locked SDK 0.47.0 does
not do that lookup, so 8098 would work today with `connection_parameters: {_port: 8098}`,
but only by teaching every client a private argument a real Databricks user never passes.
On 443 every client works with its defaults. The fake still answers
`/.well-known/databricks-config`, so a newer SDK finds it. Binding 443 is fine where
native-services runs: a container (Docker >= 20.10 sets
`net.ipv4.ip_unprivileged_port_start=0`, and the service runs as root there and in Harbor),
and the host topology adds an offset of at least 10000.

**`SSL_CERT_FILE` as well as `REQUESTS_CA_BUNDLE`.** The SDK verifies through `requests`
(`REQUESTS_CA_BUNDLE`). The connector builds its own `ssl.create_default_context(cafile=
_tls_trusted_ca_file)`, which with no argument reads OpenSSL's default paths, i.e.
`SSL_CERT_FILE`, never `REQUESTS_CA_BUNDLE`. The databricks registry row therefore exports
`SSL_CERT_FILE` = the same `bundle.pem` (certifi + the minted certificate, so nothing else
the box trusts is lost).

## Result formats

The connector over SEA asks for `format: ARROW_STREAM`, `disposition: EXTERNAL_LINKS`
(cloud fetch) unless `use_cloud_fetch=False`, and builds its result queue from the
manifest the server answers with, not from what it asked for. The fake answers every
statement JSON_ARRAY, inline, in one chunk, and the manifest says `format: JSON_ARRAY`:
there is no cloud storage to presign external links against, and the values are the
same. Recorded as a deviation: an SDK caller that asks for EXTERNAL_LINKS gets
`result.data_array`, not `external_links`.

## SQL: fail-closed, after review (2026-10-07)

Review found sqlglot's `databricks` -> `duckdb` translation quietly wrong in places, each a
different answer rather than an error: `CAST(10.50 AS INT)` rounds (11; Databricks
truncates, 10), `dayofweek` counts Sunday as 0 (Databricks 1) and `weekday` likewise
(Sunday 6), `date_add` returns a TIMESTAMP (Databricks a DATE), `timestampdiff(MONTH,
'2024-01-31', '2024-02-29')` counts the boundary (1; Databricks counts whole months, 0),
`substring(s, 0, n)` is one character short, `hash()` is DuckDB's hash. So the translator
is now fail-closed (`gateway/src/alkera_gateway/databricks_functions.py`):

- every function must be on a vetted list - 101 sqlglot function classes, 5 functions by
  name, 9 rewritten operators (after the second review) - and every other expression
  node on a list of 82 structural and operator types; anything else is refused naming it;
- an entry is vetted as sqlglot translates it, or rewritten with Databricks' semantics:
  integral CAST truncates, dayofweek/weekday/EXTRACT(DOW), date_add/date_sub return a
  DATE, timestampdiff/datediff(unit) count whole units the way java.time does,
  substring/left/right follow Spark's positions, percentile interpolates, `/`, `div` and
  `%` raise DIVIDE_BY_ZERO as an ANSI warehouse does, current_date/current_timestamp read
  the WORLD's clock;
- argument shapes are checked: datetime formats beyond yyyy/yy/MM/dd/HH/mm/ss, EXTRACT
  fields, truncation units, CAST targets, `CAST(x AS DECIMAL)` without a precision (it
  parses like a `1.5BD` literal), LIKE escapes and backslash escapes in string literals
  are refused by name;
- `gateway/tests/test_databricks_functions.py` holds the corpus: 237 expressions and 55
  queries (after the second review), each with the result the Databricks SQL reference
  gives (its examples where it has one), and 35 refusals; a test fails when a vetted
  entry has no case.

Unqualified names are qualified with the session's schema, two-part names with catalog
`main`, so no statement reaches DuckDB's own catalogs or a table function called without
parentheses (`FROM pragma_database_list`).

## SQL: typed, after the second review (2026-10-07)

The second review found the first fix's answers right in value class but wrong in type,
scale or rendering, and one way back to the engine. Fixed:

- **CTE scope (security).** CTE names were collected statement-wide, so a WITH inside a
  subquery exempted a same-named DuckDB view outside it from qualification (`SELECT *
  FROM pragma_database_list WHERE 1 IN (WITH pragma_database_list AS ...)` read the
  engine's database list; duckdb_settings/views/secrets and a CTAS that persisted them
  likewise). A table name is now a CTE reference only when a CTE of that name is visible
  in ITS OWN scope (sqlglot's scopes, `databricks_types.cte_references`); everything
  else is qualified with the session's schema, and anything the scopes cannot place is
  qualified too. Each of the reviewer's repros is a test
  (`gateway/tests/test_databricks_review_probes.py`).
- **A statement allowlist.** Queries, DML, CREATE/DROP of a TABLE, VIEW or SCHEMA,
  TRUNCATE TABLE and COMMENT ON - anything else (CREATE SEQUENCE/INDEX/TYPE/MACRO/SECRET/
  FUNCTION, a TEMPORARY view, SET VARIABLE) is refused naming its leading words; the
  schema names `main` and `information_schema` are reserved (DuckDB's own); `CREATE
  SCHEMA x` keeps its name (the first fix renamed `main` onto the engine's catalog);
  TRUNCATE is a write, refused on information_schema.
- **Spark's types (item 4 implemented, not refused).** Before the rewrite every node is
  typed the way Spark types it (`gateway/src/alkera_gateway/databricks_types.py`: the
  lakehouse's column types, literals at minimum precision, Spark's DecimalPrecision
  rules for + - * / %, sum DECIMAL(p+10,s), avg DECIMAL(p+4,s+4), round/floor/ceil,
  widening for CASE/COALESCE/VALUES). DECIMAL division and avg are computed exactly
  (`dbx_decimal_quotient`: the rational quotient, HALF_UP to Spark's result scale; avg
  as Spark does it, the DECIMAL(p+10,s) sum over the count as DECIMAL(20,0), then to
  s+4); + - * % and sum are CAST to Spark's precision (DuckDB's values and scales are
  already exact). `d/3` on DECIMAL(18,2) is DECIMAL(22,6) 40.166667, `1.0/3`
  DECIMAL(7,6) 0.333333, `avg(d)` DECIMAL(22,6). Where a rewrite needs a type it cannot
  know it refuses (`DECIMAL arithmetic (/) on an expression of unknown type`), unless
  the unknown is a missing table, which fails TABLE_OR_VIEW_NOT_FOUND as on Databricks.
  Not modelled: Spark divides to 38 decimal places before rounding to the result scale
  (a second rounding that differs only when the quotient's digits past the result
  scale are 4999... to the 38th place).
- **div** is a BIGINT, truncating - exactly for DECIMALs (`-5.9 div 1` = -5, `0.3 div
  0.1` = 3), on the quotient for DOUBLEs.
- **CAST** truncates by its input's type (`cast(0.9999999999999999999 as int)` = 0, no
  longer through DECIMAL(38,18)); a string to an integral type must be an integer
  (`'1.9'`, `'1e3'`: CAST_INVALID_INPUT, try_cast NULL); a string to BOOLEAN takes
  t/true/y/yes/1 and f/false/n/no/0; a DOUBLE to DECIMAL or `round(DOUBLE, n)` rounds its
  decimal representation HALF_UP (`round(1.005, 2)` = 1.01); a DOUBLE or FLOAT to a
  string, and every DOUBLE/FLOAT cell, is written as Java writes it (`1.0E20`, `1.1` for
  a FLOAT); floor/ceil of a DOUBLE are BIGINT, sign a DOUBLE, the date-part functions,
  length, instr, size and the ranking window functions INT, two-argument datediff INT.
- **TIMESTAMP** is DuckDB's plain TIMESTAMP everywhere (session time zone UTC), so a
  string cast writes `2024-01-01 00:00:00`, not `+00`.
- **Smaller semantics.** concat_ws flattens ARRAY arguments and skips NULL elements;
  regexp_replace maps Java's `$n` to RE2's `\n` (named groups, `$10`, a computed
  replacement refused by name); collect_list/collect_set over no rows are `[]`, also
  under OVER; array_contains is NULL when not found in an array holding a NULL;
  add_months returns a DATE from a TIMESTAMP; lpad/rpad with an empty pad, two-argument
  replace, upper/lower's full case mapping (`upper('ß')` = SS), LIKE's `\` escape for a
  pattern read from a column, a NULL dividend over a zero divisor (NULL), INT overflow
  (ARITHMETIC_OVERFLOW), DATE - DATE (an INTERVAL: refused), regexp_extract with a group
  index beyond the pattern (refused).
- **The corpus compares exactly**: the value as the JSON_ARRAY format carries it and the
  manifest's type (`('40.166667', 'decimal(22,6)')`), no float tolerance. Unaliased
  result columns are named as Spark names them (`(d / 3)`, `count(1)`, `year(dt)`), so a
  rewrite's DuckDB form never shows in a column name.

Still different, not refused: a TIMESTAMP cell is rendered `2024-01-31T10:00:00Z` in
JSON_ARRAY (the connectors parse any ISO form; the warehouse's exact spelling was not
measured), `array()` is `array<int>` (Spark: `array<void>`), and comparing an INT to a
string such as `'3.0'` follows DuckDB's cast rather than ANSI's.

## Validated against the implementation (locked clients, before the image)

The module served over TLS on a loopback port, a fixture world behind it; the locked
venv's clients pointed at it:

- connector, `use_sea=True`: SELECT (decimal, date, string columns typed by the manifest),
  named parameters, CREATE/INSERT, `cursor.tables()` / `cursor.columns()` (sent as
  `SHOW TABLES|COLUMNS IN CATALOG main SCHEMA LIKE ...`, answered in the column layout the
  connector's own filter reads), DESCRIBE HISTORY refused by name, and the default
  Thrift transport refused with the header message;
- SDK 0.47.0: statements (`POST /api/2.0/sql/statements/`), SCIM Me, catalogs, schemas,
  tables, table get, warehouses, and Jobs - which this SDK calls at **`/api/2.2/jobs/...`**,
  so the fake serves 2.1 and 2.2 alike;
- dbt-databricks 1.10.9 with `connection_parameters: {use_sea: true}`: `dbt debug` OK,
  `dbt run` of a table and a view model OK (over SEA it opens a session per connection,
  `SHOW SCHEMAS IN main`, `CREATE SCHEMA IF NOT EXISTS`, `system.information_schema.tables`,
  `create or replace table ... using delta as ...`).

Two more measurements for port 443: the connector 4.6.0 fetches
`GET /api/2.0/connector-service/feature-flags/PYTHON/4.6.0` and posts telemetry on port
**443 whatever `_port` says**; against the loopback port every `dbt` invocation spent
300 s retrying them. On 443 the fake answers both (empty flags; telemetry accepted and
recorded).

## The image

The client lock resolves dbt-databricks 1.10.9, databricks-sql-connector 4.6.0,
databricks-sdk 0.47.0 against the base locks. Two findings:

- **certifi**: the lock pins `certifi==2025.1.31` (`databricks-clients.overrides`), the
  Snowflake lock's capped version, not because Databricks needs it but because an image
  built with both client sets installs both locks into one interpreter and the pairwise
  agreement test (`test_client_locks_agree_with_each_other`) requires one version.
- **pandas and pyarrow in a de image**: the connector requires pandas and dbt-databricks
  requires `databricks-sql-connector[pyarrow]`, so `--toolbelt de --warehouse-clients
  databricks` failed `toolbelt_check`'s de rule (no notebook libraries). The check now
  allows the hard dependencies of an installed client set (`CLIENT_IMPORTS`), and only
  those: no Jupyter, no kernel, nothing else of the da/ds roles.

## The Databricks CLI: not shipped

`databricks/cli` is under the **Databricks License**, not an open-source licence. Its grant:
"You may view, use, copy, modify, publish, and/or distribute the Licensed Materials solely
for the purposes of using the Licensed Materials within or connecting to the Databricks
Services", and "You may not use the Licensed Materials except in connection with your use
of the Databricks Services pursuant to the Agreement"
(https://github.com/databricks/cli/blob/main/LICENSE, read 2026-10-07). A local fake is not
the Databricks Services, so the CLI is not installed. The SDK, the SQL connector and
dbt-databricks are Apache-2.0 (their wheel metadata), as is dbt-spark.
