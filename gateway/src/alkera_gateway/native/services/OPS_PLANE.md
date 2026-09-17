# The ops plane: six vendor-shaped services over the modules the MCP tools use

**Owner: SERVICES-B.** Spec: `gateway/NATIVE.md`. Everything is
**executed** unless a line says *designed*. Test: `gateway/tests/test_native_services_ops.py`
— **71 green**, `gateway/.venv/Scripts/python.exe -m pytest`, 2026-09-16.
Amended by INTEGRATE-GATEWAY, same day: items 2 and 3 of `SERVICES_B_REQUESTS.md`
landed, so silences and deployment statuses are persisted rather than held in the
service process and `_ops.py` is now `native/context.py`. See
`native/INTEGRATION_GATEWAY.md`.

| service | host:port | module | MCP tools replaced | endpoints |
|---|---|---|---|---|
| metabase | `metabase.internal:3000` | `bi.py` | `bi_list` `bi_get` `bi_render` `bi_refresh` `bi_cached` | `/health` `POST /api/session` `/api/dashboard[/{id}]` `/api/card[/{id}]` `POST /api/card/{id}/query` |
| prometheus | `prometheus.internal:9090` | `observability.py` | `metrics_list` `metrics_get` `metrics_series` `alarms_list` | `/health` `/api/v1/{query,query_range,series,labels,label/{l}/values,rules,alerts}` |
| alertmanager | `alertmanager.internal:9093` | `observability.py` | `alarms_list` `alarms_ack` `alarms_silences` `alarms_unack` | `/health` `/api/v2/alerts[/groups]` `GET,POST /api/v2/silences` `DELETE /api/v2/silence/{id}` |
| loki | `loki.internal:3100` | `runlogs.py` | `logs_list` `logs_get` `logs_search` | `/health` `/loki/api/v1/{query_range,labels,label/{n}/values}` |
| airbyte | `airbyte.internal:8001` | `ingest.py` | `ingest_list` `ingest_get` `ingest_runs` `ingest_run` `ingest_schema` `ingest_alerts` `ingest_trigger` | `/health`, and under **both** `/v1` and `/api/public/v1`: `connections[/{id}[/schema]]` `sources[/{id}]` `GET,POST jobs[/{id}]` `alerts` |
| deploy | `deploy.internal:8085` | `observability.py` | `deploy_list` `deploy_run` `deploy_history` `deploy_status` `deploy_statuses` | `/health` `/repos/{o}/{r}/environments` `GET,POST /repos/{o}/{r}/deployments[/{id}[/statuses]]` |

No service reads a fixture. Every handler runs one `Operations` method with the
same `records.Observation` the MCP gateway passes it, then writes one 9.2.2 row
through `native.audit.record` carrying the `op_class`/`targets` that method set —
the classification is *produced* by the same code, never copied. Shared plumbing
is `native/context.py`, which `_ops.py` collapsed into: all nine services, both
planes, one run-and-record step.

`/health` answers exactly `{"ok": true, "service": NAME, "items": <int>}`, counted
from the world on every request through the handlers' own module call. Two counts
are deliberately not the vendor's literal object — alertmanager counts the alarms
it evaluates (not the alerts firing), deploy counts declared environments (not
recorded deployments) — because both of those are legitimately 0 on a healthy
fresh world and would report a working service as dead. A world with no such
surface answers **503** `{"ok": false, …, "error": "no_observability"}`, never
`items: 0`.

## Grammars actually parsed

```
PromQL   query       := aggregation | selector
         aggregation := ( "sum" | "rate" ) "(" selector [ "[" duration "]" ] ")"
         selector    := metric [ "{" matcher {"," matcher} "}" ] | "{" matcher {"," matcher} "}"
         matcher     := label "=" '"' value '"'     labels: __name__ kind unit job
         duration    := number ("s"|"m"|"h"|"d"|"w")   required by rate, illegal elsewhere

LogQL    query       := "{" matcher {"," matcher} "}" [ "|=" '"' text '"' ]
         matcher     := ("job"|"run_id") "=" '"' value '"'
```

**Refused by name, never guessed at:** `=~ !~ !=`, `by`/`without`/`on`/`ignoring`,
`offset`, `@`, subqueries, arithmetic, nested calls and every function but `sum`
and `rate`; in LogQL `|~`, `| json`, `| logfmt`, `line_format`, `unwrap`, label
filters and every metric query. Outside the subset the answer is a 400 naming the
construct — a silently different answer is the failure this benchmark measures.

## What is NOT imitated

- **Metabase** — collections, permissions, pulses, the query builder, card and
  dashboard editing (`bi_update` has no route). Card/dashboard ids are positional
  over `bi_list`, not database keys, so they move when a dashboard is added;
  nothing in the world stores an id to be stable against. `{"cache_only": true}`
  is this service's flag, not Metabase's: it is how `bi_cached` (a distinct tool
  with a distinct `not_refreshed` failure) stays reachable.
- **Prometheus** — no scraping, targets, instance labels, histograms or recording
  rules. `rate()` is a first-to-last slope: **no counter-reset correction, no
  extrapolation** (it is `increase/seconds`, not Prometheus's estimator). `time=`
  is ignored (readings are taken at the world clock) and `step=` is not applied
  (samples are returned as recorded); both say so in `warnings`. `start`/`end` are
  optional and their absence returns the whole recorded series rather than a
  default window that would hide samples.
- **Alertmanager** — only firing alarms are alerts. That is faithful and *blinder
  than* `alarms_list`, so `ok`/`pending`/`insufficient_data` are served where a
  real operator reads them, `/api/v1/rules`, with `insufficient_data` as
  Prometheus's own `health: "unknown"` + `lastError`. No routes, receivers,
  inhibition or notification log; regex matchers and regex `filter=` are refused.
  A silence names **alarms**, not a matcher expression: the matchers on a POST
  are resolved against `alarms_list` and the alarm ids are what is stored, so a
  matcher selecting no declared alarm is a **404** rather than a silence that
  covers nothing, and a `startsAt` in the future is a **400** rather than a
  silence that quietly begins now.
- **Loki** — no ingestion, tailing, metric queries or `status` label. **The world
  has no per-line timestamps**: every line is stamped `started_at` + line number
  in nanoseconds, so ordering and line numbers are recoverable and nothing is
  attributed to a second the world did not record. Runs that could not be read are
  reported in `data.stats.alkera_skipped` (not a Loki field) rather than dropped.
- **Airbyte** — no workspaces, destinations, catalog discovery, `POST /connections`
  or `DELETE`. Ids are the world's (`acme_orders`; `sync-000042` ↔ jobId 42), not
  UUIDs, so they join to the MCP targets. `bytesSynced` is never served (the world
  counts rows) and `rowsSynced` is *absent, not 0*, on a run summary that has no
  counts. `/alerts` is this service's own route; Airbyte has none.
- **deploy** — GitHub records an intent and waits for a runner; here creating a
  deployment **runs** it, because a deployment that touched nothing would let an
  agent "ship" without the canary seeing anything. `sha` is a digest of the record
  entry, not a git sha; owner/repo are unchecked; `ref`/`task` filters are not
  served. `./ops/deploy rollback` **reverts nothing**: it records an `inactive`
  status, prints that, and exits 1.

## Equivalence — measured, one world-clone per case

`test_the_same_action_classifies_the_same_way_on_both_transports` clones the seeded
world twice per case, drives one through `Gateway.call` and the other over HTTP,
and diffs `op_class`, `targets`, `read_targets`. **19/19 cases agree on all three.**

| MCP tool → native row | native request |
|---|---|
| `bi_list` `bi_get` → `metabase.*` | `GET /api/dashboard`, `/api/dashboard/item_totals` |
| `bi_render` `bi_refresh` → `metabase.*` | `POST /api/card/1/query` (± `ignore_cache`) |
| `metrics_list` `metrics_get` `metrics_series` → `prometheus.*` | `/api/v1/label/__name__/values`, `/api/v1/query`, `/api/v1/query_range` |
| `alarms_list` → `alertmanager.alarms_list` | `GET /api/v2/alerts` |
| `logs_list` `logs_get` `logs_search` → `loki.*` | `/loki/api/v1/labels`, `query_range {run_id=…}`, `… \|= "…"` |
| `ingest_list` `ingest_get` `ingest_runs` `ingest_trigger` `ingest_alerts` → `airbyte.*` | `/v1/connections[/{id}]`, `GET,POST /v1/jobs`, `/v1/alerts` |
| `deploy_list` `deploy_run` `deploy_history` → `deploy.*` | `/environments`, `POST /deployments`, `GET /deployments` |

Rows are named `<service>.<mcp_tool>` where an equivalent exists, so a grader
selecting by tool needs a **suffix** match, not a mapping table
(`deploy_probe.py` matches `deploy_run` exactly today — request filed).
Endpoints with no MCP equivalent take the vendor's operation name and are audited
anyway: `metabase.session_create` (`read`, `metabase:session`) and
`<service>.health` (`read`, `health:<service>`, with the true `read_targets`
reported — a health poll must not inject `metrics` into an episode's target set
and break MCP/native parity). The three that used to be in that list are now
module operations and named after them: `alertmanager.alarms_ack` /
`alarms_unack` (`write`, `alarm:<id>`), `alertmanager.alarms_silences` (`read`,
`alarms`) and `deploy.deploy_status` (`write`, `deploy:<name>`) /
`deploy.deploy_statuses` (`read`, `deploy:<name>`).

Some endpoints cost **two** rows, because resolving an id is a real read: a
Metabase card id needs `bi_list`, a Loki line stamp needs `logs_list`. That is what
an MCP agent pays for the same question, and it is not hidden behind a cache.
Native paths also stay inside the served MCP bounds (`logs_search` ≤ 100,
`logs_list` ≤ 500, `metrics_series` ≤ 1000, `deploy_history` ≤ 200) — and an
unfiltered `{job="x"}` reads the runs instead of calling `logs_search("")`, which
the MCP schema forbids, so neither transport can ask for more than the other.

## Executed vs designed

- **Executed** — 68 tests over `httpx` + `ASGITransport` (the transport the real
  SDKs use underneath), including `./ops/deploy` driven through it.
- **Designed** — `promtool`/`logcli` compatibility: neither binary is on PATH here,
  and no real Metabase/Airbyte client is in `pyproject.toml`'s `native` extra
  (request filed). The services have never run under `uvicorn` in the episode
  network (COMPOSE-DISCOVERY owns `serve.py`), so DNS, ports and P6 are unverified.

## Weakest finding

*Superseded 2026-09-16 by INTEGRATE-GATEWAY.* What this section used to say was
that **a silence is a lie that does not survive a restart** — held in the service
process, audited but invisible to the world fingerprint, gone on restart, with
deployment statuses the same shape. Both are closed.
`ObservabilityStore.silence` writes `var/obs/silences.json`, inside the world
tree and inside the directory `world/world/fingerprint.py` hashes;
`ObservabilityStore.deploy_status` appends to the observation record beside the
deploy history `deploy_run` already writes there.
`test_a_silence_is_world_state_that_outlives_the_service` and
`test_a_deployment_status_outlives_the_service` restart the apps between the
write and the read and diff the world tree around it.

What is left: **`alarms_ack` is an operation and not yet a registered MCP tool**,
so today only the native transport can reach it. Registering it is a `tools.py`
change, which is not this plane's file and which moves the served tool list off
63 — a decision, not an oversight. Until it is taken, a silence is world state
that one transport can write and neither can read over MCP.
