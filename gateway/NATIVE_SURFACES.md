# Native surfaces: the agent works the way a data engineer does

**Status: specification, 2026-09-16.** Everything below is *designed* until a
section says *measured*. Owners are the builder agents; the reviewer is external.

## Why

The 63-tool MCP list is a map of the world. `sched_trigger`, `bi_render`,
`ingest_alerts`, `deploy_run` tell the agent which surfaces exist and which matter
before it has read a file. A real engineer discovers that from `.envrc`,
`~/.dbt/profiles.yml`, the onboarding doc and `--help`. Two consequences:

1. trajectories recorded through bespoke tools teach a harness schema, not the job;
2. every difficulty number so far was measured with the map in hand.

So native mode is both the realistic substrate and an experiment: same tasks,
native surfaces, does the number move?

## The one thing that must survive: the action log

agent.md 9.2.2 - one row per action, identical from every transport - is what
blindness detection, containment proofs and `rollout.py --reclassify` rest on.
Native CLIs leave no such row by themselves, so in native mode **every row is
written on the server side** by the thing the agent talked to, or by a PATH-front
shim for binaries that touch the world directly. Consequence, decided here:

> **The native-mode warehouse is served, never a file.** Primary = Postgres
> (`log_statement=all` -> rows) or fakesnow (server log -> rows). DuckDB stays the
> host-topology development backend. `tests/test_warehouse_backends.py` already
> proves 9.2.2 rows byte-identical over `['duckdb', 'postgres']`.

### `alkera_gateway.native.audit.record(...)` - the single row writer

```python
record(*, tool: str, args: dict, op_class: str, targets: list[str],
       read_targets: list[str] | None = None, exit: str = "ok",
       error: str | None = None, latency_ms: float = 0.0,
       result: object = None, transport: str = "native", actor: str = "") -> dict
```

Appends one 9.2.2-shaped row to the file named by `ALKERA_NATIVE_AUDIT`
(default `<audit_dir>/actions.jsonl`, the same file the MCP gateway writes).
`tool` is `<service>.<operation>` (`jira.issue_get`, `psql.statement`,
`minio.PutObject`). Every fake service and every shim calls this and nothing
else; the row shape is owned by `alkera_gateway.records`, not re-invented.

### Amendment 2026-09-16 (after Phase 1-3 landed): which warehouse native mode uses

Measured facts that the paragraph above did not have: `world/world/executor.py`
runs every scheduled job with the `duckdb` library against
`var/warehouse/warehouse.duckdb`; the theme workspaces' own pipelines call the
`duckdb` CLI; dbt profiles target DuckDB; every grader reads the DuckDB file.
Serving Postgres to the agent would split the world - the agent writes one
store while the scheduler and grader read another - unless executor, dbt and
graders were all re-pointed.

Decision: **native mode keeps DuckDB as the warehouse file for the existing
tasks** (a DuckDB-on-the-laptop stack is a real engineer's setup). Its audit
comes from the PATH shims: the `duckdb` shim parses `-c`/`.read`/stdin
statements through `sql.inspect_sql` so rows carry table targets exactly as the
MCP path does; the `python` shim can only record argv, exit and the files that
changed, so a Python script embedding DuckDB yields a row naming
`var/warehouse/warehouse.duckdb`, not tables - a stated loss of resolution. The
Postgres-served path (statement-log tailer, parity measured in
`native/PARITY.md`) is the warehouse for tasks that declare
`warehouse_backend: postgres`; nothing about it is withdrawn.

## Mapping: the 63 tools -> native surfaces

| plane | MCP tools | native surface (what the agent uses) | backing | owner |
|---|---|---|---|---|
| workspace | `fs_*`, `git_*` | bash, `git` | real | - (exists) |
| data | `sql_query`, `sql_execute` | `psql`; `snowsql` / `snowflake-connector-python` | Postgres (real), fakesnow (real) | AUDIT (rows) |
| data | `dbt_*` | `dbt` CLI | real project, `profiles.yml` | AUDIT (shim rows) |
| data | `s3_*` | `aws s3` / `boto3` (`AWS_ENDPOINT_URL`) | MinIO (real) | AUDIT (webhook rows) |
| control | `sched_*` | `airflow dags trigger`, `airflow tasks logs`, Airflow REST | Airflow 3 under vclock (real) | AUDIT (access-log rows) |
| control | `deploy_*` | `./ops/deploy` in the repo + release REST API | **fake service** `deploy` | SERVICES-B |
| human | `issues_*` | Jira Cloud REST v3 subset; `jira` CLI / python `jira` / curl | **fake service** `jira` | SERVICES-A |
| human | `chat_*` | Slack Web API subset; `slack_sdk` (`base_url`) | **fake service** `slack` | SERVICES-A |
| human | `kb_*`, `docs_*` | Confluence REST subset; `atlassian-python-api` | **fake service** `confluence` | SERVICES-A |
| ops | `bi_*` | Metabase API (`/api/card/:id/query`, `/api/dashboard/:id`) | **fake service** `metabase` | SERVICES-B |
| ops | `metrics_*`, `alarms_*` | Prometheus `/api/v1/query*` + Alertmanager `/api/v2/alerts`; `promtool` | **fake services** `prometheus`, `alertmanager` | SERVICES-B |
| ops | `logs_*` | Loki `/loki/api/v1/query_range`; `logcli` | **fake service** `loki` | SERVICES-B |
| ops | `ingest_*` | Airbyte REST subset (`/v1/connections`, `/v1/jobs`) | **fake service** `airbyte` | SERVICES-B |

Rules for every fake service:

- It imitates a **real vendor's API shape** closely enough that the real client
  library works against it (the compatibility test uses that client, not curl).
- It serves and mutates the **same fixture files** the MCP tools use
  (`var/workflow/*.json`, `var/obs/*`, `var/world/live.json`, ...), through the
  existing `alkera_gateway.workflow` / `observability` / `bi` modules - the world
  stays the single source of truth; fingerprints and graders do not change.
- It calls `native.audit.record` for every request it serves.
- It is one module `alkera_gateway/native/services/<name>.py` exposing
  `NAME: str`, `HOST: str` (the DNS alias, e.g. `jira.internal`), `PORT: int`, and
  `app` (a FastAPI application) - `services/__init__.py` imports the fixed list
  `NAMES` and `native/serve.py` mounts them.
- Credentials the workspace carries are **obviously fake** (`alkera-local-dev-token`);
  never a realistic key format (repo rule).

## Native mode for the harness

`--driver native`: the subject gets bash only. No MCP server, no tool list in the
prompt. The workspace carries `.envrc`, `~/.dbt/profiles.yml`,
`~/.config/alkera/*.toml`, `docs/onboarding.md`; DNS in the episode network
resolves `*.internal` to the fake services; nothing else resolves.

Blindness in native mode: `unknown_tool` and `shell_outside_gateway` do not apply
(bash *is* the interface). What replaces them: a **network-destination audit**
(every connection the episode made, from the service side) and the unchanged
world-diff rule (a world change with no audited row is a violation).

Containment gains **P6: only the fake services are reachable** - measured the way
P0-P5 are, in `gateway/docker/containment.py`.

The world gains an opt-in seed flag `native: true` that writes the discovery
files into the workspace; OFF by default so every recorded fingerprint is unchanged.

## Phases and acceptance

| phase | builds | acceptance (must be executed, not asserted) |
|---|---|---|
| 1 | audit parity for the already-real half | the same scripted episode run via MCP and via native touches the same **target set**; blindness world-diff finds nothing unaudited |
| 2 | the eight fake services | each passes a compatibility test driven by the real client; each row it writes validates against the 9.2.2 schema |
| 3 | native driver, discovery layer, P6 | `run_rollouts.py --driver native` completes a scripted (non-model) episode end to end; P6 measured |
| 4 | re-measure | GT001/GT004/GT006/GT008 K=5 native vs the recorded MCP numbers |

## Ownership (parallel builders, disjoint files)

| builder | owns |
|---|---|
| AUDIT | `native/audit.py` (real implementation over `records`), `native/tailers/*`, `native/shims/*`, `tests/test_native_audit.py`, `native/PARITY.md` |
| SERVICES-A | `native/services/{jira,slack,confluence}.py`, their tests, `native/services/HUMAN_PLANE.md` |
| SERVICES-B | `native/services/{metabase,prometheus,alertmanager,loki,airbyte,deploy}.py`, their tests, `native/services/OPS_PLANE.md` |
| NATIVE-DRIVER | `tasks/harness/{agent_under_test,rollout,run_rollouts,blindness,surface_audit}.py` native paths, `tasks/harness/NATIVE_DRIVER.md`, selftest additions |
| COMPOSE-DISCOVERY | `native/serve.py`, `native/services/__init__.py`, `gateway/docker/compose.native.yml`, containment P6, `world/world/native_workspace.py` (+ seed flag), `gateway/docker/NATIVE_STACK.md` |

Nobody edits another builder's files. Shared interfaces are the ones on this page.
