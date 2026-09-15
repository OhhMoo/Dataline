# World — deterministic data-agent worlds

Seed → reproducible data-agent world, per `agent.md` §9.3 Track A (the
"world and determinism" track). The product here is not the tools; it is
that the world is **provably the same world** every time it is built, and
that the grader can never be reached during an episode.

## Layout

```
world/            the builder package (stdlib + duckdb + dbt-duckdb + pyyaml)
  build.py        seed.yaml -> world (python -m world.build --seed S --root R)
  themes/         load_theme(name): a world *shape* as data, not code
    <name>/       theme.yaml + gen.py hooks + workspace/ tree + bi/*.yml
                  + seed.example.yaml — see "Themes" below
  fingerprint.py  the two determinism gates + raw-byte diagnostic
  scheduler.py    the virtual clock, the schedule, and the live world
  executor.py     job execution: real dbt / SQL / loaders; derived status
  faults.py       the seeded fault layer: how the world misbehaves on its own
  observe.py      the observability writer: var/obs/samples.jsonl
  gateway_scheduler.py  the executing scheduler, shaped to the gateway's
                  Scheduler protocol (injection only — no gateway import)
  bi.py           dashboards = YAML of SQL panels -> computed values
  snapshot.py     deterministic tar save/restore + restore verify
  hashing.py      canonicalization spec (this IS the fingerprint contract)
seed.example.yaml placeholder schema — Track C's frozen §2.1 spec replaces it
docker/           image + 3-zone compose + egress gate
tests/            acceptance tests (stdlib unittest)
scripts/acceptance.sh   the full battery
grading/          skeleton for Track C's post-episode zone
requirements.lock pinned dependency closure (build provenance)
```

World layout (contract §2.1), built under `<root>` (`/world` in the episode
container): `workspace/` git repo (dbt project, landing CSVs, docs),
`var/warehouse/warehouse.duckdb`, `var/sched/state.json`, `var/bi/dashboards/`,
`build/provenance.json`. Two additions outside §2.1's agent-visible list:
`var/ingest/state.json` (connector control plane) and `var/world/live.json`
(live-world control plane) — neither is reachable from any gateway tool. One
addition that IS agent-visible: `var/obs/` (metric definitions plus the
appended sample record), per `OBS_STORE_CONTRACT.md`.

## The live world

`trigger` and `advance` **execute**. A run record says what happened; it is
never asserted.

- `trigger(root, job)` runs the job's declared work — the real dbt project, the
  real workspace loader, the real DuckDB file — and derives `status` from the
  result. A broken model **fails**, carrying dbt's own error. A job with no
  declared work fails as `unsupported`: the scheduler will not report a status
  it did not observe.
- `advance(root, hours)` materialises the span. New source partitions land and
  scheduled jobs fire, in virtual-time order, executing for real.

```
python -m world.scheduler <root> plan 30       # dry run: what would happen
python -m world.scheduler <root> advance 30    # do it
python -m world.scheduler <root> trigger dbt_build
python -m world.scheduler <root> logs          # status + first error line
python -m world.scheduler <root> live          # the control plane
```

Two files, deliberately split:

| file | holds | visible to the agent |
|---|---|---|
| `var/sched/state.json` | `vnow`, `jobs` {command, duration_s}, `runs` | yes — `sched_*` |
| `var/world/live.json` | schedule, `exec` blocks, arrival stream, traffic params | no tool reads `/var/world` |

`state.json`'s shape is unchanged from R002: `sched_state` / `sched_list` /
`sched_logs` return exactly what they returned before, and a seeded history row
is byte-for-byte what it was. Runs produced by the executor carry extra keys
(`trigger`, `executed`, `action`, `error`, `effects`, `execution_date`).

**The gateway keeps fabricating for non-dbt jobs unless it is told not to.**
`alkera_gateway.surfaces.FileScheduler` executes `dbt_build`/`dbt_run` and
reports plain `"success"` for every other command. `world.gateway_scheduler.
WorldScheduler` is a drop-in for the gateway's `Scheduler` protocol that routes
`sched_trigger` through the executor; inject it with
`Gateway(config, scheduler=WorldScheduler(root))`. It imports nothing from the
gateway (§10.2).

### Determinism of a world that moves

- one PRNG per `(seed, day_index)`, drawn inside the theme's `write_day` hook,
  so day N's bytes never depend on the path taken to day N.
- `vnow` after `advance` is exactly `start + hours` — never a function of how
  long a job ran. `advance(1)` × 30 lands the identical world as `advance(30)`.
- occurrences are deduplicated by `(job, vts_start)` against history, so
  re-advancing over a simulated span is a no-op, not a replay.
- `job_starts` resolves `after:` dependencies by repeated passes, not by
  mapping order: the manifest keeps author order, `live.json` is canonical JSON
  and sorts keys, and both must yield one schedule.
- engine output is **normalized** before it reaches the fingerprinted
  `state.json`: ANSI, `HH:MM:SS` stamps, `in 0.16s` elapsed and host-shaped
  paths are stripped (measured against real dbt output, not assumed). The raw
  output goes to `build/sched/logs/`, outside the fingerprint and the snapshot.

## The fault layer

`advance` can make the world misbehave on its own, deterministically. A theme
declares a catalogue under `live.faults`; a seed file overrides any entry by id
(`faults: {vendor_late_delivery: {enabled: false}}`). Eight kinds:

| kind | leaves behind |
|---|---|
| `late_arrival` | the partition lands `delay_minutes` late — the consumer ran at its slot and found nothing |
| `missing_arrival` | the partition never lands; a gap that does not heal (`days_scheduled` moves, `days_landed` does not) |
| `duplicate_arrival` | a byte copy under a sibling name that the loader's glob picks up |
| `torn_write` | the payload cut mid-line and the `.done` sentinel withheld — a read landing inside a write |
| `job_timeout` | one `failed` run, `executed: false`, killed at the timeout; no work ran |
| `job_retry` | a short `failed` attempt, then a real one `retry_delay_minutes` later, interleaved at its real time |
| `job_overrun` | the work really runs; the recorded window stretches over the next occurrence, or past `vnow` |
| `partial_write` | the work runs, the tail of the newest partition is gone, and the run is `failed` |

Shipped defaults: `sftp_vendor` and `mainframe_batch` ENABLED (a flaky vendor
feed; a batch window that gets missed), `revenue_daily` and `excel_close`
declared and OFF — `revenue_daily` is the seed the acceptance battery and the
cross-host gate build.

**Determinism.** One draw per `(fault_seed, fault id, occurrence)` and nothing
else: not iteration order, not accumulated PRNG state, not which other faults
are declared. Enabling one fault shifts no other fault's draws. Anything that
has to happen after the span (a late arrival, a retry attempt) is parked in
`live.json` by ABSOLUTE vts and replayed from there, which is what keeps
`advance(1)` × N equal to `advance(N)`.

**Visibility.** A fault never names itself in `var/sched/state.json`: the agent
meets a failed run, a gap, a truncated file, exactly as it would in a real
stack. `live.json`'s `fault_log` is the author-side ledger — fingerprinted, so
tamper-evident, and unreachable from any gateway tool.

## The observability store

`var/obs/definitions.yaml` (seeded verbatim from `themes/<name>/obs/`) declares
metrics and alarms; `build`, `advance` and `trigger` append one sample per
metric to `var/obs/samples.jsonl` — `OBS_STORE_CONTRACT.md`, written here and
read by the gateway. `null` means insufficient data and is never `0.0`. No
fault-specific metric exists: a late partition shows up as freshness climbing,
a timeout as an error rate stepping off zero, an overrun as a duration jumping.

Samples are appended in **measurement order** (the order jobs start), not
strictly by `vts`, because measurement order is the path-independent one — with
`job_overrun` active a line can carry an earlier `vts` than the line above it.
Sort by `vts` if you need clock order.

## Themes

`seed["theme"]` (default `revenue_daily`) selects a directory under
`world/world/themes/`; the builder is generic and a new world never touches
build.py / scheduler.py / fingerprint.py / bi.py. A theme provides:

- `theme.yaml` — manifest: `name`, `dbt_profile` (matching the theme's
  dbt_project.yml), `jobs` (scheduler run specs per past day, each with an
  `exec:` block saying what the job actually runs), and `live:` (the traffic
  parameters to carry into the world, plus the `arrival` stream). A theme with
  no `live:` block builds a static world, exactly as before.
- `gen.py` — three hooks: `write_landing(workspace, seed)`,
  `write_day(workspace, seed, day_index, day)` — one day, pure in
  `(seed, day_index)`, which `write_landing` loops over and the arrival stream
  calls — and `load_raw(workspace, db_path)`. Imported in place; themes are not
  packages. An optional fourth hook, `write_ingest(root, seed, manifest)`,
  seeds the ingestion control-plane state (`var/ingest/state.json`); only
  themes that define it get the fingerprint's conditional `ingest` section.
- `workspace/` — static files (dbt project, README, docs) copied byte-verbatim
  into the built workspace.
- `bi/*.yml` — dashboard specs copied verbatim to `var/bi/dashboards/`.
- `seed.example.yaml` — an example seed for the theme.

Themes must load their base tables into schemas `raw`, `staging`, `warehouse`
— `fingerprint.py` hashes exactly those three.

`exec` kinds an `exec:` block may name:

| kind | runs | fails when |
|---|---|---|
| `dbt` | `dbt <command> [--select …]` against the world | dbt returns non-zero; the normalized engine error is the run's `error` |
| `theme` | a `gen.py` hook, e.g. `load_raw` | the hook raises |
| `script` | a python script inside `workspace/` | non-zero exit |
| `sql` | statements, or a `.sql` file inside `workspace/` | DuckDB raises |
| `scan` | counts delivery files under `workspace/` | fewer than `min` files, or the folder is gone |

Anything else — including a job with no `exec` block at all — is reported
`unsupported` and **fails**. There is no code path that reports success without
running something.

## The two gates (and the diagnostic)

1. **Semantic fingerprint** — same seed built from scratch twice, on different
   hosts, must give the identical `semantic_hash`. Covers: warehouse schemas +
   canonically-ordered rows, scheduler state incl. v-clock, BI definitions,
   workspace files, and — conditionally, only when the theme seeds them — the
   ingestion control plane (`var/ingest/state.json`) and the live-world control
   plane (`var/world/live.json`). Canonicalization rules live in
   `world/hashing.py`.
   `scripts/acceptance.sh` gate 3 proves this live: it builds the same seed on
   the host and inside the `alkera-world:0.1.0` container and requires equal
   semantic hashes (with no docker daemon it falls back to a weaker same-host
   double-build and says so).
2. **Build provenance** — `build/provenance.json` attests seed hash,
   generator version, dependency versions, requirements.lock hash, image
   digest. The gate is that it is *recorded* and matches claims.
3. **Raw byte hash** of `warehouse.duckdb` — a diagnostic, NOT correctness.
   It IS the gate for snapshot restore, where bytes must match exactly.

## Determinism rules enforced in the builder

- Theme gen hooks (revenue_daily): RNG is per-day `Random(f"{seed}:{day}")`;
  iteration order irrelevant. Amounts are integer cents rendered `%.2f` — no
  binary floats in the pipeline.
- `PRAGMA threads=1` + `ORDER BY` on the raw load; dbt `threads: 1`.
- Git author/committer dates pinned to `vtime_start`; commit hashes reproduce.
- No wall-clock reads anywhere, in the builder or in the executor. All time
  comes from `var/sched/state.json` (`vnow`), which moves only by a seeded job
  duration (`trigger`) or by the requested span (`advance`).
- dbt runs as a subprocess (in-process `dbtRunner` leaks duckdb file locks)
  with `--target-path`/`--log-path` outside the fingerprinted workspace.

## Resolved contract wart: catalog/schema name collision

The duckdb catalog takes the file stem (`warehouse`), which collides with the
`warehouse` schema: `warehouse.fct_revenue` is ambiguous when the file is
opened directly. **Resolution (integration commit 1f7bd8c): never open the
warehouse file unprefixed — always `ATTACH` under an alias.** `bi.py` attaches
as `wh` + `USE wh.warehouse`, and Track B's gateway does the same, so world
content stays clean two-part SQL. `fingerprint.py` hashes BASE TABLES only —
a view is a deterministic function of its inputs, so hashing it is redundant
and was fragile.

**Third form of the same collision, found R003 and fixed here: dbt bakes the
catalog name into every view it creates.** A view's body is *stored*, so it can
be created successfully and be unreadable afterwards. dbt opens
`warehouse.duckdb` directly — catalog `warehouse` — and rendered
`FROM warehouse.raw.daily_revenue` into every staging view; read back through
the mandatory alias, that is `Binder Error: Catalog "warehouse" does not
exist!`, which broke `sql_query` on every staging view in every theme.
Fix: each theme's `dbt_project/macros/relations.sql` overrides `ref()` and
`source()` to render **without** the database qualifier, so bodies say
`raw.daily_revenue` and bind against whatever catalog they find themselves in —
verified under `ATTACH … AS wh` with and without `USE`, and when the file is
opened directly. Making the reader and the writer agree on one alias would have
worked too and was rejected: agreement is a convention the next tool to run dbt
(the gateway's `dbt_run`, with a `profiles.yml` this repo does not write) can
break silently, and the fix must not depend on it. The gate is
`executor.unreadable_views(root)`, asserted in all four theme suites, and it
reads *every* view `duckdb_views()` reports — GT001's hand-written view passed
throughout and proved nothing, which is why this survived so long.

## Trust zones

`docker/docker-compose.yml`: `episode` (internal network — egress impossible),
`harness` (reaches episode only), `grading` (`network_mode: none`, world
read-only, starts post-episode only). Boot restores `/snapshot/world.tar` if
mounted, else builds from the seed at `$WORLD_SEED` (default `./seed.yaml`),
so a mounted theme seed boots without an image rebuild.

## Acceptance battery

`scripts/acceptance.sh` (venv auto-detected; docker gates need a daemon):

1. Host build → semantic fingerprint.
2. Snapshot restore → identical raw bytes.
2b. **Live-world determinism** — two from-scratch builds at one seed, the same
   `advance(30)` on each: identical semantic hash, and it must have *moved*
   from the built one. Host-only, so it runs with no docker daemon.
3. **Cross-host determinism** — same seed built inside the container image;
   host and container semantic hashes must be equal. Only the semantic hash is
   compared (duckdb page bytes may differ across OS); `provenance.json` is
   attestation, not compared. No-daemon fallback: weaker same-host
   double-build, labelled as such.
4. Episode boot <60s; episode-zone egress impossible (`egress_test.py`).
5. **Quiescence** — fingerprint `/world` in the live episode, idle ~5s,
   re-fingerprint: must be equal. Then a health check plus one read-only
   `sql_query` through the gateway: a perturbation here is a Track B follow-up
   (gateway read path), reported as a warning, not patched from this track.
6. MCP smoke + read-only `sql_query` via the harness; image <2GB.
7. **/grading unreachability** (ground rule §1.1: the grader does not exist
   during the episode) — with episode+harness live: no `/grading` in episode
   or harness, no grading container in `compose ps`. Then grading is brought
   up post-episode: `NetworkMode` must be `none`, episode still has no
   `/grading`, and the `grading` hostname is unresolvable from the episode.

## Run it

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.lock
bash scripts/acceptance.sh          # the full battery (venv auto-detected)
.venv/Scripts/python -m unittest discover -s tests   # just the tests
```

## Deliberate limits (out of scope per §3)

- Placeholder schema stands in for Track C's frozen §2.1 spec.
- Cross-host fingerprint equality is proven per-run by acceptance gate 3
  (host build vs in-container build of the same seed); CI rerunning
  `scripts/acceptance.sh` on a second host adds a second data point.
- No Airflow, no vendor BI, no Snowflake fidelity — five surfaces only.
- **The fault layer stages contention; it never races.** Two overlapping runs
  are two overlapping *recorded windows* over strictly sequential execution.
  On DuckDB appends never conflict, so a live race produces no error, forces no
  winner and does not rebuild — we build the evidence of a race, never the
  race. See `implementation_structure/rounds/R003/FAULTS.md`.
- **Arrival faults do not reach the seeded backstory.** `faults_in_history`
  covers job faults only; a late or missing *historical* partition would need
  every theme's `write_landing` restructured.
- Worlds built outside `world.build` (GT001/GT002/GT003 fixtures) have no
  `var/world/live.json`, so `trigger` there reports `unsupported` and
  `advance` moves the clock only — loudly, in the run record.
