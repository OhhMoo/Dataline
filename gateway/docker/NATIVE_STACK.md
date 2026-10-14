# The native-mode stack

Spec: `gateway/NATIVE.md`. Harness side: `docs/implementation_plan/harness/NATIVE_DRIVER.md`.
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
| `powerbi.internal` | 8095 | native-services | `agent_under_test.PLUG_SERVICES`; **only** under a profile that plugs it (*designed*) |
| `tableau.internal` | 8096 | native-services | same |
| `sigma.internal` | 8097 | native-services | same |
| `native-services.internal` | 8099 | native-services | ours: the aggregate `/health` |
| `databricks.internal` | 443 | native-services | **live** for a profile plugging `warehouse: databricks` (hosted backend, `alkera_gateway.backends`; HTTPS with a certificate minted at boot, trust volume `databrickstrust`; seeded from the world's DuckDB into service state; one `databricks.statement` row per statement; `tasks/harness/databricks_e2e_check.py`, evidence `tasks/harness/evidence/databricks_e2e.json`); 443 and not 8098: `docs/environments/DATABRICKS_SPIKE.md`; containment P6 `--attach` on de/databricks passed 2026-10-13 (`evidence/containment-native-de-databricks.json`) |
| `postgres.internal` | 5432 | postgres | the warehouse, served. **Audited at teardown**: `NativeContainerEpisode.down()` runs `native/tailers/postgres.py` over the role-scoped statement log inside native-services before anything is torn down, one `psql.statement` row per `alkera` statement (`tasks/harness/postgres_live.py`). **Seeded** with the world's `warehouse.*` only for a profile plugging `warehouse: postgres` (`de/postgres`); every other box keeps compose's `warehouse.items` (2 rows). psql, psycopg and dbt-postgres live: `tasks/harness/postgres_e2e_check.py`, evidence `tasks/harness/evidence/postgres_e2e.json`. Its connections are not counted in `destinations.jsonl` |
| `minio.internal` | 9000 | minio | object storage, bucket `alkera` |
| `airflow.internal` | 8080 | airflow | *designed*, `--profile airflow` (real headless Airflow; never run) |
| `airflow.internal` | 8100 | native-services | **live** for a profile plugging `orchestrator: airflow`: the Airflow 3 REST v2 fake over the world ledger (`native/services/airflow.py`, `world/world/airflow_view.py`), a hosted row of `alkera_gateway.backends.BACKENDS`; its alias and variables come from the registry, `./ops/sched` and `sched.internal` are dropped; `tasks/harness/airflow_e2e_check.py`, evidence `tasks/harness/evidence/airflow_e2e.json`; `containment.NATIVE_BACKENDS` key `airflow-api`, P6 `--attach` on de/laptop with `orchestrator=airflow` passed 2026-10-13 (`evidence/containment-native-de-laptop-airflow.json`) |
| `snowflake.internal` | 8443 | snowflake | **live** for a profile plugging `warehouse: snowflake` (compose profile `snowflake` via `container_episode.compose_env`; seeded from the world's DuckDB, one `snowflake.statement` row per statement; `tasks/harness/snowflake_e2e_check.py`, evidence `tasks/harness/evidence/snowflake_e2e.json`); containment P6 `--attach` on da/snowflake passed 2026-10-13 (`evidence/containment-native-da-snowflake.json`) |
| `bigquery.internal` | 9050 | native-services | **live** for a profile plugging `warehouse: bigquery` (`de/gcp`): the audited BigQuery REST proxy (`native/services/bigquery.py`, hosted row in `alkera_gateway.backends`), in front of compose.yml's `bigquery` emulator, which `container_episode.HOSTED_ENGINES` starts (compose profile `bigquery`) on backend_net only - native-services joins backend_net and reaches it as `bigquery:9050`, the episode cannot. Seeded from the world's DuckDB at native-services' import, before any port binds; one `bigquery.statement` row per query; `tasks/harness/bigquery_e2e_check.py`, evidence `tasks/harness/evidence/bigquery_e2e.json`; containment P6 `--attach` on de/gcp passed 2026-10-13 (`evidence/containment-native-de-gcp.json`) |
| `spark_connect.internal` | 15002 | native-services | **live** for a profile plugging `compute: spark_connect` (`ds/databricks`): Databricks tier 2, the audited Spark Connect gRPC proxy (`native/services/spark_connect.py`, served through its own `serve_socket`, behind the episode-peer filter) in front of the `spark` engine on backend_net only (`container_episode.HOSTED_ENGINES`); one `databricks.spark` row per plan; `tasks/harness/spark_e2e_check.py`, evidence `tasks/harness/evidence/spark_e2e.json` |
| `jupyter.internal` | 8888 | jupyter | the notebook plug (da/ds): jupyter-server serving only the audited `alkera-python` kernel (`gateway/docker/jupyter/README.md`) |
| `redshift.internal` | 5439 | redshift | **live** for a profile plugging `warehouse: redshift` (`de/aws`; the whole service is the per-rollout overlay's, `container_episode.backend_overlay`, compose profile `redshift`): the audited wire proxy in front of PostgreSQL 17, which opens no TCP port at all - only a unix socket in a 0700 directory of its own that the proxy dials - so it is never on `episode_net` and nothing reaches it without a row; seeded from the world's DuckDB (database `dev`), one `redshift.statement` row per statement; `tasks/harness/redshift_e2e_check.py`, evidence `tasks/harness/evidence/redshift_e2e.json`; containment P6 `--attach` on de/aws passed 2026-10-13 (`evidence/containment-native-de-aws.json`) |

One port per service, not one socket with `Host:` routing. The registry already
declares `PORT` per service; P6's "TCP to anything but the listed ports is
refused" is vacuous on a single socket; and a `Host` header is the client's
claim while the accepted socket's local port is the server's observation. The
full argument is in `native/serve.py`'s docstring.

Nothing publishes a host port (`published_host_ports: {}` — measured).

The three BI vendors are in no static file. An unprofiled stack serves the nine
and resolves none of them; `container_episode.profile_overlay` adds a vendor's
alias to native-services' `aliases: !override` list and its variables to the
episode's (and jupyter's) environment only when the task's profile plugs it —
the notebook plug's rule (`gateway/NATIVE.md`, "explicit-only plugs").

**A profiled environment is restated whole.** Under a profile the per-rollout
overlay writes `environment: !override` with the complete mapping for
`episode` and `jupyter` — the static table (compose.yml + compose.native.yml)
minus the excluded services' variables (`SCHED_URL` without `ops_sched`,
`AWS_*` without an object store), plus the plugged vendors', plus the overlay's
own lines (`container_episode.profiled_environment`). The first design wrote
`VAR: !reset null` per excluded variable; measured 2026-10-04 with `docker
compose config` on Compose v5.6.0, those lines did nothing on the episode:
`SCHED_URL`, `METABASE_URL`, `PROMETHEUS_URL`, `AWS_ENDPOINT_URL`, … were all
still set on a da/laptop box that resolves none of them.
`test_profile_wiring.py::test_compose_config_drops_excluded_variables` runs
`docker compose config` (client side, no daemon) and asserts the profiled
environment is exactly the unprofiled one minus what the profile drops plus
what it plugs. An unprofiled overlay is the plain merge it always was.

**The world's clock.** The overlay also sets `ALKERA_VIRTUAL_TIME` in `episode`
and `jupyter` to the world's `vnow` as the episode starts
(`container_episode.world_clock`). The shims and the notebook kernel stamp rows
through `native.audit.virtual_time`, which reads `vnow` from the state file
`ALKERA_SCHEDULER` names on the host topology; here `var/sched` is not mounted
(P7), so every in-container row carried `GatewayConfig`'s literal
`2026-03-05T09:00:00Z` (BI001's world is at `2026-03-02T00:00:00Z`). A
`./ops/sched trigger` that moves `vnow` during the episode is not followed: the
services' rows carry the moved clock, the shims' rows the starting one.

**The services' own state is not in `/audit`.** `native-services` starts
through `native_services.py` (this directory, `/opt/stack` in the image), which
writes the services' gateway config into `/service-state` with
`bi_state_dir: /service-state/bi` and execs serve.py with it. `/service-state`
is `<rollout>/service-state` (`ALKERA_EPISODE_SERVICE_STATE`, set by
`NativeContainerEpisode.compose_env`), mounted into native-services and into no
other service. Before 2026-10-04 serve.py's defaults put the BI vendors'
published state (`/audit/bi`, which `tasks/harness/bistate.py` stages for the
grader as `{bi_state}`) and the config every service re-reads per request
(`/audit/native_gateway.json`) in `/audit` — which the episode mounts
read-write — so both were forgeable from the subject's shell. P7 now asserts
the absence from inside (question H) and from `docker inspect` of the whole
project (`services_state_is_mounted_only_in_native_services`).
`containment.py --native` (P6 grades nothing) makes itself a temporary one when
the variable is unset.

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
  violation on every rollout that touches storage. The episode can still
  *reach* it, so since 2026-10-07 it answers only a POST carrying the
  per-rollout `ALKERA_MINIO_AUDIT_TOKEN`, which MinIO's webhook sends as its
  `Authorization` header (`MINIO_AUDIT_WEBHOOK_AUTH_TOKEN_alkera`); anything
  else is a 401 with no row. Without a token serve.py serves no receiver.
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

### Since 2026-10-13: P6 on profiled boxes, and the box sweep

The 2026-09-16 run measured the static stack only. Three instruments now
measure a box a profile is served (`evidence/CONTAINMENT_2026-10-07.md`, "For
Andrew", points 1-5):

- **`containment.py --native --attach <project>`** runs P6 against a stack that
  is already up (a rollout's, the tool tour's) and leaves it as it stands,
  reading the environment profile off native-services (`--env-profile` /
  `--env-plug` pin it, and must agree). `p6_plan` picks backends by the service
  that carries each alias (`containment.NATIVE_CARRIERS`; a hosted row is
  native-services'), gives compose names only for services on `episode_net`,
  drops from the controls a name that is on `episode_net` (`snowflake`) and adds
  every service off it (the BigQuery and Spark engines).
- **`native_probe.py --sweep-cidr`** (question D) dials every address of the
  episode network, the bridge gateway included, on every port of each live one.
  An open port no expected door explains is a labelled `not_audited` row;
  `passed` stays P6's claim, and `every_open_port_is_an_expected_door` is
  reported beside it.
- **`box_sweep.py`**, the box driver, kept outside the repository until
  2026-10-13. It writes `evidence/containment-native-box[-<slug>].json`:
  - P6's probe on the box, with the full sweep;
  - the names the episode resolves, connects to every container's
    `backend_net` address, and Postgres logins as `episode` and as `alkera`,
    each looked up in the role-scoped statement log;
  - each open port classed as an audited door, a designed door (the notebook
    plug) or NOT an audited door;
  - `not_audited`: the last kind, plus any login that leaves no statement.

  `--project` attaches to a live stack. `--up --env-profile <role/stack>` (or
  `--task`) builds the world and stack as the tool tour (or `container_episode.py
  native-up`) does, runs P6 `--attach`, P7 and the sweep, and tears it down.
  It dials every port, so the destination counts it reads are partly its own:
  run it under `scripts/docker_slot.sh` on a stack built for measuring, never on
  a rollout that will be scored.

Measured 2026-10-13: P6 `--attach` passes on the unprofiled stack, da/snowflake,
de/aws, de/databricks, de/gcp, de/laptop + `orchestrator=airflow`, de/postgres
and ds/databricks (`evidence/containment-native{,-<slug>}.json`). Every box
sweep lists the host's sshd at `<episode_net>.1:22` (open: the INPUT rule needs
the host owner) and the token-only MinIO audit receiver on `native-services:8110`.
The sweeps before plan Tasks 7-8 also list the `episode` Postgres login and
MinIO's console, both closed since (`tasks/harness/evidence/postgres_e2e.json`,
`tasks/harness/evidence/minio_containment.json`); the ds/databricks sweep, after
them, lists only the two above. The da/snowflake sweep also caught a transient
host listener at `10.210.2.1:49743`, gone seconds later.

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

P7 is profile-aware (2026-10-04). `native_episode_probe.expectations(profile,
plugs)` computes, by serve.py's own `selected_services` / `sched_enabled` /
`object_store_enabled`, which services must answer (plugged BI vendors
included), which must not even resolve (question G: an excluded one of the
nine, an unplugged vendor, `sched.internal` without `ops_sched`,
`minio.internal` without an object store), and whether the scheduler is asked.
A box with no orchestrator records `the_scheduler_plane_answers_with_jobs`
under `checks_not_applicable` with the reason, and `spec()` refuses that
unless `sched.internal` is in the must-not-resolve list — a check replaced by a
stronger one, never switched off. `containment.py --native-episode` reads the
profile off the running native-services' `--profile/--plug` (or
`--env-profile/--env-plug`, refused if they disagree);
`evidence/containment-native-episode-bi001.json` is BI001's da/laptop box.

Measured by `gateway/docker/native_episode_probe.py` from inside that container,
through `containment.py --native-episode`:
`evidence/containment-native-episode.json`, **passed: true** — `/world` holds the
mounted warehouses and nothing else, no grading, rollout, canary, marker or
repository path exists, the ledger is present, empty and read-only, the episode
can still work, every alias is reachable, the scheduler plane answers with
jobs, egress is refused, and `published_host_ports` is `{}`. The recipe and the acceptance table are in
`docs/implementation_plan/harness/NATIVE_CONTAINER.md`.

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

On a profiled box without an object store it was worse than a tell: the
overlay dropped only `minio.internal`, and `minio` still reached a MinIO whose
audit receiver was not served (`evidence/CONTAINMENT_2026-10-07.md`, finding 1).
Since 2026-10-07 such a box does not start MinIO at all
(`container_episode.profile_overlay`), measured by
`tasks/harness/minio_containment_check.py`.

Runner-up, **closed 2026-09-16**: `<root>/home` is now a conditional
`fingerprint.py` section (`docs/implementation_plan/gateway/COMPOSE_REQUESTS.md` item 2), so a home directory
edited after the build no longer passes `semantic_hash`. Measured three ways:
the default seed rebuilds to `85ce4cbbb86d6667…`, byte-for-byte the
`gate1_host_semantic_hash` in `world/evidence/acceptance.json` (no `home/`, no
section, nothing moved); the flagged build hashes `252d5a5c3821c3d9…` (it was
`51a48a0d…` before the section existed, and that number is superseded, not
contradicted); and appending one line to the flagged world's
`home/.dbt/profiles.yml` moves it to `d9a82badaf20f10e…`.
