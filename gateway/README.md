# Alkera tool gateway

Track B exposes warehouse, dbt, Git/files, scheduler, docs, BI, object storage,
observability (metrics, alarms, canaried deploys, run logs) and the workflow
surfaces (issue tracker, chat, wiki - `WORKFLOW_CONTRACT.md`) through one audited
operation core. Track A also supplies the ingestion tools.
MCP and CLI requests use the same validation, connectors, and JSONL writer.

## Run

Create a JSON config with the world paths and an audit directory outside
the workspace:

```json
{
  "workspace": "/world/workspace",
  "warehouse": "/world/var/warehouse/warehouse.duckdb",
  "scheduler": "/world/var/sched/state.json",
  "observability": "/world/var/obs/observability.yml",
  "obs_samples": "/world/var/obs/samples.jsonl",
  "workflow_dir": "/world/var/workflow",
  "dashboards": "/world/var/bi/dashboards",
  "profiles_dir": "/world/build",
  "world_root": "/world",
  "audit_dir": "/audit",
  "episode_id": "run-001",
  "initial_world_fp": "semantic fingerprint from Track A"
}
```

```sh
alkera-gateway serve --config /etc/alkera/gateway.json
alkera-gateway call sql_query --arg "sql=select 1"
alkera-mcp-smoke http://episode:8080/mcp/
alkera-gateway signals /audit --experimental
alkera-gateway replay /previous-audit --config /etc/alkera/replay.json
```

The MCP endpoint is `http://127.0.0.1:8080/mcp/`. The CLI endpoint is local to
the episode network and sends requests to the same server.

## Audit and replay

`actions.jsonl` stores the complete normalized request and result for every
call. `requests.jsonl` is a write-ahead record used to detect interrupted
calls. Only the operation fields are transport-independent; transport, wall
time, latency, and memory remain telemetry.

Replay requires a fresh restore of the session's initial world snapshot. It
checks the initial semantic fingerprint and dependency versions, replays every
operation, and compares responses. The world snapshot supplies initial state;
the log supplies every subsequent action.

Process signals are advisory. Their output declares the false-positive rate
unmeasured and includes review evidence until a labeled calibration set exists.

## Development

```sh
uv sync --extra dbt
uv run ruff check .
uv run pytest -q
```

The suite starts disposable, digest-pinned Postgres and S3Mock containers when
Docker is available. Unavailable Docker produces explicit skips. To use an
existing test service, set `ALKERA_TEST_POSTGRES_DSN` or `ALKERA_TEST_S3_ENDPOINT`.
The Postgres DSN needs permission to create databases and roles; tests create
and remove their own uniquely named database and role, preserving existing schemas.
A supplied but unreachable Postgres DSN fails the tests.

## Multi-service stack

The frozen Python/configuration interface is in [CONNECTOR_CONTRACT.md](CONNECTOR_CONTRACT.md).
`warehouse_backend` selects DuckDB (default), Postgres, or the experimental
Snowflake connection route. `sql_*` names stay the same.

From the repository root:

```sh
docker compose -f gateway/docker/compose.yml build episode
python gateway/docker/acceptance.py --project alkera-check --skip-containment
docker compose -f gateway/docker/compose.yml --profile snowflake build snowflake
python gateway/docker/containment.py --project alkera-check --profile snowflake
python gateway/docker/snowflake_acceptance.py --project alkera-socket-check
```

Use a dedicated project name: containment and socket checks tear down their
test stacks and remove their generated volumes. They do not run model rollouts.
The compose fixture checks integration; it is not a task world generator.

Docs support list/get/search with numbered evidence and explicit unreadable-file
diagnostics. BI supports list/get/update, live rendering, and explicit cache
refreshes with virtual timestamps and source watermarks. S3 list/get/put/delete
preserve binary bodies using base64.

Known limits: this compose configuration does not persist S3 objects across
service recreation; multi-service snapshot/replay is not complete. Snowflake
semantics remain Andrew's work. Postgres dbt requires a matching profile and
adapter; SQL/BI conformance tests do not establish dbt compatibility.

## Observability

[OBSERVABILITY_CONTRACT.md](OBSERVABILITY_CONTRACT.md) covers `metrics_*`,
`alarms_*`, `deploy_*` and `logs_*`. Metric readings are taken from the world on
every call — a freshness value is the world clock minus the real maximum
timestamp, a job duration is the real run history — and each response carries
the raw components so it can be re-derived with `sql_query`. A reading that
cannot be taken is `insufficient_data`, never zero. Alarms are computed, not
cached, and `deploy_run` re-measures its canary after applying a change and
reports what broke without blocking it.

**Now is computed; then is remembered.** `metrics_get` measures live state, but
`metrics_series` and `alarms_history` read the world's recorded sample store at
`obs_samples` (`/var/obs/samples.jsonl`, OBS_STORE_CONTRACT.md), which the world
appends to during build and advance and the gateway only ever reads. Once the
world has advanced, the state that produced yesterday's reading is gone, so the
recorded sample is the only evidence it held that value; a derivation run today
describes today's world and is not offered as history. The store has a declared
retention window, and samples outside it are reported as `aged_out` rather than
silently missing. A world with no store keeps the derived behaviour unchanged.

Definitions are seeded per world at `observability` (`/var/obs/definitions.yaml`,
or `observability.yml`; a world that ships both is refused, because a reader and
a writer describing different metric sets is the one way this surface can be
wrong without looking wrong). All four world themes seed one. Observed samples,
alarm transitions and deploy records live in `obs_state_dir` outside the world
tree. Worlds without a definitions file answer `no_observability`. Some alarm
imprecision is deliberate and is declared author-side in `known_imprecision`,
which never reaches a tool response.
