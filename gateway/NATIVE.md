# Native mode — the agent works the way a data engineer does

The specification native mode was built against (2026-09-16, with its same-day
amendment) and what has been executed since, with the numbers. Companion documents:
`docker/NATIVE_STACK.md` (the compose stack), `native/PARITY.md` (row parity
measurements), `native/services/{HUMAN,OPS}_PLANE.md` (the fake services),
`tasks/harness/NATIVE_DRIVER.md` (the host-topology driver) and
`tasks/harness/NATIVE_CONTAINER.md` (the container topology). Every claim below is
**executed** unless marked *designed*.

## Why

The 63-tool MCP list is a map of the world: `sched_trigger`, `bi_render`,
`ingest_alerts`, `deploy_run` tell the agent which surfaces exist and matter before it has
read a file. A real engineer discovers that from `.envrc`, `~/.dbt/profiles.yml`, the
onboarding doc and `--help`. Two consequences: trajectories recorded through bespoke tools
teach a harness schema, not the job; and every difficulty number before native mode was
measured with the map in hand. Native mode is both the realistic substrate and an
experiment — same tasks, native surfaces, does the number move?

## The one thing that must survive: the action log

agent.md §9.2.2 — one row per action, identical from every transport — is what blindness
detection, containment proofs and `rollout.py --reclassify` rest on. Native CLIs leave no
such row, so **every row is written on the server side** by the thing the agent talked
to, or by a PATH-front shim for binaries that touch the world directly.

```python
alkera_gateway.native.audit.record(*, tool, args, op_class, targets, read_targets=None,
    exit="ok", error=None, latency_ms=0.0, result=None, transport="native", actor="") -> dict
```

Appends one 9.2.2 row to the file named by `ALKERA_NATIVE_AUDIT` (default
`<audit_dir>/actions.jsonl`, the same file the MCP gateway writes). `tool` is
`<service>.<operation>` (`jira.issues_get`, `duckdb.statement`, `minio.PutObject`). Every
fake service and shim calls this and nothing else; the row shape is owned by
`alkera_gateway.records`.

**Which warehouse.** The first draft served Postgres as the native warehouse (server log
→ rows). Amended 2026-09-16 on measured facts: `world/world/executor.py` runs every
scheduled job with the `duckdb` library against `var/warehouse/warehouse.duckdb`, the theme
pipelines call the `duckdb` CLI, dbt profiles target DuckDB, every grader reads the DuckDB
file. Serving Postgres would split the world. So **native mode keeps DuckDB for the
existing tasks** (a DuckDB-on-the-laptop stack is a real setup); its audit comes from the
PATH shims — the `duckdb` shim parses `-c` / `.read` / stdin through `sql.inspect_sql`, so
rows carry table targets exactly as MCP rows do; the `python` shim can only record argv,
exit and changed files (a stated loss of resolution). The Postgres path (statement-log
tailer; parity in `native/PARITY.md`) remains the warehouse for tasks that declare
`warehouse_backend: postgres`.

## Mapping: the 63 tools → native surfaces

| plane | MCP tools | native surface | backing |
|---|---|---|---|
| workspace | `fs_*`, `git_*` | bash, `git` | real |
| data | `sql_query`, `sql_execute` | `duckdb` (shim over the world's file); `psql` where `warehouse_backend: postgres` | DuckDB / Postgres, real |
| data | `dbt_*` | `dbt` CLI, `$DBT_PROFILES_DIR/profiles.yml` | real |
| data | `s3_*` | `aws s3` / `boto3` (`AWS_ENDPOINT_URL`) | MinIO (real; audit via webhook receiver) |
| control | `sched_*` | `./ops/sched list \| logs \| trigger` in the repo, over the served plane `sched.internal:8120` | the world's own `WorldScheduler` |
| control | `deploy_*` | `./ops/deploy` + release REST | fake service `deploy` |
| human | `issues_*` | Jira Cloud REST v3 subset; `jira` CLI / python `jira` / curl | fake service `jira` |
| human | `chat_*` | Slack Web API subset; `slack_sdk` (`base_url`) | fake service `slack` |
| human | `kb_*` | Confluence REST subset; `atlassian-python-api` | fake service `confluence` |
| ops | `bi_*` | Metabase API | fake service `metabase` |
| ops | `metrics_*`, `alarms_*` | Prometheus `/api/v1/query*`, Alertmanager `/api/v2/alerts` | fake services `prometheus`, `alertmanager` |
| ops | `logs_*` | Loki `/loki/api/v1/query_range`; `logcli` | fake service `loki` |
| ops | `ingest_*` | Airbyte REST subset | fake service `airbyte` |

`docs_*` maps to `workspace/docs` and NOT to confluence, because that is what
`Operations.docs_list` reads. The scheduler is a served plane rather than Airflow: an
Airflow-shaped CLI would have handed the native agent capabilities the MCP agent lacks on
the same `task.toml`. `./ops/sched` shipped an `advance HOURS` subcommand and it was
removed the same day — `Operations` has no `sched_advance`; the GRADER moves the clock —
because Phase 4 compares exactly those two numbers. Every remaining subcommand runs the
`Operations` method its MCP twin runs and reads `op_class` / `targets` off the
`Observation`; nothing is hand-classified, nothing imports `world/`. Measured over
byte-clones of one world (`test_native_stack.py`):

```
./ops/sched list                 sched.sched_list     read ['scheduler']
./ops/sched logs run-000363      sched.sched_logs     read ['scheduler:run-000363']
./ops/sched trigger daily_load   sched.sched_trigger  exec ['scheduler:daily_load', 'dbt_project']
```

— each identical to the `alkera_gateway.Gateway` row for the same call.

**Rules for every fake service.** It imitates a real vendor's API shape closely enough
that the real client library works (the compatibility test uses that client, not curl).
It serves and mutates the same fixture files the MCP tools use (`var/workflow/*.json`,
`var/obs/*`, `var/world/live.json`) through the existing `workflow` / `observability` / `bi`
modules — the world stays the single source of truth; fingerprints and graders do not
change. It calls `native.audit.record` for every request. It is one module
`native/services/<name>.py` exposing `NAME`, `HOST` (`jira.internal`), `PORT`, `app`;
`services/__init__.py` holds the fixed `NAMES`, `native/serve.py` mounts them in one
process. Credentials the workspace carries are obviously fake (`alkera-local-dev-token`).

## The harness

`--driver native`: the subject gets bash only — no MCP server, no tool list in the prompt.
The workspace carries `.envrc`, `~/.dbt/profiles.yml`, `~/.config/alkera/*.toml`,
`docs/onboarding.md` (world seed flag `native: true`, OFF by default so every recorded
fingerprint is unchanged; `<root>/home` is a conditional fingerprint section). DNS in the
episode network resolves `*.internal` to the fake services and nothing else.

Blindness in native mode: `unknown_tool` and `shell_outside_gateway` do not apply (bash
IS the interface). What replaces them: a **network-destination audit** (every connection
the episode made, from the service side; `destinations.jsonl`) and the unchanged
world-diff rule — a world change with no audited row is a violation, and in native mode it
is HARD where MCP made it soft. Containment gains **P6: only the fake services are
reachable**, measured like P0–P5 in `docker/containment.py`.

Acceptance was phased: (1) audit parity for the already-real half — the same scripted
episode via MCP and native touches the same target set; (2) the fake services, each
passing a compatibility test driven by the real client, each row validating against the
9.2.2 schema; (3) `run_rollouts.py --driver native` completing a scripted episode end to
end, P6 measured; (4) re-measure GT001/GT004/GT006/GT008 K=5 native vs MCP.

## Measured — 2026-09-16, the stack integrated

Host: Windows 11, docker 29.5.2, `gateway/.venv` 3.11.15.

- **The image carries the `native` extra; P6 is real.** `requirements-native.lock` (110
  packages, `--require-hashes`, from the one `uv.lock`); inside the image `services.load()`
  → 9 loaded. `containment.py --native` → `evidence/containment-native.json` **passes**:
  each `/health` 200 with a real count (jira 8, slack 3, confluence 9, metabase 1,
  prometheus 10, alertmanager 5, loki 30, airbyte 2, deploy 1); seven control names fail to
  resolve, 18 port probes refused, no published host port, positive control positive.
  A world that seeds no ingest plane makes `airbyte` 503 and P6 fail on it — P6 needs a
  world seeding all nine planes.
- **Postgres logging, role-scoped.** Server `log_statement=none`; `ALTER ROLE alkera SET
  log_statement='all'` + `log_min_duration_statement=0`. Measured: 24 log lines holding
  exactly the 2 `alkera` statements. Both settings must move together.
- **MinIO's audit receiver** is `tailers.minio.build_app()` mounted by `serve.py` on
  `native-services.internal:8110`, inside the process that writes `actions.jsonl` (one
  writer for the `seq` lock); absent from `destinations.jsonl` on purpose — MinIO dials it,
  the episode never does.
- **The services carry the world's clock.** `serve.py` exports `ALKERA_GATEWAY_CONFIG`,
  `ALKERA_NATIVE_AUDIT`, `ALKERA_SCHEDULER` from `--world`; the P6 run's first row carries
  the world's `vnow`, not `GatewayConfig().virtual_time`. Without it all nine answered 503.
- **`<root>/home` is fingerprinted**: default seed still `85ce4cbb…`; a `native: true`
  build `252d5a5c…`; one appended profiles line moves it.
- **Phase 3 acceptance**: `run_rollouts.py --task GT001 --k 1 --driver native --model
  scripted`. `native_stack.py` brought up nine services + aggregate + receiver on loopback
  with shims on PATH; the subject ran as a real bash (`duckdb` through the shim, `curl
  $JIRA_URL/…`, `sed -i`); 12 rows, each the 23 keys — 10 harness (`<service>.health`,
  `workspace.edit`) and 2 the subject's (`duckdb.statement` read of
  `warehouse.warehouse.fct_orders`; `jira.issues_list`, `exit: error` because GT001 seeds
  no human plane — the gate records it dead, correctly).
- `deploy_probe.py` suffix-matches the tool (`deploy.deploy_run` visible to GT007's grader,
  TASK VALID). No `duckdb` CLI ships on the host or in the image, so the shim runs
  statements through the `duckdb` module — same libduckdb, same file; rows say
  `duckdb_library`.

## Measured — 2026-09-16, NATIVE-FIXES

1. **A DuckDB write is attributable.** `blindness._unattributed` reads the MCP name off the
   tool SUFFIX (`sched.sched_trigger` IS `sched_trigger`), and a `duckdb.statement` /
   `shim.dbt` / `shim.python` WRITE naming a declared warehouse file (`args.database` or a
   target) explains that file and no other. `declared_warehouses` = manifest `warehouse` +
   `warehouses` specs + every `var/warehouse/*.duckdb` (the rule `fingerprint.py` hashes
   by). Measured on real bytes: a `DELETE` row naming the file against a changed
   `warehouse.duckdb` is clean; the same change with only a `jira.issues_list` row is hard.
2. **The native warehouse check is DuckDB; dbt is its own surface.** `surface_audit` opens
   the world's file through `ATTACH … AS wh (READ_ONLY)` (no write lock, no catalog
   collision); Postgres remains the check for `warehouse_backend: postgres`; a probe with
   `warehouse = "legacy"` asks about that file. The content rule binds only where the task
   declares `[harness].warehouse_schemas` — GT008's primary holds no table at t0 (building
   it IS the task) while the MCP probe is `SELECT 1`; a stricter native gate would make one
   `task.toml` two tasks. `dbt` is live iff it resolves on PATH AND a profiles output
   resolves to this world's file; `native_stack` puts the interpreter's scripts dir behind
   the shims (else `dbt` exits 127). A 503 carrying a health document reads as **dead**.
3. **`./ops/sched`** (above) materialised by `native_stack.install_workspace_cli` BEFORE
   `fingerprint` and `tree_manifest`. Swapping the acceptance subject's `advance 1` for a
   real `trigger` found `build/sched/logs/run-*.log` — the raw engine log `executor.py`
   keeps outside the fingerprint but inside the diff — now covered by the `sched_trigger`
   branch alongside `var/obs`. `native/context.py` builds `Operations` with
   `scheduling.episode_scheduler(config)` (same as `cli serve`), so services and CLI agree
   on `WorldScheduler`; `native_stack.venv_env` puts `world/` on the services'
   `PYTHONPATH` (without it all nine answered 500).
4. **Acceptance**, scripted subject (`./ops/sched list`, duckdb CREATE/DELETE/DROP,
   `./ops/sched trigger <first job>`, a jira request, `sed -i`):

   | task | gate | required live | dead | audit clean | blindness |
   |---|---|---|---|---|---|
   | GT004 | pass | 16/16 | — | yes | clean |
   | GT006 | pass | 17/17 | — | yes | clean |
   | GT008 | pass | 16/16 | `~airbyte`, declared `dead_by_design` | yes | clean |

   The three statements really change the warehouse file (sha `577b96b8…` →
   `6c177622…`), so the runs are clean because the attribution rule holds.
5. **Trigger-all smoke** (`ALKERA_TRIGGER_ALL=1`): every job every world declares, in
   declaration order — GT001 `daily_load, dbt_build`; GT004 `build_marts,
   load_orders_delta, refresh_bi_extract`; GT006 `build_payments, land_payments,
   refresh_bi_extract`; GT008 `legacy_nightly, nova_nightly` — ten triggers over a dbt
   build, a sql loader, a noop and two script nightlies; **no allowlist addition needed**,
   blindness clean on all four. GT001 reports `gate: not_declared` (no
   `required_surfaces` in its task.toml, under both drivers) and `dbt` dead with
   `no_dbt_profile` — recorded, not fixed.
6. Suites at the time: `selftest.py` 147/147, `test_native_driver.py` 122/122,
   `test_native_stack.py` 37/37, `test_native_context.py` 19, gateway 321 passed.

## Designed, not executed — and since

- **The container topology for `--driver native`** was designed only, then built and
  found false: a real model's shell stayed on the host while the record said
  `filesystem_isolated` (`implementation_structure/rounds/R005/NATIVE-CONTAINER-FALSE-GUARANTEE.md`).
  The redirect (`BASH_ENV` into `docker exec`, `verify_container_execution`, the guarantee
  refused without proof) is in `tasks/harness/container_episode.py`; the harness-side proof
  is `docker/evidence/shell-redirect-harness-proof.json`; the one-turn model smoke
  (`test_native_container.py --model-smoke`) writes `shell-redirect-bash-env.json` once
  taken. See `tasks/harness/NATIVE_CONTAINER.md`.
- **Postgres-served tasks**: `native_stack.py` starts no Postgres; nothing calls
  `warehouse_seed.seed_postgres` for native.
- **The fakesnow tailer**: no statement log to tail.
- **MinIO on the host topology**: `NativeStack._start_minio` written; no task requires s3.
- **The native world leaks authoring metadata**: `var/workflow/*.json` `authoring` blocks
  and grader commentary in `var/obs/definitions.yaml` are readable from bash (MCP strips
  them). Open.

## Weakest finding

**The world diff's allowlist is a hand-kept list of paths, and each entry was added the
day a real run tripped over it** — `var/sched` after a `sched_trigger` rollout, `var/bi`
after a `bi_update`, `var/obs` and `build/sched` after GT008's native acceptance. Every one
was a legitimate write by the surface the episode used, found by a rollout failing rather
than by reading `world/`. Native mode makes them HARD, so the list that used to produce a
footnote now throws rollouts away, and nothing enumerates what a triggered run may write
except the run itself. The trigger-all smoke narrows this to exactly what it can: for the
four worlds and four job shapes it ran, the list is complete; a fifth task or a changed
job kind restores the risk with nothing to warn anybody. A generated manifest from the
world (which planes each job kind touches) would replace the list.

Runners-up: the `dbt` surface proves the toolchain resolves and the profile points at the
right file, not that `dbt compile` succeeds (less than `dbt_compile` proved under MCP); the
warehouse content rule binds nowhere in GT004/GT006/GT008, so an emptied warehouse would
pass the native `sql` gate; `./ops/sched` is discovered by looking in the repo, because the
generated `docs/onboarding.md` does not name it.
