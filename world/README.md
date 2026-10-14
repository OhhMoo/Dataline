# world/ — the world factory

Turn a seed into a data engineer's working environment that rebuilds byte-for-byte:
warehouse, dbt project, scheduler on a virtual clock, human-context files, faults, and a
protected ledger of what the world published. Nothing here is the agent's interface; the
gateway is (`gateway/README.md`). The product is not the tools: it is that the world is
**provably the same world** every time it is built, and that the grader can never be
reached during an episode.

```
world/            the package (python -m world.build --seed S --root R)
  build.py themes/<name>/     seed -> world; five themes (each with its gen.py); opt-in seed flags `warehouses:`, `native:`, `faults:`, `profile:`, `files:`, and commerce_analytics' `bi_faults:`
  fingerprint.py hashing.py   the semantic hash every determinism claim rests on; the canonicalisation spec
  scheduler.py executor.py    virtual clock + the job runner (what a scheduled run really does)
  gateway_scheduler.py        the executing scheduler, shaped to the gateway's Scheduler protocol (injection only)
  faults.py                   seeded ways the world breaks on its own
  observe.py                  the observability writer: var/obs/samples.jsonl
  ledger.py                   protected receipts of what the world published (grading layer 4; below)
  workflow.py                 the seeded ticket / chat / wiki documents
  native_workspace.py         .envrc / profiles.yml / onboarding.md for native mode
  files.py                    the `files:` flag: parquet / JSON / JSONL user files
  bi_projects.py              `profile.plugs.bi`: each BI vendor's project, checked into the repo
  semantic/                   the BI semantic core: model, expression tree, SQL compiler, render;
                              formats/ = dbt semantic-layer YAML, Power BI (TMDL/PBIR/DAX),
                              Tableau (.tds/.twb/calcs), Sigma (workbook/data-model JSON)
  warehouse_seed.py           set-based Postgres/DuckDB seeding for the `warehouses:` flag
  bi.py snapshot.py           dashboards = YAML of SQL panels (rendered through semantic/); deterministic tar save/restore
docker/           episode image, 3-zone compose, egress gate
grading/          bind-mount source for the compose `grading` zone (see its README)
scripts/acceptance.sh   the 7-gate determinism battery -> evidence/acceptance.json
tests/            unittest + pytest (run with gateway/.venv's interpreter)
```

World layout under `<root>` (`/world` in the episode container): `workspace/` git repo
(dbt project, landing CSVs, docs), `var/warehouse/warehouse.duckdb` (+ `<name>.duckdb`
per additional warehouse), `var/sched/state.json`, `var/bi/dashboards/`,
`build/provenance.json`. Not reachable from any gateway tool: `var/ingest/state.json`
(connector control plane), `var/world/live.json` (live-world control plane),
`var/ledger/receipts.jsonl` (publication ledger), `var/workflow/*.json` (reachable only
through the workflow tools). Agent-visible: `var/obs/` (metric definitions plus the
appended sample record).

## The live world

`trigger` and `advance` **execute**. A run record says what happened; it is never
asserted.

- `trigger(root, job)` runs the job's declared work — the real dbt project, the real
  loader, the real DuckDB file — and derives `status` from the result. A broken model
  **fails** with dbt's own error. A job with no declared work fails as `unsupported`: the
  scheduler will not report a status it did not observe.
- `advance(root, hours)` materialises the span: new source partitions land and scheduled
  jobs fire, in virtual-time order, executing for real.

```
python -m world.scheduler <root> plan 30       # dry run
python -m world.scheduler <root> advance 30    # do it
python -m world.scheduler <root> trigger dbt_build
python -m world.scheduler <root> logs          # status + first error line
python -m world.scheduler <root> live          # the control plane
```

| file | holds | visible to the agent |
|---|---|---|
| `var/sched/state.json` | `vnow`, `jobs` {command, duration_s}, `runs` | yes — `sched_*` |
| `var/world/live.json` | schedule, `exec` blocks, arrival stream, traffic params, `fault_log` | no |

`state.json`'s shape is frozen: `sched_state` / `sched_list` / `sched_logs` return what
they always returned. Executor-produced runs carry extra keys (`trigger`, `executed`,
`action`, `error`, `effects`, `execution_date`).

The gateway's built-in `FileScheduler` executes only dbt jobs and reports plain
`"success"` for every other command; `world.gateway_scheduler.WorldScheduler` is the
drop-in that routes `sched_trigger` through the executor
(`Gateway(config, scheduler=WorldScheduler(root))`; `scheduling.episode_scheduler` picks
it whenever the world has `var/sched/state.json`). It imports nothing from the gateway.

**Determinism of a world that moves.** One PRNG per `(seed, day_index)` inside the theme's
`write_day`, so day N never depends on the path to day N. `vnow` after `advance` is
exactly `start + hours`, never a function of run time: `advance(1)` × 30 lands the
identical world as `advance(30)`. Occurrences are deduplicated by `(job, vts_start)`, so
re-advancing is a no-op. `after:` dependencies resolve by repeated passes, not mapping
order. Engine output is normalised before it reaches the fingerprinted `state.json`
(ANSI, `HH:MM:SS`, `in 0.16s`, host paths); raw output goes to `build/sched/logs/`,
outside the fingerprint and the snapshot.

### `exec` kinds

| kind | runs | fails when |
|---|---|---|
| `dbt` | `dbt <command> [--select …]` against the world | non-zero; normalised error is the run's `error` |
| `theme` | a `gen.py` hook, e.g. `load_raw` | the hook raises |
| `script` | a python script inside `workspace/` | non-zero exit |
| `sql` | statements, or a `.sql` file inside `workspace/` | DuckDB raises |
| `scan` | counts delivery files under `workspace/` | fewer than `min`, or the folder is gone |

Anything else — including no `exec` block — is `unsupported` and **fails**. There is no
code path that reports success without running something.

## The fault layer

`advance` can make the world misbehave on its own, deterministically. A theme declares a
catalogue under `live.faults`; a seed overrides any entry by id
(`faults: {vendor_late_delivery: {enabled: false}}`).

| kind | leaves behind |
|---|---|
| `late_arrival` | the partition lands `delay_minutes` late; the consumer ran at its slot and found nothing |
| `missing_arrival` | the partition never lands; a gap that does not heal |
| `duplicate_arrival` | a byte copy under a sibling name the loader's glob picks up |
| `torn_write` | the payload cut mid-line, `.done` sentinel withheld |
| `job_timeout` | one `failed` run, `executed: false`; no work ran |
| `job_retry` | a short `failed` attempt, then a real one `retry_delay_minutes` later |
| `job_overrun` | the work runs; the recorded window stretches over the next occurrence or past `vnow` |
| `partial_write` | the work runs, the tail of the newest partition is gone, the run is `failed` |

Defaults: `sftp_vendor` and `mainframe_batch` ENABLED; `revenue_daily` and `excel_close`
declared and OFF (`revenue_daily` is the seed the acceptance battery builds). One draw per
`(fault_seed, fault id, occurrence)` and nothing else — enabling one fault shifts no
other's draws; anything that happens after the span is parked in `live.json` by absolute
vts and replayed from there. A fault never names itself in `state.json`: the agent meets a
failed run, a gap, a truncated file. `live.json`'s `fault_log` is the author-side ledger —
fingerprinted, unreachable.

**The fault layer stages contention; it never races.** Two overlapping runs are two
overlapping *recorded windows* over strictly sequential execution — on DuckDB appends
never conflict, so we build the evidence of a race, never the race
(`docs/implementation_plan/structure/rounds/R003/FAULTS.md`). Arrival faults do not reach the seeded
backstory. Worlds built outside `world.build` (GT001–GT003 fixtures) have no `live.json`:
`trigger` reports `unsupported`, `advance` moves the clock only — loudly, in the record.

## The observability store

`var/obs/definitions.yaml` (seeded verbatim from `themes/<name>/obs/`) declares metrics
and alarms; `build`, `advance` and `trigger` append one sample per metric to
`var/obs/samples.jsonl` — written here, read by the gateway
(`gateway/SURFACES.md#observability`; row shape frozen in its "The sample store" section). `null`
means insufficient data and is never `0.0`. No fault-specific metric exists: a late
partition is freshness climbing, a timeout an error rate stepping off zero. Samples are
appended in measurement order (job start), not strictly by `vts`; sort if you need clock
order.

## Environment profiles (`profile:`)

```yaml
profile: {name: "da/laptop", slots: [files, notebook, warehouse, transform, bi, human]}
```

The harness resolves which slots an environment plugs (`alkera_gateway.plugs`) and writes
the list into the seed; the world only honours it (`world/` never imports the gateway).
Slot names: `files notebook compute warehouse transform orchestrator object_store ingest
bi observability release human`; anything else, a duplicate, or a missing `name` is
refused before anything is built. **An unplugged slot leaves no state behind**: no
`human` → no `var/workflow`; no `ingest` → no `var/ingest`; no `observability` → no
`var/obs` (and `observe.record` stays a no-op on every later advance); no `bi` → no
dashboards. Workspace, warehouse, dbt build and scheduler state are always built. Under
`native: true` the discovery files name no service from an unplugged slot (services →
slots in `native_workspace.SERVICE_SLOTS`, checked against the gateway's plugs), and the
warehouse backends follow the profile's warehouse plug(s): the record's `plugs.warehouse`,
else the stack's default (`native_workspace.STACK_WAREHOUSE`, checked against the
gateway's `STACKS`). `duckdb` names no Postgres and no Snowflake, `postgres` names
Postgres, `snowflake` names Snowflake as the warehouse; a stack the table does not know
lists what it always listed. The onboarding page of a profiled box lists wiki pages by
Confluence content id (the number `GET /wiki/rest/api/content/<id>` routes), with the
path beside it. No profile: the four files are byte for byte what they were
(`test_native_workspace.py` pins them), including the wiki listed by path, which the
Confluence API does not route.

`{name, slots}` (slots in canonical order) is recorded in `build/provenance.json` and in
`var/world/profile.json`, which is the conditional fingerprint section `profile`: two
profiles over one seed hash differently, and a profile that plugs every slot is the
unflagged world plus exactly that section. No `profile:` → no file, no key, every recorded
hash unchanged.

## User files (`files:`)

```yaml
files: {formats: [parquet, json, jsonl]}
```

Writes, into the workspace before the git commit, files derived from the theme's `raw`
tables: `data/exports/<raw table>/part-0000{0,1}.parquet` (every raw table, two halves),
`data/api/<subject>.json` (a JSON:API-shaped dump) and `data/events/<subject>.jsonl` (an
event log), where the subject is the first raw table with a money column. Each format
carries one planted pathology, documented in `world/world/files.py` and nowhere the agent
reads. Only listed formats are written; an unknown one is refused; `files:` under a
profile without the `files` slot is refused. Same seed → byte-identical files: DuckDB
`COPY` under `PRAGMA threads=1` with an `ORDER BY` over every column (the footer's
`created_by` is the locked DuckDB version, not a run property); fixed-key-order ASCII
JSON. The files move only the `workspace` section.

## BI projects (`profile: {plugs: {bi: [...]}}`)

```yaml
profile: {name: "da/laptop", slots: [files, notebook, warehouse, transform, bi, human],
          plugs: {bi: [powerbi, tableau, sigma]}}
```

A profile that plugs a BI vendor gets that vendor's dashboards-as-code in the
repository, beside the dbt project: `workspace/bi/powerbi/` (PBIP: TMDL + PBIR),
`workspace/bi/tableau/` (`datasources/*.tds`, `workbooks/*.twb`),
`workspace/bi/sigma/` (`data-models/`, `workbooks/`, `connections/` API JSON) -
the layout each fake vendor service publishes from. The theme says what they are
through an optional `gen.py` hook, `bi_projects(seed) -> {vendor: (Model,
(Report, ...))}` over `world.semantic`; each front-end's `write_project` writes
them (bi_projects.py). Plugging a vendor into a theme without the hook is refused
before anything is built; a model whose sources the built warehouse lacks is
refused at build. Connections name the world's DuckDB file (ODBC for Power BI and
Tableau; Sigma, which has no DuckDB connector, keeps its Snowflake connection,
read warehouse-relative). Written after the warehouse and BEFORE the git commit -
the services publish what was first committed - deterministic to the byte (ids
from names, no timestamps), moving only the `workspace` section. Other `bi`
plugs (`metabase_fake`), no `plugs`, or no profile write nothing.
`commerce_analytics` is the theme with the hook: Power BI's Commerce model and
Executive Overview, and the Tableau and Sigma editions of both, which agree on
every shared visual on the built warehouse (`world/tests/test_bi_projects.py`).

The step's place in `build.build`: after the warehouse, the dbt build, the
scheduler state, the plugged planes and the `files:` exports; before the native
discovery layer (its onboarding page lists the project directories, so they must
exist), the profile record and the git commit. A vendor plugged into a theme with
no hook is refused before anything is built (`bi_projects.require`); a model whose
sources the built warehouse lacks, at the step.

### `bi_faults:` (commerce_analytics)

```yaml
bi_faults: [tableau_orders_line_join, sigma_net_revenue_refunds]
```

A theme seed key, read by commerce_analytics' `bi_projects` hook
(`gen.BI_FAULTS`): each id plants a defect in one vendor's project as first
committed, so that vendor's service publishes it.
`tableau_orders_line_join` builds Sales' Tableau `Orders` logical table from Custom
SQL joining `fct_orders` to its lines (every order-grain measure summed once per
line); `sigma_net_revenue_refunds` computes Finance's Sigma Net Revenue inline as
"status is not cancelled" (refunds counted). The warehouse, the dbt project and
Power BI are untouched. Refused: an unknown id, and a fault in a vendor the
profile does not plug. Absent, the projects are the clean editions byte for byte.
BI001 builds with both on.
## Themes

`seed["theme"]` (default `revenue_daily`) selects `world/world/themes/<name>/`; the
builder is generic and a new world never touches `build.py` / `scheduler.py` /
`fingerprint.py`. A theme provides `theme.yaml` (name, `dbt_profile`, `jobs` with `exec:`
blocks, `live:` traffic and arrival stream, `warehouses:`), `gen.py` hooks
(`write_landing`, `write_day` — pure in `(seed, day_index)` — `load_raw`, optional
`write_ingest` and `seed_<warehouse>`), `workspace/` copied byte-verbatim, `bi/*.yml`,
`obs/`, and a `seed.example.yaml`. Themes load base tables into `raw`, `staging`,
`warehouse` — `fingerprint.py` hashes exactly those three. The human-context plane
(`var/workflow/*.json`, `gateway/SURFACES.md#workflow`) is seeded by each theme's
`gen.py` from content that is data, not code.

The five: `revenue_daily` (the default; the acceptance battery's seed), `sftp_vendor`
(the only one that seeds an ingest plane), `mainframe_batch`, `excel_close`, and
`commerce_analytics`: an e-commerce analytics-engineering shop - nightly CDC extracts
(orders, order lines, customers, products) into a dbt star schema (`dim_customer`,
`dim_product`, `dim_date`, `fct_orders`, `fct_order_lines`) with a MetricFlow
semantic layer, the Jira/Slack/Confluence corpus of an analytics team, and the only
`bi_projects` hook (Power BI, Tableau, Sigma). Its live faults all ship off; its BI
faults are `bi_faults:`. A build is about 9 s.

## The two gates (and the diagnostic)

1. **Semantic fingerprint** — same seed built twice on different hosts gives the identical
   `semantic_hash`. Covers warehouse schemas + canonically ordered rows, scheduler state
   incl. the v-clock, BI definitions, workspace files, and conditionally — only when the
   theme seeds them — `ingest`, `live`, `obs`, `workflow`, `warehouses`, `home`. Rules in
   `hashing.py`. Gate 3 of the battery builds the seed on the host and inside the
   container and requires equality.
2. **Build provenance** — `build/provenance.json` attests seed hash, generator version,
   dependency versions, lock hash, image digest. The gate is that it is recorded and
   matches.
3. **Raw byte hash** of `warehouse.duckdb` — a diagnostic, not correctness; it IS the gate
   for snapshot restore.

Rules the builder enforces: per-day `Random(f"{seed}:{day}")`; amounts are integer cents
rendered `%.2f`; `PRAGMA threads=1` + `ORDER BY` on the raw load, dbt `threads: 1`; git
dates pinned to `vtime_start` so commit hashes reproduce; no wall-clock reads anywhere —
all time is `vnow`; dbt runs as a subprocess with `--target-path` / `--log-path` outside
the fingerprinted workspace.

**The catalog/schema collision.** DuckDB's catalog takes the file stem (`warehouse`),
which collides with the `warehouse` schema. Never open the file unprefixed — always
`ATTACH … AS wh` (`bi.py`, the gateway). Third form, fixed in R003: dbt bakes the catalog
into every view body it creates, so views were unreadable through the alias
(`Binder Error: Catalog "warehouse" does not exist`). Each theme's
`dbt_project/macros/relations.sql` overrides `ref()` / `source()` to render without the
database qualifier; `executor.unreadable_views(root)` is the gate, asserted in every theme
suite over every view `duckdb_views()` reports. A query dbt runs at once and never stores
keeps the qualifier, because two-part `warehouse.<model>` is ambiguous against a catalog
also named `warehouse`: a test, and an inline query (`dbt show --inline` / `dbt compile
--inline`, a `sql_operation` node), in every theme. Each theme suite proves it with a
`test_dbt_show_inline_ref_*` test (`test_world.py` for revenue_daily). GT001-GT009
build from their own fixtures, not from these themes, so their worlds did not move when
the four non-commerce themes took the `sql_operation` branch (measured on every arm).

## Trust zones and the acceptance battery

`docker/docker-compose.yml`: `episode` (internal network — egress impossible), `harness`
(reaches episode only), `grading` (`network_mode: none`, world read-only, post-episode
only). Boot restores `/snapshot/world.tar` if mounted, else builds from `$WORLD_SEED`.

`scripts/acceptance.sh` (venv auto-detected; docker gates need a daemon):

1. Host build → semantic fingerprint.
2. Snapshot restore → identical raw bytes. **2b.** Live-world determinism: two from-scratch
   builds, the same `advance(30)`, identical hash that has *moved* from the built one.
3. Cross-host determinism: host vs in-container build, semantic hashes equal (no-daemon
   fallback: same-host double build, labelled).
4. Episode boot < 60 s; episode-zone egress impossible.
5. Quiescence: fingerprint `/world` live, idle, re-fingerprint, equal; then one read-only
   `sql_query` through the gateway.
6. MCP smoke; image < 2 GB.
7. `/grading` unreachability: none in episode or harness during the episode; brought up
   post-episode with `NetworkMode: none`, hostname unresolvable from the episode.

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.lock   # .venv/Scripts/ on Windows
bash scripts/acceptance.sh
(cd .. && gateway/.venv/bin/python -m pytest -q world/tests)       # from the repo root, as the baseline sweep runs it
```

## Publication ledger

*Frozen 2026-09-16. Writer `ledger.py` + `scheduler.py`; reader `tasks/harness/ledger.py`.
This seam is all the two share. Claims are **[measured]** (a test or probe ran it) or
**[designed]**.*

**Why.** A grader reading only the final world cannot tell a pipeline that was right all
along from one that published a wrong number on Tuesday and corrected it on Thursday;
`state.json` says a job succeeded, not what it made visible. The ledger records, at the
virtual moment of each publication, the canonical contents of what a consumer could see —
so a good final state cannot erase a bad intermediate one, and invariants can be graded at
every checkpoint.

**Schema.** `<root>/var/ledger/receipts.jsonl`, one canonical-JSON object per line
(`sort_keys`, tight separators, `ensure_ascii`), exactly these keys:

| key | value |
|---|---|
| `seq` | int, 1-based, contiguous |
| `vnow` | the run's `vts_end` — visible when the job finished |
| `day` | the **execution** day the occurrence was responsible for |
| `job` | scheduler job name |
| `target` | `catalog.schema.table`, `schema.table` (primary), or a workspace-relative file path |
| `kind` | `"table"` \| `"file"` |
| `canonical_hash` | sha256 of canonical logical contents |
| `row_count` | int, or null (file, or absent target) |
| `sample` | ≤5 rows as lists in canonical order, or null |
| `consumer` | string from the spec, or null |

Table hash = exactly `fingerprint._warehouse_hash`'s per-table rule (sha256 over
`{"table", "columns"}` then one canonical row per line, rows ordered by every column) —
typed values, sorted rows, not file bytes; a primary-warehouse receipt's hash *is*
`fingerprint.semantic()["warehouse"]["tables"][target]`, and the same table in another
row order hashes the same **[measured]**. File = sha256 of bytes **[measured]**. An absent
target hashes to the sentinel `ledger.ABSENT`, null `row_count`/`sample`, no raise
**[measured]**.

**Trigger.** A job spec may carry `publishes: [{target, kind, consumer?}]`, read from
`live["jobs"][job]["publishes"]`, then `…["exec"]["publishes"]`, then
`state["jobs"][job]["publishes"]` for a fixture world **[measured]**; `state.json`'s frozen
shape is never written to. `WorldScheduler._simulate_job` appends one receipt per entry
after the command completes, at the three sites where `_execute` actually ran: the clean
path, `job_overrun`, `partial_write`. `job_timeout` and a burned `job_retry` attempt never
reached the work and publish nothing **[measured]**. `partial_write` *does* publish — the
torn table is exactly the bad intermediate publication this exists to pin **[designed]**.
**`trigger()` writes no receipt** **[measured]**: a manual trigger runs inside the episode
container, where `var/ledger` is shadowed by an empty read-only mount, so a receipt there
would vanish at teardown — a stated boundary beats a silently incomplete ledger.
**Consequence:** a publication an agent causes by `sched_trigger` is not in the ledger;
grade publications the clock advances over. This is the weakest part of the design.

**Determinism.** Same seed + same advances ⇒ byte-identical file; `advance(1)×30` equals
`advance(30)` **[measured]**. Nothing reads a wall clock; the file only grows
(`append_receipt` opens `"ab"`) **[measured]**.

**Fingerprint.** `var/ledger` is *not* a fingerprint section — an output of advances, like
`var/sched`. Forging a line leaves `semantic_hash` unchanged **[measured]**; no theme
declares `publishes` (it would move `live.json` and every recorded hash), so no existing
world changes **[measured]**. Integrity is the agent's inability to reach it. It *is*
inside `snapshot.SCOPE` **[designed]**.

**Protection.** Gateway: the `fs_*` root is `workspace`; every escaping spelling
(`../var/ledger/…`, `/world/var/ledger/…`, `/workspace/../…`) is `path_denied`;
`var/ledger/receipts.jsonl` unprefixed resolves inside the agent's repo and leaves the
ledger untouched **[measured: P5]**. Container shell: `compose.yml` /
`compose.episode.yml` mount an empty read-only tmpfs at `/world/var/ledger`; from inside,
against a receipt planted from a second container, the kernel reports `tmpfs
ro,nosuid,nodev,noexec`, the directory lists empty, read is `FileNotFoundError`, write is
`Read-only file system`, a canary sweep finds nothing **[measured: P5,
`gateway/docker/evidence/containment-ledger.json`]**. The writer runs on the host, outside
the container. Grading: `tasks/harness/ledger.py stage(world_root, grading_dir)` copies the
ledger to `<grading>/ledger/receipts.jsonl` after teardown, before the grader
(`{ledger}` in `grade_argv`); `load_receipts` prefers that copy and refuses malformed
evidence (unknown/missing keys, a hole in `seq`); `receipts_between(r, a, b)` is
half-open; `last_receipt` follows `seq` **[measured]**.

Not covered: `sched_trigger` publications; files record no `row_count`/`sample`; P5 ran
against the fixture stack, not a live rollout world **[designed]**; a target the world
never writes records `ABSENT` every time, loudly.

## Deliberate limits

No Airflow fidelity, no Snowflake fidelity beyond the connector route. Vendor BI is
the semantic core's subset of each product, refused by name outside it and not yet
calibrated against the real Power BI, Tableau or Sigma. An
unwired headless-Airflow prototype (`vclock.py`, `airflow_bridge.py`, `airflow_logs.py`;
nothing imported it and Airflow is not installed) is archived under `_archive/world/world/`.
Cross-host equality is proven per run by gate 3; a second host adds a second data point.
