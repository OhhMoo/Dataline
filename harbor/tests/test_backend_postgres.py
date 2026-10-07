"""The Harbor postgres plug's launcher (harbor/template/backends/postgres_start.py) is
the measured topology's server, not a lookalike. No Docker.

The image cannot ship tasks/harness/postgres_live.py (no file under tasks/ enters an
agent image), so the launcher carries its own copy of the seed; here it is held equal
to postgres_live's, byte for byte on the same tables, and its role SQL to
compose.native.yml's `pg_native_role`. And the export: the de/postgres variant runs
it as the server's user, folds its statement log at world-down, and its Dockerfile
COPYs exactly what the launcher reads.

    gateway/.venv/bin/python -m pytest -q harbor/tests/test_backend_postgres.py
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import test_export_backends as teb
import test_export_support  # noqa: F401 - puts harbor/ on sys.path
from alkera_harbor import variant
from alkera_harbor.constants import REPO

HARNESS = REPO / "tasks" / "harness"
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

import postgres_live  # noqa: E402

LAUNCHER = REPO / "harbor" / "template" / "backends" / "postgres_start.py"


def launcher():
    name = "postgres_start_for_tests"
    spec = importlib.util.spec_from_file_location(name, LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module                       # dataclasses resolve it by name
    spec.loader.exec_module(module)
    return module


def test_the_seed_is_postgres_lives():
    start = launcher()
    assert start.SCALAR_TYPES == postgres_live.SCALAR_TYPES
    assert start.DECIMAL.pattern == postgres_live.DECIMAL.pattern
    for name in ("DATABASE", "SUPERUSER", "COMPOSE_OWNER", "OWNER", "AGENT_ROLE",
                 "AGENT_PRIVILEGES", "SCHEMA"):
        assert getattr(start, name) == getattr(postgres_live, name), name
    for dtype in ("DECIMAL(18,2)", "varchar", "TIMESTAMP WITH TIME ZONE", "HUGEINT"):
        assert start.pg_type(dtype) == postgres_live.pg_type(dtype)
    with pytest.raises(start.SeedError, match="no Postgres mapping"):
        start.pg_type("STRUCT(a INT)")
    tables = [("fct_orders", (("order_id", "integer"), ("order_amount", "numeric(18,2)"),
                              ("order_status", "varchar")), 2,
               "1,10.50,completed\n2,,\"\"\n"),
              ("empty", (("x", "integer"),), 0, "")]
    ours = [start.Table(*t) for t in tables]
    theirs = [postgres_live.Table(*t) for t in tables]
    assert start.seed_script(ours) == postgres_live.seed_script(theirs)
    assert start.count_query(ours) == postgres_live.count_query(theirs)


def test_the_seed_reads_a_real_world_file_like_postgres_live(tmp_path):
    import duckdb

    db = tmp_path / "warehouse.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE SCHEMA warehouse.warehouse")
    con.execute("CREATE TABLE warehouse.warehouse.fct_orders (order_id INTEGER, "
                "amount DECIMAL(18,2), status VARCHAR, placed DATE)")
    con.execute("INSERT INTO warehouse.warehouse.fct_orders VALUES "
                "(2, 3.50, 'x', DATE '2026-02-01'), (1, NULL, '', NULL)")
    con.execute("CREATE VIEW warehouse.warehouse.v AS SELECT 1 AS one")
    con.close()
    start = launcher()
    start.DATA = tmp_path                     # where the launcher keeps its scratch
    ours, our_views = start.read_tables(db)
    theirs, their_views = postgres_live.read_tables(db)
    assert our_views == their_views == ["v"]
    assert [(t.name, t.columns, t.rows, t.csv) for t in ours] == \
        [(t.name, t.columns, t.rows, t.csv) for t in theirs]


def _statements(sql: str) -> list[str]:
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    return [re.sub(r"\s+", " ", s).strip() for s in body.split(";") if s.strip()]


class _ComposeLoader(yaml.SafeLoader):
    """compose's own tags (`!override`, `!reset`) read as nothing."""


_ComposeLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


def test_the_role_is_compose_natives():
    compose = yaml.load((REPO / "gateway" / "docker" / "compose.native.yml")
                        .read_text(encoding="utf-8"), Loader=_ComposeLoader)
    assert _statements(launcher().NATIVE_ROLE_SQL) == \
        _statements(compose["configs"]["pg_native_role"]["content"])


def test_the_server_logs_the_agents_role_only():
    start = launcher()
    conf = start.POSTGRESQL_CONF
    assert "log_statement = 'none'" in conf and "log_min_duration_statement = -1" in conf
    assert "logging_collector = on" in conf and f"log_directory = '{start.LOG_DIR}'" in conf
    assert "ALTER ROLE alkera SET log_statement = 'all'" in start.NATIVE_ROLE_SQL
    assert "ALTER ROLE alkera SET log_min_duration_statement = 0" in start.NATIVE_ROLE_SQL
    # one door: over TCP only the agent's role, by password; the superuser on the
    # private socket only
    hba = [line.split() for line in start.PG_HBA.splitlines() if line.strip()]
    assert hba == [["local", "all", "postgres", "trust"],
                   ["host", "all", "alkera", "127.0.0.1/32", "scram-sha-256"],
                   ["host", "all", "alkera", "::1/128", "scram-sha-256"]]
    assert "unix_socket_permissions = 0700" in conf
    assert start.server_argv("127.0.0.1")[-2:] == ["-c", "listen_addresses=127.0.0.1"]
    assert start.server_argv("")[-1] == "listen_addresses="


def test_the_statement_log_is_where_world_down_folds_it():
    start = launcher()
    [entry] = variant.STATEMENT_LOGS[variant.POSTGRES_PLUG]
    assert entry["log"] == str(start.LOG_DIR / "postgresql.log")
    assert entry["tailer"] == postgres_live.TAILER


def test_de_postgres_runs_it_as_the_server_user(tmp_path):
    v = variant.variant_of(SimpleNamespace(profile="de/postgres", plugs={}))
    assert v.backends == () and v.harbor_backends == ("postgres",) and v.clients == ()
    [spec] = variant.backend_specs(v)
    assert spec == {"name": "postgres", "argv": ["/usr/local/bin/python",
                                                 "/opt/alkera/backends/postgres_start.py"],
                    "user": "postgres", "ports": [5432], "hosts": ["postgres.internal"],
                    "health": "tcp"}
    text = teb.assert_exportable(v, tmp_path)
    assert "COPY alkera/harbor/backends/postgres_start.py " \
           "/opt/alkera/backends/postgres_start.py" in text
    # the launcher reads compose's seed SQL from beside itself
    assert launcher().COMPOSE_SQL.name == "postgres.sql"
    assert "COPY alkera/gateway/docker/postgres.sql /opt/alkera/backends/postgres.sql" in text
    assert sum("postgresql-17=17.11-0+deb13u1" in r for r in teb.runs(text)) == 1


def test_two_backends_on_one_server_package_install_it_once():
    both = variant.Variant(role="de", clients=("redshift",), profile="de/x",
                           backends=("redshift",), harbor_backends=("redshift", "postgres"),
                           slots=(("warehouse", ("postgres", "redshift")),))
    text = teb.dockerfile(both)
    assert sum("postgresql-17=17.11-0+deb13u1" in r for r in teb.runs(text)) == 1
    assert "/opt/stack/redshift_start.py" in text and "postgres_start.py" in text
