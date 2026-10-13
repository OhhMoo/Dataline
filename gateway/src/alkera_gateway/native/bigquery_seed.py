"""Seed the BigQuery emulator with the world's warehouse, straight upstream.

    seed("http://bigquery:9050", "alkera-local", [Path("/world/var/warehouse/warehouse.duckdb")])
    # -> {"raw.customers": 339, ..., "warehouse.fct_orders": 16825}

The BigQuery plug (`alkera_gateway.backends.BACKENDS["bigquery"]`) is goccy's
emulator behind an audited proxy in native-services
(`native/services/bigquery.py`). The emulator starts empty; this module gives it
the world, once, at boot - before the proxy's port is bound, so before
native-services is healthy and before the episode exists.

STRAIGHT UPSTREAM, SO NO AUDIT ROWS. Every call here goes to the emulator's own
REST endpoint, never through the proxy, and writes nothing to actions.jsonl:
provisioning is not something the agent did, and a row for it would be a row
no probe and no grader could attribute (the tool tour's `no_stray_rows`).

WHAT IS COPIED. Every relation of every warehouse file, by the rule
`world/world/fingerprint.py` hashes by (`var/warehouse/*.duckdb`): one dataset per
DuckDB schema (`raw`, `staging`, `warehouse`), one table per relation, so
`alkera-local.warehouse.fct_orders` is the world's `warehouse.fct_orders`. A view
is copied as a TABLE of its rows, the way the Snowflake plug's seed copies it
(`gateway/docker/snowflake_start.seed`): its SQL is DuckDB's, and re-creating it
as a BigQuery view would be a translation nobody checked. Two files that both
hold a schema of one name would be two datasets of one name; that is refused,
naming both, rather than merged.

TYPES ARE EXACT OR REFUSED. Column types come from `alkera_gateway.bigquery
.duckdb_rows` (DECIMAL(12,2) -> NUMERIC, TIMESTAMP -> DATETIME, ...), which raises
for a DuckDB type with no exact BigQuery equivalent. Values travel as JSON in the
`tabledata.insertAll` body; NUMERIC and BIGNUMERIC as decimal strings, so no
float ever rounds a money column.

A PARTIAL SEED IS NOT A SEED. An insert error from the emulator raises, and every
table's row count is read back (`tables.get` numRows - no query, so seeding
creates no job) after it is loaded and compared with the source: the returned
counts are the emulator's, not this module's bookkeeping. Re-seeding is
idempotent - a table that already exists is dropped and loaded again - so a
restarted native-services gives the agent the world, not the world twice.
"""
from __future__ import annotations

import base64
import datetime as dt
import decimal
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

#: Rows per `tabledata.insertAll` request.
BATCH = 2000
#: DuckDB's own catalog schemas: not the world's data.
SYSTEM_SCHEMAS = ("information_schema", "pg_catalog")
#: How long `wait_for` gives the emulator to answer before seeding gives up.
WAIT_S = 60.0
#: One request's budget. A seed request that takes longer is a stuck emulator.
REQUEST_S = 120.0


class SeedError(RuntimeError):
    """The emulator could not be given the world, with the reason."""


def _api(project: str) -> str:
    return f"/bigquery/v2/projects/{project}"


def _error(response) -> str:
    try:
        body = response.json()
        return str(body["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return response.text[:300]


def wait_for(upstream: str, project: str, timeout: float | None = None) -> float:
    """Block until the emulator serves `project` (at most `timeout`, WAIT_S by
    default); the seconds it took.

    The emulator image has no shell for a healthcheck to run in, so compose can
    only say it STARTED; this is the poll that says it answers."""
    import httpx

    timeout = WAIT_S if timeout is None else timeout
    started = time.monotonic()
    last = "never answered"
    while time.monotonic() - started < timeout:
        try:
            response = httpx.get(f"{upstream.rstrip('/')}/bigquery/v2/projects", timeout=5)
            ids = [p.get("id") for p in response.json().get("projects", [])]
            if response.status_code == 200 and project in ids:
                return round(time.monotonic() - started, 3)
            last = f"HTTP {response.status_code}, projects {ids}"
        except (httpx.HTTPError, ValueError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.2)
    raise SeedError(f"the BigQuery emulator at {upstream} did not serve project "
                    f"{project!r} within {timeout:.0f}s ({last}); is it started with "
                    f"--project={project}?")


def json_cell(value: Any) -> Any:
    """One DuckDB value as `tabledata.insertAll` JSON. Exact: decimals as text."""
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, float) and value != value:          # NaN
        return "NaN"
    return value


def relations(path: Path) -> list[tuple[str, str]]:
    """(schema, relation) for every table and view in one DuckDB file, sorted."""
    import duckdb

    con = duckdb.connect(str(path), read_only=True)
    try:
        return [tuple(row) for row in con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_catalog = current_database() "
            f"AND table_schema NOT IN {SYSTEM_SCHEMAS!r} ORDER BY 1, 2").fetchall()]
    finally:
        con.close()


def _ensure_dataset(http, project: str, dataset: str) -> None:
    response = http.get(f"{_api(project)}/datasets/{dataset}")
    if response.status_code == 200:
        return
    response = http.post(f"{_api(project)}/datasets", json={
        "datasetReference": {"projectId": project, "datasetId": dataset}})
    if response.status_code not in (200, 409):
        raise SeedError(f"creating dataset {project}.{dataset}: HTTP "
                        f"{response.status_code} {_error(response)}")


def _replace_table(http, project: str, dataset: str, table: str,
                   columns: list[tuple[str, str]]) -> None:
    base = f"{_api(project)}/datasets/{dataset}/tables"
    if http.get(f"{base}/{table}").status_code == 200:
        response = http.delete(f"{base}/{table}")
        if response.status_code >= 300:
            raise SeedError(f"dropping {project}.{dataset}.{table} to re-seed it: HTTP "
                            f"{response.status_code} {_error(response)}")
    response = http.post(base, json={
        "tableReference": {"projectId": project, "datasetId": dataset, "tableId": table},
        "schema": {"fields": [{"name": name, "type": kind, "mode": "NULLABLE"}
                              for name, kind in columns]}})
    if response.status_code != 200:
        raise SeedError(f"creating {project}.{dataset}.{table}: HTTP "
                        f"{response.status_code} {_error(response)}")


def _insert(http, project: str, dataset: str, table: str,
            columns: list[tuple[str, str]], rows: list[list]) -> None:
    names = [name for name, _ in columns]
    url = f"{_api(project)}/datasets/{dataset}/tables/{table}/insertAll"
    for start in range(0, len(rows), BATCH):
        body = {"skipInvalidRows": False, "ignoreUnknownValues": False, "rows": [
            {"json": {n: json_cell(v) for n, v in zip(names, row, strict=True)
                      if v is not None}}
            for row in rows[start:start + BATCH]]}
        response = http.post(url, json=body)
        errors = response.json().get("insertErrors") if response.status_code == 200 else None
        if response.status_code != 200 or errors:
            raise SeedError(f"loading {project}.{dataset}.{table} rows {start}..: HTTP "
                            f"{response.status_code} "
                            f"{errors[:3] if errors else _error(response)}")


def _count(http, project: str, dataset: str, table: str) -> int:
    """The table's `numRows` as `tables.get` reports it. Read from the table
    resource rather than by a COUNT(*) query because a query is a JOB, and the
    emulator's job list is what the proxy's statement rows are checked 1:1
    against (tasks/harness/bigquery_e2e_check.py): seeding must leave none."""
    response = http.get(f"{_api(project)}/datasets/{dataset}/tables/{table}")
    if response.status_code != 200:
        raise SeedError(f"reading back {project}.{dataset}.{table}: HTTP "
                        f"{response.status_code} {_error(response)}")
    return int(response.json().get("numRows") or 0)


def seed(upstream: str, project: str, warehouse_files: Sequence[Path]) -> dict[str, int]:
    """Copy every relation of `warehouse_files` into `project`; rows per table.

    Keys are `<dataset>.<table>`, values the row count the EMULATOR reports for
    the loaded table, which must equal the source's or this raises `SeedError`.
    Goes straight to `upstream` (the emulator, never the proxy): no audit rows.
    """
    import duckdb
    import httpx

    from ..bigquery import duckdb_rows

    counts: dict[str, int] = {}
    owner: dict[str, Path] = {}
    with httpx.Client(base_url=upstream.rstrip("/"), timeout=REQUEST_S) as http:
        for path in warehouse_files:
            path = Path(path)
            listing = relations(path)
            for schema in sorted({schema for schema, _ in listing}):
                if schema in owner and owner[schema] != path:
                    raise SeedError(
                        f"dataset {schema!r} would be seeded from both {owner[schema]} and "
                        f"{path}: one BigQuery project holds one dataset of a name")
                owner[schema] = path
                _ensure_dataset(http, project, schema)
            con = duckdb.connect(str(path), read_only=True)
            try:
                catalog = con.execute("SELECT current_database()").fetchone()[0]
                for schema, table in listing:
                    try:
                        columns, rows = duckdb_rows(
                            con, f'SELECT * FROM "{catalog}"."{schema}"."{table}"')
                    except ValueError as exc:
                        raise SeedError(f"{path.name} {schema}.{table}: {exc}") from None
                    _replace_table(http, project, schema, table, columns)
                    _insert(http, project, schema, table, columns, rows)
                    loaded = _count(http, project, schema, table)
                    if loaded != len(rows):
                        raise SeedError(f"{project}.{schema}.{table} holds {loaded} rows "
                                        f"after seeding {len(rows)} from {path.name}")
                    counts[f"{schema}.{table}"] = loaded
            finally:
                con.close()
    return counts
