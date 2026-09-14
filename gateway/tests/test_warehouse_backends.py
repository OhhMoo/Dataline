"""Every warehouse backend runs as its own parametrised test id.

The failure mode this file exists to prevent: a connector interface, a factory, a
config field, a registered tool and a green suite, while every fixture still runs
DuckDB because standing up the other backend was the expensive part. A branch
inside one test hides that. A distinct id cannot - test *count* is diffed by
tooling, so a backend that was never exercised fails to move the number and is
visible in one second of runtime.

So: `test_sql_query[duckdb]` and `test_sql_query[postgres]`, never one test with
an `if backend ==` inside it. When Postgres genuinely cannot be stood up the id
still appears, as a skip that names the reason - an honest gap. A pass that
silently ran DuckDB twice is the defect.

Postgres comes from a real server. The fixture prefers `ALKERA_TEST_POSTGRES_DSN`
when set, otherwise starts the same digest-pinned image `docker/compose.yml`
pins, so the suite and the stack exercise the same build.
"""

import os
import subprocess
import time

import pytest
from conftest import config

from alkera_gateway import Gateway
from alkera_gateway.audit import read_rows
from alkera_gateway.config import GatewayConfig
from alkera_gateway.records import digest, operation_record
from alkera_gateway.warehouse import (
    DuckDBWarehouse,
    PostgresWarehouse,
    create_warehouse,
)

BACKENDS = ["duckdb", "postgres"]

# The digest docker/compose.yml pins. Pinning it here too means the assertion
# below is about the server the episode actually gets, not "some Postgres".
PG_IMAGE = "postgres@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94"
PG_SUPERUSER_PASSWORD = "alkera-test"
# Mirrors docker/postgres.sql: the gateway never connects as a superuser.
PG_ROLE_DSN = "postgresql://episode:episode-local@127.0.0.1:{port}/episode"
PG_ADMIN_DSN = f"postgresql://postgres:{PG_SUPERUSER_PASSWORD}@127.0.0.1:{{port}}/episode"
# A container we just started deserves patience; a DSN the operator supplied does not.
PG_START_TIMEOUT_S = 90
PG_SUPPLIED_TIMEOUT_S = 10

DECIMAL_10_50 = {"type": "decimal", "value": "10.50"}
DECIMAL_20_25 = {"type": "decimal", "value": "20.25"}
DECIMAL_5_00 = {"type": "decimal", "value": "5.00"}

SEED = (
    "DROP SCHEMA IF EXISTS warehouse CASCADE",
    "CREATE SCHEMA warehouse AUTHORIZATION episode",
    "SET ROLE episode",
    "CREATE TABLE warehouse.items(id INTEGER, amount DECIMAL(10,2))",
    "INSERT INTO warehouse.items VALUES (1, 10.50), (2, 20.25)",
)


def assert_ok(result):
    assert result["ok"], result["error"]
    return result["result"]


def docker(*args, timeout=180):
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, encoding="utf-8", timeout=timeout
    )


def wait_for_postgres(dsn, timeout_s):
    import psycopg

    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
            return None
        except Exception as exc:  # noqa: BLE001 - the reason is what we report
            last = exc
            time.sleep(0.5)
    return last


@pytest.fixture(scope="session")
def postgres_admin_dsn():
    """A real Postgres, or a skip that says exactly why there is not one."""
    supplied = os.environ.get("ALKERA_TEST_POSTGRES_DSN")
    if supplied:
        failure = wait_for_postgres(supplied, PG_SUPPLIED_TIMEOUT_S)
        if failure is not None:
            pytest.skip(f"ALKERA_TEST_POSTGRES_DSN is set but unreachable: {failure}")
        yield supplied
        return

    try:
        version = docker("version", "--format", "{{.Server.Version}}", timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"Postgres backend not exercised: docker is unusable ({exc})")
    if version.returncode:
        pytest.skip(
            "Postgres backend not exercised: no docker daemon and ALKERA_TEST_POSTGRES_DSN "
            f"is unset ({version.stderr.strip().splitlines()[:1]})"
        )

    started = docker(
        "run",
        "-d",
        "--rm",
        # Port 0: the OS picks. A fixed port can land inside a Windows excluded
        # port range (netsh int ipv4 show excludedportrange), where Docker
        # accepts -p and then silently publishes nothing.
        "-p",
        "127.0.0.1::5432",
        "-e",
        f"POSTGRES_PASSWORD={PG_SUPERUSER_PASSWORD}",
        "-e",
        "POSTGRES_DB=episode",
        PG_IMAGE,
    )
    if started.returncode:
        pytest.skip(f"Postgres backend not exercised: {started.stderr.strip()[:300]}")
    container = started.stdout.strip()
    try:
        mapping = docker("port", container, "5432/tcp", timeout=30)
        published = mapping.stdout.strip().splitlines()
        if mapping.returncode or not published:
            pytest.skip(
                "Postgres backend not exercised: docker published no host port for the "
                "container, so the test process cannot reach it"
            )
        dsn = PG_ADMIN_DSN.format(port=int(published[0].rsplit(":", 1)[1]))
        failure = wait_for_postgres(dsn, PG_START_TIMEOUT_S)
        if failure is not None:
            pytest.skip(
                f"Postgres backend not exercised: server never accepted a connection "
                f"within {PG_START_TIMEOUT_S}s ({failure})"
            )
        yield dsn
    finally:
        docker("rm", "-f", container, timeout=60)


@pytest.fixture
def postgres_dsn(postgres_admin_dsn):
    """Reseed the warehouse schema so each test starts from the fixture world."""
    import psycopg

    with psycopg.connect(postgres_admin_dsn, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'episode'").fetchone():
            conn.execute(
                "CREATE ROLE episode LOGIN PASSWORD 'episode-local' "
                "NOSUPERUSER NOCREATEDB NOCREATEROLE"
            )
        for statement in SEED:
            conn.execute(statement)
    port = postgres_admin_dsn.rsplit(":", 1)[1].split("/")[0]
    return PG_ROLE_DSN.format(port=port)


@pytest.fixture(params=BACKENDS)
def backend_gateway(request, world, tmp_path, monkeypatch):
    """One gateway per backend. The id is the backend, so the run names what ran."""
    backend = request.param
    if backend != "duckdb":
        monkeypatch.setenv("ALKERA_POSTGRES_DSN", request.getfixturevalue(f"{backend}_dsn"))
    gateway = Gateway(config(world, tmp_path / "audit", warehouse_backend=backend))
    try:
        yield gateway
    finally:
        gateway.close()


CALLS = (
    ("sql_query", {"sql": "SELECT id, amount FROM warehouse.items ORDER BY id"}),
    ("sql_execute", {"sql": "UPDATE warehouse.items SET amount = 12.75 WHERE id = 1"}),
    ("sql_query", {"sql": "SELECT count(*) AS n FROM warehouse.items"}),
    ("sql_execute", {"sql": "DELETE FROM warehouse.items"}),
)


def expected_row(seq, tool, sql, result, **observed):
    args = {"sql": sql} if tool == "sql_execute" else {"sql": sql, "limit": 1000}
    return {
        "seq": seq,
        "vts": "2026-03-05T09:00:00Z",
        "tool": tool,
        "args": args,
        "args_hash": digest(args),
        "op_class": observed.get("op_class", "read"),
        "targets": ["warehouse.items"],
        "read_targets": observed.get("read_targets", []),
        "flags": observed.get("flags", []),
        "rows_affected": observed.get("rows_affected"),
        "result": result,
        "result_hash": digest(result),
        "exit": "ok",
        "error": None,
    }


def counted(count):
    return {"columns": [{"name": "Count", "type": "BIGINT"}], "rows": [[count]], "truncated": False}


# agent.md 9.2.2 is frozen: one row per tool call, identical from MCP and CLI and
# - the point of the connector seam - identical whichever backend served it.
# Both parametrisations compare against this same literal, so equality between
# the backends follows from each of them passing.
EXPECTED_ROWS = [
    expected_row(
        1,
        "sql_query",
        CALLS[0][1]["sql"],
        {
            "columns": [
                {"name": "id", "type": "INTEGER"},
                {"name": "amount", "type": "DECIMAL(10,2)"},
            ],
            "rows": [[1, DECIMAL_10_50], [2, DECIMAL_20_25]],
            "truncated": False,
        },
        read_targets=["warehouse.items"],
    ),
    expected_row(2, "sql_execute", CALLS[1][1]["sql"], counted(1), op_class="write",
                 rows_affected=1),
    expected_row(
        3,
        "sql_query",
        CALLS[2][1]["sql"],
        {"columns": [{"name": "n", "type": "BIGINT"}], "rows": [[2]], "truncated": False},
        read_targets=["warehouse.items"],
    ),
    expected_row(4, "sql_execute", CALLS[3][1]["sql"], counted(2), op_class="destructive",
                 flags=["delete", "unbounded_delete"], rows_affected=2),
]


def test_sql_query(backend_gateway):
    """The read path, on the backend named in the test id."""
    backend = backend_gateway.config.warehouse_backend
    connector = backend_gateway.operations.warehouse
    # The dialect is the connector's property, not a string sql.py holds.
    assert connector.dialect == backend

    result = assert_ok(
        backend_gateway.call(
            "sql_query", {"sql": "SELECT id, amount FROM warehouse.items ORDER BY id"}
        )
    )
    assert result["columns"] == [
        {"name": "id", "type": "INTEGER"},
        {"name": "amount", "type": "DECIMAL(10,2)"},
    ]
    # Decimals survive as decimals on both engines; a float here is silent money loss.
    assert result["rows"] == [[1, DECIMAL_10_50], [2, DECIMAL_20_25]]
    assert result["truncated"] is False

    limited = assert_ok(
        backend_gateway.call(
            "sql_query", {"sql": "SELECT id FROM warehouse.items ORDER BY id", "limit": 1}
        )
    )
    assert limited["rows"] == [[1]] and limited["truncated"] is True

    rejected = backend_gateway.call("sql_query", {"sql": "DELETE FROM warehouse.items"})
    assert rejected["error"]["code"] == "read_only"


def test_sql_execute(backend_gateway):
    """Multi-statement atomicity, on the backend named in the test id."""
    result = assert_ok(
        backend_gateway.call(
            "sql_execute",
            {
                "sql": "DELETE FROM warehouse.items WHERE id = 1; "
                "INSERT INTO warehouse.items VALUES (3, 5.00)"
            },
        )
    )
    assert len(result["statements"]) == 2

    failed = backend_gateway.call(
        "sql_execute",
        {
            "sql": "UPDATE warehouse.items SET amount = 99 WHERE id = 2; "
            "INSERT INTO warehouse.missing VALUES (1)"
        },
    )
    assert not failed["ok"]

    rows = assert_ok(
        backend_gateway.call(
            "sql_query", {"sql": "SELECT id, amount FROM warehouse.items ORDER BY id"}
        )
    )["rows"]
    # The failed batch rolled back whole: 99 never landed.
    assert rows == [[2, DECIMAL_20_25], [3, DECIMAL_5_00]]


def test_action_log_rows(backend_gateway):
    """The frozen 9.2.2 row, on the backend named in the test id.

    Both parametrisations assert against one literal, so this is the
    backend-independence claim itself, not a shape check that each backend
    could satisfy differently.
    """
    for tool, args in CALLS:
        assert_ok(backend_gateway.call(tool, args))
    rows = [operation_record(row) for row in read_rows(backend_gateway.audit.actions)]
    assert rows == EXPECTED_ROWS


def test_a_world_without_a_declared_backend_still_gets_duckdb():
    """Deliverable 3's real requirement: every existing world keeps working untouched."""
    assert GatewayConfig().warehouse_backend == "duckdb"
    assert "warehouse_backend" not in GatewayConfig().model_dump(exclude_unset=True)


def test_create_warehouse_selects_the_declared_backend(world, tmp_path):
    duck = create_warehouse(config(world, tmp_path / "a1"))
    postgres = create_warehouse(config(world, tmp_path / "a2", warehouse_backend="postgres"))
    assert isinstance(duck, DuckDBWarehouse) and duck.dialect == "duckdb"
    assert isinstance(postgres, PostgresWarehouse) and postgres.dialect == "postgres"
