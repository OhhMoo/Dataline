"""The BigQuery warehouse backend, against a real goccy/bigquery-emulator process.

The emulator is the pinned release binary `docker/bigquery/fetch.py` downloads and
sha256-verifies into ~/.cache/alkera. When it cannot be fetched (offline, an arch
with no pin) every test here skips with the reason - a gap the run names, never a
pass that quietly exercised DuckDB.

What is asserted is the connector contract (SURFACES.md): result shapes, type
normalisation, row counts, read-only refusal, audit targets - plus the two fences
this backend adds: no cost fields ever reach a result, and a dry-run (cost
estimate) is refused loudly rather than answered with the emulator's numbers.
"""

import importlib.util
import json
import socket
import subprocess
import time
import urllib.request
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest
from conftest import config

from alkera_gateway import Gateway
from alkera_gateway.audit import read_rows
from alkera_gateway.config import GatewayConfig
from alkera_gateway.records import Observation, ToolError, canonical, operation_record
from alkera_gateway.warehouse import create_warehouse

FETCH = Path(__file__).resolve().parents[1] / "docker" / "bigquery" / "fetch.py"
PROJECT = "test"
START_TIMEOUT_S = 30

DECIMAL_10_5 = {"type": "decimal", "value": "10.5"}
DECIMAL_20_25 = {"type": "decimal", "value": "20.25"}


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def load_fetch():
    spec = importlib.util.spec_from_file_location("bigquery_fetch", FETCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def emulator():
    """A running emulator's REST endpoint, or a skip that says why there is none."""
    try:
        binary = load_fetch().fetch()
    except (OSError, SystemExit) as exc:
        pytest.skip(f"BigQuery backend not exercised: emulator binary unavailable ({exc})")
    port, grpc_port = free_port(), free_port()
    process = subprocess.Popen(
        [
            str(binary),
            f"--project={PROJECT}",
            "--host=127.0.0.1",
            f"--port={port}",
            f"--grpc-port={grpc_port}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    endpoint = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + START_TIMEOUT_S
    try:
        while True:
            try:
                urllib.request.urlopen(
                    f"{endpoint}/bigquery/v2/projects/{PROJECT}/datasets", timeout=2
                ).read()
                break
            except OSError as exc:
                if process.poll() is not None or time.monotonic() > deadline:
                    pytest.skip(f"BigQuery backend not exercised: emulator never served ({exc})")
                time.sleep(0.2)
        yield endpoint
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def options(emulator, monkeypatch):
    """A fresh dataset per test, seeded through the helper the world builder uses."""
    from alkera_gateway.bigquery import seed_table

    dataset = f"wh_{uuid4().hex[:12]}"
    opts = {"project": PROJECT, "dataset": dataset, "api_endpoint": emulator}
    seed_table(
        opts,
        "items",
        [("id", "INT64"), ("amount", "NUMERIC"), ("name", "STRING")],
        [{"id": 1, "amount": "10.50", "name": "a"}, {"id": 2, "amount": "20.25", "name": "b"}],
    )
    monkeypatch.setenv("ALKERA_BIGQUERY_OPTIONS", json.dumps(opts))
    return opts


@pytest.fixture
def connector(options, world, tmp_path):
    return create_warehouse(config(world, tmp_path / "audit", warehouse_backend="bigquery"))


@pytest.fixture
def bq_gateway(options, world, tmp_path):
    gateway = Gateway(config(world, tmp_path / "audit", warehouse_backend="bigquery"))
    try:
        yield gateway
    finally:
        gateway.close()


def assert_ok(result):
    assert result["ok"], result["error"]
    return result["result"]


def counted(count):
    return {"columns": [{"name": "Count", "type": "BIGINT"}], "rows": [[count]], "truncated": False}


def items(options):
    return f"{options['dataset']}.items"


# --- configuration -----------------------------------------------------------


def test_bigquery_is_a_remote_backend_and_needs_a_fingerprint():
    with pytest.raises(ValueError, match="initial_world_fp"):
        GatewayConfig(warehouse_backend="bigquery")
    configured = GatewayConfig(warehouse_backend="bigquery", initial_world_fp="x")
    assert configured.bigquery_options_env == "ALKERA_BIGQUERY_OPTIONS"


def test_create_warehouse_selects_bigquery(world, tmp_path):
    from alkera_gateway.bigquery import BigQueryWarehouse

    connector = create_warehouse(config(world, tmp_path / "a", warehouse_backend="bigquery"))
    assert isinstance(connector, BigQueryWarehouse) and connector.dialect == "bigquery"


def test_missing_options_are_unconfigured(world, tmp_path, monkeypatch):
    monkeypatch.delenv("ALKERA_BIGQUERY_OPTIONS", raising=False)
    connector = create_warehouse(config(world, tmp_path / "a", warehouse_backend="bigquery"))
    with pytest.raises(ToolError) as caught:
        connector.execute("SELECT 1", Observation())
    assert caught.value.code == "warehouse_unconfigured"


def test_unknown_option_is_refused(world, tmp_path, monkeypatch):
    monkeypatch.setenv(
        "ALKERA_BIGQUERY_OPTIONS",
        json.dumps({"project": "p", "dataset": "d", "api_endpoint": "http://x", "pool": 3}),
    )
    connector = create_warehouse(config(world, tmp_path / "a", warehouse_backend="bigquery"))
    with pytest.raises(ToolError, match="pool"):
        connector.execute("SELECT 1", Observation())


# --- the cost fence ----------------------------------------------------------


def test_dry_run_is_refused(connector):
    with pytest.raises(ToolError, match="bigquery cost estimation is not modelled"):
        connector.execute("SELECT 1", Observation(), dry_run=True)


def test_dry_run_in_options_is_refused(world, tmp_path, monkeypatch, options):
    monkeypatch.setenv("ALKERA_BIGQUERY_OPTIONS", json.dumps({**options, "dry_run": True}))
    connector = create_warehouse(config(world, tmp_path / "a", warehouse_backend="bigquery"))
    with pytest.raises(ToolError, match="bigquery cost estimation is not modelled"):
        connector.execute("SELECT 1", Observation())


def test_no_cost_field_reaches_a_result(connector, options):
    observation = Observation()
    result = connector.execute(f"SELECT * FROM {items(options)}", observation)
    assert set(result) == {"columns", "rows", "truncated"}
    text = canonical(result).decode().lower()
    for word in ("bytes", "billed", "processed", "slot", "cache"):
        assert word not in text


# --- reads -------------------------------------------------------------------


def test_query_types_are_normalised(connector, options):
    observation = Observation()
    result = connector.execute(
        f"SELECT id, amount, name FROM {items(options)} ORDER BY id", observation, read_only=True
    )
    assert result == {
        "columns": [
            {"name": "id", "type": "BIGINT"},
            {"name": "amount", "type": "DECIMAL"},
            {"name": "name", "type": "VARCHAR"},
        ],
        "rows": [[1, DECIMAL_10_5, "a"], [2, DECIMAL_20_25, "b"]],
        "truncated": False,
    }
    assert observation.op_class == "read"
    assert observation.targets == [items(options)]
    assert observation.read_targets == [items(options)]

    scalar = connector.execute(
        "SELECT TRUE AS ok, 1.5 AS f, DATE '2026-01-01' AS d, "
        "TIMESTAMP '2026-01-01 00:00:00+00' AS ts, DATETIME '2026-01-01 01:02:03' AS dt, "
        "JSON '{\"a\": 1}' AS j, b'ab' AS raw, [1, 2] AS arr",
        Observation(),
        read_only=True,
    )
    assert scalar["columns"] == [
        {"name": "ok", "type": "BOOLEAN"},
        {"name": "f", "type": "DOUBLE"},
        {"name": "d", "type": "DATE"},
        {"name": "ts", "type": "TIMESTAMP WITH TIME ZONE"},
        {"name": "dt", "type": "TIMESTAMP"},
        {"name": "j", "type": "JSON"},
        {"name": "raw", "type": "BLOB"},
        {"name": "arr", "type": "BIGINT[]"},
    ]
    assert scalar["rows"] == [
        [
            True,
            1.5,
            "2026-01-01",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T01:02:03",
            {"a": 1},
            {"type": "bytes", "base64": "YWI="},
            [1, 2],
        ]
    ]


def test_limit_truncates(bq_gateway, options):
    limited = assert_ok(
        bq_gateway.call(
            "sql_query", {"sql": f"SELECT id FROM {items(options)} ORDER BY id", "limit": 1}
        )
    )
    assert limited == {
        "columns": [{"name": "id", "type": "BIGINT"}],
        "rows": [[1]],
        "truncated": True,
    }


def test_read_only_refuses_writes(bq_gateway, options):
    rejected = bq_gateway.call("sql_query", {"sql": f"DELETE FROM {items(options)} WHERE true"})
    assert rejected["error"]["code"] == "read_only"


def test_query_error_carries_the_emulator_message(connector, options):
    with pytest.raises(ToolError) as caught:
        connector.execute(f"SELECT nope FROM {items(options)}", Observation())
    assert caught.value.code == "warehouse_error"
    assert "Unrecognized name: nope" in str(caught.value)


# --- writes ------------------------------------------------------------------


def test_dml_counts_rows(connector, options):
    table = items(options)
    observation = Observation()
    assert connector.execute(
        f"INSERT INTO {table} (id, amount, name) VALUES (3, 1, 'c'), (4, 2, 'd')", observation
    ) == counted(2)
    assert observation.op_class == "write" and observation.rows_affected == 2

    observation = Observation()
    assert connector.execute(
        f"UPDATE {table} SET amount = 0 WHERE id >= 2", observation
    ) == counted(3)
    assert observation.rows_affected == 3

    observation = Observation()
    assert connector.execute(
        f"INSERT INTO {table} (id, amount, name) SELECT id + 10, amount, name FROM {table} "
        "WHERE id < 3",
        observation,
    ) == counted(2)
    assert observation.rows_affected == 2

    observation = Observation()
    assert connector.execute(f"DELETE FROM {table} WHERE id > 10", observation) == counted(2)
    assert observation.op_class == "destructive" and observation.rows_affected == 2

    rows = connector.execute(f"SELECT id, amount FROM {table} ORDER BY id", Observation())["rows"]
    assert rows == [
        [1, DECIMAL_10_5],
        [2, {"type": "decimal", "value": "0"}],
        [3, {"type": "decimal", "value": "0"}],
        [4, {"type": "decimal", "value": "0"}],
    ]


def test_merge_count_is_flagged_not_guessed(connector, options):
    table = items(options)
    observation = Observation()
    result = connector.execute(
        f"MERGE {table} t USING {table} s ON t.id = s.id AND s.id = 1 "
        "WHEN MATCHED THEN UPDATE SET name = 'z'",
        observation,
    )
    assert result == {
        "columns": [{"name": "Count", "type": "BIGINT"}],
        "rows": [],
        "truncated": False,
    }
    assert observation.rows_affected is None
    assert "rows_affected_unreported" in observation.flags
    names = connector.execute(f"SELECT id, name FROM {table} ORDER BY id", Observation())
    assert names["rows"] == [[1, "z"], [2, "b"]]


def test_ddl_returns_an_empty_result(connector, options):
    observation = Observation()
    result = connector.execute(f"CREATE TABLE {options['dataset']}.extra (x INT64)", observation)
    assert result == {
        "columns": [{"name": "Count", "type": "BIGINT"}],
        "rows": [],
        "truncated": False,
    }
    assert observation.op_class == "ddl" and observation.rows_affected is None
    dropped = connector.execute(f"DROP TABLE {options['dataset']}.extra", Observation())
    assert dropped["columns"] == [{"name": "Success", "type": "BOOLEAN"}]


def test_multi_statement(bq_gateway, options):
    table = items(options)
    result = assert_ok(
        bq_gateway.call(
            "sql_execute",
            {
                "sql": f"DELETE FROM {table} WHERE id = 1; "
                f"INSERT INTO {table} (id, amount, name) VALUES (3, 5, 'c'); "
                f"SELECT count(*) AS n FROM {table}"
            },
        )
    )
    assert result["statements"] == [
        counted(1),
        counted(1),
        {"columns": [{"name": "n", "type": "BIGINT"}], "rows": [[2]], "truncated": False},
    ]


def test_multi_statement_failure_says_what_was_applied(bq_gateway, options):
    table = items(options)
    failed = bq_gateway.call(
        "sql_execute",
        {
            "sql": f"UPDATE {table} SET amount = 99 WHERE id = 2; "
            f"INSERT INTO {options['dataset']}.missing VALUES (1)"
        },
    )
    assert not failed["ok"]
    message = failed["error"]["message"]
    # The emulator has no transactions. Loud, with the count of what landed.
    assert "statement 2" in message and "1 earlier statement" in message
    assert "not rolled back" in message


def test_audit_rows_carry_targets(bq_gateway, options):
    table = items(options)
    assert_ok(bq_gateway.call("sql_query", {"sql": f"SELECT id FROM {table}"}))
    assert_ok(
        bq_gateway.call("sql_execute", {"sql": f"UPDATE {table} SET amount = 1 WHERE id = 1"})
    )
    rows = [operation_record(row) for row in read_rows(bq_gateway.audit.actions)]
    assert [
        (r["tool"], r["op_class"], r["targets"], r["read_targets"], r["rows_affected"])
        for r in rows
    ] == [
        ("sql_query", "read", [table], [table], None),
        ("sql_execute", "write", [table], [], 1),
    ]
    assert "bytes" not in canonical(rows).decode().lower()


# --- seeding -----------------------------------------------------------------


def test_seed_from_duckdb(connector, options):
    from alkera_gateway.bigquery import duckdb_rows, seed_table

    with duckdb.connect(":memory:") as conn:
        conn.execute("CREATE TABLE src(id INTEGER, amount DECIMAL(10,2), name VARCHAR, d DATE)")
        conn.execute(
            "INSERT INTO src VALUES (7, 1.25, 'x', DATE '2026-03-01'), (8, NULL, NULL, NULL)"
        )
        columns, rows = duckdb_rows(conn, "SELECT * FROM src ORDER BY id")
    assert columns == [("id", "INT64"), ("amount", "NUMERIC"), ("name", "STRING"), ("d", "DATE")]
    seed_table(options, "copied", columns, rows)
    result = connector.execute(
        f"SELECT * FROM {options['dataset']}.copied ORDER BY id", Observation()
    )
    assert result["rows"] == [
        [7, {"type": "decimal", "value": "1.25"}, "x", "2026-03-01"],
        [8, None, None, None],
    ]


# ------------------------------------------- the registry (no emulator needed)
def test_spec_options_env_follows_the_backend():
    from alkera_gateway.warehouse_spec import WarehouseSpec

    assert WarehouseSpec(name="bq", backend="bigquery",
                         attach=False).options_env == "ALKERA_BIGQUERY_OPTIONS"
    assert WarehouseSpec(name="sf", backend="snowflake",
                         attach=False).options_env == "ALKERA_SNOWFLAKE_OPTIONS"
    assert WarehouseSpec(name="bq", backend="bigquery", attach=False,
                         options_env="MY_BQ").options_env == "MY_BQ"


def test_primary_and_peer_configs_read_the_bigquery_variable(world, tmp_path):
    from alkera_gateway.warehouse_spec import WarehouseSpec
    from alkera_gateway.warehouses import derive_config, warehouse_specs

    cfg = config(world, tmp_path / "a", "x", warehouse_backend="bigquery",
                 bigquery_options_env="PRIMARY_BQ")
    assert warehouse_specs(cfg)["primary"].options_env == "PRIMARY_BQ"
    peer = WarehouseSpec(name="bq", backend="bigquery", attach=False,
                         options_env="PEER_BQ")
    assert derive_config(cfg, peer).bigquery_options_env == "PEER_BQ"


def test_bigquery_peer_cannot_attach_but_can_be_a_dbt_target(world, tmp_path, monkeypatch):
    """A bigquery peer is never attached to a catalog. It is a dbt target like any
    other where dbt-bigquery is installed - images built with --warehouse-clients
    bigquery (requirements-bigquery.lock) - and refused at construction, by name,
    in an image without it."""
    from alkera_gateway import warehouses

    with pytest.raises(ValueError, match="bigquery and cannot be attached"):
        Gateway(config(world, tmp_path / "a", "x",
                       warehouses=[{"name": "bq", "backend": "bigquery",
                                    "attach": True}]))
    cfg = config(world, tmp_path / "b", "x",
                 warehouses=[{"name": "bq", "backend": "bigquery",
                              "attach": False, "dbt_target": "bq"}])
    specs = warehouses.warehouse_specs(cfg)
    assert warehouses.DBT_ADAPTERS["bigquery"] == ("dbt.adapters.bigquery", "dbt-bigquery")
    monkeypatch.setattr(warehouses, "dbt_adapter_missing", lambda backend: None)
    assert warehouses._peer_plan(cfg, specs) == ([], {"bq": "attach disabled by the world"})
    monkeypatch.setattr(warehouses, "dbt_adapter_missing",
                        lambda backend: "dbt-bigquery" if backend == "bigquery" else None)
    with pytest.raises(ValueError, match="the dbt-bigquery adapter is not installed"):
        warehouses._peer_plan(cfg, specs)


def test_dbt_bigquery_is_held_below_the_routines_listing():
    """dbt-bigquery 1.12 lists each dataset's routines inside the `try` that lists its
    tables, and answers NotFound with no relations at all. The emulator cannot serve
    routines.list, and the proxy answers it as an unknown route (404), so under 1.12
    dbt's relation cache would be empty everywhere: an incremental model silently
    rebuilt, a snapshot silently restarted. The client lock holds the adapter at 1.11
    (tables only) for as long as the proxy has no routines.list route
    (docker/bigquery-clients.in)."""
    import re

    from alkera_gateway.native.services import bigquery as proxy

    assert proxy.route("GET", "/bigquery/v2/projects/p/datasets/d/routines") is None
    lock = (Path(__file__).resolve().parents[1] / "requirements-bigquery.lock").read_text(
        encoding="utf-8")
    major, minor = re.search(r"^dbt-bigquery==(\d+)\.(\d+)\.", lock, re.MULTILINE).groups()
    assert (int(major), int(minor)) < (1, 12)
