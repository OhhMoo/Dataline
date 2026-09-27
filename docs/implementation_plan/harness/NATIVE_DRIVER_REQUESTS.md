# What `--driver native` needs from the other builders

NATIVE-DRIVER owns `tasks/harness/{agent_under_test,rollout,run_rollouts,
blindness,surface_audit}.py`, `test_native_driver.py` and `NATIVE_DRIVER.md`,
and nothing else. Everything below is a contract the harness now programs
against and cannot supply for itself. Each item says what breaks if it is not
supplied, so none of them can be quietly dropped.

## SERVICES-A and SERVICES-B

1. **`GET <base>/health`, 2xx, unauthenticated, on every service**, answering
   exactly:

   ```json
   {"ok": true, "service": "jira", "items": 14}
   ```

   `items` is the count of that service's primary objects — issues, channels,
   pages, dashboards, metric series, alerts, streams, connections, deployments.
   This is the native surface gate (`surface_audit.probe_native`) and it is
   **content-bearing**: `ok` true with `items > 0` is live; `ok:false` or
   `items == 0` is DEAD, so a task that requires that surface is refused as
   `harness_error` and never scored. A body with no `ok`, or with no/non-int
   `items`, is SUSPECT — the gate will not fail on it, and the report will not
   call the run clean either. Nine services: `jira slack confluence metabase
   prometheus alertmanager loki airbyte deploy`.
2. `/health` must be cheap and must not mutate the world: the gate calls it once
   per surface per rollout, against the episode's real world (there is no
   gateway config to re-root, so `probe_native` cannot use a copy).
3. **Audit `/health` as `targets: ["health:<service>"]`** with the real
   `read_targets`, as SERVICES-B has already specified. The harness treats the
   `health:` namespace as its own bookkeeping (`blindness.is_harness_row`) and
   strips those rows from target-set comparison, world-diff attribution and the
   report's tool-call count. Any other `targets` shape on a health row would put
   the gate's nine pre-episode polls inside the episode's measurement.

## COMPOSE-DISCOVERY

4. **Ports.** `agent_under_test.NATIVE_SERVICES` fixes the defaults the subject's
   environment carries: jira 8080, slack 8081, confluence 8090, metabase 3000,
   prometheus 9090, alertmanager 9093, loki 3100, airbyte 8001, deploy 8085,
   MinIO 9000, Postgres 5432 — the vendor's own default wherever one exists. If
   `compose.native.yml` or a service module's `PORT` disagrees, change one of the
   two: a mismatch means the subject's `JIRA_URL` points at nothing and the gate
   reports a dead world.
5. **DNS.** `<name>.internal` for every service, resolving only inside the
   episode network. The harness relies on nothing else resolving: it is what
   makes "the defaults fail on a laptop" a safety property rather than a bug.
6. **`<audit_dir>/destinations.jsonl`.** One JSON object per line,
   `{"host": str, "port": int, "count": int}`, written **on the service side**
   (a log the episode could write is a log the episode could edit). `host` is
   the destination as the episode addressed it — the DNS alias in-network, the
   literal address otherwise. Aggregate per (host, port); `count` 0 means seen
   but never dialled. **Absence is not "no connections"**: `blindness` treats a
   missing file as `unobserved_network`, hard, and quarantines the rollout, so
   every native rollout without this file is thrown away.
7. **A native container topology.** `rollout.py` still refuses
   `--topology container` for any driver but `mcp`, because the terminal is only
   safe inside a probed boundary and `compose.native.yml` is yours. When it
   exists, the harness needs `container_episode`-shaped equivalents of
   `containment_probe()` and P6, and this file's owner will wire the driver up.
8. **Postgres and MinIO identity.** The harness ships `PGUSER=alkera`,
   `PGDATABASE=alkera`, bucket `alkera`, password/keys `alkera-local-dev`. Say
   so if the stack uses different ones; `--native-endpoints` can carry
   `{"postgres": {...}, "s3_bucket": "...", "s3_access_key": "..."}`, but the
   defaults should be right.
9. **The bucket must hold at least one object**, and MinIO must answer a SigV4
   path-style `HEAD <bucket>` and `list_objects_v2` from those credentials. The
   s3 surface check signs with boto3 rather than issuing raw HTTP, because an
   unsigned list answers 403 and a 403 says nothing about what is in the bucket.
   An existing but EMPTY bucket is recorded `no_content` and is DEAD.

## AUDIT

10. **Row shape.** `blindness._unattributed` reads `tool`, `op_class`
    (`write/ddl/destructive/exec` counts as a write) and `targets`, and reads a
    row's targets as WORKSPACE-relative unless `tool == "bash"`. Native rows that
    name a file must give a path relative to the workspace.
11. `native.audit.record` must keep honouring `ALKERA_NATIVE_AUDIT` and must keep
    flagging a row it could not place as `"unrecorded"`. The harness asserts both
    (`test_native_driver.py`): a row silently dropped is an episode silently
    unobserved.
12. **Workspace-plane rows are no longer requested of you.** The harness now
    closes that gap itself — `rollout._stage_workspace_edits` appends one
    `workspace.edit` row per changed file after teardown, and the strict
    off-channel rule applies only to the served planes. PATH shims for binaries
    that write into the tree would still improve resolution (see
    NATIVE_DRIVER.md's weakest finding: the transcript records commands, not
    effects), but nothing is blocked on them.

## Task packages (whoever owns a task.toml)

13. **`[harness].warehouse_schemas = ["ops", "mart"]`**, optional. The native
    warehouse check looks for a table with at least one row in these schemas; a
    task that declares none gets the broader check over every non-system schema.
    The harness reads this off `TaskSpec.raw` and never invents it.
14. **Graders must suffix-match tool names.** A native row is
    `<service>.<mcp_tool>` wherever an MCP equivalent exists — `deploy.deploy_run`,
    `jira.issues_list` — so a grader asserting `tool == "deploy_run"` matches
    nothing in a native episode. Not a harness change; listed here because the
    harness is where the naming is visible.
