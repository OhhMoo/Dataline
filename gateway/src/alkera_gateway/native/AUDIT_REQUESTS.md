# Changes AUDIT needs in files it does not own

**Status: requests, 2026-09-16.** Nothing here has been applied. Each item says
what was measured, what it costs today, and the smallest change that closes it.
Evidence for every measurement is in `native/PARITY.md`.

## 1. `storage.py` - S3 credentials must be configurable (owner: gateway core)

`ObjectStorage.client` hardcodes `aws_access_key_id="episode"`,
`aws_secret_access_key="episode"`. `episode` is seven characters; MinIO's
minimum root secret is eight, so **the MCP transport cannot authenticate against
the MinIO whose audit webhook produces the native rows**. Measured: the Phase 1
parity test therefore puts the MCP leg's object into `adobe/s3mock` and the
native leg's into MinIO, which is the one place the two legs talk to different
servers.

Request: read the two values from `GatewayConfig` (defaulting to today's
literals, so every recorded evidence file is unchanged), e.g.
`s3_access_key: str = "episode"`, `s3_secret_key: str = "episode"`.

## 2. `docker/compose.yml` - the servers must be told to talk (owner: COMPOSE-DISCOVERY)

- `postgres`: add `command: ["-c","log_statement=none","-c","log_min_duration_statement=-1"]`
  and, in `postgres.sql`, `ALTER ROLE episode SET log_statement='all'` plus
  `ALTER ROLE episode SET log_min_duration_statement=0`. Measured on
  postgres 16.15: with this, the `episode` role's statements are logged and the
  superuser's provisioning statements are not. Global `log_statement=all`
  instead puts the entrypoint's `CREATE DATABASE`, the healthcheck and every
  harness connection into the agent's action log - 5 unrelated rows before the
  agent ran anything.
- `s3`: replace `adobe/s3mock` with `minio/minio` plus
  `MINIO_AUDIT_WEBHOOK_ENABLE_alkera=on` and
  `MINIO_AUDIT_WEBHOOK_ENDPOINT_alkera=http://<receiver>:<port>/audit`. s3mock
  has no audit stream at all, so on today's stack the native S3 plane is
  unauditable.
- a receiver service running
  `python -m alkera_gateway.native.tailers.minio --host 0.0.0.0`, on
  `backend_net` only, with the audit volume mounted.
- the postgres tailer as `python -m alkera_gateway.native.tailers.postgres`
  reading the server's log; in compose that means postgres logging to a shared
  volume (`-c logging_collector=on -c log_directory=/var/log/postgresql`)
  rather than to stderr, because `docker logs` is not available inside the
  network.

## 3. `snowflake_proxy.py` - one hook for a fakesnow tailer (owner: warehouse/Snowflake)

There is no statement log on the Snowflake side today: `snowflake_fidelity.py`
logs only warnings, and fakesnow's server keeps no query history file
(grepped, 2026-09-16). The SQL text does pass through one place -
`snowflake_proxy.forward` sees the body of `POST /queries/v1/query-request`.

Request: have `forward` append `{"vts", "sql", "status", "latency_ms"}` to the
path named by an env var when that var is set. `native/tailers/snowflake.py`
is then the postgres tailer's `classify` over that file and nothing else. Until
then the Snowflake plane is **designed, not built**.

## 4. `tasks/harness/blindness.py` - `actor:` flags are provenance (owner: NATIVE-DRIVER)

9.2.2 has no `actor` key, and adding one would break the key-for-key property
this whole deliverable rests on. `native.audit.record` therefore carries its
`actor` argument as the flag `actor:<name>` (`actor:postgres-tailer`,
`actor:minio-tailer`, `actor:airflow-tailer`, `actor:shim`).

Request: treat any flag matching `^actor:` as provenance rather than as a
semantic classifier, so it is not counted as an anomaly. The same applies to
`catalog_only`, which marks the catalog SELECTs psql issues for a `\d` - eight
of them per `\d`, measured - and which a world-diff rule should be free to
ignore.

## 5. `tasks/harness/*` - one writer per audit directory (owner: NATIVE-DRIVER)

`native.audit.record` allocates `seq` under `<audit_dir>/writer.lock`, the same
lock a live `AuditLog` holds for the whole episode, and raises a `RuntimeError`
naming the holder if it cannot take it. An episode that serves MCP and native
into one directory will therefore fail loudly rather than write a log with a
duplicated `seq`. If a mixed transport is ever wanted, the sequence allocator
has to move into one process; it cannot be two.
