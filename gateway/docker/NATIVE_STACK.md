# The native-mode stack

Spec: `gateway/NATIVE.md`. Harness side: `tasks/harness/NATIVE_DRIVER.md`.
Owner: COMPOSE-DISCOVERY. **Measured 2026-09-16 unless a line says *designed*.**

    docker compose -p <project> \
      -f compose.yml -f compose.episode.yml -f compose.native.yml up -d --wait

## Topology

```
                         episode_net  (internal: true, no route out)
  +-------------+        .-------------------------------------------.
  |  episode    |--------| resolves: <name>.internal                  |
  |  bash only  |        |           + the compose service names      |
  |  HOME=/root |        |             (measured deviation, below)    |
  | /workspace  |        '-------------------------------------------'
  | the .duckdb |             |               |                |
  | files, /audit  and nothing else - P7      |                |
         v                    v               v                v
  +----------------+  +----------------+  +--------+  +------------------+
  | native-services|  | postgres       |  | minio  |  | airflow snowflake|
  | ONE process    |  | log_statement  |  | audit  |  |  (profile-gated, |
  | 9 uvicorn      |  |   = all        |  | webhook|  |   *designed*)    |
  | servers + the  |  | log -> PGDATA  |  |  ->    |  +------------------+
  | aggregate      |  |    /log        |  | tailer |
  +-------+--------+  +-------+--------+  +---+----+
          |                   |               |
          v                   v               v
   /audit/actions.jsonl  (one 9.2.2 row per action, written server-side)
   /audit/destinations.jsonl (one row per destination, written server-side)
```

`backend_net` still exists and the episode is **not** on it: the overlay sets
`networks: !override [episode_net]`. Everything the agent may reach carries an
`episode_net` alias; anything that does not is unreachable, which is the
property rather than an oversight.

## Ports and aliases

| alias | port | served by | source of the number |
|---|---|---|---|
| `jira.internal` | 8080 | native-services | `agent_under_test.NATIVE_SERVICES` |
| `slack.internal` | 8081 | native-services | same |
| `airbyte.internal` | 8001 | native-services | same |
| `deploy.internal` | 8085 | native-services | same |
| `confluence.internal` | 8090 | native-services | same |
| `metabase.internal` | 3000 | native-services | same (vendor default) |
| `prometheus.internal` | 9090 | native-services | same (vendor default) |
| `alertmanager.internal` | 9093 | native-services | same (vendor default) |
| `loki.internal` | 3100 | native-services | same (vendor default) |
| `sched.internal` | 8120 | native-services | ours: the backend of the workspace's own `./ops/sched` |
| `native-services.internal` | 8099 | native-services | ours: the aggregate `/health` |
| `postgres.internal` | 5432 | postgres | the warehouse, served |
| `minio.internal` | 9000 | minio | object storage, bucket `alkera` |
| `airflow.internal` | 8080 | airflow | *designed*, `--profile airflow` |
| `snowflake.internal` | 8443 | snowflake | *designed*, `--profile snowflake` |

One port per service, not one socket with `Host:` routing. The registry already
declares `PORT` per service; P6's "TCP to anything but the listed ports is
refused" is vacuous on a single socket; and a `Host` header is the client's
claim while the accepted socket's local port is the server's observation. The
full argument is in `native/serve.py`'s docstring.

Nothing publishes a host port (`published_host_ports: {}` — measured).

## The DNS restriction

`networks.<net>.aliases` plus `internal: true`, and no dnsmasq. Docker's
embedded resolver answers the aliases it was given and forwards everything else
to the daemon's upstream resolvers, which sit on a route `episode_net` does not
have — so an unknown name fails. Measured: `example.internal`,
`warehouse.internal`, `s3`, `snowflake`, `acme.snowflake.local`, `example.com`
and `pypi.org` all return `gaierror` from inside the episode container.

## The seed flag

`native: true` in the seed file. OFF by default, on the same terms as `faults:`
and `warehouses:`. `world/world/native_workspace.py` then writes four files:

    <root>/workspace/.envrc                    a delimited block appended
    <root>/workspace/docs/onboarding.md        assembled from var/workflow/*
    <root>/home/.dbt/profiles.yml              COPIED to /root per rollout (P7)
    <root>/home/.config/alkera/services.toml   the same table, machine-readable

Measured (`world/tests/test_native_workspace.py`, 21 tests): the unflagged build
reproduces `85ce4cbb…`, the `gate1_host_semantic_hash` in
`world/evidence/acceptance.json`; two flagged builds are byte-identical; the
flagged world's own hash differs (`252d5a5c…` since the `home` section landed)
because the workspace is fingerprinted, two of the four files live in it, and
the other two are now hashed as `home`. The onboarding page names
only wiki page ids and job names the built world already contains, carries no
`authoring.note` and no ticket body.

## P6 — measured, with a stated scope

`python containment.py --native --project alkera-p6 --out
evidence/containment-native.json`, docker 29.5.2, artifact
`evidence/containment-native.json`, **passed: true**.

Scope, exactly, **re-measured 2026-09-16 by INTEGRATE-STACK**: the nine
services were served as the **real service modules**, not stubs
(`stub_services: ""` in the evidence). `gateway/requirements-native.lock` — `uv
export --locked --extra native` — is installed by the Dockerfile, so
`services.load()` returns nine modules and no `ModuleNotFoundError`; the
startup table in the evidence reads `loaded=10 missing=0`, the tenth being
MinIO's audit receiver on 8110. Each `/health` answered 200 with a real count:
jira 8, slack 3, confluence 9, metabase 1, prometheus 10, alertmanager 5, loki
30, airbyte 2, deploy 1.

The world under it is a `sftp_vendor` build with `native: true`, not the
default `revenue_daily` seed. That is not decoration: `revenue_daily` seeds no
ingest control plane, so `airbyte`'s `/health` answers **503** and P6 fails on
`airbyte.internal` — measured, the run before this one. A service whose world
is empty is a dead surface and says so, which is the content-bearing rule
working; it also means P6 needs a world that seeds all nine planes to be a
claim about all nine.

`airflow` and `snowflake` are profile-gated and were not running, so P6 claims
nothing about them.

### The rest of the stack, measured the same day

- **Postgres logging is role-scoped.** `compose.native.yml` now runs the server
  with `log_statement=none` and `log_min_duration_statement=-1`, and the inline
  init config adds `ALTER ROLE alkera SET log_statement='all'` and
  `... SET log_min_duration_statement=0` (native/AUDIT_REQUESTS.md item 2).
  Measured on the running stack: after one superuser statement and one `alkera`
  statement, `/var/lib/postgresql/data/log/postgresql.log` is 24 lines holding
  exactly **two** statement entries, both the `alkera` role's — the superuser's
  and the entrypoint's provisioning statements are gone from the agent's action
  log.
- **The audit receiver is served, and is not a destination.**
  `serve.py` mounts `tailers.minio.build_app()` on 8110 in the same process
  that writes `actions.jsonl`, so there is one writer for the `seq` lock. It is
  excluded from `destinations.jsonl` on purpose: MinIO dials it and the episode
  never does, and a counted row there is a hard `network_destination`
  violation on every rollout that touches storage.
- **The services carry the world's clock.** `serve.py` derives a
  `GatewayConfig` from `--world` and exports `ALKERA_GATEWAY_CONFIG`,
  `ALKERA_NATIVE_AUDIT` and `ALKERA_SCHEDULER` (SERVICES_A_REQUESTS.md item 5).
  Proof, one row: the first row of the P6 run's `actions.jsonl` carries
  `"vts": "2026-03-03T00:00:00Z"`, which is that world's `var/sched/state.json`
  `vnow` — not `GatewayConfig().virtual_time`, which is `2026-03-05T09:00:00Z`.
  Before this, every service answered 503: `_ops.process_context()` fell back
  to `/tmp/gateway.json`, which the episode image writes only when the MCP
  gateway starts, and nothing starts it here.

What was measured from inside the episode container (`native_probe.py`):

- all nine `<name>.internal` resolve, answer `GET /health` 200 with
  `{"ok": true, "service": …, "items": 1}` — `items > 0`, not merely 200;
- `postgres.internal:5432` and `minio.internal:9000` accept TCP;
- seven control names do not resolve (list above);
- 18 connection attempts to ports 22/80/443/3306/6379/8888 on every address
  that answered: all `ConnectionRefusedError`;
- the container has `lo` and `eth0` only and **no default route**;
- positive control: the same two functions resolve `jira.internal` and connect
  to 8080, so a probe that could see nothing would not have passed;
- `destinations.jsonl` was written before the first socket opened and carries
  ten rows; `native-services.internal:8099` ends at `count: 0` — offered,
  never dialled, which is the distinction the harness reads.

## P7 — the episode's filesystem, measured 2026-09-16

P6 is the network. **P7 is the mounts**, and the two came apart the day
`--driver native` met real models: three of five GT001 rollouts quarantined on a
`cp` of the warehouse file into the world tree and on an `ls` of the rollout
directory, neither of which the network has anything to say about. So
`compose.native.yml` now gives the `episode` service its OWN mount set with
`!override` — `/workspace`, `/audit`, and the read-only ledger tmpfs, and no
`/world` bind at all. The warehouse FILES are added per rollout by
`tasks/harness/container_episode.py`, which writes a fourth compose file into the
rollout directory; `<world>/home` is copied in rather than mounted.

One plane had to MOVE for that mount set to be liveable. `./ops/sched` drives
`var/sched/state.json`, its run logs, `var/obs` and `build/sched`, none of which
is in the episode any more, so `alkera_gateway/native/sched_plane.py` serves the
same three operations at `sched.internal:8120` from this process - which mounts
the whole world - and the CLI switches onto it when `SCHED_URL` is set. Not a
tenth vendor: the aggregate does not report it, P6's registry does not expect
it, and `agent_under_test.NATIVE_SERVICES` is still nine.

Measured by `gateway/docker/native_episode_probe.py` from inside that container,
through `containment.py --native-episode`:
`evidence/containment-native-episode.json`, **passed: true** — `/world` holds the
mounted warehouses and nothing else, no grading, rollout, canary, marker or
repository path exists, the ledger is present, empty and read-only, the episode
can still work, every alias is reachable, the scheduler plane answers with
jobs, egress is refused, and `published_host_ports` is `{}`. The recipe and the acceptance table are in
`tasks/harness/NATIVE_CONTAINER.md`.

Two changes this forced on the stanzas above. The `native-services` healthcheck
no longer dials its own aggregate over HTTP — `serve.py` counts every accepted
connection, so it was putting 23 connections the episode never made into
`destinations.jsonl` and quarantining the rollout; it now reads
`destinations.jsonl` and `/proc/net/tcp` instead. And the `episode` healthcheck
names `/usr/local/bin/python`, because the shim directory is first on that
container's PATH and every health poll was writing a `shim.python` row into the
episode's own action log.

## Weakest finding

**Docker's embedded resolver also answers the compose service names, and there
is no way to turn that off.** `postgres`, `minio`, `native-services` and
`episode` resolve from the episode container alongside their `.internal`
aliases, because Docker registers a container's service name on every network
it joins. `NATIVE.md` says "nothing else resolves", and that sentence
is not literally true today. It is recorded rather than excused: P6 pins each
such name to an address that is already one of this stack's own services, so a
name resolving anywhere else still fails. The cost is a tell — an agent that
tries `dig postgres` learns the box is docker-compose — and the fix is a
resolver of our own (a dnsmasq answering only the alias table, with
`dns: [<its address>]` on the episode service) which was rejected here as one
more container to go wrong for a property nothing else depends on.

Runner-up, **closed 2026-09-16**: `<root>/home` is now a conditional
`fingerprint.py` section (COMPOSE_REQUESTS.md item 2), so a home directory
edited after the build no longer passes `semantic_hash`. Measured three ways:
the default seed rebuilds to `85ce4cbbb86d6667…`, byte-for-byte the
`gate1_host_semantic_hash` in `world/evidence/acceptance.json` (no `home/`, no
section, nothing moved); the flagged build hashes `252d5a5c3821c3d9…` (it was
`51a48a0d…` before the section existed, and that number is superseded, not
contradicted); and appending one line to the flagged world's
`home/.dbt/profiles.yml` moves it to `d9a82badaf20f10e…`.
