"""The Snowflake plug, end to end against a REAL fakesnow server.

Not a mock of the connector: `snowflake-connector-python` logs in over HTTP to
a fakesnow server started in this process, with `docker/snowflake_fidelity.py`
applied exactly the way `docker/snowflake_start.py` applies it in the episode's
snowflake service. So every refusal and every count asserted below is the one
the agent gets, not the one stock fakesnow would have given.

What is NOT exercised here, because it cannot be in-process: the TLS front door
(`snowflake_proxy`, tested on its own in test_snowflake_proxy.py), the
`acme.snowflake.local` hostname and the trust bundle. The options JSON points the
connector straight at the upstream with `protocol: http`.

Governing rule: loud unsupported is fine, silent wrong is disqualifying.
"""

import contextlib
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from conftest import config

from alkera_gateway import Gateway
from alkera_gateway.audit import read_rows
from alkera_gateway.records import Observation, ToolError, operation_record

snowflake_connector = pytest.importorskip("snowflake.connector")
pytest.importorskip("fakesnow")

DOCKER = Path(__file__).resolve().parents[1] / "docker"
OPTIONS_ENV = "ALKERA_TEST_SNOWFLAKE_OPTIONS"


def fidelity():
    if str(DOCKER) not in sys.path:
        sys.path.insert(0, str(DOCKER))
    import snowflake_fidelity

    return snowflake_fidelity


@pytest.fixture(scope="module")
def fakesnow_server():
    """The episode's snowflake service, minus TLS: patched fakesnow on loopback."""
    import fakesnow.server as fakesnow_server
    import uvicorn
    from fakesnow.instance import FakeSnow

    fakesnow_server.shared_fs = FakeSnow()
    app = fidelity().apply(fakesnow_server)
    with contextlib.closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, name="fakesnow", daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while thread.is_alive() and not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        pytest.fail("fakesnow did not start")
    yield {"host": "127.0.0.1", "port": port}
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def options(fakesnow_server, monkeypatch):
    """Connection options for one test: its own database, so tests never share state."""
    value = {
        "account": "acme",
        "user": "alkera",
        "password": "local-only",
        "host": fakesnow_server["host"],
        "port": fakesnow_server["port"],
        "protocol": "http",
        "database": f"T{uuid4().hex[:12].upper()}",
        "schema": "WAREHOUSE",
        "warehouse": "COMPUTE_WH",
        "session_parameters": {"CLIENT_OUT_OF_BAND_TELEMETRY_ENABLED": False},
    }
    monkeypatch.setenv(OPTIONS_ENV, json.dumps(value))
    return value


def raw(options, *statements):
    """Seed / inspect behind the gateway's back - never through the connector under test."""
    with snowflake_connector.connect(**options, login_timeout=5, network_timeout=10) as conn:
        with conn.cursor() as cursor:
            result = None
            for statement in statements:
                cursor.execute(statement)
                result = cursor.fetchall() if cursor.description else None
            return result


def seed(options):
    raw(
        options,
        "CREATE TABLE items (id INTEGER, amount NUMBER(10,2))",
        "INSERT INTO items VALUES (1, 10.50), (2, 20.25)",
    )


@pytest.fixture
def gateway(world, tmp_path, options):
    seed(options)
    instance = Gateway(
        config(
            world,
            tmp_path / "audit",
            fingerprint="snowflake-test-fixture",
            warehouse_backend="snowflake",
            snowflake_options_env=OPTIONS_ENV,
        )
    )
    yield instance
    instance.close()


def assert_ok(result):
    assert result["ok"], result["error"]
    return result["result"]


def rows(instance):
    return [operation_record(row) for row in read_rows(instance.audit.actions)]


DEC = lambda value: {"type": "decimal", "value": value}  # noqa: E731


# ------------------------------------------------------------------ sql_query


def test_sql_query_reads_through_the_real_connector(gateway):
    result = assert_ok(
        gateway.call("sql_query", {"sql": "SELECT id, amount FROM items ORDER BY id", "limit": 10})
    )
    assert result["rows"] == [[1, DEC("10.50")], [2, DEC("20.25")]]
    # NUMBER stays NUMBER with its precision and scale - never a float.
    assert result["columns"] == [
        {"name": "ID", "type": "NUMBER(38,0)"},
        {"name": "AMOUNT", "type": "NUMBER(10,2)"},
    ]
    assert result["truncated"] is False
    row = rows(gateway)[0]
    assert row["op_class"] == "read"
    assert row["targets"] == ["items"]


def test_sql_query_truncates_at_the_limit(gateway):
    result = assert_ok(gateway.call("sql_query", {"sql": "SELECT id FROM items", "limit": 1}))
    assert len(result["rows"]) == 1 and result["truncated"] is True


def test_sql_query_refuses_a_write_before_connecting(gateway):
    refused = gateway.call("sql_query", {"sql": "DELETE FROM items", "limit": 10})
    assert refused["error"]["code"] == "read_only"


def test_session_lands_in_the_configured_database_and_schema(gateway, options):
    result = assert_ok(
        gateway.call(
            "sql_query",
            {"sql": "SELECT CURRENT_DATABASE(), CURRENT_SCHEMA(), CURRENT_ACCOUNT()", "limit": 1},
        )
    )
    assert result["rows"] == [[options["database"], "WAREHOUSE", "ACME"]]


def test_missing_options_secret_is_unconfigured(gateway, monkeypatch):
    monkeypatch.delenv(OPTIONS_ENV)
    refused = gateway.call("sql_query", {"sql": "SELECT 1", "limit": 1})
    assert refused["error"]["code"] == "warehouse_unconfigured"


# ---------------------------------------------------------------- sql_execute


def test_single_dml_returns_the_count_shape_and_audits_rows_affected(gateway, options):
    result = assert_ok(
        gateway.call("sql_execute", {"sql": "UPDATE items SET amount = 11.00 WHERE id = 1"})
    )
    # The connector contract's DML shape, the one DuckDB and Postgres return -
    # not Snowflake's ("number of rows updated", "number of multi-joined rows updated").
    assert result == {
        "columns": [{"name": "Count", "type": "BIGINT"}], "rows": [[1]], "truncated": False,
    }
    row = rows(gateway)[0]
    assert row["op_class"] == "write"
    assert row["targets"] == ["items"]
    assert row["rows_affected"] == 1
    assert raw(options, "SELECT amount FROM items WHERE id = 1")[0][0] == pytest.approx(11)


def test_multi_statement_execute_commits_atomically_with_one_result_per_statement(
    gateway, options
):
    result = assert_ok(
        gateway.call(
            "sql_execute",
            {
                "sql": "INSERT INTO items VALUES (3, 5.00); "
                "UPDATE items SET amount = amount + 1 WHERE id = 3; "
                "SELECT id, amount FROM items WHERE id = 3"
            },
        )
    )
    count = [{"name": "Count", "type": "BIGINT"}]
    assert result["statements"][0] == {"columns": count, "rows": [[1]], "truncated": False}
    assert result["statements"][1] == {"columns": count, "rows": [[1]], "truncated": False}
    assert result["statements"][2]["rows"] == [[3, DEC("6.00")]]
    assert len(result["statements"]) == 3
    row = rows(gateway)[0]
    assert row["rows_affected"] == 2
    assert row["targets"] == ["items"]
    assert sorted(raw(options, "SELECT id FROM items")) == [(1,), (2,), (3,)]


def test_a_failed_statement_rolls_back_the_whole_batch_and_says_which(gateway, options):
    """Atomic: the first INSERT is undone. The failure names its statement.

    fakesnow's transaction is aborted by the failing statement (DIVERGENCES
    `failed_statement_aborts_the_transaction`); the gateway never tries to
    continue past it, so that divergence cannot change what the batch does -
    the batch fails as a whole and is rolled back, on Snowflake and here alike.
    """
    refused = gateway.call(
        "sql_execute",
        {"sql": "INSERT INTO items VALUES (3, 5.00); INSERT INTO no_such_table VALUES (1)"},
    )
    assert refused["error"]["code"] == "warehouse_error"
    message = refused["error"]["message"]
    assert "statement 2 of 2" in message and "rolled back" in message
    assert "NO_SUCH_TABLE" in message.upper()
    assert sorted(raw(options, "SELECT id FROM items")) == [(1,), (2,)]


def test_a_batch_that_divides_by_zero_rolls_back_loudly(gateway, options):
    refused = gateway.call(
        "sql_execute",
        {"sql": "DELETE FROM items WHERE id = 1; "
                "UPDATE items SET amount = amount / 0 WHERE id = 2"},
    )
    assert refused["error"]["code"] == "warehouse_error"
    assert "Division by zero" in refused["error"]["message"]
    assert sorted(raw(options, "SELECT id FROM items")) == [(1,), (2,)]


def test_ddl_inside_a_batch_is_refused_before_anything_runs(gateway, options):
    """Snowflake commits DDL implicitly, so a batch containing it cannot be
    atomic there, while fakesnow (DuckDB) would roll the DDL back. Executing it
    would teach a false lesson; refusing is loud."""
    refused = gateway.call(
        "sql_execute",
        {"sql": "INSERT INTO items VALUES (3, 5.00); CREATE TABLE other (id INTEGER)"},
    )
    assert refused["error"]["code"] == "unsupported_sql"
    assert "DDL" in refused["error"]["message"]
    assert sorted(raw(options, "SELECT id FROM items")) == [(1,), (2,)]
    # ...and the same DDL on its own is fine.
    assert_ok(gateway.call("sql_execute", {"sql": "CREATE TABLE other (id INTEGER)"}))


def test_audit_targets_come_from_the_snowflake_dialect(gateway):
    assert_ok(
        gateway.call(
            "sql_execute",
            {"sql": "INSERT INTO items SELECT id + 10, amount FROM items; DELETE FROM items"},
        )
    )
    row = rows(gateway)[0]
    assert row["op_class"] == "destructive"
    assert "unbounded_delete" in row["flags"]
    assert row["targets"] == ["items"]
    assert row["rows_affected"] == 6


def test_snowflake_qualified_names_are_targets(gateway, options):
    database = options["database"]
    assert_ok(
        gateway.call(
            "sql_query",
            {"sql": f"SELECT COUNT(*) FROM {database}.WAREHOUSE.items", "limit": 1},
        )
    )
    assert rows(gateway)[0]["targets"] == [f"{database}.WAREHOUSE.items"]


# --------------------------------------------------- fences: loud, never silent


def test_division_by_zero_is_snowflakes_error_not_inf(gateway):
    refused = gateway.call("sql_query", {"sql": "SELECT amount / 0 FROM items", "limit": 10})
    assert refused["error"]["code"] == "warehouse_error"
    assert "Division by zero" in refused["error"]["message"]


@pytest.mark.parametrize("statement", [
    "UPDATE ratios SET r = r / 0",
    "UPDATE ratios SET r = 1 WHERE r / 0 > 1",
    "DELETE FROM ratios WHERE r % 0 = 1",
    "INSERT INTO ratios VALUES (1 / 0)",
    "INSERT INTO ratios SELECT r / 0 FROM ratios",
    "MERGE INTO ratios USING (SELECT 2.0 AS r) s ON ratios.r = s.r "
    "WHEN MATCHED THEN UPDATE SET r = s.r / 0",
])
def test_division_by_zero_in_a_write_is_refused_not_stored_as_inf(gateway, options, statement):
    """DuckDB answers x/0 with inf and x%0 with NULL. A FLOAT column takes either
    without complaint, so outside a SELECT the stock fence let a write store
    `inf` and report success - silent wrong. Snowflake raises 100051."""
    raw(options, "CREATE TABLE ratios (r FLOAT)", "INSERT INTO ratios VALUES (2.0)")
    refused = gateway.call("sql_execute", {"sql": statement})
    assert refused["ok"] is False, statement
    assert "Division by zero" in refused["error"]["message"]
    assert raw(options, "SELECT r FROM ratios") == [(2.0,)]


def test_dynamic_table_is_refused_by_name(gateway):
    refused = gateway.call(
        "sql_execute",
        {"sql": "CREATE DYNAMIC TABLE d TARGET_LAG = '1 minute' WAREHOUSE = COMPUTE_WH "
                "AS SELECT * FROM items"},
    )
    assert refused["ok"] is False
    assert "DYNAMIC" in refused["error"]["message"].upper()


def test_number_division_is_reported_as_float_not_dressed_up_as_number(gateway):
    """DIVERGENCES `number_arithmetic_is_float`: Snowflake returns NUMBER here.

    The gateway does not approximate it back into a decimal. The column type
    says FLOAT, so the difference is visible in the result rather than hidden
    in the trailing digits.
    """
    result = assert_ok(
        gateway.call("sql_query", {"sql": "SELECT amount / 3 AS third FROM items", "limit": 10})
    )
    assert result["columns"] == [{"name": "THIRD", "type": "FLOAT"}]
    assert all(isinstance(value, float) for value, in result["rows"])
    ids = [entry[0] for entry in fidelity().DIVERGENCES]
    assert "number_arithmetic_is_float" in ids


# ---------------------------------------------------------- the connector itself


def test_the_connector_ignores_caller_timeouts_in_the_options(world, tmp_path, options,
                                                              monkeypatch):
    """`fakesnow.server()` hands out `network_timeout`; the gateway's own bound wins
    instead of `connect()` failing on a duplicated keyword."""
    from alkera_gateway.snowflake import SnowflakeWarehouse

    monkeypatch.setenv(OPTIONS_ENV, json.dumps({**options, "network_timeout": 1,
                                                "login_timeout": 1}))
    seed(options)
    cfg = config(world, tmp_path / "audit", fingerprint="x", warehouse_backend="snowflake",
                 snowflake_options_env=OPTIONS_ENV)
    result = SnowflakeWarehouse(cfg).execute("SELECT COUNT(*) FROM items", Observation(),
                                             read_only=True)
    assert result["rows"] == [[2]]


def test_invalid_sql_never_reaches_the_server(world, tmp_path, options):
    from alkera_gateway.snowflake import SnowflakeWarehouse

    cfg = config(world, tmp_path / "audit", fingerprint="x", warehouse_backend="snowflake",
                 snowflake_options_env=OPTIONS_ENV)
    with pytest.raises(ToolError) as excinfo:
        SnowflakeWarehouse(cfg).execute("SELEC 1 FROM", Observation())
    assert excinfo.value.code == "invalid_sql"


# ------------------------------------------------------------------------ dbt

VIEW_MODEL = """{{ config(materialized='view') }}
select id, amount from {{ source('raw', 'items') }}
"""
INCREMENTAL_MODEL = """{{ config(materialized='incremental', unique_key='id') }}
select id, amount from {{ ref('items_view') }}
{% if is_incremental() %}
where id >= (select max(id) from {{ this }})
{% endif %}
"""
SCHEMA = """version: 2
sources:
  - name: raw
    schema: WAREHOUSE
    tables:
      - name: items
models:
  - name: items_view
  - name: items_incremental
    columns:
      - name: id
        data_tests: [not_null, unique]
"""


@pytest.fixture
def dbt_gateway(world, tmp_path, options):
    """The conftest world, its dbt project replaced by a two-model Snowflake project
    and its profiles.yml rendered by `warehouses.dbt_output` - the secret stays in
    the environment; the profile only names the variable."""
    from alkera_gateway.warehouses import dbt_output

    seed(options)
    project = world / "workspace" / "dbt_project"
    for path in (project / "models").iterdir():
        path.unlink()
    (project / "models" / "items_view.sql").write_text(VIEW_MODEL, encoding="utf-8")
    (project / "models" / "items_incremental.sql").write_text(INCREMENTAL_MODEL, encoding="utf-8")
    (project / "models" / "schema.yml").write_text(SCHEMA, encoding="utf-8")
    profiles = {"fixture": {"target": "dev", "outputs": {
        "dev": dbt_output("snowflake", schema="MARTS", options_env=OPTIONS_ENV),
    }}}
    text = yaml.safe_dump(profiles)
    assert options["password"] not in text and str(options["port"]) not in text
    (world / "build" / "profiles.yml").write_text(text, encoding="utf-8")
    instance = Gateway(
        config(world, tmp_path / "audit", fingerprint="snowflake-dbt-fixture",
               warehouse_backend="snowflake", snowflake_options_env=OPTIONS_ENV)
    )
    yield instance
    instance.close()


def dbt_ok(result):
    payload = assert_ok(result)
    assert payload["returncode"] == 0, payload["output"]
    return payload


def test_dbt_run_builds_a_view_and_an_incremental_model_twice(dbt_gateway, options):
    """The run that matters is the SECOND one: the incremental merge path, which
    stock fakesnow breaks five ways (snowflake_fidelity items 1 and the four
    found by running dbt). The second run must merge, not duplicate."""
    run = {"selector": "*", "variables": {}, "full_refresh": False}
    first = dbt_ok(dbt_gateway.call("dbt_run", run))
    assert "PASS=2" in first["output"]
    assert sorted(raw(options, "SELECT id, amount FROM MARTS.items_incremental")) == [
        (1, pytest.approx(10.5)), (2, pytest.approx(20.25))]

    raw(options, "INSERT INTO items VALUES (3, 7.25)", "UPDATE items SET amount = 21 WHERE id = 2")
    second = dbt_ok(dbt_gateway.call("dbt_run", run))
    assert "PASS=2" in second["output"]
    merged = raw(options, "SELECT id, amount FROM MARTS.items_incremental ORDER BY id")
    # id 2 was re-selected (id >= max) and MERGED onto, id 3 inserted, id 1 untouched
    assert [row[0] for row in merged] == [1, 2, 3]
    assert [str(row[1]) for row in merged] == ["10.50", "21.00", "7.25"]
    assert raw(options, "SELECT COUNT(*) FROM MARTS.items_view") == [(3,)]

    actions = rows(dbt_gateway)
    assert [row["tool"] for row in actions] == ["dbt_run", "dbt_run"]
    assert all(row["targets"] == ["dbt_project"] for row in actions)


def test_dbt_test_and_compile_run_against_snowflake(dbt_gateway):
    dbt_ok(dbt_gateway.call("dbt_run", {"selector": "*", "variables": {},
                                        "full_refresh": False}))
    tested = dbt_ok(dbt_gateway.call("dbt_test", {"selector": "*", "variables": {}}))
    assert "PASS=2" in tested["output"]
    dbt_ok(dbt_gateway.call("dbt_compile", {"selector": "items_incremental", "variables": {}}))


def test_dbt_full_refresh_rebuilds_the_incremental(dbt_gateway, options):
    run = {"selector": "*", "variables": {}, "full_refresh": False}
    dbt_ok(dbt_gateway.call("dbt_run", run))
    raw(options, "DELETE FROM items WHERE id = 1")
    dbt_ok(dbt_gateway.call("dbt_run", {**run, "full_refresh": True}))
    assert raw(options, "SELECT id FROM MARTS.items_incremental") == [(2,)]
    assert rows(dbt_gateway)[-1]["op_class"] == "destructive"


# ---------------------------------------------------------- profile rendering


def render(output, profile="fixture"):
    """Resolve a rendered output the way the dbt CLI does, without a server."""
    from argparse import Namespace

    from dbt.config.profile import Profile
    from dbt.config.renderer import ProfileRenderer
    from dbt.flags import set_from_args
    from dbt_common.context import set_invocation_context

    set_from_args(Namespace(), None)
    set_invocation_context(os.environ)
    raw_profile = {"target": "dev", "outputs": {"dev": output}}
    return Profile.from_raw_profile_info(raw_profile, profile, ProfileRenderer({})).credentials


def test_snowflake_output_resolves_from_the_options_secret(options):
    from alkera_gateway.warehouses import dbt_output

    output = dbt_output("snowflake", schema="MARTS", options_env=OPTIONS_ENV)
    assert output["type"] == "snowflake"
    assert options["password"] not in json.dumps(output)
    credentials = render(output)
    assert credentials.account == "acme"
    assert credentials.user == "alkera"
    assert credentials.password == "local-only"
    assert credentials.host == "127.0.0.1"
    assert credentials.port == options["port"]
    assert credentials.protocol == "http"
    assert credentials.database == options["database"]
    assert credentials.schema == "MARTS"
    assert credentials.warehouse == "COMPUTE_WH"


def test_snowflake_output_defaults_to_the_https_front_door(monkeypatch):
    """The episode's options carry only the hostname; dbt must then use the same
    TLS route the connector does, not some default of its own."""
    from alkera_gateway.warehouses import dbt_output

    monkeypatch.setenv(OPTIONS_ENV, json.dumps({
        "account": "acme", "user": "alkera", "password": "x",
        "host": "acme.snowflake.local", "database": "ANALYTICS"}))
    credentials = render(dbt_output("snowflake", schema="MARTS", options_env=OPTIONS_ENV))
    assert (credentials.host, credentials.port, credentials.protocol) == (
        "acme.snowflake.local", 443, "https")
    assert credentials.warehouse is None and credentials.role is None


def test_postgres_output_resolves_from_the_dsn_secret(monkeypatch):
    from alkera_gateway.warehouses import dbt_output

    monkeypatch.setenv("ALKERA_TEST_PG_DSN", "postgresql://episode:episode-local@postgres:5433/episode")
    output = dbt_output("postgres", schema="analytics", dsn_env="ALKERA_TEST_PG_DSN")
    assert output["type"] == "postgres"
    assert "episode-local" not in json.dumps(output)
    credentials = render(output)
    assert (credentials.host, credentials.port, credentials.user, credentials.password,
            credentials.database, credentials.schema) == (
        "postgres", 5433, "episode", "episode-local", "episode", "analytics")

    # the compose DSN has no port: libpq's default
    monkeypatch.setenv("ALKERA_TEST_PG_DSN", "postgresql://episode:episode-local@postgres/episode")
    assert render(output).port == 5432


def test_postgres_output_fails_loudly_on_a_dsn_it_cannot_read(monkeypatch):
    from dbt.exceptions import DbtProfileError

    from alkera_gateway.warehouses import dbt_output

    monkeypatch.setenv("ALKERA_TEST_PG_DSN", "host=postgres user=episode dbname=episode")
    with pytest.raises(DbtProfileError):
        render(dbt_output("postgres", schema="analytics", dsn_env="ALKERA_TEST_PG_DSN"))


def test_duckdb_output_is_the_world_builders_shape(tmp_path):
    from alkera_gateway.warehouses import dbt_output

    path = tmp_path / "warehouse.duckdb"
    assert dbt_output("duckdb", schema="main", path=path) == {
        "type": "duckdb", "path": str(path), "schema": "main", "threads": 1}


def test_every_shipped_backend_has_an_installed_adapter():
    from alkera_gateway.warehouses import DBT_ADAPTERS, dbt_adapter_missing

    # bigquery is declared so a bigquery dbt_target is refused by name
    # (dbt-bigquery is in no image yet); redshift's adapter is in images built
    # with --warehouse-clients redshift only; the other three must be installed.
    assert set(DBT_ADAPTERS) == {"duckdb", "postgres", "snowflake", "bigquery", "redshift"}
    for backend in ("duckdb", "postgres", "snowflake"):
        assert dbt_adapter_missing(backend) is None, backend


# ------------------------------------------- the raw wire (native mode's route)


def test_ddl_inside_a_raw_transaction_is_refused_and_leaves_it_usable(options):
    """DIVERGENCES `ddl_inside_a_transaction_is_refused`. Unfenced, fakesnow rolled
    a DROP TABLE back with the transaction; Snowflake would have committed both
    the DROP and every statement before it. The refusal happens before DuckDB
    sees the statement, so the transaction is not aborted by it."""
    seed(options)
    with snowflake_connector.connect(**options, login_timeout=5, network_timeout=10) as conn:
        cursor = conn.cursor()
        cursor.execute("BEGIN")
        cursor.execute("INSERT INTO items VALUES (3, 1.00)")
        for statement in ("DROP TABLE items", "CREATE TABLE other (id INTEGER)",
                          "ALTER TABLE items ADD COLUMN note VARCHAR",
                          "TRUNCATE TABLE items"):
            with pytest.raises(snowflake_connector.errors.ProgrammingError) as excinfo:
                cursor.execute(statement)
            assert "DDL inside an explicit transaction" in excinfo.value.msg
        cursor.execute("INSERT INTO items VALUES (4, 1.00)")
        cursor.execute("CREATE TEMPORARY TABLE scratch (id INTEGER)")
        cursor.execute("DROP TABLE scratch")
        cursor.execute("COMMIT")
        cursor.execute("CREATE TABLE other (id INTEGER)")
    assert sorted(raw(options, "SELECT id FROM items")) == [(1,), (2,), (3,), (4,)]
    ids = [entry[0] for entry in fidelity().DIVERGENCES]
    assert "ddl_inside_a_transaction_is_refused" in ids


def test_use_warehouse_is_routed_and_answers_current_warehouse(options):
    with snowflake_connector.connect(**options, login_timeout=5, network_timeout=10) as conn:
        cursor = conn.cursor()
        cursor.execute("USE WAREHOUSE WH_ETL")
        assert cursor.execute("SELECT CURRENT_WAREHOUSE()").fetchall() == [("WH_ETL",)]
        with pytest.raises(snowflake_connector.errors.ProgrammingError):
            cursor.execute("USE WAREHOUSE NOPE")
