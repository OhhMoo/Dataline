# gateway/ — every way an agent touches the world

One audited chokepoint between the agent and the world, with two transports over it: the
**MCP gateway** (63 tools: warehouse, dbt, git/files, scheduler, docs, BI, object storage,
observability, ingestion, and the workflow plane — tickets, chat, wiki) and **native
mode** (real CLIs and SDKs against vendor-shaped fake services, no tool list). Both write
the same action-log row (agent.md §9.2.2); that row is what blindness detection and
containment rest on.

```
src/alkera_gateway/
  core.py tools.py operations.py    the chokepoint, the tool registry, op_class + targets
  records.py audit.py               the 9.2.2 row (sealable Observation), the log writer
  sql.py files.py storage.py workflow.py observability.py bi.py ingest.py scheduling.py
  warehouse_spec.py warehouses.py   DuckDB / Postgres / fakesnow registry (dual warehouse)
  server.py cli.py replay.py signals.py
  native/                           NATIVE MODE
    audit.py context.py             the one row writer; the shared run-and-record span
    services/                       jira slack confluence metabase prometheus alertmanager loki airbyte deploy
    tailers/ shims/ serve.py        server logs -> rows; PATH wrappers (duckdb, ./ops/sched); one process, nine ports
docker/         compose.yml (+ .episode, .native), Dockerfile, build_episode.py, containment.py P0-P6, evidence/
tests/          pytest
```

**Documents.** `SURFACES.md` — the five surface contracts (warehouse connector, dual
warehouse, ingest, workflow, observability). `NATIVE.md` — native mode: spec, mapping, what
is measured. Deeper: `docker/NATIVE_STACK.md`, `native/PARITY.md`,
`native/services/{HUMAN,OPS}_PLANE.md`, and the harness side in
`tasks/harness/{NATIVE_DRIVER,NATIVE_CONTAINER}.md`.

## Run

A JSON config with the world paths and an audit directory outside the workspace:

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
  "initial_world_fp": "semantic fingerprint from the world builder"
}
```

```sh
alkera-gateway serve --config /etc/alkera/gateway.json      # MCP at http://127.0.0.1:8080/mcp/
alkera-gateway call sql_query --arg "sql=select 1"          # CLI, same server
alkera-mcp-smoke http://episode:8080/mcp/
alkera-gateway signals /audit --experimental
alkera-gateway replay /previous-audit --config /etc/alkera/replay.json
```

`warehouse_backend` selects DuckDB (default), Postgres, or the experimental Snowflake
route; `sql_*` names do not change. Additional warehouses are declared under `warehouses`
(`SURFACES.md#dual-warehouse`).

## Audit and replay

`actions.jsonl` stores the complete normalised request and result for every call;
`requests.jsonl` is a write-ahead record used to detect interrupted calls. Only the
operation fields are transport-independent; transport, wall time, latency and memory are
telemetry.

Replay requires a fresh restore of the session's initial world snapshot. It checks the
initial semantic fingerprint and dependency versions, replays every operation and compares
responses. Process signals (`signals`) are advisory: their output declares the
false-positive rate unmeasured until a labelled calibration set exists.

## Development

```sh
uv sync --extra dbt          # --extra native for the fake services
uv run ruff check .
uv run pytest -q
```

The suite starts disposable, digest-pinned Postgres and S3Mock/MinIO containers when
Docker is available; without Docker it skips explicitly. `ALKERA_TEST_POSTGRES_DSN` /
`ALKERA_TEST_S3_ENDPOINT` point it at existing services (the DSN needs permission to
create databases and roles; tests create and remove their own). A supplied but unreachable
DSN fails the tests.

## The stacks

From the repository root:

```sh
python gateway/docker/build_episode.py --tag alkera-episode:head     # from a clean HEAD; records evidence/image.json
docker compose -f gateway/docker/compose.yml build episode
python gateway/docker/acceptance.py --project alkera-check --skip-containment
python gateway/docker/containment.py --project alkera-check --profile snowflake   # P0-P5
python gateway/docker/containment.py --native --project alkera-p6                 # P6
python gateway/docker/snowflake_acceptance.py --project alkera-socket-check
```

Use a dedicated project name: containment and socket checks tear down their stacks and
remove generated volumes. They run no model rollouts. `test_image_parity` fails when
`alkera-episode:head` was built from a revision behind HEAD on image-bearing paths —
rebuild before any container measurement.

Known limits: this compose configuration does not persist S3 objects across service
recreation; multi-service snapshot/replay is not complete; Snowflake semantics are not
claimed; Postgres dbt requires a matching profile and adapter.
