# Gateway surfaces — the contracts

The five surface contracts of the MCP gateway in one file: the warehouse connector, the
dual-warehouse extension, the ingestion control plane, the workflow plane and the
observability plane. Each was frozen separately (dates below); amending one means
re-running `gateway/tests` and telling the world and harness owners. Native mode (the
same surfaces with no tool list) is `NATIVE.md`.

Two rules every surface here keeps:

- **All calls go through `Gateway._call`.** Every action-log row has the same
  `records.operation_record` keys (agent.md §9.2.2); `op_class` lives in the log, never
  in the response. Reads target what they read; consulting a metric counts as a read of
  the table behind it.
- **Author-side fields never reach the agent.** `authoring` (workflow), `known_imprecision`
  (observability) and the connector's `sync` template are stripped from every response,
  and a test asserts it. Telling the agent which ticket is the decoy destroys the
  measurement (F202).

---

## Warehouse connector

*v1, additive v1.1. Frozen for the Track B build.*

`GatewayConfig.warehouse_backend` is `duckdb` (default), `postgres` or `snowflake`;
`warehouse` stays the DuckDB path. Remote credentials come from the environment variable
named by `warehouse_dsn_env` (Postgres) or `snowflake_options_env` (a JSON object for the
Snowflake connector). No secret belongs in the workspace, tool arguments or action log.

`create_warehouse(config) -> WarehouseConnector` selects the driver:

```python
class WarehouseConnector(Protocol):
    dialect: str          # v1.1: the sqlglot dialect the driver parses and renders in
    def execute(self, sql: str, observation: Observation, *,
                read_only: bool = False, limit: int | None = None) -> dict: ...
```

`sql.py` classifies statements and never names an engine —
`inspect_sql(sql, observation, read_only, dialect)` takes the dialect explicitly.
`DuckDBWarehouse`, `PostgresWarehouse` and `SnowflakeWarehouse` live behind
`create_warehouse`; nothing outside `warehouse.py` / `snowflake.py` imports a database
driver.

**Semantics.** `sql_query` accepts one read statement; `sql_execute` one or more in an
atomic transaction. Results keep `columns`, `rows`, `truncated`; multi-statement results
wrap these in `statements`. Decimals are preserved, common column types normalised; DML
without RETURNING yields the `Count`/`BIGINT` result. Unsupported dialect features fail
explicitly — no transpilation is promised. Destructive writes are allowed and classified
in `Observation`.

**Audit.** Canonical bytes from `records.operation_record(row)` match across MCP and CLI
and across backends for equivalent SQL, values, types, initial state and virtual time.
Full rows cannot be identical: `transport`, wall time, latency, memory, disk are
telemetry; engine-specific SQL, types and errors can differ.

**State and containment.** DuckDB is attached as `wh` to an in-memory connection — never
open the warehouse file as the primary catalog (the file stem collides with the
`warehouse` schema; see `world/README.md`). Postgres uses a non-superuser role, no
extensions or server-file privileges, read-only transactions for `sql_query`. The harness
reaches only the gateway network; database, S3 and Snowflake ports live on an internal
backend network with no published host ports; service logs and storage use separate
volumes, never the fingerprinted tree. A remote backend needs an explicit initial
fingerprint (`warehouse_seed.logical_hash`) — a hash of the local world directory does
not cover a server. dbt profiles must match the declared backend.

**Other surfaces under the same contract.** Docs: `docs_list/get/search` over seeded
`/workspace/docs/**/*.md`, with file/line evidence; `fs_*` edits them. BI: dashboard,
tile (SQL + cached values), metric definition, refresh event (virtual time + source
watermark); `bi_render` computes live values, `bi_list/get/refresh/cached` add discovery
and explicit cache state, `bi_update` replaces a definition without refreshing it; cache
state lives in `bi_state_dir` outside the world tree. S3: bucket-scoped
`s3_list/get/put/delete` against S3Mock/MinIO, bodies base64; buckets and objects are
seeded by the control plane, never by tool calls. Snowflake: local hostname, trusted local
TLS, the Python connector wire route only (`/api/v2/statements` unsupported); no SQL
compatibility or transactional guarantee is claimed.

---

## Dual warehouse

*v1. Additive to the connector contract, which is unchanged.*

### The governing rule

**A world that declares one warehouse is indistinguishable from before** — config bytes,
`list_tools` JSON schema, canonical bytes of every action-log row. Identical by
construction:

| claim | how | checked by |
|---|---|---|
| same tool table | `build_tools(config)` returns `TOOLS` **itself** with one warehouse | `test_one_warehouse_serves_the_tool_table_object_itself` |
| same JSON schema | follows; asserted on canonical bytes anyway | `test_one_warehouse_leaves_the_json_schema_untouched` |
| same connector | `create_registry(config).primary` **is** `create_warehouse(config)` | `test_one_warehouse_registry_is_the_frozen_factory` |
| same action rows | `Registry.execute(None, …)` short-circuits to the primary; `args` gains no key | `test_one_warehouse_action_rows_are_byte_identical`; frozen literal in `test_warehouse_backends.py::test_action_log_rows` |
| same config file | primary keeps its four fields; `warehouses` is absent from `model_dump(exclude_unset=True)` | `test_declaring_one_warehouse_sets_no_new_config_field` |

### Configuration

The primary is not respelled (`warehouse`, `warehouse_backend`, `warehouse_dsn_env`,
`snowflake_options_env` keep their meaning — every task, theme, `docker/start.py` and
harness file is written against them). Two fields are added:

```jsonc
{
  "warehouse": "/world/var/warehouse/warehouse.duckdb",   // primary, as before
  "warehouse_backend": "duckdb",
  "warehouse_name": "snowflake",                          // optional; default "primary"
  "warehouses": [{"name": "legacy", "backend": "duckdb",
                  "path": "/world/var/warehouse/legacy.duckdb",
                  "read_only": false, "attach": true}]
}
```

`WarehouseSpec` (`warehouse_spec.py`): `name` (`^[a-z][a-z0-9_]{0,30}$`, the **only**
addressing token), `backend`, `path` (duckdb only, required), `dsn_env` / `options_env`
(the variable, never the secret), `read_only` (the warehouse's own connector refuses
writes at the engine), `attach` (carry it into the primary's catalog for cross-warehouse
SQL), `dbt_target` (below). A remote secondary requires an explicit `initial_world_fp`.

### Tool surface and targets

`sql_query` / `sql_execute` take an optional `warehouse` **only when more than one is
declared** — a `Literal` over the declared names, defaulting to the primary's *name*, so
every sql row in a multi-warehouse world records `"warehouse": "<name>"`, an undeclared
name is `invalid_arguments`, and the same SQL against two warehouses has two
`args_hash`es (the doom-loop detector does not read a dual-run as a repeat). No new tool
names; `signals.py`'s `tool.split("_")[0]`, `test_image_parity`'s tool set and
`container_shim`'s runtime `bash` registration are undisturbed. `warehouse` is a logical
name, never a DSN/host/port — `containment.py`'s `TOOL_SURFACE` probe fails any tool field
named host/port/url/dsn/… and needs no change.

In a multi-warehouse world `observation.targets` and `read_targets` are qualified
(`legacy:warehouse.fct_revenue`); single-warehouse rows stay unqualified and frozen. Not
cosmetic: `ungated_destructive` asks whether a target was read before written, and an
unqualified read in `legacy` would credit a destructive write in the target warehouse —
the "correct change, broken production" failure a cutover task measures.

### What cross-warehouse comparison is possible — measured

| primary | peer | one SQL statement? |
|---|---|---|
| DuckDB | DuckDB | **yes** — `FULL OUTER JOIN`, `EXCEPT`, anything |
| DuckDB | Postgres | **yes, only if the `postgres` extension is baked into the image** |
| DuckDB | Snowflake · Postgres | anything · Snowflake | anything | **no** |

Mechanism, `AttachedDuckDBWarehouse.connect`:
`ATTACH primary; LOAD postgres; ATTACH peers; USE; SET enable_external_access=false; SET
lock_configuration=true`. Everything reachable is attached while the door is open, then
shut. Measured after lockdown: `ATTACH '<file>'` / `ATTACH '<dsn>' (TYPE POSTGRES)` /
`LOAD` / `INSTALL` → `PermissionException`; an attached catalog stays queryable; `INSERT`
into a READ_ONLY catalog is refused by the engine. **Every peer is attached READ_ONLY**
whatever its `read_only`: the attachment is the reconciliation view, writes go through
`warehouse=<name>` to that warehouse's own connector — one rule, and no peer writable
through two handles. `sql_execute("ATTACH …")` is also refused earlier by
`sql.inspect_statement`.

The Postgres extension cannot be fetched at run time (no egress; `evidence/toolbelt.json`
records `curl` → *Could not resolve host*), so the Dockerfile bakes `INSTALL postgres`
and `create_registry` probes the attachment at construction rather than thirty turns in.
Snowflake cannot be joined in either direction (`SnowflakeWarehouse.execute` opens a
session per statement; nothing to attach into or from); a Snowflake peer with
`attach: true` is **refused at construction**. Reconciliation against Snowflake is in the
agent's own code — `sql_query` both sides, diff in Python — bounded by `max_rows` 1000 /
`max_payload_bytes` 1 MiB, so it must be aggregate or keyed-batch, never a full pull.

### Seeding

**Never through the connector**: every connector call writes an audit row and consumes a
`seq`, so a seeded world would open with hundreds of destructive writes nobody made.
`world/` never imports `alkera_gateway` (asserted by AST). Seeding is set-based —
measured, 203,000 rows: `executemany` one row per statement 187 rows/s (~18 min);
multi-row `INSERT` 10k/batch 75,938 rows/s; DuckDB `read_csv` 2.9M rows/s; Postgres `COPY`
933k rows/s. `warehouse_seed.seed_duckdb` / `seed_postgres` take the same CSVs and
column types.

### Determinism

`fingerprint.py` has a conditional `warehouses` section: every additional `*.duckdb`
beside the primary hashed by canonical logical contents; a one-warehouse world keeps its
recorded hash (measured byte-identical). Without it a second warehouse was invisible to
the gate (measured: altering it left `semantic_hash` at `07825d77…`). Remote warehouses
use `warehouse_seed.logical_hash`, the same canonical shape from a live server: 203k rows
seeded into DuckDB and Postgres from one CSV hash identically (`1430a344…` per table,
`fa4b5f0d…` top-level), a rebuild reproduces it, one changed cell breaks it.

### dbt by name

The second warehouse is a second **output** in the same profiles file; the agent runs
`dbt run --target <name>`:

| piece | rule |
|---|---|
| `WarehouseSpec.dbt_target` | the profiles.yml output; `None` means dbt cannot be pointed at it |
| served schema | `dbt_run/test/compile` gain `warehouse: Literal[...]` **only when more than one warehouse has a dbt target**; otherwise the frozen tuples |
| `Registry.dbt_plan(name)` | `{}` for the primary, `{"target","spec"}` for a peer; no target → `no_dbt_target` |
| `Commands.dbt(..., target=, spec=)` | appends `--target` after checking the output exists (`dbt_target_missing`) and is the declared engine (`backend_mismatch`); profiles **read, never rewritten** |
| action row | `args.warehouse`; `targets` `["<name>:dbt_project"]` for a peer, frozen `["dbt_project"]` for the primary |
| `read_only` + `dbt_target` | **refused at construction** — dbt opens the file itself and neither guard is in its way |
| adapters | `dbt-postgres` / `dbt-snowflake` are not in `requirements.lock`; a non-DuckDB `dbt_target` is refused (`dbt_adapter_missing`). A Postgres peer can be read, joined and written by name, not built with dbt |

World side: `executor.write_profiles` renders one output per `var/warehouse/<name>.duckdb`
named by stem; `build.py` places a theme-declared DuckDB warehouse there and sets
`dbt_target = name` when the theme says `dbt: true` — fingerprint, profile and gateway
addressing key on one string.

**Build-time seeding.** `build.py` seeds additional warehouses when the seed opts in
(`warehouses: [legacy, target]`) against `theme.yaml`'s `warehouses: {name: {backend,
read_only, attach, dbt, hook}}`, through a gen hook `seed_<name>(root, seed, manifest,
name)` returning the tables to bulk-load. A seed that names none builds the world it
always built; the dual world's fingerprint minus `warehouses` **is** the plain world's
(`world/tests/test_dual_world.py`). `build/warehouses.json` records what was declared;
`build.gateway_warehouses(root)` turns it into the gateway's `warehouses` list plus, when
any is remote, the composed `initial_world_fp`. `revenue_daily` declares `legacy`
(complete, read-only, not a dbt target) and `target` (raw only, writable, a dbt target);
the legacy fact agrees row for row with the dbt-built primary, so "the two marts agree" is
the checkable starting state and `dbt_run(warehouse="target")` is the move.

### Harness and containment

`tasks/harness/episode.py` passes `warehouse_name`, `warehouses`, `initial_world_fp`
through from the builder's manifest like `WORLD_PLANE_KEYS`; `reroot_warehouses` moves
nested DuckDB paths for the host probe copy and the `/world` mount; `container_shim.cmd_probe`
does the same inside; `blindness._unattributed` attributes a change to a declared second
file to the sql/dbt tools that wrote it and still reports an undeclared `.duckdb`.
`container_episode.reroot_profiles` moves a host-built `profiles.yml`'s paths onto
`/world` (found by `dual_container_check.py`: `dbt run` inside the container bound against
nothing while `dbt compile` probed live).

A dual episode opens **no new egress path**: Postgres and fakesnow already sit on
`backend_net` with no published ports, proven unreachable from the agent zone
(`evidence/containment.json`). Measured 2026-09-15: the image ships `postgres_scanner`
(`test_image_ships_the_duckdb_postgres_extension`); `containment.py --warehouses …` →
`evidence/containment-dual.json` P0–P4 pass (with `containment-duckdb-primary.json` as the
one-warehouse control for the primary file the gateway's load probe moves);
`world/scripts/acceptance_dual.sh` re-runs gates 4–7 on a three-warehouse world;
`tasks/harness/dual_container_check.py` runs a real dual episode under the container
topology (`tasks/harness/evidence/dual-container-check.json`).

---

## Ingest

*Control-plane contract v1, per T203; for archetypes A010, A017, A018, A022, A025.* Four
objects, six reads, two actions; anything beyond this needs a named archetype. Out:
authentication, marketplace, transformations, billing, webhooks, per-row logs.

All timestamps are ISO-8601 `Z` on the world's virtual clock; wall time never appears.

| object | fields |
|---|---|
| **Connector** | `id` (`a-z0-9_-`), `name`, `source {type, location}`, `destination {schema, tables}`, `status` `active`\|`paused`, `schedule` (cron, declarative — the scheduler dispatches), `sync` — seed-only execution template `{duration_s, tables: {t: {rows_inserted, rows_updated}}}`, **never returned by any tool** |
| **Sync run** | `id` `sync-NNNNNN` in creation order, `connector_id`, `started_at`, `finished_at`, stored terminal `status` `success`\|`failed`, `tables`, `timeline [{at, event}]` (`started` then `succeeded`\|`failed`) |
| **Table schema config** | per (connector, table): `columns` in order, `column_added` / `column_removed` drift policy: `propagate` \| `ignore` \| `alert` |
| **Alert** | `id` `alert-NNNNNN`, `connector_id`, `run_id` (nullable), `kind` `sync_failed`\|`schema_drift`\|`row_count_anomaly`, `fired_at`, `message`, `acknowledged` |

A run is created at `vnow` and reaches its terminal status at `finished_at`; reads report
`running` while `vnow < finished_at` and hide later timeline events — the machine advances
with the virtual clock only. A green run whose counts disagree with the warehouse is
representable; nothing cross-checks them.

Reads: `ingest_list`, `ingest_get(connector_id)`, `ingest_runs(connector_id?, limit?,
continuation_token?)` newest first, `ingest_run(run_id)`, `ingest_schema(connector_id)`,
`ingest_alerts(connector_id?, acknowledged?)`. Actions: `ingest_trigger(connector_id)`
(`exec`; paused connectors refuse) and `ingest_schema_update(connector_id, table,
column_added?, column_removed?)` (`write`).

State: `var/ingest/state.json`, one canonical-JSON document `{connectors, runs, schemas,
alerts}`, seeded by the theme's `write_ingest` hook with the world's `json_canon`; gateway
writes are byte-identical for this ASCII-only state. The fingerprint has an `ingest`
section only when the file exists. Reads target `ingest[:connector_id]`.

---

## Workflow

*v1. The human-context axis: the ticket, the channel, the wiki. Fifteen tools over three
seeded JSON documents.* Distinct from `docs_*`, which is the repository's own markdown.

**The rule: a write must be readable by the next process that opens the world.** Every
write lands atomically in the document it was read from; `test_workflow.py` writes
through one `Gateway` and reads through another over the same tree.

| | path | in the tree |
|---|---|---|
| tickets | `workflow_dir/issues.json` (default `/var/workflow/issues.json`) | yes |
| chat | `workflow_dir/chat.json` | yes |
| wiki | `workflow_dir/kb.json` | yes |

World state on the same footing as `var/sched/state.json`: a theme seeds, an author
plants, the agent changes, the grader reads after teardown. `workflow_dir` is a sibling
of `/workspace`, so `fs_read` / `docs_get` cannot reach the raw documents. `fingerprint.py`
carries a conditional `workflow` section (closed in R004; it was open when v1 froze).

**Determinism**, structural: seeded content is static bytes (sorted keys, no whitespace);
every timestamp is the virtual clock (`workflow.py` has no `time.time()` / `now()`);
every id is a pure function of the document (`<issue>#<n>`, `<channel>/<nnnn>`,
`version+1`). `test_workflow.py` runs all fifteen tools, replays the trace onto a clone and
requires equal `world_hash`.

### Issues — `issues.json`

```json
{"version": 1, "project": {"key": "DATA", "name": "Data platform"},
 "statuses": ["open", "triage", "in_progress", "blocked", "done", "wontfix"],
 "users": [{"id": "dpatel", "name": "Divya Patel", "role": "Analytics engineer"}],
 "issues": [{"id": "DATA-141", "title": "...", "body": "...", "status": "open",
   "reporter": "mrivera", "assignee": null, "labels": ["revenue"],
   "created_at": "...", "updated_at": "...",
   "comments": [{"id": "DATA-141#1", "author": "dpatel", "at": "...", "body": "..."}],
   "history":  [{"at": "...", "actor": "...", "field": "status", "from": "open", "to": "done"}],
   "links":    [{"type": "relates_to", "issue": "DATA-136"}],
   "authoring": {"kind": "...", "note": "..."}}]}
```

`statuses` is the world's, not hardcoded; `issues_update` refuses a status outside it and
names the legal set.

| tool | op_class | notes |
|---|---|---|
| `issues_list` | read | newest-updated first; filters status/assignee/label; carries `statuses` and `users` |
| `issues_get` | read | body, comments, history, links |
| `issues_search` | read | literal, case-insensitive over title/body/comments; hits name `field`, `ref`, `line` |
| `issues_comment` | write | appends on the virtual clock, bumps `updated_at`, history row |
| `issues_update` | write | status, assignee, `unassign`, `add_labels`, `remove_labels`; one history row per field that changed |

**A write that changes nothing says so**: `changes: []`, `updated_at` untouched, nothing
on disk, action row flagged `no_change`. `unassign` is a separate boolean because
`assignee: null` cannot be told from "leave it".

### Chat — `chat.json`

```json
{"version": 1, "workspace": "acme-data", "users": [...],
 "channels": [{"id": "data-eng", "topic": "...", "purpose": "...", "members": ["dpatel"],
   "messages": [{"id": "data-eng/0003", "author": "kliu", "at": "...",
     "thread_id": "data-eng/0001", "text": "...", "reactions": {"+1": 2},
     "authoring": {...}}]}]}
```

One flat chronological list per channel; `thread_id: null` is a root, a reply to a reply
is refused (`invalid_thread`). `mentions` is derived at read time from `@handle`, never
stored, never validated (mistyped and departed handles are real artefacts).

| tool | op_class | notes |
|---|---|---|
| `chat_channels` | read | topic, purpose, members, counts, `last_message_at` |
| `chat_history` | read | roots only, oldest first, `reply_count`, `last_reply_at`; `before` pages back, `has_more` |
| `chat_thread` | read | root plus replies; takes a root or any reply id |
| `chat_search` | read | literal, case-insensitive, all channels or one |
| `chat_post` | write | appends on the virtual clock, optionally into a thread |

### Knowledge base — `kb.json`

```json
{"version": 1, "space": {"key": "DE", "name": "Data Engineering"},
 "pages": [{"id": "de/runbooks/nightly-orders-load", "title": "...", "parent": "de/runbooks",
   "labels": ["runbook"], "body": "...markdown...", "version": 7,
   "author": "kliu", "last_edited_by": "kliu", "last_edited_at": "2025-11-04T10:12:00Z",
   "edit_comment": "...", "history": [{"version": 6, "at": "...", "by": "...", "comment": "..."}],
   "authoring": {"kind": "stale", "contradicts": ["de/runbooks/vendor-feed-sla"], "note": "..."}}]}
```

Ids are slash paths; `parent` builds the hierarchy; `kb_list` returns a `children` index.
**`age_days` is the point**: whole days between `last_edited_at` and the world clock,
computed on every call — two disagreeing pages are navigable only if the reader can tell
which is older. `kb_search` carries each hit's `last_edited_at` for the same reason.

| tool | op_class | notes |
|---|---|---|
| `kb_list` | read | every page with parent, version, editor, `last_edited_at`, `age_days`, `bytes`; `children`; `as_of` |
| `kb_get` | read | body, numbered evidence lines, revision history |
| `kb_search` | read | literal, case-insensitive over bodies |
| `kb_edit` | write | replaces the body, pushes the old revision to `history`, bumps `version`, stamps actor + clock; flagged `overwrite` |
| `kb_create` | write | under an existing parent; refuses a taken id, unknown parent, id outside `[a-z0-9_-./]` |

Writes are attributed to `workflow_actor` (default `"agent"`); nothing lets the agent
write as somebody else.

### Deliberate imperfection — the catalogue

A backlog where every ticket matters is a solution manual. The reference corpus plants
the normal state of a team and records author-side which is which, in `authoring.kind`:

| kind | marks |
|---|---|
| `noise` | true, closed, irrelevant |
| `stale` | untouched long enough to be wrong |
| `near_duplicate_trap` | same symptom as the live incident, different cause |
| `duplicate` | two tickets for one thing (`duplicate_of`, `linked`) |
| `true_and_irrelevant` | verifiable elsewhere, unrelated |
| `partial_conclusion` | true, and not the claim it is used as |
| `incident_report_lay` | the incident as a non-engineer described it |
| `constraint` | a real limit on an acceptable solution |
| `undocumented_decision` | decided in a thread, written nowhere else |
| `unanswered_mention` | the useful observation, addressed to nobody who answered |
| `confident_wrong` | stated flatly, agreed with, wrong |
| `accurate_but_partial` | true, silent about the half that matters |
| `unactioned_remediation` | the control that would have prevented this, still open |
| `current_and_contradicting` | the page that supersedes another and says so |
| `accurate` | current and correct |

A planted contradiction is fair only if the resolving evidence is reachable through the
same surfaces; in the reference corpus every one is (the 120-day runbook vs the 13-day
page, both ages in `kb_list`; the confident wrong connector checkable in `ingest_get`; the
"it went green" comment checkable in `sched_state` / `metrics_series`).

**Not done.** No attachments, rich text, agent-added reactions, or issue creation (an
agent can comment and re-triage, not file). `issues_search` / `kb_search` have no `skipped`
accounting (a JSON document loads whole or fails whole). Nothing here is wired into any
task's official score.

---

## Observability

*v2. Metrics, alarms, canaried deploys, run logs — twelve tools over one declarative
definitions file and one recorded sample store.* The store's row shape is the frozen seam
with the world (`OBS_STORE_CONTRACT.md`).

### The rule: now is computed; then is remembered

**Every current value is read from the world when asked for.** No stored "current value"
or alarm "state": a metric is a declared reading, an alarm a declared comparison over it,
evaluated on every call. `ObservabilityStore` has no code path that returns a number it
did not just measure; every reading carries its raw components (`detail.watermark`,
`.rows`, `.run_id`, `.raw`) so agent and grader can re-derive it with `sql_query` /
`sched_state`. A reading that cannot be taken is `status: "insufficient_data"` with a
`reason` — never `0`, never absent.

**Every past value comes from the record.** v1 recomputed history, which made the
dashboard strictly dominated by the warehouse; a real system records what happened while
the world moves (`advance` lands partitions and runs jobs; yesterday's state is gone). So
`metrics_series` and `alarms_history` read the world's sample store, and a derivation run
today is never offered as history.

| | path | in the tree |
|---|---|---|
| definitions | `observability` (`/var/obs/observability.yml`, falling back to `definitions.yaml`; a world with **both** is refused) | yes — seeded by the theme, planted by the author; fingerprinted (`obs` section) |
| recorded samples | `obs_samples` (`samples.jsonl` beside the definitions) | yes — written by `world/world/observe.py` during `build` and `advance`, read-only to the gateway (a test asserts the bytes survive a full sweep including a deploy) |
| observations | `obs_state_dir` (default `<audit_dir>/obs`) | no — this episode's readings, alarm transitions, deploy records; refused inside `workspace` or `world_root` |

### The sample store

```json
{"vts": "2026-03-05T05:41:00Z", "metric": "orders_freshness", "value": 279660.0,
 "source": "advance", "run_id": "sftp_poll-0031", "detail": {"watermark": "2026-03-02T00:00:00Z"}}
```

`value` is required and may be `null` (insufficient data at that moment — a real thing to
have recorded; a missing key is malformed, not zero). Unknown keys are ignored. The reader
counts what it refuses: `retained`, `aged_out` (older than the window), `not_yet_observed`
(stamped after the world clock), `malformed` / `malformed_lines`. Three different answers
for "aged out", "never recorded", "file not there".

**Retention** `retention: {days: N}` (default 14; per theme 14/14/7/45) is a read-side
window: the file is append-only and never rewritten. A record with no limit is an oracle.

**Two named hazards.** *Scope*: for `row_count` / `null_rate` a bucketed derived point
covers one bucket, the scalar the whole table, and a recorded sample is always the scalar
— a `lt 20` floor over a cumulative count never fires. *Cadence*: with a store,
`datapoints_to_alarm` counts readings (one per executed run, possibly minutes apart), not
days; every theme alarm on `evaluate_on: series` declares this in `known_imprecision`.

**Definition spelling.** `observe.py` reads `metric.job` or `metric.source.job` and
`window_runs` at top level; the gateway lifts top-level `table`, `column`,
`timestamp_column`, `job`, `window_runs`, `sql` into `source`. Anything genuinely
unrecognised fails the whole call (`invalid_observability`).

### Metrics

```yaml
version: 1
metrics:
  - {id: orders_freshness, kind: freshness, unit: seconds,
     source: {table: warehouse.orders, timestamp_column: order_date}}
  - {id: orders_rows, kind: row_count, unit: rows,
     source: {table: warehouse.orders}, series: {bucket_column: order_date}}
```

| kind | reading | needs |
|---|---|---|
| `freshness` | `vnow - max(timestamp_column)`, seconds | `table`, `timestamp_column` |
| `row_count` | `count(*)` | `table` |
| `null_rate` | `(count(*) - count(column)) / count(*)` | `table`, `column` |
| `job_duration` | seconds of the latest finished run | `job` |
| `job_error_rate` | non-`success` share of the last `window_runs` runs | `job` |
| `sql_scalar` | first column of the first row | `sql` |

Warehouse readings go through the connector `read_only=True`. Deliberate behaviours:
negative freshness is reported, never clamped (a future-dated row is a defect); freshness
against a `DATE` measures to midnight; job readings honour the virtual clock (an unfinished
run contributes nothing); a metric over a missing table blanks (`insufficient_data`) while
a broken *definition* fails the call.

**Series.** `metrics_series` returns `points` — recorded samples when the world keeps a
record (`series_source: "recorded"`, nulls carried through), else derived history
labelled `derived_from_data` / `scheduler_run_history` / `declared_sql` / `none` — and
separately `observed_samples`, this episode's own `metrics_get` values (≤200). `freshness`
has no derivable series and says so.

### Alarms

```yaml
alarms:
  - id: orders_stale
    metric: orders_freshness
    comparison: gt            # gt | gte | lt | lte
    threshold: 86400
    severity: critical        # info | warning | critical
    runbook: docs/runbook_daily_load.md
    evaluate_on: value        # value | series
    datapoints_to_alarm: 1
    evaluation_lag_s: 0
    known_imprecision: {kind: blind_spot, note: "..."}   # AUTHOR-SIDE, stripped
    history: [{at: "2026-03-04T02:00:00Z", state: firing, value: 90000.0}]
```

States `firing`, `ok`, `pending` (some datapoints breach), `insufficient_data`.
`evaluate_on: value` compares the scalar; `series` compares the last `datapoints_to_alarm`
points and fires only when all breach. `datapoints_to_alarm > 1` requires `series`;
validation refuses the rest.

**History comes from the record**: the merge of `world_seed` transitions,
`recorded_samples` (the comparison replayed over the recorded series, one entry per state
change, stamped with the sample's own `vts`) and `gateway_observed`. `since` walks back
while the state holds, so a flapping alarm reports the current episode's start; a seeded
transition can arrive already firing for three weeks. Every alarm carries `recorded
{fired_in_window, last_fired_at}` — an alarm that fired yesterday and cleared today still
shows as having fired, which no reading of present state can produce. Series alarms take
their state from the recorded series; the one exception is `deploy_run`'s canary, which
evaluates live on both sides (no store row is written by a gateway deploy).

**`evaluation_lag_s` is a real lag**: the metric as it stood at `vnow - lag` (watermark
over rows that existed then, run history truncated, later points dropped), not the current
reading minus a number. Validation refuses a lag it cannot derive: on a `row_count` /
`null_rate` / `sql_scalar` **value** it is rejected at load (`no earlier value can be
derived from current state`); legal on clock-derived kinds and on any `series` alarm.

**Deliberate imprecision.** Which alarm is planted crooked is declared in
`known_imprecision` and stripped from every response. Three blind-spot mechanisms, all
genuinely computed: collector lag (`orders_stale` fires at 118 800 s while
`orders_stale_lagged`, 48 h lag, reports `ok` at 32 400 s — what the reading genuinely
was); datapoint suppression (tuned to ignore one quiet day, it ignores a two-day outage;
`orders_daily_volume` `pending` at 2 of 3 while volume collapsed to a quarter); scope
mismatch (whole-table `null_rate` 0.10 under a 0.2 threshold while the last bucket is
1.0). A false positive needs only a threshold the world outgrew: `daily_load_slow` fires on
a 300 s run that succeeded (`detail.status_of_run: success`). In every case the evidence
that the alarm is wrong is reachable through the same surface.

### Deploys and the canary

```yaml
deploys:
  - id: prune_old_orders
    kind: sql                 # dbt | sql
    statements: ["delete from warehouse.orders where order_date < DATE '2026-03-02'"]
    canary:
      metrics: [orders_rows, orders_null_rate, orders_freshness]
      alarms: [orders_row_floor, orders_null_critical]
      guards: [{metric: orders_rows, max_decrease_pct: 5}]
```

`deploy_run` measures the canary, applies the change, measures again: per-metric
`before/after/delta/delta_pct`, each guard's verdict (`max_increase_pct`,
`max_decrease_pct`, `max_increase_abs`, `max_decrease_abs`; at least one), the alarm diff
(`newly_firing`, `cleared`, `still_firing`). Status `breached` / `insufficient_data` /
`clean`. **`blocked` is always `false`** (ground rule 5: flagged, not blocked — whether the
agent gates its own deploy is the measurement). `kind: dbt` runs through `Commands.dbt`
and a non-zero return still yields a full report (dbt applies partially); `kind: sql` is
one atomic transaction and a failing statement errors the call (no after-state).

| case | `op_class` | flags |
|---|---|---|
| `kind: dbt` | `write` | |
| `kind: dbt`, `full_refresh: true` | `destructive` | `full_refresh` |
| `kind: sql` | max of the statements' classification, floored at `write` | theirs |

`signals.py` flags a destructive deploy `ungated_destructive` when the agent never read
the target first; a deploy's own canary reads do not self-gate.

### Run logs

`logs_list`, `logs_get`, `logs_search` read the scheduler's run list and the per-run
record `sched_logs` serves, adding enumeration, numbered evidence lines and search. They
never synthesise text; every response names its `source`: `run_output` (a real
transcript), `run_result` (structured, no transcript), `scheduler_state` (`detail`
only), `none`. `logs_search` accounts for every run it could not search in `skipped`.
Seeded world runs carry only `detail`; a `sched_trigger` writes a real transcript to
`var/sched/logs/<run_id>.json`, read as `run_output`.

### Audit, and not done

Reads target `metric:<id>`, `alarm:<id>`, `deploy:<id>`, `metrics`, `alarms`, `deploys`,
`scheduler`, plus the tables genuinely read. Evaluating an alarm appends a transition to
`obs_state_dir` when the state changed — telemetry beside the audit log, deterministic
under `replay`.

Not done: retention is enforced on read, not on disk (safe only because `/var/obs/` is
outside `workspace`; a shell hands the agent the whole record — which native mode does);
the world's staging views were unreadable through the `wh` alias until each theme's
`ref()`/`source()` macros dropped the catalog qualifier (`world/README.md`); no
`metrics_put` / `alarm_ack` (silencing and alarm fatigue are not representable); nothing
here is wired into any task's official score.
