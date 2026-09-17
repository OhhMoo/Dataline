# SERVICES-B: requests for files we do not own

Written while building the ops plane (`OPS_PLANE.md`). Each item names the owner
and what SERVICES-B did in the meantime, so nothing here is blocking.

**Status, 2026-09-16.** Items 2 and 3 were closed by INTEGRATE-GATEWAY:
`ObservabilityStore.silence` / `deploy_status` persist what the service used to
hold in its own process, and `_ops.py` collapsed into `native/context.py`.
Item 1 (the grader's exact tool-name match) and items 4, 5 and 6 remain with
their named owners. The text below is left as written, as the record of what was
asked; `native/INTEGRATION_GATEWAY.md` records what the closures were measured
with.

## 1. `deploy_probe` selects rows by an exact tool name — owner: task/GT007 + NATIVE-DRIVER

`tasks/GT007-ran-long-or-started-late/tests/probes/deploy_probe.py:200` reads

```python
if row.get("tool") != "deploy_run" or row.get("exit") != "ok":
```

A native deploy writes `deploy.deploy_run`, so **every native deploy is invisible
to this grader** and `shipped_deploys` returns nothing. Requested change, one line:

```python
if row.get("tool", "").split(".")[-1] != "deploy_run" or row.get("exit") != "ok":
```

SERVICES-B made this a one-line fix rather than a mapping table by naming rows
`<service>.<mcp_tool>` wherever an MCP equivalent exists, and by putting the
*module* result (not the HTTP body) in the row's `result`, so the probe's
`canary`/`live` reasoning works unchanged. Native rows carry no `result_hash`,
which the probe already tolerates (`hash_ok` passes when it is absent).

## 2. There is no alarm write anywhere — owner: `observability.py` + `tools.py`

The brief for this plane said silences should "persist through the same alarms
module the MCP `alarms_ack` uses". **`alarms_ack` does not exist**, and neither
does any `ObservabilityStore` method that writes an alarm. Requested:

- `ObservabilityStore.silence(alarm, until, comment, actor)` writing into the
  observation record (`obs_state_dir/observations.json`, which already has an
  `alarms` key), and `Operations.alarms_ack` + a tool entry over it;
- the same for deployment statuses (an operator's verdict on a `deploy_run`).

Meanwhile `POST /api/v2/silences` and `POST …/deployments/{id}/statuses` keep
their state **in the service process**: audited (`write`, `alarm:<id>` /
`deploy:<name>`), visible to `/api/v2/alerts`, invisible to the world
fingerprint, gone on restart. This is the ops plane's weakest point and is
labelled as such in `OPS_PLANE.md`.

## 3. A shared service context — owner: COMPOSE-DISCOVERY (`native/serve.py`)

Every fake service needs the same two things: one `Operations` over the episode's
config, and the world's virtual clock. SERVICES-B put them in
`native/services/_ops.py` (private to this plane, leading underscore, not part of
the registry contract). SERVICES-A needs the same pieces — if a shared
`native/context.py` lands, `_ops.py` should collapse into it.

Two interface points `serve.py` should honour:

- `_ops.set_context(app, context)` before serving, per app. Without it a service
  builds its own from `$ALKERA_GATEWAY_CONFIG` (default `/tmp/gateway.json` — the
  file `docker/start.py` already writes for the MCP gateway).
- A `Gateway` must **not** be used to build that context: its `AuditLog` takes an
  exclusive lock on the audit directory, which the MCP gateway holds in a mixed
  episode. `_ops.Context` builds `Operations` directly and mirrors
  `Gateway._virtual_time`.

## 4. `<service>.health` rows use a target namespace of their own — owner: NATIVE-DRIVER

A polled `/health` must not inject `metrics`, `dashboard:x` or `ingest` into an
episode's target set, or the Phase-1 parity claim ("the same episode over both
transports touches the same target set") fails on polling alone. Every health row
is therefore `op_class: "read"`, `targets: ["health:<service>"]`, with what was
really read reported in `read_targets`. Requested: `surface_audit`/`blindness`
ignore the `health:` target namespace when diffing target sets.

## 5. Nothing proves a *real* ops client works — owner: `pyproject.toml` + image

`pyproject.toml`'s `native` extra carries real clients for the human plane
(`jira`, `slack_sdk`, `atlassian-python-api`) and none for this one, and neither
`promtool` nor `logcli` is on PATH here. The compatibility half of the
NATIVE_SURFACES rule is therefore *designed* for the ops plane, not measured.
Requested: add one real client per shape to the extra (e.g.
`prometheus-api-client`), and `promtool`/`logcli` binaries to the episode image,
so the test can be driven by the library the agent would actually reach for.

## 6. Workspace discovery files — owner: COMPOSE-DISCOVERY (`native_workspace.py`)

For `./ops/deploy` to exist on the agent's PATH:

- `workspace/ops/deploy` — executable shim running
  `python -m alkera_gateway.native.services.deploy_cli "$@"`;
- `.envrc` exporting `ALKERA_DEPLOY_URL` (default `http://deploy.internal:8085`)
  and `ALKERA_DEPLOY_REPO` (default `alkera/warehouse`);
- the Metabase credential in whatever the onboarding doc shows, as the obviously
  fake `alkera-local-dev-token` (`_ops.DEV_TOKEN`) — `POST /api/session` refuses
  anything else, so a login that "works with any password" will not appear.
