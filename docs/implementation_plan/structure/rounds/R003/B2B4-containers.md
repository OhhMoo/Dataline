# B2 + B4 — containment evidence and the Snowflake socket

**Agent B2B4-FINISH, Track B, 2026-09-13.** Finishing container work Astra started.
Deliverables: `gateway/docker/containment.py`, `gateway/docker/snowflake_acceptance.py`,
and the two artifacts they write under `gateway/docker/evidence/`.

Everything marked **[V]** I ran on this machine today and read the output of. Everything
marked **[R]** is a claim I read and did not execute. The split is on every line that
needs one.

Environment for all **[V]**: Windows 11 Pro for Workstations 10.0.26200, Docker Desktop
server 29.5.2 (linux/overlayfs), Python 3.11.15 in `gateway/.venv`,
`alkera-stack:track-b` = `sha256:3cac276a5624…`, `alkera-snowflake:plumbing` =
`sha256:099a7f120c63…`. Compose runs used project names `alkera-b2b4*` so that the
`alkera-track-b` stack another agent was using stayed untouched.

---

## 0. What I did not rebuild

Astra's topology stands. Six services, two internal networks, the profile split, the
pinned digests, `Dockerfile`, `Dockerfile.snowflake`, `postgres.sql` — none of it was
redesigned. What was missing was the **proof**, one piece of **trust plumbing**, and —
found on the way — three things that were wired in and had never once run.

---

## 1. B2 — egress and containment, proven per service

`gateway/docker/containment.py` runs the whole lifecycle and writes
`gateway/docker/evidence/containment.json`. It never asserts anything from the compose
file alone: `internal: true` is a property of a *network*, and says nothing about
whether a given service is attached to the network you think it is, or whether it has a
second interface. Every service is probed **from inside its own network namespace**
(`docker run --network container:<id>`), reading `/proc/net/dev` and `/proc/net/route`,
which are per-netns, and then trying to leave.

One command, one artifact: `python containment.py --profile snowflake`. **[V]**

### The probe is shown to be capable of detecting egress

The first thing the artifact records is a **positive control**: the same probe, same
image, on the default bridge network.

```
probe_control: egress_blocked=false
  8.8.8.8:53 CONNECTED · 1.1.1.1:443 CONNECTED
  example.com RESOLVED · google.com RESOLVED · pypi.org RESOLVED
```

Without that line, "no service can reach the internet" is indistinguishable from a probe
that can never connect to anything. **[V]**

### Per-service evidence

| service | interfaces | addresses | default route | TCP out | DNS out | egress |
|---|---|---|---|---|---|---|
| `episode` | `lo, eth0, eth1` | 172.23.0.5 (backend), 172.22.0.2 (episode) | **none** | 3× OSError | 3× gaierror | blocked |
| `postgres` | `lo, eth0` | 172.23.0.4 | **none** | 3× OSError | 3× gaierror | blocked |
| `s3` | `lo, eth0` | 172.23.0.2 | **none** | 3× OSError | 3× gaierror | blocked |
| `snowflake` | `lo, eth0` | 172.23.0.3 | **none** | 3× OSError | 3× gaierror | blocked |
| `harness` | `lo, eth0` | 172.22.0.3 | **none** | 3× OSError | 3× gaierror | blocked |
| `grading` | `lo` only | — | **none** | 3× OSError | 3× gaierror | blocked |

`extra_interfaces: {}` — no service carries an interface beyond the ones its zone
explains. `episode` has exactly two, one per declared network, which is the thing the
compose file could not tell you. **[V]**

`grading` is probed separately and deliberately (§1.1).

### P1 — the grading zone

This is ground rule 1 and the single most important invariant, so it is checked in two
halves and both are recorded.

*During the episode:* `services_live_during_episode = [episode, postgres, s3, snowflake]`,
`absent_during_episode = true`. The grader is not merely isolated, it **does not exist**.
From inside `episode`, `os.path.exists('/grading')` is `false`. **[V]**

*After teardown:* the script stops every running service — the stop list is derived from
what is actually running, not hardcoded — records
`still_running_when_grader_started = []`, and only then raises the grader. **[V]**

```
declared_network_mode : none
grader interfaces     : ['lo']          routes: []      default_route: false
name resolution       : 5/5 gaierror (postgres, s3, snowflake, acme.snowflake.local, episode)
address probes        : 18 attempted, 0 CONNECTED
/world                : exists, 21 files, WRITE REFUSED
                        OSError [Errno 30] Read-only file system: '/world/.containment-write-probe'
```

The read-only mount is proven by attempting a write, not by reading `:ro` out of the
compose file. **[V]**

An earlier draft of this check passed while being hollow: `docker compose config` omits
services whose profile is not enabled, so `config()["services"]["grading"]` returned
`{}` and every "declared" assertion was vacuously true. It also left `snowflake` running
while the grader came up. Both are fixed; the artifact now shows the populated values
above. Worth recording because it is the exact defect shape this round keeps finding.

### P3 — the gateway is the only channel

The agent's network vantage is the `harness` container (`episode_net` only):

```
resolved   : ['episode']  — postgres, s3, snowflake, acme.snowflake.local all gaierror
reachable  : ['172.22.0.2:8080']  — the gateway, and nothing else
backends_reachable_from_agent : []   (24 address probes across 5432/9090/8443/8080/80/443)
published_host_ports          : {}
```

**[V]**

The brief asked me to check this from the `episode` container *by trying*, so I did, and
the honest answer is the interesting one: **the episode container reaches Postgres,
S3Mock and the Snowflake socket, and must.** It is the gateway; they are its backends.
The boundary there is not the network, it is the tool surface. Both halves are in the
artifact rather than one being quietly omitted:

- `episode_direct_backend_connection`: connects to Postgres with the gateway's own DSN
  as `episode/episode`, **`rolsuper = false`**. A superuser here would make one
  `sql_execute` a second channel regardless of topology, so this is a pass condition.
- `tool_surface`: 28 tools, every field name of every argument model enumerated,
  **zero** fields named like a socket (`host`, `port`, `url`, `dsn`, `endpoint`, …).
- `acceptance_inside.py` separately confirms `pg_read_file` and `CREATE EXTENSION dblink`
  are refused — the in-database escapes. **[V]**

This check is a **tripwire, not a proof**; see §5.

### P4 — the determinism hazard

`worlddata` writable in exactly one running service (`episode`); declared `:ro` for
`grading`. The tree is hashed path→sha256 three times: at start, after 20 idle seconds,
and after every backend is exercised through the gateway.

```
tree_digest_start / after_idle / after_load : 651f4974766df5ea… (identical)
files_changed_while_idle : []      files_changed_under_load : []
load_exercised : warehouse_rows=2 · s3_roundtrip=true · bi_panels=true · docs_lines=true
writable state outside /world:
  episode   /audit, /service-state/bi, /etc/alkera/snowflake-tls
  postgres  /var/lib/postgresql/data
  snowflake /snowflake-data, /tls, /etc/alkera/snowflake-tls
  s3        (none — see §3.1)
  harness   (none)
```

**[V]** No added service has a writable path inside the fingerprinted tree, and none
perturbs it idle or under load. The honest limit on this result is in §5.

---

## 2. B4 — the fakesnow socket

Scope held to the socket. **Andrew owns Snowflake semantics** and nothing here asserts
any.

### What was actually missing

The hostname alias and the TLS listener already existed. What did not exist was **trust**:
the certificate lived only in the `snowflaketls` volume, mounted only into the Snowflake
service, so no client could verify it. A driver pointed at the socket got
`CERTIFICATE_VERIFY_FAILED`, which is not "a client believes it is talking to Snowflake".

So I measured which trust knob the real driver honours, rather than guessing. Against
`snowflake-connector-python` 4.7.3 in this image: **[V]**

| configuration | result |
|---|---|
| default trust store | `250001 Could not connect to Snowflake backend` |
| `SSL_CERT_FILE` = our cert | **fails** — never reaches the driver |
| `insecure_mode=True` | **fails** — governs OCSP, not chain verification |
| `disable_ocsp_checks=True` | **fails** — same |
| `REQUESTS_CA_BUNDLE` = our cert | **round-trips** |
| `REQUESTS_CA_BUNDLE` = certifi + our cert | **round-trips** |

OCSP is a non-issue: with the bundle and the default `ocsp_fail_open=True` it simply
works. The whole missing piece is one environment variable and getting the certificate
to the client.

### What I built

- `snowflake_start.py` regenerates key + certificate **every boot** (SAN now
  `DNS:acme.snowflake.local, DNS:*.snowflake.local, DNS:localhost, IP:127.0.0.1`) so a
  certificate surviving in a volume can never disagree with the published bundle.
- It publishes the **public half only** — `server.crt` plus `bundle.pem` (certifi + our
  cert) — to a new `snowflaketrust` volume. The private key never leaves the service.
- `Dockerfile` seeds `/etc/alkera/snowflake-tls/bundle.pem` with certifi and sets
  `ENV REQUESTS_CA_BUNDLE` to it, so the path is valid whether or not the Snowflake
  profile runs, the named volume seeds deterministically, and there is no boot-order race.
- `compose.yml` mounts `snowflaketrust` into `episode` and `snowflake`; **not** into
  `harness` or `grading`.

No source file under `gateway/src/` was touched. `snowflake_proxy.py` already serves
8443 from `/tls` and allow-lists the wire routes, which is exactly what this layout needs.

### Acceptance — `python snowflake_acceptance.py` → `evidence/snowflake_socket.json`

Twelve checks, all green, run from a **separate container** on `backend_net` so DNS, TLS
and the wire are all crossed for real. **[V]**

```
hostname_resolves_in_backend_zone      acme.snowflake.local -> 172.23.0.3
hostname_resolves_for_gateway          same, from inside the episode container
hostname_hidden_from_agent_zone        gaierror for both DNS and TCP from harness
tls_verifies_against_published_bundle  TLSv1.3, CN=acme.snowflake.local
san_covers_hostname                    DNS:acme.snowflake.local, DNS:*.snowflake.local,
                                       DNS:localhost, IP:127.0.0.1
tls_rejected_without_bundle            SSLCertVerificationError: self-signed certificate
driver_roundtrip                       create db/schema/table, insert, read back
                                       [['1','10.50']], drop — all through the driver
driver_refuses_untrusted               OperationalError 250001 without the bundle
gateway_carries_bundle                 241,458 bytes at REQUESTS_CA_BUNDLE
private_key_stays_in_snowflake_service /tls not mounted in episode; server.key not visible
connector_wire_route_served            /session/v1/login-request -> 200, real token
rest_api_absent_as_documented          /api/v2/statements -> 404
```

**The two negative controls are the load-bearing part.** A client that trusts everything
passes a TLS test that proves nothing, so the same connection is made twice — once with
the bundle, once without — and the second must fail. It does.

### S951 confirmed, and made precise

I hit fakesnow's own upstream on loopback, bypassing our proxy's allow-list, so the claim
is about fakesnow and not about our filter: **[V]**

```
fakesnow  POST /api/v2/statements          -> 404 Not Found
fakesnow  POST /telemetry/send             -> 404 Not Found
fakesnow  POST /monitoring/queries/telemetry -> 405 Method Not Allowed
```

S951's finding holds in our stack. The REST surface is not built and is not proxied —
out of B4's scope, and left that way.

Minor, recorded so it is not rediscovered: the real connector posts telemetry to
`/telemetry/send`, which is neither in `snowflake_proxy.WIRE_PATHS` nor implemented
upstream, and `/monitoring/queries/telemetry` (which *is* in `WIRE_PATHS`) rejects POST
upstream. Neither is fatal — telemetry failures are swallowed by the driver, and the full
round trip works — but the wire path list and fakesnow do not quite line up.

---

## 3. Three things that were wired in and had never run

### 3.1 The `landing` bucket did not exist — the S3 half of `acceptance_inside.py` had never passed

Flagged by **B3-FINISH**, confirmed here independently before acting on it: **[V]**
`ListAllMyBuckets` against the live `alkera-track-b-s3-1` returned `<Buckets/>`, `/s3-data`
was empty, and so was the image's own `/s3mockroot`. On the pinned `adobe/s3mock` digest,
`initialBuckets`, `root` and `retainFilesOnExit` are **all ignored**; the file store is a
random `/tmp/s3mockFileStore*` directory. A plain `PUT /landing` does create the bucket.

Fixes:
- `start.py` gains `provision_storage()` — the control plane creates the buckets, waits
  out S3Mock's JVM boot, then **reads the bucket list back and raises if anything is
  missing**. A world that serves a missing bucket fails every storage task for a reason
  that has nothing to do with the agent, so it fails loudly at boot instead.
- The three ignored settings and the `s3data` volume are **removed** from `compose.yml`,
  with the measurement in a comment. Leaving them is why nobody noticed.
- `containment.py` now reads the bucket list **off the wire from inside the container**
  and does a full put/get/list/delete round trip through the gateway. `p0_world_provisioned`
  is a top-level pass condition: `s3_buckets_on_the_wire: ["landing"]`,
  `s3_roundtrip_through_gateway: true`. **[V]**

`acceptance_inside.py` now passes end to end, including the storage half:
`{"parity":"4 backend/transport combinations, 7 calls each", "surfaces":"docs, BI
staleness, binary S3 overwrite/delete", "postgres":"atomic rollback and server-file
denial passed"}`. **[V]**

**Consequence Track C must know: object storage in this stack is effectively in-memory
and does not survive a restart of the `s3` service.** The connector contract says Track C
restores all participating services before replay/grading; for S3 there is currently
nothing to restore. That needs a different image or a different mechanism.

### 3.2 `FAKESNOW_DB_PATH` was a no-op — the `snowflakedata` volume was never written

`snowflake_start.py` passed `session_parameters={"FAKESNOW_DB_PATH": "/snowflake-data"}`
to `fakesnow.server()`. Reading fakesnow's source: that kwarg only decorates the
connection dict the context manager *yields*, which the script discards. The server reads
`FAKESNOW_DB_PATH` from the **client's** login-request session parameters. Measured before
the fix: `/snowflake-data` empty, state in per-session `/tmp/fakesnow_bucket_*`. **[V]**

Fixed by driving uvicorn on `fakesnow.server.app` directly and binding
`shared_fs = FakeSnow(db_path="/snowflake-data")` before any login lands. (The context
manager could not be used and the module imported in the same file: importing
`fakesnow.server` shadows the same-named function on the package.) After the fix,
`/snowflake-data/PERSISTED.db` + `.wal` appear on a `CREATE DATABASE`. **[V]**

That makes the declared volume the service's real state location, which is what P4 needs
to be non-vacuous. **It does not make the state survive a restart**: a fresh `FakeSnow`
does not re-attach the `.db` files it finds, so after `compose restart snowflake` the
database is gone (`Binder Error: Catalog "PERSISTED" does not exist!`). **[V]** Same shape
as the S3 gap, and it is Andrew's call whether re-attach belongs in the mock.

### 3.3 `up --wait` returned before the gateway was listening

With no healthcheck on `episode`, `--wait` returns as soon as the container is *running* —
which is before provisioning finishes and long before uvicorn binds. The first MCP call
from the harness failed with `ConnectError` against a perfectly healthy stack. **[V]**
`episode` now has a healthcheck on the existing `/health` route.

---

## 4. Also done: one egress implementation, not two

`acceptance.py` and `containment.py` both grew per-container egress logic. Two
implementations of a safety check is one implementation plus something that can quietly
disagree with it. `acceptance.py` is now the **functional** half only (MCP over
`episode_net` from the harness — `{"ok":true,"tools":28}` **[V]** — plus
`acceptance_inside.py`), and it delegates containment to `containment.py`, which owns the
lifecycle because the grading zone can only be probed after teardown.

## 4.1 Verification run

```
gateway/.venv/Scripts/python -m pytest tests   ->  35 passed, 1 xfailed   [V]
python docker/acceptance.py --profile snowflake ->  functional: all green [V]
python docker/containment.py --profile snowflake -> p0–p4 all true       [V]
python docker/snowflake_acceptance.py           ->  12/12 checks true    [V]
```

The 16 tests named in my brief are a subset; the suite has since grown to 36 under B1 and
B3 and **nothing fails**. No file under `gateway/src/` or `tests/` was edited by me, and
nothing under `world/` or `tasks/` was touched. Changes are confined to
`gateway/docker/`: `compose.yml`, `Dockerfile`, `start.py`, `snowflake_start.py`,
`acceptance.py`, and the new `containment.py`, `snowflake_acceptance.py`, `evidence/`.

I also created the missing `landing` bucket by hand in the still-running `alkera-track-b`
project so whoever is testing against it now is not blocked by §3.1. My own compose runs
used separate project names and were torn down with `-v`.

---

## 5. The single biggest weakness in my own output

**My containment evidence describes a topology nobody runs episodes in.**

`tasks/harness/agent_under_test.py` launches the gateway as
`python -m alkera_gateway.cli serve --transport stdio` — a host subprocess. **[V]** The
Pass@1 numbers C1 produced this round were measured with the agent, the gateway and the
world in one host process, with `/grading` one `..` away on the same filesystem. Every
property in `containment.json` is true, and none of it was in force for a single rollout.
C1 wrote the other half of this sentence in its own §8 ("these numbers need Kimi's
container"). The gap is not mine to close alone, but it is the reason nobody should quote
my green artifact as if it protected the measurement. **The integration checkpoint in
§6 of `3_track.md` — one task, one world, through the multi-container gateway — is still
unowned, and it is the thing that would connect these two halves.**

Three narrower weaknesses, each of which could have been the headline:

1. **P4 is measured on a three-file world.** The fingerprinted tree in this stack is
   `docs/metrics.md`, `dashboards/revenue.yml` and `warehouse.duckdb` — Track B's own
   integration fixture. With `ALKERA_BACKEND=postgres` the warehouse file is inert, so
   the file most likely to churn is not even in play. "No service perturbs the tree" over
   three files is a much weaker statement than over a real Track A world with
   `/var/sched/state.json`, dbt target directories and git objects. The mechanism is
   right; the sample is thin. Run `containment.py` against a Track A world before
   treating A4 as closed.
2. **The "no socket primitive" check is a name-based tripwire.** It reads the field names
   of all 28 argument models and flags anything called `host`, `port`, `url`, `dsn` and
   so on. A tool that took a socket under another name would pass it. It is also silent
   about `dbt_run`, which executes a project the agent can write — audited and profile-
   pinned, but it is a subprocess the agent influences, and I did not attack it.
3. **The egress probe is TCP and DNS over IPv4.** Three addresses, three names, three
   ports. I did not probe UDP, ICMP, IPv6, or raw sockets. `internal: true` removes the
   default route — which the artifact confirms per service — so I expect those to hold,
   but *expect* is the operative word and it is not what the artifact says.

One thing I want on the record as **not** verified: the grading-zone result is for
`gateway/docker/compose.yml`. Track A's `world/docker/docker-compose.yml` is a separate
topology with a `../grading:/grading:ro` mount that this one does not have, and I did not
test it. A3 remains Kimi's owed item.
