# Ingestion connector control-plane contract v1

Frozen per T203 (`implementation_structure/state/inventory/T203.yaml`) for the
archetypes that need a Fivetran-style control plane: A010, A017, A018, A022,
A025. FOUR objects, SIX reads, TWO actions. Anything beyond this list needs a
named archetype demanding it. Explicitly out: authentication flows, connector
marketplace, transformations, dbt integration, billing, webhooks, per-row logs.

## Objects

All timestamps are ISO-8601 `Z` values of the world's virtual clock
(`var/sched/state.json` `vnow`). Wall time never appears.

### Connector

- `id` — stable key, `a-z0-9_-`
- `name` — display name
- `source` — `{type, location}` (e.g. `{"type": "sftp", "location": "data/drop/acme"}`)
- `destination` — `{schema, tables: [str]}`
- `status` — `active` | `paused`
- `schedule` — cron string (declarative; the scheduler owns actual dispatch)
- `sync` — seed-only execution template `{duration_s, tables: {table: {rows_inserted, rows_updated}}}`.
  Mock plumbing: never returned by any tool.

### Sync run

- `id` — `sync-NNNNNN`, assigned in creation order
- `connector_id`
- `started_at`, `finished_at`
- `status` — stored terminal status: `success` | `failed`
- `tables` — `{table: {rows_inserted, rows_updated}}`
- `timeline` — `[{at, event}]`, events `started` then `succeeded` | `failed`

State machine: a run is created at `vnow` (`started`) and reaches its stored
terminal status at `finished_at`. Reads report `running` while
`vnow < finished_at`, and hide timeline events later than `vnow` — the machine
advances exactly when the world's virtual clock does, never on read and never
on wall time. A green run whose `tables` counts disagree with the warehouse is
representable: nothing in this model cross-checks them.

### Table schema config

One per (connector, destination table):

- `connector_id`, `table`
- `columns` — destination column list, in order
- `column_added` — drift policy when a column appears in the source
- `column_removed` — drift policy when a column disappears from the source

Drift policy enum: `propagate` (apply the change to the destination) |
`ignore` (keep the destination schema fixed) | `alert` (keep the schema, fire
an alert).

### Alert

- `id` — `alert-NNNNNN`
- `connector_id`, `run_id` (nullable)
- `kind` — `sync_failed` | `schema_drift` | `row_count_anomaly`
- `fired_at`, `message`
- `acknowledged` — bool

## Tools

Reads:

- `ingest_list` — list connectors with last-run summary.
- `ingest_get(connector_id)` — one connector's config and status.
- `ingest_runs(connector_id?, limit?, continuation_token?)` — sync runs,
  newest first, paged.
- `ingest_run(run_id)` — one run: effective status, timeline to `vnow`,
  per-table row counts.
- `ingest_schema(connector_id)` — table-level schema configs and drift policies.
- `ingest_alerts(connector_id?, acknowledged?)` — alerts, optional filters.

Actions:

- `ingest_trigger(connector_id)` — start a sync run at `vnow` from the
  connector's seeded template (`op_class: exec`). Paused connectors refuse.
- `ingest_schema_update(connector_id, table, column_added?, column_removed?)` —
  set the drift policy for one table (`op_class: write`).

## State

`var/ingest/state.json` in the world root, one canonical-JSON document:
`{connectors: {id: spec}, runs: [...], schemas: [...], alerts: [...]}`.
Seeded by the world builder (theme `write_ingest` hook) and canonicalized with
the world package's `json_canon`; gateway writes use its own `canonical`,
byte-identical for the ASCII-only state this contract allows. The world
fingerprint includes an `ingest` section only when this file exists, so worlds
without a control plane keep their recorded semantic hashes.

## Audit compatibility

All calls go through the same `Gateway._call` path as every other tool, so
action-log rows are byte-identical in shape (`operation_record` keys
unchanged). Reads target `ingest[:connector_id]`; `ingest_trigger` is
`op_class: exec`, `ingest_schema_update` is `op_class: write`.
