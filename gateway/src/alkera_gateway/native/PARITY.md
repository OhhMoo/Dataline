# Native-mode audit parity

**2026-09-16.** Each line is **[executed]** (a command ran; the number is its
output) or **[designed]** (written, never run against the real thing). Host:
Windows 11, `gateway/.venv/Scripts/python.exe`, Docker Desktop; containers are
digest-pinned to what `docker/compose.yml` and the existing tests pin.

`native.audit.record` builds its row from `alkera_gateway.records`, the
canonicaliser and hasher `Gateway._call` uses, so a native row carries the 23
agent.md 9.2.2 keys and no others. `seq` is allocated under
`<audit_dir>/writer.lock`, the lock a live `AuditLog` holds, so MCP and native
cannot both allocate into one log; the second writer raises and names the first.
9.2.2 has no `actor` key, so `record`'s `actor` becomes the flag `actor:<name>`.
**[executed]** `pytest tests/test_native_audit.py -q -p no:randomly` ->
**11 passed in 23.69s**, 0 skipped; with docker removed from `PATH` the three
container tests **skip** naming the reason - never a vacuous pass.

## Target-set parity - the Phase 1 acceptance
**[executed]** `test_target_sets_are_identical_over_mcp_and_native`. One sequence,
twice: `sql_query`/`sql_execute` vs real `psql -c` against a statement-logging
postgres; `s3_put` vs boto3 into MinIO's audit webhook; `fs_write` vs a PATH shim
whose targets are a workspace sha256 diff. The two logs' target sets are
**equal**, and equal to `{"warehouse.items", "s3://episode/exports/report.csv",
"docs/runbook.md"}`. The SQL target is backend-independent by construction (both
sides call `sql.inspect_sql`), which `test_warehouse_backends.py` already proves.
The object put is the one leg where the transports talk to **different servers**:
`storage.ObjectStorage` hardcodes the secret `episode`, seven characters, below
MinIO's eight-character minimum, so the gateway cannot authenticate against the
audited MinIO (`AUDIT_REQUESTS.md` item 1).

> *Amended 2026-09-16 by INTEGRATE-GATEWAY.* Item 1 is closed:
> `storage.credentials()` reads `ALKERA_S3_ACCESS_KEY`/`ALKERA_S3_SECRET_KEY` and
> defaults to `alkera-local-dev`, and `test_object_storage.py` now runs the MCP
> tools against the MinIO digest `compose.native.yml` pins. The paragraph above
> is left as the record of what was measured then; the two legs can now talk to
> one server, and re-running this parity check on one is *designed, not done*.
> See `native/INTEGRATION_GATEWAY.md`.

## postgres (`tailers/postgres.py`) - **[executed]**
`docker run postgres@sha256:f1c337... -c log_statement=all -c
log_min_duration_statement=0 -c "log_line_prefix=%m [%p] "`, then real `psql`:
over a 12-statement session, **12 rows, 12 statements**, no misses.
`op_class`/`targets`/`flags` come from `sql.inspect_sql` at `dialect="postgres"`,
the gateway's own call, so `ALTER` is `destructive` on both transports rather
than `ddl` here and `destructive` there. The brief's verb table is only the
fallback, for statements `sql.py` refuses, flagged `outside_tool_surface`.

Captured: multi-line statements (tab continuations joined, measured on psql's own
8-line catalog queries); `psql -f script.sql` exactly like `-c`, since the server
logs each statement whatever the client read it from; extended-protocol
`execute <unnamed>:` with its `DETAIL: parameters:`; `latency_ms` from the
`duration:` entry (0.066-205.266 ms measured); failures from the following
`ERROR:` (`exit: "error"`, `error.code: "sql_error"`); and the combined
`duration: N ms  statement: ...` entry the server writes when only
`log_min_duration_statement` covers a statement - which a parser that stops at
the duration drops silently.

Not captured: a `\!` shell escape never reaches the server (the shims' job;
`\copy` does, as `COPY ... FROM STDIN`); SQL-level `PREPARE`/`EXECUTE` measures
`op_class: exec`, `targets: []`, the server logging the portal name and not the
statement behind it - the extended protocol has no such problem. `COPY` also
contradicts its verb, so `copy_direction` ignores a parenthesised source query
and both `COPY t TO STDOUT` forms measure read.

## MinIO (`tailers/minio.py`) - **[executed]**
`minio/minio@sha256:14cea4...` with `MINIO_AUDIT_WEBHOOK_ENABLE_alkera=on` and
`..._ENDPOINT_alkera=http://alkera-audit-host:<port>/audit`, receiver in-process:
over a boto3 sequence, **7 requests, 7 rows**. Targets are `s3://bucket/key` -
the string `ObjectStorage.call` writes - with a listing's prefix read from
`requestQuery` so `s3_list` and `aws s3 ls` agree; `latency_ms` from
`timeToResponseInNS`; a 404 becomes `exit: "error"`, `error.code: "s3_404"`.
Entries with no `api.name` (health probes) are dropped; an unknown operation gets
an `op_class` from its verb prefix and the flag `unclassified_api`. Two measured
surprises, both in `test_native_audit.py`: MinIO names bucket creation
`PutBucket`, and `--add-host ...:host-gateway` publishes an AAAA record it fails
on, so the receiver's IPv4 is resolved inside a container.

## Airflow (`tailers/airflow.py`) - **[executed, live, outside pytest]**
`docker run alkera-airflow:3.0.2 python3 -m airflow standalone`, then
`POST /auth/token` and three REST calls; the api-server's uvicorn access lines fed
to the tailer: **4 lines -> 3 rows** (`/auth/token` is outside `/api/vN` and
correctly produces none). `POST .../dagRuns` ->
`["scheduler:vendor_feeds", "dbt_project"]`, matching `Operations.sched_trigger`
exactly - targets use the MCP scheduler's vocabulary, not `dag:<id>`, so the two
logs compare as sets. The pytest case asserts over those captured lines rather
than starting Airflow. Gap: 3.0.2 ignores `--access-logfile` (measured).

## fakesnow / Snowflake - **[designed]**
No statement log exists to tail: `snowflake_fidelity.py` logs only warnings and
fakesnow's server keeps no query history file. The SQL text exists in one place,
`snowflake_proxy.forward` handling `POST /queries/v1/query-request`, where
`AUDIT_REQUESTS.md` item 3 asks for a four-field append. **Not built.**

## Shims (`shims/`) - **[executed]**
`install.py` writes two files per binary - `<name>.cmd` for cmd.exe/PowerShell,
an extensionless `#!/bin/sh` `<name>` for Git Bash and WSL - both execing
`shims.run` with the installing interpreter's absolute path baked in. Wrapped:
`dbt`, `python`, `git`; not `psql`/`aws`, whose rows are the tailers'. Measured:
a wrapper invoked from the workspace recorded `tool: "shim.python"`,
`op_class: "exec"`, `targets: ["docs/runbook.md"]` - the file the binary changed,
named as `files.Workspace.name` names it - plus `latency_ms` and the exit code. A
binary missing from PATH outside the shim directory is an audited failure
(`error.code: "binary_missing"`, exit 127), never a silent one. Not captured:
changes outside the workspace, and a file written and deleted within one call;
above `ALKERA_SHIM_MAX_FILES` (20000) the row carries `targets: []` **with the
flag `workspace_unscanned`**, never a quiet empty set. `dbt` is a known mismatch:
MCP writes `["dbt_project"]`, the shim writes the changed files.

## Weakest finding
**The postgres tailer audits the server, not the agent, and cannot tell them
apart.** With `log_statement=all` the log holds every statement every connection
made: measured, a bare container start plus one readiness probe put **5** rows
(`SELECT 1 FROM pg_database`, `CREATE DATABASE "episode"`, `BEGIN`, `SELECT 1`,
`COMMIT`) into the log before the agent existed, and one `\d warehouse.items`
adds **8** catalog SELECTs the agent never typed. Those 8 are flagged
`catalog_only` so a grader can exclude them; the provisioning rows are
indistinguishable from agent work by content alone.

The mitigation is per-role logging and it is **[executed]**: with
`log_statement=none` globally and `ALTER ROLE episode SET log_statement='all'`,
the `episode` role's statement was logged and the superuser's was not. But it
holds only if `log_min_duration_statement` is role-scoped too - measured, set
globally the superuser's statement came back anyway, in the combined
`duration: ... statement: ...` form. Two settings must move, and getting one
wrong silently re-pollutes the log. `AUDIT_REQUESTS.md` item 2 asks
COMPOSE-DISCOVERY for both.
