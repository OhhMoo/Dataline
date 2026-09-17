# gateway/ — every way an agent touches the world

**Purpose.** One audited chokepoint between the agent and the world. Two transports
over it: the MCP gateway (63 tools) and **native mode** (real CLIs/SDKs against
vendor-shaped fake services, no tool list). Both write the same action-log row
(`agent.md` §9.2.2); that row is what blindness detection and containment rest on.

```
src/alkera_gateway/
  core.py tools.py operations.py    the chokepoint, the tool registry, op_class + targets
  records.py audit.py               the 9.2.2 row (sealable Observation), the log writer
  sql.py files.py storage.py workflow.py observability.py bi.py ingest.py scheduling.py   connectors
  warehouse_spec.py warehouses.py   DuckDB / Postgres / fakesnow registry
  server.py cli.py replay.py signals.py
  native/                           NATIVE MODE — see NATIVE_SURFACES.md
    audit.py context.py             the one row writer; the shared run-and-record span
    services/                       jira slack confluence metabase prometheus alertmanager loki airbyte deploy
    tailers/ shims/ serve.py        server logs -> rows; PATH wrappers (duckdb, ./ops/sched); one process, nine ports
docker/         compose.yml (+ .episode, .native), Dockerfile, containment.py P0-P6, evidence/
tests/          321
```

Contracts: `NATIVE_SURFACES.md` (spec + amendment), `NATIVE_INTEGRATION.md` (numbers),
`CONNECTOR_CONTRACT.md`, `DUAL_WAREHOUSE_CONTRACT.md`, `WORKFLOW_CONTRACT.md`,
`OBSERVABILITY_CONTRACT.md`, `INGEST_CONTRACT.md`. Full guide: `README.md`.
