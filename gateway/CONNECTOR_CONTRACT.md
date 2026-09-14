# Warehouse connector contract v1

Frozen for the `3_track.md` Track B build. Tracks A and C may consume this
interface without importing a particular database driver.

## Configuration and Python interface

`GatewayConfig.warehouse_backend` is `duckdb` (default), `postgres`, or
`snowflake`. Existing `warehouse` remains the DuckDB path. Remote credentials
come from the environment variable named by `warehouse_dsn_env` (Postgres),
or `snowflake_options_env` (a JSON object for the Snowflake Python connector).
Neither secret belongs in the workspace, tool arguments, or action log.

`create_warehouse(config) -> WarehouseConnector` selects the driver.

```python
class WarehouseConnector(Protocol):
    dialect: str

    def execute(
        self,
        sql: str,
        observation: Observation,
        *,
        read_only: bool = False,
        limit: int | None = None,
    ) -> dict: ...
```

**v1.1, additive.** `dialect` is the sqlglot dialect the driver parses and renders
in. It is read-only, and it is the connector's property rather than a string held
elsewhere: `sql.py` classifies statements and never names an engine, so
`inspect_sql(sql, observation, read_only, dialect)` now takes the dialect
explicitly with no default. Consumers that only call `execute` are unaffected.
`DuckDBWarehouse`, `PostgresWarehouse` and `SnowflakeWarehouse` all live behind
`create_warehouse`; nothing outside `warehouse.py` and `snowflake.py` imports a
database driver.

`sql_query` accepts one read statement; `sql_execute` accepts one or more
statements in an atomic transaction. Query results retain `columns`, `rows`,
and `truncated`. Multi-statement results wrap these in `statements`.
Drivers preserve decimal values and normalize common column types. DML without
RETURNING yields the existing `Count`/`BIGINT` result. Unsupported dialect
features fail explicitly; this interface does not promise SQL transpilation.
Destructive writes are allowed and classified in `Observation`.

## Audit compatibility

The existing action-row schema is unchanged. Canonical bytes from
`records.operation_record(row)` match across MCP and CLI, and across backends
for equivalent supported SQL, values, types, initial state and virtual time.
Full rows cannot be identical: `transport`, wall time, latency, memory and disk
usage are measured telemetry. Engine-specific SQL, types and errors can differ.

## State and containment

DuckDB is attached as `wh` to an in-memory connection; never open the warehouse
file as the primary catalog. Postgres uses a non-superuser role, no extensions
or server-file privileges, and read-only transactions for `sql_query`.
The harness reaches only the gateway network. Database, S3 and Snowflake ports
live on a separate internal backend network with no published host ports.
Service logs and storage use separate volumes, never the fingerprinted tree.

Track A owns seeded data and snapshots. A remote backend snapshot must include
its logical contents; a hash of the local world directory alone is insufficient.
Track C must restore all participating services before replay/grading. Remote
backends require an explicit initial fingerprint; the gateway does not pretend
the DuckDB fingerprint covers them. dbt profiles must match the declared backend.

## Additional surfaces

Docs remain seeded `/workspace/docs/**/*.md`: `docs_list`, `docs_get`, and
`docs_search`, with file/line evidence. Existing fs tools can edit these files.

BI's four objects are the dashboard, tile/panel (SQL and cached values), metric
definition (expression, owner, documentation), and refresh event (virtual time
and source watermark), as specified in T204. Existing `bi_render`
still computes live values. `bi_list`, `bi_get`, `bi_refresh`, and `bi_cached`
add discovery and explicit cache state; `bi_update` replaces a definition without
implicitly refreshing it. Cache state lives in `bi_state_dir`,
outside the world tree; snapshot it alongside remote services.

S3 tools expose bucket-scoped list/get/put/delete against S3Mock using the S3
wire API. Bodies are base64 for lossless binary data. The control plane creates
buckets and seeds objects; tool calls cannot change endpoint or credentials.

Snowflake integration supplies a local hostname, trusted local TLS and the
Python connector route. Andrew owns semantics. Only the connector wire routes
are proxied; `/api/v2/statements` remains unsupported. No SQL compatibility or
Snowflake-grade transactional guarantee is claimed until his conformance gates pass.
