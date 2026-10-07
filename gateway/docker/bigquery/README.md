# BigQuery emulator (goccy/bigquery-emulator v0.8.1)

The `bigquery` warehouse backend (`src/alkera_gateway/bigquery.py`) talks to
[goccy/bigquery-emulator](https://github.com/goccy/bigquery-emulator) through
`google-cloud-bigquery` with an `api_endpoint` override and anonymous credentials.
What the emulator gets wrong, and how the connector fences it, is in that module's
docstring: cost fields are never surfaced and dry runs are refused ("bigquery cost
estimation is not modelled"), DML row counts are counted by the connector, and
multi-statement batches are not atomic (a failure says what already landed).

## Binary (tests, host runs)

`fetch.py` downloads the release tarball for this host's arch into
`~/.cache/alkera/bigquery-emulator/v0.8.1/<arch>/` and checks it against the sha256
pinned in the script (copied from the release's `checksums.txt`) before unpacking:

| asset | sha256 |
|---|---|
| `bigquery-emulator_v0.8.1_linux_amd64.tar.gz` | `8dbe6d3ebe3e8963523bada4a88a012d6a1bbc2ea779fbdc2e31d2e8c8c51c36` |
| `bigquery-emulator_v0.8.1_linux_arm64.tar.gz` | `4c6913619c90b4a8a9378993e797774390289f9c94deba483d2a6db9cefbc8c5` |

```sh
python gateway/docker/bigquery/fetch.py               # prints the binary path
python gateway/docker/bigquery/fetch.py --arch arm64 --dest /opt/bq
```

`tests/test_bigquery_plug.py` calls the same `fetch()` and starts the binary on free
ports with `--project=test`; if the fetch fails, every test there skips with the
reason.

## Compose service

The stack reaches the emulator as `bigquery.internal:9050` on `backend_net` only
(no published host port; the agent's network cannot resolve it). The upstream
image is multi-arch; the index digest below is the `0.8.1` tag at the time of
pinning (amd64 `sha256:b49d6843…`, arm64 `sha256:a49adb7e…`).

```yaml
  bigquery:
    profiles: [bigquery]
    image: ghcr.io/goccy/bigquery-emulator:0.8.1@sha256:f4e428d265a93dc5ce36c294e1c584c7c9b384117d47ab8ddbb63d8d50b7f393
    command: ["--project=episode", "--port=9050", "--grpc-port=9060",
              "--database=/bigquery-data/emulator.db"]
    networks:
      backend_net:
        aliases: [bigquery.internal]
    volumes:
      - bigquerydata:/bigquery-data
```

plus `bigquerydata:` under `volumes:`, and on the `episode` service:

```yaml
      ALKERA_BIGQUERY_OPTIONS: >-
        {"project": "episode", "dataset": "warehouse",
         "api_endpoint": "http://bigquery.internal:9050"}
```

The gateway config then declares `"warehouse_backend": "bigquery"` and an explicit
`initial_world_fp` (a remote backend's state is not covered by a hash of the local
world tree). Seed worlds with `alkera_gateway.bigquery.seed_table` (rows as dicts or
sequences) or `duckdb_rows(conn, sql)` + `seed_table` to copy a DuckDB table; DuckDB
types with no exact BigQuery equivalent raise rather than round.

Not verified here: the container image (no Docker on the build host) and the arm64
binary running (its tarball digest and ELF header were checked, not executed).

## Native container topology (the `bigquery` plug, `de/gcp`)

The agent's own clients reach BigQuery at `bigquery.internal:9050`, which is NOT the
emulator: it is an audited proxy inside native-services
(`src/alkera_gateway/native/services/bigquery.py`, the hosted row
`backends.BACKENDS["bigquery"]`). The emulator is this compose stanza, started by
`tasks/harness/container_episode.py` (compose profile `bigquery`, `HOSTED_ENGINES`)
with `--project=alkera-local` and without its `bigquery.internal` alias; it stays on
backend_net, which native-services joins and the episode does not. native-services
gets `ALKERA_BIGQUERY_UPSTREAM=http://bigquery:9050` (Harbor: the supervised child on
`127.0.0.1:19050`) and, at import, before it binds a port, seeds the emulator from the
world's warehouse straight upstream (`native/bigquery_seed.py`: one dataset per
schema, views copied as tables, counts read back; no audit row, no job).

The proxy writes one 9.2.2 row per request before it answers (`bigquery.statement`
for a query, `bigquery.<op>` for every other route, `bigquery.refused`,
`bigquery.unknown_route`) and corrects what v0.8.1 gets wrong in a way a client
would act on: cost fields stripped and dry runs refused (as above); legacy SQL, load
and extract jobs and `ALTER` refused by name (501 `notImplemented`); a query error's
`jobInternalError` - which google-cloud-bigquery retries for ten minutes - answered as
`invalidQuery` / `notFound`; `statementType` recomputed from the SQL (the emulator
says `SELECT` for everything, which breaks dbt-bigquery); `CREATE SCHEMA` made to
create the dataset and `CREATE OR REPLACE` made to replace (the emulator does
neither); the emulator's per-job result datasets hidden from `datasets.list`. The
module docstring has each measurement. With that, dbt-bigquery (method
`oauth-secrets` with the box's fake `BIGQUERY_TOKEN`, `api_endpoint`) runs `debug`,
`run`, re-`run`, `test` and `show`; `dbt seed` is a load job and is refused by name.

Measured end to end by `tasks/harness/bigquery_e2e_check.py` (evidence
`tasks/harness/evidence/bigquery_e2e.json`); the tool tour's `bigquery` probes are in
`tasks/harness/tool_tour_probes/bigquery.py`.
