# Containment re-measured with every backend: 2026-10-07

This is step 1 of Task 12 in `docs/superpowers/plans/2026-10-07-remaining-tools-and-harbor.md`. P6 and
P7 were re-run for the unprofiled box and six environment profiles. Andrew's tools were run unchanged
(`containment.py`, `native_probe.py`, `native_episode_probe.py`). No repository code changed.

- **Measured at:** `main-2` 7bfe09a, on branch `evidence/containment-p6`.
- **Host:** docker 29.8.2, Compose v5.6.0, linux amd64 (kernel 7.0.0-1006-aws).
- **Images:** `alkera-episode:head*` (`org.alkera.source.revision` 5ed3ed5), tagged `alkera-episode:p6*`
  for this run so that the backend images built on top of them got tags of their own.
  - Drift between 5ed3ed5 and 7bfe09a in the image scope: the Databricks run-now queues
    (`native/services/databricks.py`, `world/world/gateway_scheduler.py`), the note text in `plugs.py`,
    and `{redshift,snowflake}_start.py`.
  - The two backend images are built from the working tree at 7bfe09a (`pull_policy: build`).
  - None of these changes touches a port, an alias, a network or a mount.
- **The services-config lane** (native services answering 500 on warehouse profiles) did not interfere.
  Every P7 `/health` returned 200, or 503 for a plane the world does not seed. None returned 500.

## Read this first

1. **REGRESSION. On every `da/*` box, MinIO is reachable and unaudited.** This was measured on
   `da/snowflake`. By `serve.object_store_enabled`, all six `da/*` profiles plug no `object_store`,
   and that includes BI001/BI002's `da/laptop`.
   - `profile_overlay` removes the `minio.internal` alias (`aliases: !reset []`), and P7's question G
     passes. MinIO, however, stays on `episode_net`, and Docker still resolves its compose name `minio`.
   - With no store plugged, `serve.py` hosts no `:8110` receiver. The webhook fails with *"connection
     refused"* and every MinIO event goes unrecorded.
   - Tried from the episode (`containment-native-minio-da-snowflake.json`), using the stack's shared
     credential `alkera-local-dev`: ListBuckets, PutObject, GetObject, ListObjects and DeleteObject all
     succeeded. The run wrote **0 rows** to `actions.jsonl`, and nothing was counted in
     `destinations.jsonl`.
   - On the unprofiled box, `:9000` is an audited door, so on `da/*` this is a reachable destination
     that no audit records.
   - It dates from the profile overlay (2026-10-04), not from this plan's `NATIVE_BACKENDS` rows.
2. **The host's sshd is reachable from every episode container.** This holds on all 7 boxes, the
   unprofiled one included, so it is pre-existing and not a profile regression.
   - `episode_net` is `internal: true`, but its bridge gateway `<subnet>.1` is the host. Port 22 answers
     `SSH-2.0-OpenSSH_10.2p1 Ubuntu-2ubuntu3.6`.
   - On the host, `ss -ltn` shows sshd as the only TCP listener that is not bound to loopback. Any service
     that someone later binds to `0.0.0.0` will be reachable from the agent's box too.
   - P2, P6 and P7 never probe the gateway address.
3. **No new door comes from the new backends.** On the rollout-built boxes:
   - Every backend that a profile plugs is reachable at its audited port: Snowflake 8443, Redshift 5439,
     Databricks 443, the BigQuery proxy 9050, and the Airflow REST fake 8100.
   - The backend containers expose nothing else. All 65535 ports were scanned on each: Snowflake has
     only 8443 and Redshift only 5439.
   - The BigQuery engine is unreachable: its name does not resolve and its `backend_net` address has no
     route.
   - Every `backend_net` address of every box failed with `OSError`: 18 to 36 attempts per box.
4. **Unprofiled baseline: the verdicts are unchanged.** P6 passes with the same services, controls,
   compose names, closed ports, netns and self-test. The only difference is that
   `backends_declared_but_not_running` now also lists `airflow-api`, `bigquery`, `databricks` and
   `redshift`. P7 passes with all 12 checks identical. Both files were updated in place.

## The three instruments, per profile

| file | instrument | what it can say |
|---|---|---|
| `containment-native[-<slug>].json` | **P6 as written**: `containment.py --native --project alkera-p6m-<slug> [--profile <compose profile>]` | the static stack (compose.yml + episode + native overlays) plus the compose profiles that `container_episode.compose_env` would enable for the environment profile: `snowflake`, `bigquery`, and `redshift`, which selects nothing in the static files. The world is the `sftp_vendor` `native: true` build (seed 1), as in the committed baseline, because P6's registry expectation needs all nine planes seeded. `ALKERA_EPISODE_IMAGE` is the profile's image. `ALKERA_NATIVE_PROFILE_ARGS` is empty: **P6 has no environment-profile input**, and serving a profile would fail the registry's nine on the excluded services. The static `snowflake` stanza needs `alkera-snowflake:plumbing`, which was built on `alkera-episode:p6-da-snowflake` from `Dockerfile.snowflake`, because the stanza's default `BASE` (`alkera-stack:track-b`) does not exist on this host |
| `containment-native-episode[-<slug>].json` | **P7**: `containment.py --native-episode --project alkera-p6b-<slug> --world <d>/world [--env-profile P --env-plug S=P]` | the stack a rollout builds. Unprofiled: `container_episode.py native-up --task GT001-aov-halving`, the committed recipe. Profiled: `tool_tour.world_seed(profile, plugs)` (commerce_analytics seed 11) + `bi_e2e_check.build_world` + `tool_tour.TourTopology`, i.e. `NativeContainerEpisode.up()` + `prepare()`, as in the tool tour and the e2e checks. The env profile was read off native-services' argv, and `--env-profile/--env-plug` agreed |
| `containment-native-box[-<slug>].json` | **P6's probe on the rollout box**, plus a sweep | (a) `native_probe.py`, sent on stdin to the episode's `/usr/local/bin/python`, as its docstring allows: `--expect` = `native_episode_probe.expectations(profile, plugs)` probed as TCP (P7 records each `/health`), plus `jupyter.internal:8888` when the notebook slot is plugged; `--backend` = postgres, minio when the store is plugged, and every backend the profile plugs **including the hosted ones**; `--compose-name` = services on `episode_net`; controls = `CONTROL_NAMES` minus those names, plus every excluded host, plus running services that are not on `episode_net`. (b) A read-only sweep from the episode: liveness of `.1`–`.40` of the on-link subnet; a TCP connect to **every port from 1 to 65535** on every live address (1 s timeout, 17–26 s per box); connects to the `backend_net` addresses; Postgres logins as `episode` and as `alkera`, each tagged with a marker that is then looked up in the server's statement log; and a banner from every open port that is not an audited door. Each open port is classified as an audited door when it is declared in `destinations*.jsonl` (or is postgres:5432 or minio:9000), and otherwise as not audited |

## Results

| profile | P6 as written | dialed by P6 as written | P7 | P6 probe on the rollout box | backends dialed on the box |
|---|---|---|---|---|---|
| unprofiled | **pass** | postgres 5432, minio 9000 | **pass** | **pass** | postgres 5432, minio 9000 |
| `da/snowflake` | **FAIL**: control `snowflake` resolves. It is the compose name of the snowflake service, now on `episode_net`, and it points at the `snowflake.internal` address, so this is P6's control list, not a hole | + `snowflake.internal:8443` CONNECTED | **pass** (scheduler n/a) | **FAIL**: `minio` resolves to an address that is not an expected service. This is finding 1, a real one | postgres 5432, snowflake 8443 (plus jupyter 8888, the notebook plug) |
| `de/postgres` | pass (no compose profile, so this is the unprofiled run) | postgres, minio | **pass** | **pass** | postgres, minio |
| `de/aws` | pass (`--profile redshift` selects nothing in the static files; redshift never runs under P6) | postgres, minio | **pass** | **pass** | + `redshift.internal:5439` (own container, `episode_net` only) |
| `de/databricks` | pass (hosted, so no compose service) | postgres, minio | **FAIL**: P7 asks `http://databricks.internal:443/health` in plain HTTP on a TLS port and gets `RemoteDisconnected`. This is a false negative. The box probe shows `databricks.internal:443` CONNECTED | **pass** | + `databricks.internal:443` (native-services) |
| `de/gcp` | **FAIL**: `bigquery.internal:9050` does not resolve, because without the per-rollout overlay nobody carries the proxy alias, and the compose name `bigquery` is counted as pointing elsewhere because it does not resolve. Both mean the engine is *correctly* unreachable | (tried `bigquery.internal:9050`) | **pass** (`bigquery` 200 through the proxy) | **pass** | + `bigquery.internal:9050` (native-services proxy) |
| `de/laptop` + `orchestrator=airflow` | pass (hosted, so no compose service) | postgres, minio | **pass** (`airflow` 200; scheduler n/a) | **pass** | + `airflow.internal:8100` (native-services) |

None of the boxes publishes a host port. The episode has `lo` and `eth0` and no default route. Egress was
refused on every box (P7), and every service-state check was isolated (P7 H).

### Declared but never dialed by P6 as written

| `NATIVE_BACKENDS` key | why P6 as written does not dial it | dialed where instead |
|---|---|---|
| `airflow` (8080, real Airflow) | compose profile `airflow` (*designed*, never run); no environment profile enables it | nowhere (not built) |
| `airflow-api` (8100) | hosted in native-services, so there is never a compose service of that name | box `de-laptop-airflow` |
| `databricks` (443) | hosted, so there is never a compose service | box `de-databricks` |
| `bigquery` (9050) | keyed by the **engine** service. When the engine runs (`--profile bigquery`), P6 dials an alias that only the per-rollout overlay gives native-services, so it fails | box `de-gcp` |
| `redshift` (5439) | the service exists only in the per-rollout overlay (`backend_overlay`), so `containment.py --native` can never run it | box `de-aws` |
| `snowflake` (8443) | dialed under `--profile snowflake`, after which P6 fails on its own control `snowflake` | P6 `da-snowflake` and box `da-snowflake` |

## Reachable destinations that are not audited doors

| destination | boxes | what answers | status |
|---|---|---|---|
| `minio:9000` (compose name; the alias is gone) | `da/snowflake` (and every `da/*`, by code) | MinIO S3 API; reads and writes succeed; 0 rows; webhook refused | **regression against the unprofiled box** (finding 1) |
| `<episode_net>.1:22` | all 7 | the host's OpenSSH | pre-existing, never measured before (finding 2) |
| `minio:<random port>` | all 7 | MinIO Console (`200 text/html`); `minio server /data` has no `--console-address` | pre-existing |
| `native-services:8110` | the boxes that serve a store (unprofiled and `de/*`) | MinIO audit receiver, `405 … allow: POST` | pre-existing and uncounted by design. It is also a way to **forge `minio.*` rows**, because anything on `episode_net` can POST to it. That was not tried, since a POST writes rows |
| `postgres.internal:5432` as role `episode` | all 7 | login works with the credentials in `ALKERA_POSTGRES_DSN`, which every box carries. Its database `episode` does not exist, but `alkera` accepts the login. The marker statement is **absent** from the role-scoped log, while the `alkera` marker is present. The role owns `raw`/`staging`, and also `warehouse` everywhere except `de/postgres`, where `warehouse` belongs to `warehouse_owner` and is closed to it | pre-existing, already open in TOOL_INVENTORY §1a |

Doors that work as designed but have side notes:

- `jupyter:8888` on `da/*` is the notebook plug: every cell is a `jupyter.cell` row and terminals are
  off. The connection itself is not counted in `destinations.jsonl`.
- The TLS doors **do not count a bare TCP dial**. After the probes connected, `snowflake.internal:8443`
  (in `destinations.snowflake.jsonl`) and `databricks.internal:443` still read `count: 0`, while plain
  TCP doors counted theirs: redshift 5439 counted 2, bigquery 9050 counted 4 and airflow 8100 counted 4.
  So a dial, or a port scan, that never completes a TLS handshake is invisible to blindness.

## For Andrew: what P6/P7 would need (a description only; nothing here was implemented)

1. **P6 needs an environment-profile input, or it should attach to a rollout-built stack the way P7
   does.** As written it brings up the static overlays only. It never applies `profile_overlay` (alias
   list, hosted aliases, the engine wiring), `backend_overlay` (redshift) or the per-rollout environment.
   It expects the registry's unprofiled nine (`registry_expectation`) and serves no `--profile`. So for a
   hosted or overlay-only backend it either measures nothing or fails for the wrong reason. The shortest
   path is `--native --project <live project>` with no lifecycle, plus the box run's inputs:
   `expectations(profile, plugs)` for `--expect` and `backends.plugged(profile.slots)` for `--backend`.
2. **Choose the backends to dial by who carries the alias, not by which compose service is running.**
   The hosted rows (`airflow-api`, `databricks`) are never "running". `bigquery` is keyed by the engine
   while its alias belongs to the proxy. `airflow.internal` names two different rows (8080 for real
   Airflow, 8100 for the hosted fake). Give each row the service that carries it (native-services for
   hosted rows), or derive the table from `alkera_gateway.backends`.
3. **The control list and the compose names.** Drop a `CONTROL_NAMES` entry when it is the compose name
   of a service that is now on `episode_net` (`snowflake`). Pass `--compose-name` only for services on
   `episode_net`. Turn a running service that is not on it (the BigQuery engine) into a must-not-resolve
   control, as the box run did.
4. **Probe more addresses and more ports.** Probe the bridge gateway and the whole on-link subnet, not
   only the addresses that answered. Probe every port, not six: the full sweep costs 17–26 s per box and
   is what found the console port, the gateway's sshd and the 8110 receiver.
5. **P7.**
   - (a) It probes every served service as `http://`, so a TLS hosted backend (`databricks`, 443) fails.
     It needs https with the box's trust bundle, or a TCP probe for TLS rows.
   - (b) It already asks about the hosted backends that `serve.selected_services` lists (airflow,
     bigquery, databricks). It does not ask about the container backends (snowflake, redshift) or about
     postgres and minio. Adding them as TCP reachability would cover every plugged door on every rollout.
   - (c) Question G resolves only `<name>.internal`. It should also resolve the compose name and the
     container name of an excluded service (`minio`), which is how finding 1 went unseen.
6. **Candidate fixes**, to be measured afterwards:
   - MinIO on store-less boxes: take it off `episode_net` (a profile-overlay `networks: !override
     [backend_net]`) or do not start it.
   - The gateway: stop the host from answering on the `episode_net` bridge address. Options are a host
     INPUT/DOCKER-USER rule, `ListenAddress` for sshd, or a bridge option that keeps the host off the
     subnet. This needs measuring on Docker 29.
   - The console: `--console-address 127.0.0.1:<port>` or `MINIO_BROWSER=off`.
   - The 8110 receiver: an auth token on the webhook (`MINIO_AUDIT_WEBHOOK_AUTH_TOKEN_*`) checked by the
     receiver, or the receiver on `backend_net` only.
   - The `episode` role: drop `ALKERA_POSTGRES_DSN` from the native environment, and `NOLOGIN` the role
     in `zz-native.sql`, or log it as well.

## Reproduce

`containment.py` can be run directly as above. The box runs used a driver kept outside the repository,
and its steps are the ones described in the instruments table. Every stack (projects `alkera-p6m-*`,
`alkera-p6b-*` and `alkera-p6c-*`) was brought up one at a time and torn down with `down -v`. No
containers, volumes or networks were left behind. The `alkera-episode:p6*` tags and
`alkera-snowflake:plumbing` remain on the host.
