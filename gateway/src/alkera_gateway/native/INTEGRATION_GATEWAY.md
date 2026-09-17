# Gateway-side integration: the items five builders filed, closed

**Status: executed, 2026-09-16.** Owner: INTEGRATE-GATEWAY. Spec:
`gateway/NATIVE.md`. Every number below came out of
`gateway/.venv/Scripts/python.exe -m pytest tests -q`, run from `gateway/` on this
machine; anything not measured says *designed*.

**Suite: 320 passed, 0 skipped in 227.3s**, against **290** before. Per file:
`test_native_services_ops` 71 (was 68), `test_native_services_human` 41 (was 40),
**`test_native_context` 18 (new)**, `test_native_audit` 11 (unchanged),
`test_object_storage` 10 (was 7), `test_snowflake_proxy` 7 (was 2).
`test_warehouse_backends`'s byte-identical-row test passes on both parametrisations;
`ruff check --select E,F,I,UP,B --line-length 100` is clean over every file touched.

## 1. `Observation` nests and seals; the human plane runs on it (SERVICES-A 2)

`records.Observation` gained `child()` (a fresh observation for a nested call, whose
classification cannot reach the caller's row), `seal()` (a later classifying assignment
raises `SealedObservation`) and `classified` (did a module method assign one at all);
the two new fields are `compare=False, repr=False`, so comparing two observations still
means what it did. jira, slack and confluence then stopped calling
`workflow.{IssueTracker,Chat,KnowledgeBase}` and started calling the `Operations`
methods the MCP tools are. Every `op_class`/`targets` literal in the three modules is
gone: `Span.run` hands the module the observation, seals it on return, and the row is
written off it; header and resolution reads use `Span.aside`, which passes
`observation.child()`. `no_change` and `overwrite` now arrive from `operations.py`. One
endpoint is exempt, visibly: `GET /rest/api/3/field` serves a constant vocabulary and
asks the world nothing, so it uses `context.note` — the hatch `metabase.session_create`
already used — under `targets: ["issues"]`.

*Proof.* `test_one_action_two_transports_one_classification` — 16 pairs, each run
natively and through an MCP `Gateway` over a pristine clone — is green unchanged, and
now measures a property the code produces rather than a table someone kept.
`test_a_composed_read_costs_what_an_mcp_agent_would_have_paid` still reads
`issues_list`, `issues_search`, `issues_get` ×2 for one JQL search, so the aside
boundary did not move. New negative,
`test_an_endpoint_that_ran_no_module_call_cannot_ship_a_row`: a route that opens a span
and renders without a module call raises `UnclassifiedObservation` naming
`jira.issues_get`, with **zero rows** on file. `test_native_context` covers seal, child,
a double `run`, and an aside's targets never reaching the row.

## 2. `native/context.py` replaced four copies of one step (A 1, B 3)

`services/_ops.py` is deleted and the verbatim `world()`/`Ledger` block in each of jira,
slack and confluence is gone: all nine services import `Context`, `process_context`,
`set_context`, `context_of`, `Ledger`, `Span`, `served`, `service_health`, `note`,
`ServiceError` and `DEV_TOKEN` from one module. `process_context()` is cached against
the config file's **bytes**, so a service still resolves its world per request — a test
or a reload can repoint a running app — without rebuilding `Operations` every call.
SERVICES_A_REQUESTS item 5's three environment variables are unchanged. *Proof:*
`test_native_context` (18) drives a bare app through the env-var path, rebuild-on-change
included; both service test files pass with only the import changed. *For its owner:*
`serve.py:346` and `docker/NATIVE_STACK.md:144` still name `_ops.py` in prose; neither
imports it.

## 3. Silences and deployment statuses are state, not memory (B 2)

`ObservabilityStore.silence`/`list_silences`/`expire_silence` write and read
`var/obs/silences.json` — **inside the world tree**, inside the directory
`world/world/fingerprint.py` hashes, because a silence changes what the alarm surface
reports to every later reader. `deploy_status`/`deploy_statuses` go into the observation
record beside the deploy history `deploy_run` already writes there, because a verdict
changes nothing another reader sees. `operations.py` serves `alarms_ack`,
`alarms_silences`, `alarms_unack`, `deploy_status`, `deploy_statuses`. Alertmanager's
`POST /api/v2/silences` resolves matchers against `alarms_list` (a resolution read, no
row) and stores **alarm ids**, so its one row carries `targets: ["alarm:orders_stale"]`.

*Proof.* `test_a_silence_is_world_state_that_outlives_the_service` digests every file in
the world tree before and after: the diff is exactly `{"var/obs/silences.json"}`,
nothing else changed. It then rebuilds every service app over the same tree — the
restart — and the silence is still there, still suppressing `orders_stale`, with the
audited row naming it. `test_a_deployment_status_outlives_the_service` does the same for
a verdict and asserts the world tree is *not* touched. *Designed, not built:*
`alarms_ack` is an `Operations` method, not a registered MCP tool; registering it is one
entry in `tools.py`, not this work's file, and moves the served tool list off 63.

## 4. `project` / `workspace`, S3 credentials, the statement log, ruff

**(A 3)** `Operations.issues_list` carries `project` and `chat_channels` carries
`workspace`, with obviously-fake fallbacks for a world declaring neither, so jira and
slack read their headers off a result rather than a raw workflow document. Two MCP
results changed, additively; the suite is green.

**(AUDIT 1)** `storage.credentials()` reads `ALKERA_S3_ACCESS_KEY` /
`ALKERA_S3_SECRET_KEY`, defaulting to `alkera-local-dev` — obviously fake, and the
string `compose.native.yml` sets for MinIO's root user and password. The old literal
`episode` was seven characters, one short of MinIO's minimum. *Proof:* three new tests,
all **executed** here, not skipped — a put/get/list/delete round trip through the MCP
tools against `minio/minio@sha256:14cea49…`, the digest `compose.native.yml` pins; a
test that wrong credentials are refused (`InvalidAccessKeyId`/`SignatureDoesNotMatch`),
without which the first would pass against a server checking nothing; and a docker-free
test of the default. s3mock's seven are unchanged; both fixtures skip with a reason when
docker is away.

**(AUDIT 3)** `snowflake_proxy.forward` appends one canonical-JSON object per `POST
/queries/v1/query-request` to `$ALKERA_SNOWFLAKE_STATEMENT_LOG`: `{"vts", "sql", "rows",
"error", "latency_ms"}`, one per line, `vts` from `native.audit.virtual_time()`. Five
keys for four facts — the outcome is flattened into `rows`/`error` so a tailer maps one
key to one `native.audit.record` argument. Gzipped bodies are decoded (the real
connector compresses); unset, nothing is written. **The tailer is not here**;
`native/tailers/` is INTEGRATE-STACK's. *Proof:* `test_snowflake_proxy` 2 → 7.

**(AUDIT 7)** Both filed ruff findings were **already clean** when this started:
`loki.py:229` binds its loop variable and `alertmanager.py:115` is wrapped. Two remain,
neither this work's: `config.py:1`, `tests/test_dual_dbt.py:12`, I001.

## Weakest finding

**A wide read is O(n) module calls, and each one re-reads the whole document.** Rule 1
says a JQL search pays one `issues_get` per row returned and `Operations.issues_get`
loads `issues.json` from disk every time; likewise an expanded wiki listing. Correct —
it is what an MCP agent pays — but at fixture size (8 tickets, 9 pages) the cost is
invisible and has never been measured above it. Second, self-inflicted: `issues_list`
and `chat_channels` load their document a *second* time to serve `project`/`workspace`,
because `workflow.py` was not this work's file to change. Third: sealing is strong for a
handler using `run`/`aside` and silent for one reaching past them — `Span.observation`
is a plain attribute, so handing it to a module call *before* `run` still overwrites;
only doing so afterwards raises.
