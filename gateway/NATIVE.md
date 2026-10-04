# Native mode — the agent works the way a data engineer does

The specification native mode was built against (2026-09-16, with its same-day
amendment), and what is still open. The day's measurements, with their commands and
numbers, are in `docs/implementation_plan/gateway/NATIVE_MEASURED_2026-09-16.md`.
Companion documents:
`docker/NATIVE_STACK.md` (the compose stack), `native/PARITY.md` (row parity
measurements), `native/services/{HUMAN,OPS}_PLANE.md` (the fake services),
`docs/implementation_plan/harness/NATIVE_DRIVER.md` (the host-topology driver) and
`docs/implementation_plan/harness/NATIVE_CONTAINER.md` (the container topology). Every claim below is
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
| ops | — (BI as code) | Power BI REST + Fabric item definitions + Entra token; `tabcmd` / Tableau Server Client; Sigma REST API v2 | fake services `powerbi`, `tableau`, `sigma` — **explicit-only**, see below |
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

**The BI vendors are explicit-only plugs.** `powerbi` (8095), `tableau` (8096) and `sigma`
(8097) are `services.PLUG_NAMES`, not `NAMES`: an unprofiled stack, world, endpoint map
and compose overlay are byte for byte what they were before them. They are served only
when an environment profile plugs one (`plugs.py`: `[environment.plugs] bi = "powerbi"`,
or a stack whose default is one — `snowflake` → `sigma`, `aws` → `tableau`,
`databricks` → `powerbi`), exactly like the Jupyter notebook plug. Then: `serve.py
--profile` mounts it (`serve.PLUG_PORTS`); the episode's endpoint map, allowed
destinations and environment gain it (`agent_under_test.PLUG_SERVICES`,
`plug_service_env`); the container overlay adds `<vendor>.internal` to native-services'
aliases and the vendor's variables to the episode (and jupyter) environment; the surface
gate sweeps it as `~<vendor>`. The world names it in `.envrc`, `services.toml` and
`docs/onboarding.md` only when its seed records the vendor — `profile: {name, slots,
plugs: {bi: [powerbi]}}` — and the harness refuses a rollout whose world was built for a
different BI than the task's profile serves (`episode.check_world_profile`).
**Their published state is the services', not the episode's**: under the
container topology native-services keeps it in `/service-state/bi`
(`<rollout>/service-state`, a mount no other container has; its entrypoint
`docker/native_services.py` writes the services' config there too), never in
`/audit`, which the episode mounts read-write and from which a subject could
forge a "published" workbook until 2026-10-04. The grader stages it from there
(`tasks/harness/bistate.py`, `{bi_state}`); P7 asserts the episode cannot see
it. On the host topology the harness's rollout config points `bi_state_dir` at
the same `<rollout>/service-state/bi` - outside every directory the subject is
pointed at, but a host subject can write anywhere, which is one more reason host
native is not `measurement_valid`. Variables:

| vendor | variables (values: the `.internal` URL, or `alkera-local-dev`) |
|---|---|
| powerbi | `POWERBI_API_URL`, `FABRIC_API_URL`, `AZURE_AUTHORITY_HOST` (one URL: one service answers all three hosts), `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID` |
| tableau | `TABLEAU_SERVER_URL`, `TABLEAU_SITE` (`""`, the Default site), `TABLEAU_PAT_NAME`, `TABLEAU_PAT_SECRET` |
| sigma | `SIGMA_BASE_URL`, `SIGMA_CLIENT_ID`, `SIGMA_CLIENT_SECRET` |

## The harness

`--driver native`: the subject gets bash only — no MCP server, no tool list in the prompt.
The workspace carries `.envrc`, `~/.dbt/profiles.yml`, `~/.config/alkera/*.toml`,
`docs/onboarding.md` (world seed flag `native: true`, OFF by default so every recorded
fingerprint is unchanged; `<root>/home` is a conditional fingerprint section). DNS in the
episode network resolves `*.internal` to the fake services and nothing else.

In the container the workspace is a bind mount owned by the operator's uid and
the agent is root, so the episode image marks exactly `/workspace` as a git
`safe.directory` (system config, as a dev container with a mounted checkout
does); without it every `git status` died on "dubious ownership". The shims'
rows carry the world's clock through `ALKERA_VIRTUAL_TIME` (the per-rollout
overlay), because `var/sched` is not mounted there.

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

## Designed, not executed — and since

- **The container topology for `--driver native`** was designed only, then built and
  found false: a real model's shell stayed on the host while the record said
  `filesystem_isolated` (`implementation_structure/rounds/R005/NATIVE-CONTAINER-FALSE-GUARANTEE.md`).
  The redirect (`BASH_ENV` into `docker exec`, `verify_container_execution`, the guarantee
  refused without proof) is in `tasks/harness/container_episode.py`; the harness-side proof
  is `docker/evidence/shell-redirect-harness-proof.json`; the one-turn model smoke
  (`test_native_container.py --model-smoke`) writes `shell-redirect-bash-env.json` once
  taken. See `docs/implementation_plan/harness/NATIVE_CONTAINER.md`.
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
