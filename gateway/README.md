# Alkera tool gateway

Track B exposes the five v1 world surfaces through one audited operation core.
MCP and CLI requests use the same validation, connectors, and JSONL writer.

## Run

Create a JSON config with the six world paths and an audit directory outside
the workspace:

```json
{
  "workspace": "/world/workspace",
  "warehouse": "/world/var/warehouse/warehouse.duckdb",
  "scheduler": "/world/var/sched/state.json",
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
