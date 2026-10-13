# Containment re-measured after the sandbox lane: 2026-10-13

Every profile was measured on images rebuilt at `96abefc` with `--no-overlay`; each box JSON
records `image_revision`. The previous record is `CONTAINMENT_2026-10-07.md`, and its
"For Andrew" section is the spec this round implemented.

## Read this first

- **P6 and P7 pass on all nine boxes.** That is the seven measured on 10-07, plus `da/laptop`
  (the BI box, with the notebook plug and the browser) and `ds/databricks` (Databricks tier 2,
  Spark Connect). The three 10-07 FAILs were false negatives in the checkers, and they pass now
  that the checkers are fixed (`83bf36f`, `50d3095`).
- **Two doors remain on every box. Neither is a backend:**
  1. **The host's sshd at the episode network's gateway address, port 22**
     (`<episode_net>.1:22`, `SSH-2.0-OpenSSH_10.2p1`). The proposed host rule is
     `iptables -I INPUT -s 10.210.0.0/16 -p tcp --dport 22 -j DROP`, made persistent. It was
     NOT applied: the coordinator's session was not permitted to change the host firewall, so
     it is left to the host owner. Pre-check done: the only established ssh peer came from
     outside 10.210/16.
  2. **native-services' MinIO audit receiver, `:8110`.** It is token-only: a GET answers 405,
     and only a POST carrying the per-rollout token is accepted. This is a designed door, not
     an unaudited one; it is listed because the sweep reports every open port it finds.
- **One transient host port, once:** `10.210.4.1:52899` on de/aws, like `10.210.2.1:49743` on
  10-13's first sweep. Both were open during the scan and gone seconds later. The likely source
  is a test server bound to `0.0.0.0` on the host, since this night ran up to a dozen
  concurrent suites. Only a host INPUT policy for 10.210/16 closes that class of door. Measure
  it first against Harbor's and the subject sandbox's egress paths.
- **Every door the 10-07 note listed is closed,** apart from the two above:

| 10-07 finding | closed by | measured |
|---|---|---|
| P6 dialled only running compose services; its control list pinned `snowflake`; it never probed the gateway or all ports | `83bf36f` (`--attach`, carriers, a sweep over the gateway, the subnet and every port), plus `box_sweep.py` | P6 passes on 9/9; the sweep runs on every box |
| P7 sent plain HTTP to TLS backends | `83bf36f` (https or tcp per door); `50d3095` (gRPC doors by connect) | P7 passes on 9/9 (ds/databricks was 11/12 before `50d3095`) |
| TLS doors did not count a bare TCP dial | `7d1a208` (counted at accept, by uvicorn's protocol factory) | `test_tls_dial_without_handshake_is_counted_once`; snowflake/databricks e2e |
| backend_net peers could reach native-services' agent listeners | `09463d2` (episode peers only; `native.refused_peer` rows); `462ffc4` (Spark Connect behind the same filter) | `PEERS_2026-10-13.md` |
| MinIO's root credential in every box (found tonight: the agent could drive MinIO to dial any episode service) | `d02a988` (the box is a scoped user; root is minted per start, inside MinIO only) | `PEERS_2026-10-13.md` addendum; `tasks/harness/evidence/minio_containment.json` |
| MinIO's web console reachable where MinIO runs | `3e33f06` (`MINIO_BROWSER=off`) | absent from every sweep |
| the unlogged `episode` Postgres role logged in | `f6b4dd0` (NOLOGIN); `8efe9da` (no DSN in the box) | absent from every sweep; `postgres_e2e.json` 31/31 |
| graders ran on the host, as the host user, with the host environment | `aa40680`: a `--network none --read-only` container, env of five keys, in the container topology | 483 recorded baselines regraded identical; the host topology still grades on the host |
| BigQuery refused in Harbor | not changed (a single container cannot fence the emulator); Spark Connect is refused there too | `harbor/README.md` decision 11 |

## Results

| box | image | P6 | P7 | open doors the sweep found |
|---|---|---|---|---|
| unprofiled (GT001) | `head` | static stack, `containment-native.json` (10-13 T4) | pass | :22, :8110 |
| da/laptop (BI box) | `head-da` | pass | pass | :22 |
| da/snowflake | `head-da-snowflake` | pass | pass | :22 |
| de/aws | `head-redshift` | pass | pass | :22, :8110, transient `10.210.4.1:52899` |
| de/databricks | `head-databricks` | pass | pass | :22, :8110 |
| de/gcp | `head-bigquery` | pass | pass | :22, :8110 |
| de/laptop + airflow | `head-airflow` | pass | pass | :22, :8110 |
| de/postgres | `head` | pass | pass | :22, :8110 |
| ds/databricks (tier 2) | `head-ds-databricks-spark` | pass | pass | :22, :8110 |

No store-less (`da/*`) box runs MinIO, so `:8110` is absent there, as designed. No backend_net
address was reachable from any box.

## Reproduce

One docker slot per box (`scripts/docker_slot.sh`), from the repository root:

```
gateway/.venv/bin/python gateway/docker/box_sweep.py --up --image alkera-episode:head \
    --task GT001-aov-halving --slug '' --skip-p6 --work <dir>
gateway/.venv/bin/python gateway/docker/box_sweep.py --up --image <profile image> \
    --env-profile <role/stack> [--env-plug SLOT=PLUG,...] --slug <slug> --work <dir>
```

The profile, image and plug triples are the rows of the Results table. da/laptop is
`--env-plug bi=powerbi,sigma,tableau`; de/laptop + airflow is
`--env-plug orchestrator=airflow`.
