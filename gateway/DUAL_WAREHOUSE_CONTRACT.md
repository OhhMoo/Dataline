# Dual-warehouse contract v1

Additive to `CONNECTOR_CONTRACT.md`, which is unchanged. Nothing here alters
`WarehouseConnector`, `create_warehouse(config)`, `sql.py`, or `tools.py`'s
`TOOLS` table; a world that declares one warehouse does not reach any of it.

## The governing rule

**A world that declares one warehouse is indistinguishable from today** — in
the config bytes, in the JSON schema served by `list_tools`, and in the
canonical bytes of every action-log row. Not "equivalent modulo a null field".
Identical, and identical by construction rather than by care:

| claim | how it is guaranteed | where it is checked |
|---|---|---|
| same tool table | `build_tools(config)` **returns `TOOLS` itself** when one warehouse is declared — the same object, not a copy | `test_one_warehouse_serves_the_tool_table_object_itself` |
| same JSON schema | follows from the above; asserted on canonical bytes anyway | `test_one_warehouse_leaves_the_json_schema_untouched` |
| same connector | `create_registry(config).primary` **is** `create_warehouse(config)` | `test_one_warehouse_registry_is_the_frozen_factory` |
| same action rows | `Registry.execute(None, ...)` short-circuits to the primary with the arguments `Operations` passes today; `args` gains no key | `test_one_warehouse_action_rows_are_byte_identical`, and the frozen literal in `test_warehouse_backends.py::test_action_log_rows` |
| same config file | the primary keeps its four existing fields; `warehouses` is *additional* and absent from `model_dump(exclude_unset=True)` | `test_declaring_one_warehouse_sets_no_new_config_field` |

## Configuration

The primary warehouse is **not respelled**. `warehouse`, `warehouse_backend`,
`warehouse_dsn_env` and `snowflake_options_env` keep their exact meaning,
because every existing task, all four themes, `gateway/docker/start.py`,
`tasks/harness/episode.py` and `tasks/harness/container_episode.py` are written
against those names. Two fields are added:

```jsonc
{
  "warehouse": "/world/var/warehouse/warehouse.duckdb",   // the primary, as today
  "warehouse_backend": "duckdb",                          // the primary, as today
  "warehouse_name": "snowflake",                          // optional; default "primary"
  "warehouses": [                                         // the ADDITIONAL warehouses
    {"name": "legacy", "backend": "duckdb",
     "path": "/world/var/warehouse/legacy.duckdb",
     "read_only": false, "attach": true}
  ]
}
```

`WarehouseSpec` fields (`warehouse_spec.py`):

| field | meaning |
|---|---|
| `name` | `^[a-z][a-z0-9_]{0,30}$`. The **only** addressing token. |
| `backend` | `duckdb` \| `postgres` \| `snowflake` |
| `path` | duckdb only, and required for duckdb; forbidden otherwise |
| `dsn_env` / `options_env` | the environment variable holding the secret, never the secret |
| `read_only` | the warehouse's own connector refuses writes, at the engine |
| `attach` | carry this warehouse into the primary's catalog for cross-warehouse SQL |

A remote secondary requires an explicit `initial_world_fp`, the same rule the
primary already lives under — the gateway does not pretend a hash of the local
world tree covers a server. `world/world/warehouse_seed.logical_hash` produces
that fingerprint; see below.

## Tool surface

`sql_query` and `sql_execute` take an optional `warehouse`. **The field exists
only when the episode declares more than one warehouse.** With one warehouse,
`build_tools` returns the untouched `TOOLS`, so the field is not in the schema,
not in the description, and not in `model_dump()` — which is what keeps the
action log byte-identical.

With two or more warehouses the field is a `Literal` over the declared names,
defaulting to the primary. So:

* the agent is shown exactly which warehouses exist, as a schema `enum`;
* an undeclared name is rejected as `invalid_arguments` before anything runs;
* **every** sql row in a multi-warehouse world records `"warehouse": "<name>"`,
  because the default is the primary's *name*, not `null`. The grader can read
  a dual-run off the action log without reconstructing intent from SQL text;
* two calls with the same SQL against different warehouses have different
  `args_hash`, so `process_signals`'s doom-loop detector does not read a
  dual-run as a repeat.

No new tool names. `signals.py` derives a surface from `tool.split("_")[0]`,
`test_image_parity` compares the tool *set*, and `container_shim` registers
`bash` into `TOOLS` at runtime — all three are undisturbed.

`warehouse` is a **logical name**, never a DSN, host or port.
`gateway/docker/containment.py`'s `TOOL_SURFACE` probe fails the run if any tool
field is named host/hostname/port/url/uri/endpoint/dsn/server/address/addr/
proxy/connection/conn/account/network. That probe needs no change and
`no_socket_primitive` stays true.

## Targets

In a multi-warehouse world `observation.targets` and `read_targets` are
qualified: `legacy:warehouse.fct_revenue`. In a single-warehouse world they are
not, because there is nothing to disambiguate and the existing rows are frozen.

This is not cosmetic. `process_signals`'s `ungated_destructive` asks whether a
target was **read** before it was **written**. Unqualified, a read of
`warehouse.fct_revenue` in the legacy warehouse would credit a destructive write
to `warehouse.fct_revenue` in the target warehouse — which is exactly the
"correct change, broken production" failure a cutover task exists to measure.

## What cross-warehouse comparison is actually possible

Measured, not assumed. Every result below was executed.

| primary | peer | in one SQL statement? |
|---|---|---|
| DuckDB | DuckDB | **Yes.** `FULL OUTER JOIN`, `EXCEPT`, anything. |
| DuckDB | Postgres | **Yes, but only if the `postgres` extension is baked into the image.** |
| DuckDB | Snowflake | **No.** |
| Postgres | anything | **No.** |
| Snowflake | anything | **No.** |

The mechanism is attachment order in `AttachedDuckDBWarehouse.connect`:

```
ATTACH primary;  LOAD postgres;  ATTACH peers;  USE;
SET enable_external_access = false;  SET lock_configuration = true;
```

Everything the episode may reach is attached while the door is open, then the
door is shut. Measured consequences:

* `ATTACH '<file>' AS x` after the lockdown → `PermissionException: file system
  operations are disabled by configuration`
* `ATTACH '<dsn>' AS x (TYPE POSTGRES)` after the lockdown → `PermissionException:
  Attaching Postgres databases is disabled through configuration`
* `LOAD postgres` with `enable_external_access=false` → `PermissionException:
  Loading external extensions is disabled through configuration`
* `INSTALL <ext>` with `enable_external_access=false` → `PermissionException:
  Cannot access directory ... file system operations are disabled`
* an already-attached catalog stays queryable after the lockdown
* `INSERT` into a `READ_ONLY`-attached catalog → `Cannot execute statement of
  type "INSERT" on database "legacy" which is attached in read-only mode!`

**Every peer is attached READ_ONLY**, whatever its `read_only` setting. The
attached catalog is the *reconciliation view*; writes go through
`warehouse=<name>`, which reaches that warehouse's own connector. One rule
rather than two, and it avoids a peer writable through two paths — one of which
would open a second handle on the same DuckDB file or a second Postgres
transaction.

So the set of warehouses the agent can reach is exactly the set the world
declared, enforced by the engine. `sql_execute("ATTACH ...")` is additionally
refused one layer earlier, by `sql.inspect_statement`, as outside the warehouse
tool surface.

**The Postgres extension cannot be fetched at run time.** `INSTALL` reaches
`extensions.duckdb.org`; the episode zone has no egress (gate 4;
`gateway/docker/evidence/toolbelt.json` records `curl` → *Could not resolve
host*). It must be baked in at build time. `create_registry` therefore probes
the attachment once at construction and raises a message that says so, rather
than letting the first agent query fail thirty turns into an episode.

**Snowflake cannot be joined to anything, in either direction.**
`SnowflakeWarehouse.execute` opens a session, runs one statement and closes it
(`snowflake.py:34-52`); it has no catalog to attach into and nothing can attach
into it. A world that declares a Snowflake peer with `attach: true` is
**refused at construction** with an error that names the alternative. This is
deliberate: a task whose instruction promises a cross-engine join the stack
cannot perform is unsolvable as written, and it should fail at boot where a task
author sees it.

Reconciliation against Snowflake therefore happens **in the agent's own code**:
`sql_query` both sides, diff in Python. That is a real constraint on task
design, and it bounds row counts — `max_rows` is 1000 and
`max_payload_bytes` is 1 MiB, so a Snowflake-side reconciliation must be
written as aggregate or keyed-batch comparison (row counts, checksums,
group-by-day sums, sampled keys), never a full-table pull. A task that expects
the agent to diff 203k rows through the tool surface is not solvable either.

## Seeding

**World seeding never goes through the connector**, and the reason is not
throughput. Every connector call writes an audit row and consumes a `seq`; a
world seeded through `sql_execute` would be indistinguishable from the agent's
own work in the action log, and every episode would open with several hundred
destructive writes nobody made. `world/` already never imports `alkera_gateway`
(`world/world/gateway_scheduler.py:8`) and `world/world/warehouse_seed.py`
keeps that property — asserted by parsing its own AST.

Seeding must also be **set-based**. Measured on one host, 203,000 rows:

| path | rows/s | time |
|---|---|---|
| `duckdb.executemany`, one row per statement | 187 | ~18 min |
| multi-row `INSERT ... VALUES`, 10k per batch | 75,938 | 2.7 s |
| DuckDB `read_csv` | 2,904,622 | 0.07 s |
| Postgres `COPY ... FROM STDIN` | 933,310 | 0.2 s |

The ~100 rows/s regime is the cost of **one statement per row**, not a property
of any one backend. The Snowflake connector is worse again for a structural
reason: `snowflake.py:36` opens a fresh `snowflake.connector.connect(...)` on
every `execute`, so a row-at-a-time seed pays a TLS handshake and a login
request per row.

`warehouse_seed.seed_duckdb` and `seed_postgres` take the same CSV files and
the same explicit column types, so the two engines are seeded from one source
of truth.

## Determinism

`world/world/fingerprint.py` gains one conditional section, `warehouses`, on the
pattern the `ingest`, `live`, `obs` and `workflow` sections already set: every
additional `*.duckdb` beside the primary is hashed by canonical logical
contents. A world with one warehouse finds nothing and **keeps its recorded
hash** — verified: the one-warehouse `semantic_hash` is byte-identical before
and after the patch.

Without it a second warehouse is **invisible to the determinism gate** —
measured: adding a second warehouse file, and then altering it, left
`semantic_hash` unchanged at `07825d77…`.

A warehouse that does not live in the tree cannot be reached from
`fingerprint.py`. `warehouse_seed.logical_hash` covers it, producing the same
canonical shape from a live server. **Measured**: 203,000 rows seeded from one
CSV into DuckDB and into Postgres produce the identical table hash
`1430a344…` and the identical top-level hash `fa4b5f0d…`; a rebuild reproduces
it; one changed cell breaks it. That equality is what makes "the two marts
agree" a checkable claim rather than the agent's word.

## dbt: building the target by name

`commands.py` runs one dbt invocation against one profile, and dbt itself
selects the warehouse through the profile's `target`. So the second warehouse
is a second **output** in the same profiles file, and the agent chooses it the
way a data engineer does - `dbt run --target <name>`:

| piece | rule |
|---|---|
| `WarehouseSpec.dbt_target` | the profiles.yml output this warehouse is reached through; `None` (the default) means dbt cannot be pointed at it |
| the served schema | `dbt_run` / `dbt_test` / `dbt_compile` gain `warehouse: Literal[...]` **only when more than one warehouse has a dbt target** (the primary always has one: the profile's own default). One warehouse, or one warehouse plus a reconciliation-only peer, keeps today's dbt schema - the same tuples out of `TOOLS` |
| `Registry.dbt_plan(name)` | `{}` for the primary (so `Commands.dbt` gets exactly the arguments it always got), `{"target", "spec"}` for a peer; a declared warehouse without a target is refused by name (`no_dbt_target`) |
| `Commands.dbt(..., target=, spec=)` | appends `--target <output>` after checking the output exists (`dbt_target_missing`) and is the declared engine (`backend_mismatch`). The profiles file is **read, never rewritten**: it belongs to the world |
| the action row | `args` carries `"warehouse": "<name>"` like the sql rows; `targets` is `["<name>:dbt_project"]` for a peer and the frozen `["dbt_project"]` for the primary |
| `read_only` + `dbt_target` | **refused at construction.** dbt opens the file (or the DSN) itself; neither the `ReadOnlyWarehouse` connector nor the READ_ONLY attachment is in its way, so a world that declared both would be wrong about one of them |
| the adapter | `dbt-postgres` and `dbt-snowflake` are **not in `gateway/requirements.lock`**. A `dbt_target` on a non-DuckDB peer is refused at construction with the distribution named (`dbt_adapter_missing`). Until the lock carries the adapter and the image is rebuilt, the only warehouse the agent can *build with dbt* is a DuckDB one; a Postgres target can be **read, joined, and written with SQL by name**, and reconciled, but its marts are built another way |

On the world side the same name is the same file: `world/world/executor.py
write_profiles` renders one output per `var/warehouse/<name>.duckdb` beside the
primary, named by the stem, and `dbt_invoke(..., target=)` / a scheduler job's
`exec: {kind: dbt, target: <name>}` reach it. That is why `build.py` places a
theme-declared DuckDB warehouse at `var/warehouse/<name>.duckdb` and sets
`dbt_target = name` when the theme says `dbt: true` - fingerprint.py's
`warehouses` section, the dbt profile and the gateway's addressing all key on
one string, with no config any of the three could drift from. A world with one
file renders the byte-identical profiles.yml it always rendered.

## Seeding at build time

`world/world/build.py` seeds additional warehouses when the **seed file** opts
in (`warehouses: [legacy, target]`) against warehouses the **theme manifest**
declares (`theme.yaml` `warehouses: {name: {backend, read_only, attach, dbt,
hook}}`), through a gen hook `seed_<name>(root, seed, manifest, name)` that
returns the `tables` mapping `warehouse_seed.seed_duckdb` / `seed_postgres`
bulk-load. Nothing goes through the connector. A seed that names none builds
the world it always built: same sections, and the dual world's fingerprint
minus its `warehouses` section **is** the plain world's fingerprint
(`world/tests/test_dual_world.py`). What was declared is recorded in
`build/warehouses.json`; `build.gateway_warehouses(root)` turns it into the
`warehouses` list a gateway config takes plus, when any of them is remote, the
`initial_world_fp` composed from the tree's semantic hash and each remote
warehouse's `logical_hash`.

`revenue_daily` declares two: `legacy` (the pre-dbt mart, complete, read-only,
not a dbt target) and `target` (raw layer only, writable, a dbt target). The
legacy fact agrees row for row with the dbt-built primary, so "the two marts
agree" is the checkable starting state of a cutover and `dbt_run(warehouse=
"target")` is the move.

## The harness

`tasks/harness/episode.py` passes `warehouse_name`, `warehouses` and
`initial_world_fp` through from the world builder's manifest on the exact
pattern of `WORLD_PLANE_KEYS`: a builder that publishes nothing changes
nothing, `initial_world_fp: null` included. `reroot_warehouses` moves the
nested DuckDB paths for the probe's throwaway copy on the host and for the
`/world` mount in the container (`container_episode.container_world`);
`container_shim.cmd_probe` does the same inside, and keeps a published
fingerprint when a declared warehouse is remote. `blindness._unattributed`
attributes a change to a declared second file to the sql/dbt tools that wrote
it, and still reports an undeclared `.duckdb` that moved.
`world/scripts/dual_world_builder.py` is a builder of that shape.

## Containment

A dual-warehouse episode opens **no new egress path**. Postgres and the
Snowflake fake already sit on `backend_net` in `gateway/docker/compose.yml`,
already have no published host ports, and are already proven unreachable from
the agent zone by `gateway/docker/evidence/containment.json`
(`backends_reachable_from_agent: []`, `agent_resolved: ["episode"]`,
`published_host_ports: {}`). Declaring a second warehouse changes which of those
existing services the *gateway* connects to. It adds no service, no network and
no tool field that names a socket.

Gate 4 (no TCP, no DNS from the episode zone) and gate 7 (the grader is
unreachable) are properties of the compose topology and the `network_mode: none`
grading container. Neither is touched.

One thing that **is** new and must be re-run rather than assumed: the episode
image must now contain the DuckDB `postgres` extension if any world attaches a
Postgres peer, so `gateway/docker/evidence/image.json` has to be regenerated and
`test_image_parity` re-satisfied.

**Measured, 2026-09-15** (the paragraphs above were inference until then):

* `gateway/docker/Dockerfile` bakes `INSTALL postgres` at build time.
  `evidence/image.json` (`alkera-episode:head`, rev `3db7cf0` + pending
  `gateway/docker/**`) records `postgres_scanner` v`41223e5` loaded on a
  connection with autoinstall/autoload off, sha256 `b1ced4cf…`;
  `test_image_ships_the_duckdb_postgres_extension` asserts it.
* `containment.py --backend duckdb --warehouses '[{"name":"pg","backend":
  "postgres"}]' --profile snowflake` → `evidence/containment-dual.json`: P0
  (every declared name answers, the Postgres peer is attached read-only, an
  undeclared `ATTACH` is refused), P1, P2 (all five services egress-blocked,
  the control probe can detect egress), P3 (the *served* tool table has
  `warehouse` on the sql tools and no socket primitive; no backend reachable
  from the agent's vantage), P4 all pass. The one thing the dual stack
  changes in P4 is that a DuckDB primary's own file moves under the gateway's
  load probe - and `evidence/containment-duckdb-primary.json`, a one-
  warehouse DuckDB-primary control, moves the same file for the same reason;
  P4 now records it as the gateway's expected write and fails on anything
  else.
* `world/scripts/acceptance_dual.sh` re-executes gates 4-7 against a
  three-warehouse world (`docker/compose.dual.yml`): boot 16s, egress blocked,
  `warehouses` section present and stable, every name answers, the
  DuckDB<->DuckDB join agrees on 30 days, a write to `legacy` by name is
  refused, `/grading` absent and the world read-only to the grader.
* `tasks/harness/dual_container_check.py` runs a real dual-warehouse episode
  under the container topology (world built on the host by
  `world/scripts/dual_world_builder.py`; the Postgres peer seeded from inside
  the container over backend_net; MCP over `docker exec -i`). See
  `tasks/harness/evidence/dual-container-check.json`. It found one harness
  gap on the way: a host-built world's `profiles.yml` carries host paths, so
  `dbt run` inside the container bound against nothing while `dbt compile`
  (which never binds) probed live. `container_episode.reroot_profiles` now
  moves those paths onto `/world` and stages the file at `/opt/episode/dbt`.
