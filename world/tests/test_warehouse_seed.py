"""A second warehouse, seeded without the connector and fingerprinted honestly.

Three properties, and the third is the one a cutover task actually rests on.

1. Seeding is set-based and never touches `alkera_gateway`. A world built
   through the connector would put its own construction into the episode's
   action log, where the grader cannot tell it from the agent's work. That is
   the reason, not the throughput - though the throughput is decisive too, and
   this file measures it rather than quoting it.
2. The same seed reproduces: build the file twice, get the same logical hash.
3. **The same data in two different engines produces the same fingerprint.**
   Without this a dual-run reconciliation is not checkable: "the two marts
   agree" would mean "the agent said so". With it, a Postgres warehouse can
   carry a real `initial_world_fp` instead of the gateway being told to take a
   hash of the local world tree on faith
   (`GatewayConfig` in `gateway/src/alkera_gateway/config.py`).

Postgres comes from a real server, on the pattern `gateway/tests/
test_warehouse_backends.py` established: `ALKERA_TEST_POSTGRES_DSN` when set,
otherwise the digest the episode stack pins, otherwise a skip that names the
reason. A skip here is an honest gap; a pass that quietly hashed DuckDB twice
would be the defect.
"""

import ast
import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import pytest
from world.warehouse_seed import (
    duckdb_logical,
    logical_hash,
    postgres_logical,
    seed_duckdb,
    seed_postgres,
    write_rows_csv,
)

PG_IMAGE = "postgres@sha256:f1c3376c26f2609ab9f29f71f824103fe2fcd8ee0346485cb6122a4f93df6f94"
PG_PASSWORD = "alkera-seed-test"
ROWS = 20_000
TYPES = {"day": "DATE", "customer_id": "INTEGER", "amount": "DECIMAL(12,2)", "label": "VARCHAR"}


def sample_rows(n=ROWS, bump=0):
    return [
        (
            f"2026-02-{1 + i % 28:02d}",
            i % 500,
            f"{(i % 9973) + 1 + bump}.{i % 100:02d}",
            f"c{i % 37}",
        )
        for i in range(n)
    ]


@pytest.fixture
def landing(tmp_path):
    return write_rows_csv(tmp_path / "landing" / "fct_revenue.csv", list(TYPES), sample_rows())


def docker(*args, timeout=180):
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, encoding="utf-8", timeout=timeout
    )


@pytest.fixture(scope="session")
def postgres_dsn():
    supplied = os.environ.get("ALKERA_TEST_POSTGRES_DSN")
    if supplied:
        yield supplied
        return
    try:
        version = docker("version", "--format", "{{.Server.Version}}", timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"cross-engine fingerprint not exercised: docker is unusable ({exc})")
    if version.returncode:
        pytest.skip(
            "cross-engine fingerprint not exercised: no docker daemon and "
            "ALKERA_TEST_POSTGRES_DSN is unset"
        )
    started = docker(
        "run", "-d", "--rm", "-p", "127.0.0.1::5432",
        "-e", f"POSTGRES_PASSWORD={PG_PASSWORD}", "-e", "POSTGRES_DB=episode", PG_IMAGE,
    )
    if started.returncode:
        pytest.skip(f"cross-engine fingerprint not exercised: {started.stderr.strip()[:300]}")
    container = started.stdout.strip()
    try:
        mapping = docker("port", container, "5432/tcp", timeout=30)
        published = mapping.stdout.strip().splitlines()
        if mapping.returncode or not published:
            pytest.skip("cross-engine fingerprint not exercised: no published host port")
        port = int(published[0].rsplit(":", 1)[1])
        dsn = f"postgresql://postgres:{PG_PASSWORD}@127.0.0.1:{port}/episode"
        import psycopg

        deadline = time.monotonic() + 90
        last = None
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(dsn, connect_timeout=2) as conn:
                    conn.execute("SELECT 1")
                break
            except Exception as exc:  # noqa: BLE001 - the reason is what we report
                last = exc
                time.sleep(0.5)
        else:
            pytest.skip(f"cross-engine fingerprint not exercised: server never came up ({last})")
        yield dsn
    finally:
        docker("rm", "-f", container, timeout=60)


@pytest.fixture
def scratch_dsn(postgres_dsn):
    """An isolated database, so this never resets an operator's schema."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    name = f"alkera_seed_{uuid4().hex}"
    identifier = sql.Identifier(name)
    with psycopg.connect(postgres_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(identifier))
        try:
            yield make_conninfo(postgres_dsn, dbname=name)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(identifier))


def test_seeding_never_imports_the_gateway():
    """The property `world/world/gateway_scheduler.py:8` states in prose, tested.

    World construction that went through a tool call would land in the episode's
    action log and consume `seq` numbers, and the grader could no longer tell
    the world from the agent.
    """
    source = Path(__file__).resolve().parents[1] / "world" / "warehouse_seed.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "alkera_gateway" not in imported, sorted(imported)
    # Nor any row-at-a-time loop: the seed paths are read_csv and COPY. Checked
    # against the parsed call names, so the module may still explain in prose
    # why executemany is the thing it does not do.
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "executemany" not in called


def test_a_duckdb_warehouse_seeds_and_reproduces(tmp_path, landing):
    tables = {"warehouse.fct_revenue": (landing, TYPES)}
    first, second = tmp_path / "a.duckdb", tmp_path / "b.duckdb"
    seed_duckdb(first, tables)
    seed_duckdb(second, tables)
    assert logical_hash(first) == logical_hash(second)
    logical = duckdb_logical(first)
    assert list(logical["tables"]) == ["warehouse.fct_revenue"]


def test_a_changed_cell_changes_the_fingerprint(tmp_path):
    """A fingerprint that survives an edit is not a fingerprint."""
    a = write_rows_csv(tmp_path / "a.csv", list(TYPES), sample_rows(200))
    b = write_rows_csv(tmp_path / "b.csv", list(TYPES), sample_rows(200, bump=1))
    seed_duckdb(tmp_path / "a.duckdb", {"warehouse.fct_revenue": (a, TYPES)})
    seed_duckdb(tmp_path / "b.duckdb", {"warehouse.fct_revenue": (b, TYPES)})
    assert logical_hash(tmp_path / "a.duckdb") != logical_hash(tmp_path / "b.duckdb")


def test_the_same_data_fingerprints_the_same_in_both_engines(tmp_path, landing, scratch_dsn):
    """The claim a dual-run cutover rests on.

    Two engines, one seed file, one hash. If this ever stops being true the
    reconciliation half of a migration task becomes unscoreable, so it is
    asserted on the full canonical structure and not just the top-level digest.
    """
    tables = {"warehouse.fct_revenue": (landing, TYPES)}
    seed_duckdb(tmp_path / "legacy.duckdb", tables)
    seed_postgres(scratch_dsn, tables)
    duck = duckdb_logical(tmp_path / "legacy.duckdb")
    postgres = postgres_logical(scratch_dsn)
    assert duck["tables"] == postgres["tables"]
    assert duck["hash"] == postgres["hash"]


def test_bulk_seeding_is_orders_of_magnitude_faster_than_a_row_at_a_time_loop(tmp_path, landing):
    """The measurement that settles "seeding must not go through the connector".

    Not an assertion about a particular machine - the bound is deliberately
    loose (10x) while the real measured gap on this host was ~400x for DuckDB
    and ~5000x for Postgres COPY. What this pins is the SHAPE: one statement
    per row is a different order of cost, so any second warehouse has to be
    seeded set-based whatever the backend.
    """
    import duckdb

    rows = sample_rows(2_000)
    slow_db = tmp_path / "slow.duckdb"
    with duckdb.connect(str(slow_db)) as conn:
        conn.execute("CREATE SCHEMA warehouse")
        conn.execute(
            "CREATE TABLE warehouse.fct_revenue"
            "(day DATE, customer_id INTEGER, amount DECIMAL(12,2), label VARCHAR)"
        )
        started = time.perf_counter()
        conn.executemany("INSERT INTO warehouse.fct_revenue VALUES (?, ?, ?, ?)", rows)
        row_at_a_time = time.perf_counter() - started

    small = write_rows_csv(tmp_path / "small.csv", list(TYPES), rows)
    started = time.perf_counter()
    seed_duckdb(tmp_path / "fast.duckdb", {"warehouse.fct_revenue": (small, TYPES)})
    bulk = time.perf_counter() - started

    assert row_at_a_time > bulk * 10, (
        f"row-at-a-time {row_at_a_time:.2f}s vs bulk {bulk:.2f}s for {len(rows)} rows"
    )
