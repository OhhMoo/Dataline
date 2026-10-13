"""The BigQuery plug's front door: the audited proxy and the boot seed, against a
real goccy/bigquery-emulator process.

    gateway/.venv/bin/python -m pytest gateway/tests/test_bigquery_proxy.py -q

The emulator is the pinned release binary `docker/bigquery/fetch.py` downloads and
sha256-verifies; when it cannot be fetched every test here that needs it skips with
the reason. The proxy (`alkera_gateway.native.services.bigquery`) is served by
uvicorn on loopback and driven with the real client, google-cloud-bigquery, exactly
as the agent drives it in the container (AnonymousCredentials + api_endpoint).

What is held: one 9.2.2 row per request, on file before the response starts; the
seed's counts are the world's; Google's error envelope for a route BigQuery does
not publish; GoogleSQL answers equal to DuckDB's on the same rows; and the fences -
cost, legacy SQL, load jobs, ALTER, transactions, EXECUTE IMMEDIATE, ASSERT, a
truncating destination, a schema patch, external tables (no Cloud Storage here) -
refused by name, never answered wrongly; and a refused query is still a
`bigquery.statement` row (`refused:<feature>`, exit error), so the path rules read
the attempt. The external-data fences are held against an engine double that hangs
(`gcs_less_engine`), so they run without the emulator binary too.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import importlib.util
import json
import os
import random
import socket
import subprocess
import threading
import time
import urllib.request
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from alkera_gateway.native import bigquery_seed
from alkera_gateway.native.audit import ROW_KEYS
from alkera_gateway.native.services import bigquery as bq

FETCH = Path(__file__).resolve().parents[1] / "docker" / "bigquery" / "fetch.py"
PROJECT = "alkera-local"
START_TIMEOUT_S = 30


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def rows_of(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --------------------------------------------------------------------- a world
def build_world(root: Path) -> Path:
    """A small world warehouse shaped like commerce_analytics': raw, staging (a
    VIEW, which the seed copies as a table) and warehouse, the types the real
    world uses (INTEGER, DATE, DECIMAL(12,2), VARCHAR, TIMESTAMP, BOOLEAN)."""
    path = root / "var" / "warehouse" / "warehouse.duckdb"
    path.parent.mkdir(parents=True)
    rng = random.Random(11)
    statuses = ("completed", "completed", "completed", "cancelled", "refunded")
    with duckdb.connect(str(path)) as con:
        for schema in ("raw", "staging", "warehouse"):
            con.execute(f"CREATE SCHEMA {schema}")
        con.execute("CREATE TABLE warehouse.warehouse.dim_customer (customer_id INTEGER, "
                    "segment VARCHAR, is_active BOOLEAN)")
        con.executemany("INSERT INTO warehouse.warehouse.dim_customer VALUES (?, ?, ?)",
                        [(c, ("smb", "mid", "enterprise")[c % 3], c % 5 != 0)
                         for c in range(1, 41)])
        con.execute("CREATE TABLE warehouse.warehouse.fct_orders (order_id INTEGER, "
                    "order_date DATE, customer_id INTEGER, order_status VARCHAR, "
                    "order_amount DECIMAL(12,2), placed_at TIMESTAMP)")
        start = dt.date(2025, 12, 1)
        orders = []
        for order_id in range(1, 401):
            day = start + dt.timedelta(days=rng.randrange(0, 120))
            orders.append((order_id, day, rng.randrange(1, 41), rng.choice(statuses),
                           Decimal(rng.randrange(500, 250000)) / 100,
                           dt.datetime.combine(day, dt.time(rng.randrange(24),
                                                            rng.randrange(60)))))
        con.executemany("INSERT INTO warehouse.warehouse.fct_orders VALUES (?,?,?,?,?,?)",
                        orders)
        con.execute("CREATE TABLE warehouse.raw.orders AS SELECT order_id, customer_id, "
                    "order_status AS status FROM warehouse.warehouse.fct_orders")
        con.execute("CREATE VIEW warehouse.staging.stg_orders AS SELECT order_id, status "
                    "FROM warehouse.raw.orders WHERE status <> 'cancelled'")
    return path


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> Path:
    return build_world(tmp_path_factory.mktemp("world"))


def duck(warehouse: Path, sql: str) -> list[tuple]:
    with duckdb.connect(str(warehouse), read_only=True) as con:
        return con.execute(sql).fetchall()


# ------------------------------------------------------------------ the engine
def load_fetch():
    spec = importlib.util.spec_from_file_location("bigquery_fetch_proxy", FETCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def emulator():
    """A running emulator's REST endpoint, serving PROJECT, or a skip with why."""
    try:
        binary = load_fetch().fetch()
    except (OSError, SystemExit) as exc:
        pytest.skip(f"BigQuery proxy not exercised: emulator binary unavailable ({exc})")
    port = free_port()
    process = subprocess.Popen([str(binary), f"--project={PROJECT}", "--host=127.0.0.1",
                                f"--port={port}", f"--grpc-port={free_port()}"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    endpoint = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + START_TIMEOUT_S
    try:
        while True:
            try:
                urllib.request.urlopen(f"{endpoint}/bigquery/v2/projects", timeout=2).read()
                break
            except OSError as exc:
                if process.poll() is not None or time.monotonic() > deadline:
                    pytest.skip(f"BigQuery proxy not exercised: emulator never served ({exc})")
                time.sleep(0.2)
        yield endpoint
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture(scope="module")
def audit(tmp_path_factory):
    """The rows file every request in this module is recorded to."""
    path = tmp_path_factory.mktemp("audit") / "actions.jsonl"
    saved = os.environ.get("ALKERA_NATIVE_AUDIT")
    os.environ["ALKERA_NATIVE_AUDIT"] = str(path)
    yield path
    if saved is None:
        os.environ.pop("ALKERA_NATIVE_AUDIT", None)
    else:
        os.environ["ALKERA_NATIVE_AUDIT"] = saved


#: What the engine and the audit file held right after seeding.
AFTER_SEED: dict = {}


@pytest.fixture(scope="module")
def seeded(emulator, world, audit) -> dict[str, int]:
    """The world, seeded straight upstream - before any proxy request."""
    counts = bigquery_seed.seed(emulator, PROJECT, [world])
    AFTER_SEED.update(rows=rows_of(audit), jobs=upstream_jobs(emulator))
    return counts


@pytest.fixture(scope="module")
def proxy(emulator, seeded, audit):
    """The proxy, served by uvicorn on loopback; its base URL."""
    import uvicorn

    app = bq.build_app(upstream=emulator, seed={"tables": seeded})
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def client(proxy):
    from google.api_core.client_options import ClientOptions
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import bigquery

    made = bigquery.Client(project=PROJECT, credentials=AnonymousCredentials(),
                           client_options=ClientOptions(api_endpoint=proxy))
    yield made
    made.close()


def upstream_jobs(emulator: str) -> list[str]:
    with urllib.request.urlopen(f"{emulator}/bigquery/v2/projects/{PROJECT}/jobs",
                                timeout=10) as response:
        return [j["jobReference"]["jobId"] for j in json.load(response).get("jobs", [])]


# ------------------------------------------------------------------- the seed
def test_seed_counts_equal_world_tables(emulator, world, seeded, audit):
    world_counts = {f"{s}.{t}": duck(world, f'SELECT count(*) FROM "warehouse"."{s}"."{t}"')
                    [0][0] for s, t in bigquery_seed.relations(world)}
    assert seeded == world_counts
    assert set(seeded) == {"raw.orders", "staging.stg_orders", "warehouse.dim_customer",
                           "warehouse.fct_orders"}     # the view, copied as a table
    # straight upstream: no row for provisioning, and no job in the engine's log
    # (the log the e2e check holds the statement rows 1:1 against)
    assert AFTER_SEED == {"rows": [], "jobs": []}
    # exact types: money is NUMERIC, a timestamp without zone is DATETIME
    with urllib.request.urlopen(f"{emulator}/bigquery/v2/projects/{PROJECT}/datasets/"
                                "warehouse/tables/fct_orders", timeout=10) as response:
        fields = {f["name"]: f["type"] for f in json.load(response)["schema"]["fields"]}
    assert fields["order_amount"] == "NUMERIC" and fields["placed_at"] == "DATETIME"


def test_reseeding_replaces_rather_than_doubles(emulator, world, seeded):
    assert bigquery_seed.seed(emulator, PROJECT, [world]) == seeded


def test_seed_refuses_one_dataset_from_two_files(emulator, world, tmp_path):
    other = build_world(tmp_path / "other")
    with pytest.raises(bigquery_seed.SeedError, match="both"):
        bigquery_seed.seed(emulator, PROJECT, [world, other])


# ------------------------------------------------------------- the audit row
def _asgi(app, method: str, path: str, body: bytes = b"", query: bytes = b""):
    """Call the ASGI app directly, noting how many rows the audit file holds at
    the moment the response STARTS - the client cannot have seen a byte yet."""
    seen: dict = {"body": b""}
    audit_file = Path(os.environ["ALKERA_NATIVE_AUDIT"])

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            seen["status"] = message["status"]
            seen["rows_at_start"] = len(rows_of(audit_file))
        elif message["type"] == "http.response.body":
            seen["body"] += message.get("body", b"")

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
             "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
             "query_string": query, "root_path": "", "client": ("127.0.0.1", 50000),
             "server": ("bigquery.internal", 9050),
             "headers": [(b"host", b"bigquery.internal:9050"),
                         (b"content-type", b"application/json")]}
    asyncio.run(app(scope, receive, send))
    return seen


def test_query_job_writes_one_statement_row_before_response(emulator, seeded, audit):
    app = bq.build_app(upstream=emulator)
    sql = (f"SELECT COUNT(*) AS n FROM `{PROJECT}.warehouse.fct_orders` "
           "WHERE order_status = 'completed'")
    before = len(rows_of(audit))
    seen = _asgi(app, "POST", f"/bigquery/v2/projects/{PROJECT}/jobs", json.dumps({
        "jobReference": {"projectId": PROJECT, "jobId": "row-before-response"},
        "configuration": {"query": {"query": sql, "useLegacySql": False}}}).encode())
    assert seen["status"] == 200
    assert seen["rows_at_start"] == before + 1          # on file before the first byte
    row = rows_of(audit)[-1]
    assert row["tool"] == "bigquery.statement" and row["args"]["sql"] == sql
    assert row["op_class"] == "read" and row["exit"] == "ok"
    assert row["targets"] == ['"alkera-local"."warehouse"."fct_orders"']
    assert row["result"]["job_id"] == "row-before-response"
    assert set(row) == set(ROW_KEYS)
    # and jobs.query, the other statement route
    before = len(rows_of(audit))
    seen = _asgi(app, "POST", f"/bigquery/v2/projects/{PROJECT}/queries",
                 json.dumps({"query": "SELECT 1 AS one", "useLegacySql": False}).encode())
    assert seen["status"] == 200 and seen["rows_at_start"] == before + 1
    assert rows_of(audit)[-1]["tool"] == "bigquery.statement"


def test_unknown_route_google_error_shape(emulator, audit):
    app = bq.build_app(upstream=emulator)
    for method, path in (("GET", f"/bigquery/v2/projects/{PROJECT}/nothing"),
                         ("GET", "/"), ("HEAD", "/bigquery/v2/projects"),
                         ("POST", f"/bigquery/v2/projects/{PROJECT}/datasets/x/routines")):
        before = len(rows_of(audit))
        seen = _asgi(app, method, path)
        assert seen["status"] == 404 and seen["rows_at_start"] == before + 1, path
        if method != "HEAD":
            error = json.loads(seen["body"])["error"]
            assert error["code"] == 404 and error["status"] == "NOT_FOUND"
            assert error["errors"][0]["reason"] == "notFound"
            assert path in error["message"]
        row = rows_of(audit)[-1]
        assert row["tool"] == "bigquery.unknown_route" and row["exit"] == "error"
        assert row["error"]["code"] == "unknown_route" and row["targets"] == []


def test_every_row_is_a_9_2_2_row_with_contiguous_seq(client, audit):
    list(client.query("SELECT 1 AS x").result())
    rows = rows_of(audit)
    assert rows and all(set(r) == set(ROW_KEYS) for r in rows)
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))
    assert all(r["transport"] == "native" for r in rows)


# -------------------------------------------- GoogleSQL against DuckDB truth
#: (GoogleSQL through the real client and proxy, the same question in DuckDB).
CORPUS = [
    (f"SELECT SUM(order_amount) FROM `{PROJECT}.warehouse.fct_orders` "
     "WHERE order_status = 'completed' AND order_date >= '2026-02-01' "
     "AND order_date < '2026-03-01'",
     "SELECT SUM(order_amount) FROM warehouse.warehouse.fct_orders "
     "WHERE order_status = 'completed' AND order_date >= DATE '2026-02-01' "
     "AND order_date < DATE '2026-03-01'"),
    ("SELECT DATE_TRUNC(order_date, MONTH) AS m, COUNT(*) AS n, SUM(order_amount) AS s "
     f"FROM `{PROJECT}.warehouse.fct_orders` GROUP BY m ORDER BY m",
     "SELECT date_trunc('month', order_date)::DATE AS m, COUNT(*), SUM(order_amount) "
     "FROM warehouse.warehouse.fct_orders GROUP BY m ORDER BY m"),
    ("SELECT c.segment, COUNT(DISTINCT o.customer_id) AS buyers "
     "FROM warehouse.fct_orders AS o JOIN warehouse.dim_customer AS c USING (customer_id) "
     "WHERE c.is_active GROUP BY c.segment ORDER BY c.segment",
     "SELECT c.segment, COUNT(DISTINCT o.customer_id) FROM warehouse.warehouse.fct_orders o "
     "JOIN warehouse.warehouse.dim_customer c USING (customer_id) WHERE c.is_active "
     "GROUP BY c.segment ORDER BY c.segment"),
    ("SELECT EXTRACT(YEAR FROM order_date) AS y, COUNTIF(order_status = 'refunded') AS r "
     "FROM warehouse.fct_orders GROUP BY y ORDER BY y",
     "SELECT EXTRACT(YEAR FROM order_date), COUNT(*) FILTER (WHERE order_status = "
     "'refunded') FROM warehouse.warehouse.fct_orders GROUP BY 1 ORDER BY 1"),
    ("SELECT FORMAT_DATE('%Y-%m', order_date) AS ym, ROUND(AVG(order_amount), 2) AS a "
     "FROM warehouse.fct_orders WHERE order_status = 'completed' GROUP BY ym "
     "HAVING COUNT(*) > 5 ORDER BY ym",
     "SELECT strftime(order_date, '%Y-%m') AS ym, ROUND(AVG(order_amount), 2) "
     "FROM warehouse.warehouse.fct_orders WHERE order_status = 'completed' GROUP BY ym "
     "HAVING COUNT(*) > 5 ORDER BY ym"),
    ("SELECT customer_id, order_id FROM (SELECT customer_id, order_id, ROW_NUMBER() OVER "
     "(PARTITION BY customer_id ORDER BY order_amount DESC, order_id) AS rk "
     "FROM warehouse.fct_orders) WHERE rk = 1 ORDER BY customer_id",
     "SELECT customer_id, order_id FROM (SELECT customer_id, order_id, ROW_NUMBER() OVER "
     "(PARTITION BY customer_id ORDER BY order_amount DESC, order_id) AS rk "
     "FROM warehouse.warehouse.fct_orders) WHERE rk = 1 ORDER BY customer_id"),
    ("SELECT status, COUNT(*) AS n FROM `alkera-local`.staging.stg_orders "
     "GROUP BY status ORDER BY status",
     "SELECT status, COUNT(*) FROM warehouse.staging.stg_orders GROUP BY status "
     "ORDER BY status"),
    ("SELECT ROUND(SAFE_DIVIDE(SUM(IF(order_status = 'cancelled', order_amount, 0)), "
     "SUM(order_amount)), 6) AS share, MAX(placed_at) AS last_at "
     "FROM warehouse.fct_orders",
     "SELECT ROUND(SUM(CASE WHEN order_status = 'cancelled' THEN order_amount ELSE 0 END) "
     "/ SUM(order_amount), 6), MAX(placed_at) FROM warehouse.warehouse.fct_orders"),
]


def _norm(value):
    if isinstance(value, (Decimal, float)):
        return round(Decimal(str(value)), 6)
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    return value


def test_bigquery_sql_matches_duckdb_truth(client, world, audit):
    from google.cloud import bigquery

    config = bigquery.QueryJobConfig(default_dataset=f"{PROJECT}.warehouse")
    for bq_sql, duck_sql in CORPUS:
        got = [tuple(_norm(v) for v in row.values())
               for row in client.query(bq_sql, job_config=config).result()]
        want = [tuple(_norm(v) for v in row) for row in duck(world, duck_sql)]
        assert got == want, bq_sql
        assert rows_of(audit)[-1]["tool"] in ("bigquery.query_results", "bigquery.job_get",
                                              "bigquery.tabledata_list")
        statement = [r for r in rows_of(audit) if r["tool"] == "bigquery.statement"][-1]
        assert statement["args"]["sql"] == bq_sql and statement["op_class"] == "read"


# ---------------------------------------------------------------- the fences
def _raises(call, status: int, text: str):
    from google.api_core import exceptions

    started = time.monotonic()
    with pytest.raises(exceptions.GoogleAPICallError) as raised:
        call()
    assert raised.value.code == status, raised.value
    assert text in str(raised.value)
    assert time.monotonic() - started < 10, "the client retried what it must not"
    return raised.value


def test_dry_run_refused_with_the_cost_refusal(client, emulator, audit):
    from google.cloud import bigquery

    jobs = set(upstream_jobs(emulator))
    _raises(lambda: client.query("SELECT 1", job_config=bigquery.QueryJobConfig(
        dry_run=True)), 501, bq.COST_REFUSAL)
    row = rows_of(audit)[-1]
    assert row["tool"] == "bigquery.statement" and row["args"]["sql"] == "SELECT 1"
    assert row["exit"] == "error" and "refused:dry_run" in row["flags"]
    assert row["args"]["feature"] == "dry_run" and row["error"]["code"] == "not_available"
    assert set(upstream_jobs(emulator)) == jobs          # never reached the engine


def test_legacy_sql_load_jobs_and_alter_refused_by_name(client, audit):
    from google.cloud import bigquery

    _raises(lambda: client.query("SELECT 1", job_config=bigquery.QueryJobConfig(
        use_legacy_sql=True)).result(), 501, "legacy SQL")
    _raises(lambda: client.load_table_from_json(
        [{"a": 1}], f"{PROJECT}.warehouse.loaded").result(), 501, "load jobs")
    _raises(lambda: client.query(f"ALTER TABLE `{PROJECT}.warehouse.fct_orders` "
                                 "ADD COLUMN note STRING").result(), 501, "ALTER")
    refused = {r["args"].get("feature"): r for r in rows_of(audit)
               if any(f.startswith(bq.REFUSED_FLAG) for f in r["flags"])}
    assert {"legacy_sql", "load_job", "alter"} <= set(refused)
    # a refused query is the statement it was, classified by its SQL
    alter = refused["alter"]
    assert alter["tool"] == "bigquery.statement"
    assert alter["op_class"] == bq.classify(alter["args"]["sql"]).op_class != "read"
    assert alter["targets"] and alter["exit"] == "error"
    assert refused["load_job"]["tool"] == "bigquery.job_upload"


def test_query_errors_are_bigquerys_and_not_retried(client):
    error = _raises(lambda: client.query("SELEC 1").result(), 400, "Syntax error")
    assert error.errors[0]["reason"] == "invalidQuery"
    error = _raises(lambda: client.query("SELECT * FROM warehouse.nope").result(),
                    404, "Table not found")
    assert error.errors[0]["reason"] == "notFound"
    # only a missing table or dataset is a 404: a missing function is the query's
    error = _raises(lambda: client.query("SELECT NOPE_FN(1)").result(), 400,
                    "Function not found")
    assert error.errors[0]["reason"] == "invalidQuery"


def test_engine_gaps_are_not_available_not_internal_errors():
    status, body = bq.vendor_error(500, {"error": {"code": 500, "message": "boom", "errors": [
        {"reason": "internalError", "message": "boom"}]}})
    assert status == 501 and body["error"]["errors"][0]["reason"] == "notImplemented"
    assert body["error"]["message"].startswith(bq.NOT_AVAILABLE)
    assert bq.query_error("currently unsupported DROP SCHEMA statement")[:2] == (
        501, "notImplemented")
    duplicate = "duplicate: table t: table is already created"
    assert bq.query_error(duplicate)[:2] == (409, "duplicate")
    # never 409 on jobs.insert: the client would re-fetch a job that does not exist
    assert bq.query_error(duplicate, "job_insert")[:2] == (400, "duplicate")
    assert bq.query_error("dataset s is not found")[:2] == (404, "notFound")
    assert bq.query_error("failed to analyze: Function not found: X")[:2] == (
        400, "invalidQuery")


def test_cost_fields_never_reach_the_client(client, proxy):
    job = client.query("SELECT COUNT(*) FROM warehouse.fct_orders")
    job.result()
    assert job.total_bytes_processed is None and job.total_bytes_billed is None
    raw = urllib.request.urlopen(f"{proxy}/bigquery/v2/projects/{PROJECT}/jobs/{job.job_id}",
                                 timeout=10).read().decode()
    assert not any(field in raw for field in bq.COST_FIELDS)


def test_statement_type_ddl_target_and_unreported_dml(client, audit):
    table = f"{PROJECT}.warehouse.ctas_check"
    job = client.query(f"CREATE TABLE `{table}` AS SELECT 1 AS a, 'x' AS b")
    job.result()
    assert job.statement_type == "CREATE_TABLE_AS_SELECT"
    assert job.ddl_target_table.table_id == "ctas_check"
    assert job.destination.table_id == "ctas_check"      # what dbt-bigquery reads
    insert = client.query(f"INSERT INTO `{table}` (a, b) VALUES (2, 'y')")
    insert.result()
    assert insert.statement_type == "INSERT" and insert.num_dml_affected_rows is None
    row = [r for r in rows_of(audit) if r["tool"] == "bigquery.statement"][-1]
    assert row["op_class"] == "write" and bq.UNREPORTED in row["flags"]
    assert row["rows_affected"] is None
    client.query(f"DROP TABLE `{table}`").result()


def test_create_schema_and_or_replace_take_effect(client, audit):
    client.query(f"CREATE SCHEMA IF NOT EXISTS `{PROJECT}`.scratch").result()
    assert "scratch" in {d.dataset_id for d in client.list_datasets()}
    table = f"`{PROJECT}.scratch.notes`"
    client.query(f"CREATE OR REPLACE TABLE {table} AS SELECT 1 AS a").result()
    client.query(f"CREATE OR REPLACE TABLE {table} AS SELECT 'z' AS z").result()
    assert [f.name for f in client.get_table(f"{PROJECT}.scratch.notes").schema] == ["z"]
    assert [tuple(r.values()) for r in client.query(f"SELECT * FROM {table}").result()] \
        == [("z",)]
    first, second = [r for r in rows_of(audit) if r["tool"] == "bigquery.statement"
                     and r["args"]["sql"].startswith("CREATE OR REPLACE")][-2:]
    assert bq.REPLACED not in first["flags"] and bq.REPLACED in second["flags"]
    _raises(lambda: client.query(f"CREATE SCHEMA `{PROJECT}`.scratch").result(),
            400, "Already Exists")


def test_anonymous_result_datasets_are_hidden(client, emulator, proxy):
    list(client.query("SELECT 2 AS b").result())
    jobs = set(upstream_jobs(emulator))
    shown = {d.dataset_id for d in client.list_datasets()}
    assert {"raw", "staging", "warehouse"} <= shown and not shown & jobs
    every = json.load(urllib.request.urlopen(
        f"{proxy}/bigquery/v2/projects/{PROJECT}/datasets?all=true", timeout=10))
    assert {d["datasetReference"]["datasetId"] for d in every["datasets"]} & jobs


def test_remote_write_leaves_the_world_file_untouched(client, world, audit):
    digest = hashlib.sha256(world.read_bytes()).hexdigest()
    client.query(f"INSERT INTO `{PROJECT}.warehouse.fct_orders` (order_id, order_status) "
                 "VALUES (999999, 'completed')").result()
    errors = client.insert_rows_json(f"{PROJECT}.warehouse.dim_customer",
                                     [{"customer_id": 999, "segment": "x"}])
    assert errors == []
    assert hashlib.sha256(world.read_bytes()).hexdigest() == digest
    writes = [r for r in rows_of(audit)[-6:] if r["op_class"] in ("write", "ddl",
                                                                   "destructive")]
    assert {r["tool"] for r in writes} >= {"bigquery.statement",
                                           "bigquery.tabledata_insert_all"}
    assert [r["rows_affected"] for r in writes
            if r["tool"] == "bigquery.tabledata_insert_all"] == [1]


def test_unreachable_engine_fails_fast_with_a_row(audit):
    app = bq.build_app(upstream=f"http://127.0.0.1:{free_port()}")
    before = len(rows_of(audit))
    seen = _asgi(app, "GET", f"/bigquery/v2/projects/{PROJECT}/datasets")
    assert seen["status"] == 503 and seen["rows_at_start"] == before + 1
    reason = json.loads(seen["body"])["error"]["errors"][0]["reason"]
    assert reason == "backendUnavailable"              # not a reason the client retries
    assert rows_of(audit)[-1]["error"]["code"] == "upstream_unavailable"


# ------------------------------- what the engine would answer wrongly, fenced
def _scratch(client, name: str, rows: int = 0) -> str:
    """A fresh `alkera-local.review.<name>` (a INT64) holding rows 0..rows-1."""
    from google.cloud import bigquery

    client.create_dataset(f"{PROJECT}.review", exists_ok=True)
    table = f"{PROJECT}.review.{name}"
    client.delete_table(table, not_found_ok=True)
    client.create_table(bigquery.Table(table, schema=[bigquery.SchemaField("a", "INT64")]))
    if rows:
        assert client.insert_rows_json(table, [{"a": i} for i in range(rows)]) == []
    return table


def _count(client, table: str) -> int:
    return list(client.query(f"SELECT COUNT(*) FROM `{table}`").result())[0][0]


def _statement(audit, needle: str) -> dict:
    return [r for r in rows_of(audit) if r["tool"] == "bigquery.statement"
            and needle in r["args"].get("sql", "")][-1]


def test_transactions_and_blocks_refused_by_name(client, audit):
    """The engine keeps an INSERT across an ERROR() inside a transaction and
    cannot ROLLBACK: every transaction or BEGIN...END block is refused."""
    table = _scratch(client, "tx", rows=1)
    for sql in (f"BEGIN TRANSACTION; INSERT INTO `{table}` (a) VALUES (7); "
                "SELECT ERROR('boom'); COMMIT TRANSACTION",
                f"BEGIN TRANSACTION; INSERT INTO `{table}` (a) VALUES (8); "
                "ROLLBACK TRANSACTION",
                f"BEGIN INSERT INTO `{table}` (a) VALUES (9); END",
                "COMMIT TRANSACTION", "ROLLBACK"):
        _raises(lambda sql=sql: client.query(sql).result(), 501, "transactions")
        row = _statement(audit, sql)
        assert "refused:transaction" in row["flags"] and row["exit"] == "error"
    assert _count(client, table) == 1                     # nothing reached the engine
    row = _statement(audit, "VALUES (7)")                 # read as what it is
    assert row["op_class"] == bq.classify(row["args"]["sql"]).op_class != "read"


def test_query_destination_dispositions(client, audit):
    """The engine appends to a query job's destination whatever the
    writeDisposition: WRITE_TRUNCATE is refused, WRITE_EMPTY (the default)
    answers `duplicate` on a table with rows, CREATE_NEVER `notFound` on a
    missing one; the row is a write naming the destination."""
    from google.cloud import bigquery

    full, empty = _scratch(client, "dest_full", rows=2), _scratch(client, "dest_empty")
    fresh, missing = f"{PROJECT}.review.dest_new", f"{PROJECT}.review.dest_missing"
    for table in (fresh, missing):
        client.delete_table(table, not_found_ok=True)

    def run(dest, sql="SELECT 7 AS a", **config):
        job = client.query(sql, job_config=bigquery.QueryJobConfig(destination=dest,
                                                                  **config))
        return list(job.result())

    _raises(lambda: run(full, write_disposition="WRITE_TRUNCATE"), 501, "WRITE_TRUNCATE")
    assert "refused:write_truncate" in _statement(audit, "SELECT 7")["flags"]
    _raises(lambda: run(full), 400, "Already Exists")
    assert _count(client, full) == 2
    run(full, write_disposition="WRITE_APPEND")
    assert _count(client, full) == 3
    run(empty)
    assert _count(client, empty) == 1
    run(fresh, sql="SELECT 8 AS a")
    assert _count(client, fresh) == 1
    row = _statement(audit, "SELECT 8")
    assert row["op_class"] == "write" and row["targets"][0] == fresh
    assert row["args"]["destination"] == fresh and row["exit"] == "ok"
    _raises(lambda: run(missing, create_disposition="CREATE_NEVER"), 404, "Not found")


def test_list_rows_and_query_results_page(client, proxy):
    """tabledata.list honours maxResults, startIndex and pageToken (the engine
    answers every row); getQueryResults pages too."""
    table = _scratch(client, "paged", rows=23)
    every = [r["a"] for r in client.list_rows(table)]
    assert sorted(every) == list(range(23))
    assert [r["a"] for r in client.list_rows(table, max_results=5)] == every[:5]
    assert [r["a"] for r in client.list_rows(table, start_index=10, max_results=4)] \
        == every[10:14]
    pages = client.list_rows(table, page_size=7).pages
    assert [len(list(p)) for p in pages] == [7, 7, 7, 2]
    raw = json.load(urllib.request.urlopen(
        f"{proxy}/bigquery/v2/projects/{PROJECT}/datasets/review/tables/paged/data"
        "?maxResults=5", timeout=10))
    assert len(raw["rows"]) == 5 and raw["totalRows"] == "23" and raw["pageToken"]
    job = client.query(f"SELECT a FROM `{table}` ORDER BY a")
    assert [r["a"] for r in job.result(max_results=5)] == list(range(5))
    assert [r["a"] for r in job.result(page_size=10)] == list(range(23))


def test_alter_execute_immediate_assert_and_schema_patches_refused(client, audit):
    """Each one the engine acknowledges and does not do."""
    from google.cloud import bigquery

    table = _scratch(client, "fenced", rows=1)
    for sql, feature, text in (
            (f"ALTER TABLE `{table}` ALTER COLUMN a SET OPTIONS (description='d')",
             "alter", "ALTER"),
            (f"EXECUTE IMMEDIATE 'INSERT INTO `{table}` (a) VALUES (99)'",
             "execute_immediate", "EXECUTE IMMEDIATE"),
            (f"ASSERT (SELECT COUNT(*) FROM `{table}`) = 12345 AS 'never true'",
             "assert", "ASSERT"),
            ("SELECT 1; ASSERT FALSE AS 'x'", "assert", "ASSERT")):
        _raises(lambda sql=sql: client.query(sql).result(), 501, text)
        assert f"refused:{feature}" in _statement(audit, sql)["flags"]
    assert _count(client, table) == 1
    # a keyword inside a string is not a statement
    assert list(client.query("SELECT 'ALTER TABLE x' AS s").result())[0][0] == \
        "ALTER TABLE x"
    current = client.get_table(table)
    current.schema = [*current.schema, bigquery.SchemaField("b", "STRING")]
    _raises(lambda: client.update_table(current, ["schema"]), 501, "schema")
    assert [f.name for f in client.get_table(table).schema] == ["a"]
    patch = [r for r in rows_of(audit) if r["tool"] == "bigquery.table_patch"][-1]
    assert "refused:table_schema_change" in patch["flags"] and patch["exit"] == "error"
    same = client.get_table(table)                     # an unchanged schema goes through
    assert [f.name for f in client.update_table(same, ["schema"]).schema] == ["a"]


def test_or_replace_reading_itself_refused_and_a_failed_replace_says_dropped(client, audit):
    table = _scratch(client, "rep", rows=3)
    _raises(lambda: client.query(f"CREATE OR REPLACE TABLE `{table}` AS SELECT DISTINCT a "
                                 f"FROM `{table}`").result(), 501, "its own query reads")
    assert _count(client, table) == 3
    source = _scratch(client, "rep_src", rows=1)
    sql = f"CREATE OR REPLACE TABLE `{table}` AS SELECT no_such_column FROM `{source}`"
    error = _raises(lambda: client.query(sql).result(), 400, "was dropped before")
    assert table in str(error)
    row = _statement(audit, "no_such_column")
    assert bq.REPLACED in row["flags"] and "was dropped before" in row["error"]["message"]
    # the synchronous route says it too
    _scratch(client, "rep", rows=3)
    _raises(lambda: list(client.query_and_wait(sql)), 400, "was dropped before")


def test_scripts_with_schema_or_replace_ddl_refused_temp_tables_kept(client):
    script = (f"CREATE SCHEMA `{PROJECT}`.s9; CREATE TABLE `{PROJECT}.s9.t` (a INT64)")
    _raises(lambda: client.query(script).result(), 501, "multi-statement script")
    assert "s9" not in {d.dataset_id for d in client.list_datasets()}
    rows = list(client.query("CREATE TEMP TABLE tmp AS SELECT 1 AS a; "
                             "SELECT a FROM tmp").result())
    assert [r["a"] for r in rows] == [1]
    client.query(f"CREATE SCHEMA `{PROJECT}`.fresh_schema").result()
    assert "fresh_schema" in {d.dataset_id for d in client.list_datasets()}


def test_a_script_is_as_serious_as_its_worst_statement(client, audit):
    """The gateway's classifier refuses most scripts and the verb rule behind it
    read the FIRST verb: `SELECT 1; DROP TABLE t` was a read row, which the path
    rules skip. Each statement is classified; the row takes the worst."""
    for name, script, op_class in (
            ("s_drop", "SELECT 1; /* c */ DROP TABLE `{t}`", "destructive"),
            ("s_delete", "SELECT 1; /* c */ delete from `{t}` where true", "destructive"),
            ("s_set", "SELECT 1; SET @@dataset_id = 'review'; DROP TABLE `{t}`",
             "destructive"),
            ("s_insert", "SELECT 1; INSERT INTO `{t}` (a) VALUES (5)", "write")):
        table = _scratch(client, name, rows=2)
        sql = script.format(t=table)
        try:
            client.query(sql).result()
        except Exception:  # noqa: BLE001 - what the engine does is not under test
            pass
        row = _statement(audit, sql)
        assert row["op_class"] == op_class, (sql, row["op_class"])
        assert table in " ".join(row["targets"]).replace('"', "").replace("`", "") or \
            name in " ".join(row["targets"]), row["targets"]
    refused = "SELECT 1; /* c */ alter table `{t}` add column b INT64".format(
        t=_scratch(client, "s_alter"))
    _raises(lambda: client.query(refused).result(), 501, "ALTER")
    assert _statement(audit, refused)["op_class"] != "read"


def test_procedural_scripting_refused_by_name(client, audit):
    """IF / FOR / WHILE / LOOP / REPEAT / CASE bodies never run on the engine,
    CALL runs nothing, RAISE answers 200, and `IF TRUE THEN ASSERT FALSE; END
    IF` passes: refused under `scripting`. Functions of the same names are not."""
    table = _scratch(client, "scripted", rows=1)
    for sql in ("IF TRUE THEN ASSERT FALSE; END IF",
                f"IF TRUE THEN ALTER TABLE `{table}` ADD COLUMN b INT64; END IF",
                f"FOR x IN (SELECT 1 AS a) DO INSERT INTO `{table}` (a) VALUES (x.a); END FOR",
                "WHILE FALSE DO SELECT 1; END WHILE",
                "LOOP LEAVE; END LOOP",
                "REPEAT SELECT 1; UNTIL TRUE END REPEAT",
                "CASE WHEN TRUE THEN SELECT 1; END CASE",
                f"CALL `{PROJECT}.review.proc`()",
                "RAISE USING MESSAGE = 'stop'",
                f"CREATE OR REPLACE PROCEDURE `{PROJECT}.review.proc`() BEGIN SELECT 1; END",
                "DECLARE n INT64 DEFAULT 1; IF n = 1 THEN SELECT 1; END IF"):
        _raises(lambda sql=sql: client.query(sql).result(), 501, "procedural scripting")
        row = _statement(audit, sql)
        assert "refused:scripting" in row["flags"] and row["exit"] == "error"
    assert _count(client, table) == 1
    got = list(client.query("SELECT IF(TRUE, 1, 2) AS i, REPEAT('a', 2) AS r").result())
    assert tuple(got[0].values()) == (1, "aa")


def test_untokenizable_sql_is_one_row_and_a_400(client, emulator, audit):
    """sqlglot's tokenizer cannot read an unterminated string: before, the
    proxy crashed - a bare 500, no row, and the client retried it for ten
    minutes. Now: BigQuery's 400 invalidQuery, at once, and exactly one row."""
    jobs = set(upstream_jobs(emulator))
    for sql in ("SELECT 'abc", "SELECT r'\\'", "SELECT 1; DROP TABLE x; SELECT 'abc"):
        for call in (lambda sql=sql: client.query(sql).result(),
                     lambda sql=sql: list(client.query_and_wait(sql))):
            before = len(rows_of(audit))
            error = _raises(call, 400, "Syntax error")
            assert error.errors[0]["reason"] == "invalidQuery"
            rows = rows_of(audit)[before:]
            assert len(rows) == 1, [r["tool"] for r in rows]
            row = rows[0]
            assert row["tool"] == "bigquery.statement" and row["args"]["sql"] == sql
            assert row["exit"] == "error" and row["error"]["code"] == "invalidQuery"
            assert "unparsed_statement" in row["flags"]
    assert _statement(audit, "DROP TABLE x")["op_class"] == "destructive"
    assert set(upstream_jobs(emulator)) == jobs            # nothing was forwarded


# ------------------------------------------- external data: no Cloud Storage here
#: How long the engine double holds a request: what a gs:// URI did to the real
#: engine in the container (gateway/docker/evidence/PEERS_2026-10-13.md: it dials
#: Cloud Storage, which has no route, past a 30 s client timeout), cut short.
ENGINE_HANG_S = 5.0
#: A refusal by name is answered before any upstream call: well under a second.
FAST_S = 1.0
GCS_CSV = "gs://alkera-world/orders/*.csv"
EXTERNAL_CONFIG = {"sourceUris": [GCS_CSV], "sourceFormat": "CSV",
                   "schema": {"fields": [{"name": "order_id", "type": "INTEGER"}]}}


@pytest.fixture
def gcs_less_engine():
    """A test double for the engine with no route to Cloud Storage: every request
    is recorded, then held ENGINE_HANG_S before a bare `{}`. A request that
    reaches it is seen, and slow."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from types import SimpleNamespace

    seen: list[tuple[str, str]] = []

    class Hang(BaseHTTPRequestHandler):
        def hang(self):
            seen.append((self.command, self.path))
            time.sleep(ENGINE_HANG_S)
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = hang

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Hang)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_address[1]}", requests=seen)
    server.shutdown()
    server.server_close()


@pytest.fixture
def gcs_less_client(gcs_less_engine, audit):
    """The real client, through the proxy served on loopback, in front of the
    engine double."""
    import uvicorn
    from google.api_core.client_options import ClientOptions
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import bigquery

    app = bq.build_app(upstream=gcs_less_engine.url, seed={"tables": {"x": 1}})
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    made = bigquery.Client(project=PROJECT, credentials=AnonymousCredentials(),
                           client_options=ClientOptions(api_endpoint=f"http://127.0.0.1:{port}"))
    yield made
    made.close()
    server.should_exit = True
    thread.join(timeout=10)


def _refused_at_once(engine, audit, method: str, path: str, body: dict,
                     feature: str = "external_table") -> dict:
    """One request straight into the ASGI app: refused by name in well under a
    second, the engine never called, Google's error envelope with the feature's
    own message, and exactly one row - on file before the response - flagged
    `refused:<feature>`. That row."""
    app = bq.build_app(upstream=engine.url)
    before = len(rows_of(audit))
    started = time.monotonic()
    seen = _asgi(app, method, path, json.dumps(body).encode())
    elapsed = time.monotonic() - started
    assert elapsed < FAST_S, f"{method} {path}: {elapsed:.2f} s - it reached the engine"
    assert engine.requests == [], engine.requests
    assert seen["status"] == 501, seen
    error = json.loads(seen["body"])["error"]
    assert error == {"code": 501, "status": "UNIMPLEMENTED",
                     "message": bq.REFUSALS[feature],
                     "errors": [{"message": bq.REFUSALS[feature], "domain": "global",
                                 "reason": "notImplemented"}]}
    assert seen["rows_at_start"] == before + 1           # the row, before the answer
    rows = rows_of(audit)[before:]
    assert len(rows) == 1, [r["tool"] for r in rows]
    row = rows[0]
    assert [f for f in row["flags"] if f.startswith(bq.REFUSED_FLAG)] == \
        [f"{bq.REFUSED_FLAG}{feature}"]
    assert row["exit"] == "error" and row["error"] == {"code": "not_available",
                                                       "message": bq.REFUSALS[feature]}
    assert row["args"]["feature"] == feature and set(row) == set(ROW_KEYS)
    return row


def _client_refused_at_once(engine, audit, call, text: str) -> dict:
    """The real client's call raises the refusal at once, unretried, and leaves
    exactly one new row, flagged; the engine never saw it. That row."""
    from google.api_core import exceptions

    before = len(rows_of(audit))
    started = time.monotonic()
    with pytest.raises(exceptions.GoogleAPICallError) as raised:
        call()
    assert time.monotonic() - started < FAST_S, "it reached the engine, or was retried"
    assert raised.value.code == 501 and text in str(raised.value), raised.value
    assert raised.value.errors[0]["reason"] == "notImplemented"
    assert engine.requests == [], engine.requests
    rows = rows_of(audit)[before:]
    assert len(rows) == 1, [r["tool"] for r in rows]
    assert "refused:external_table" in rows[0]["flags"]
    return rows[0]


def test_external_table_over_gcs_is_refused_by_name(gcs_less_engine, gcs_less_client,
                                                     audit):
    """CREATE EXTERNAL TABLE over gs:// URIs: the engine dials Cloud Storage, which
    has no route here, and hangs - and every request after it. Refused by name,
    before any engine call, on both statement routes; so is every other external
    data statement, an OPTIONS list naming `uris` or `format` included, whether
    sqlglot parses it or falls back to an opaque Command."""
    jobs = f"/bigquery/v2/projects/{PROJECT}/jobs"
    queries = f"/bigquery/v2/projects/{PROJECT}/queries"
    for sql, op_class in (
            (f"CREATE EXTERNAL TABLE `{PROJECT}.warehouse.ext_orders` "
             f"OPTIONS (format = 'CSV', uris = ['{GCS_CSV}'])", "ddl"),
            ("create or replace external table warehouse.ext_orders (order_id INT64) "
             "options (format='PARQUET', uris=['gs://alkera-world/orders.parquet'])",
             "destructive"),                       # a replace drops: the classifier's rule
            ("CREATE EXTERNAL TABLE IF NOT EXISTS warehouse.ext_orders WITH CONNECTION "
             f"`us.gcs` OPTIONS (format='CSV', uris=['{GCS_CSV}'])", "ddl"),
            ("CREATE EXTERNAL TABLE warehouse.ext_orders WITH PARTITION COLUMNS OPTIONS "
             "(format='CSV', uris=['gs://alkera-world/o/*'], "
             "hive_partition_uri_prefix='gs://alkera-world/o')", "ddl"),
            ("CREATE TEMP EXTERNAL TABLE ext OPTIONS (format='CSV', "
             f"uris=['{GCS_CSV}'])", "ddl"),
            (f"SELECT 1; CREATE EXTERNAL TABLE warehouse.ext_orders OPTIONS (format='CSV', "
             f"uris=['{GCS_CSV}'])", "ddl"),
            (f"CREATE TABLE warehouse.ext_orders OPTIONS (format='CSV', uris=['{GCS_CSV}'])",
             "ddl"),
            ("CREATE EXTERNAL SCHEMA fed WITH CONNECTION `us.glue` OPTIONS "
             "(external_source='aws-glue://arn:aws:glue:us-east-1:1:database/x', "
             "location='aws-us-east-1')", "ddl"),
            ("DROP EXTERNAL TABLE warehouse.ext_orders", "destructive")):
        for path, body in ((jobs, {"configuration": {"query": {
                                "query": sql, "useLegacySql": False}}}),
                           (queries, {"query": sql, "useLegacySql": False})):
            row = _refused_at_once(gcs_less_engine, audit, "POST", path, body)
            # a refused query is the statement it was, classified by its SQL
            assert row["tool"] == "bigquery.statement" and row["args"]["sql"] == sql
            assert row["op_class"] == op_class, (sql, row["op_class"])
    assert "external tables" in bq.REFUSALS["external_table"]
    assert "Cloud Storage" in bq.REFUSALS["external_table"]
    # the real client, as the agent sends it
    row = _client_refused_at_once(gcs_less_engine, audit, lambda: gcs_less_client.query(
        f"CREATE EXTERNAL TABLE `{PROJECT}.warehouse.ext_orders` OPTIONS (format='CSV', "
        f"uris=['{GCS_CSV}'])").result(), "external tables")
    assert row["tool"] == "bigquery.statement"
    # words only: a keyword in a string, a column or a table named like an
    # option, a gs:// path in a description, FORMAT() the function - forwarded
    for sql in ("SELECT 'CREATE EXTERNAL TABLE t OPTIONS (uris = [1])' AS s",
                "CREATE TABLE warehouse.notes (uris STRING, format STRING) OPTIONS "
                "(description = 'copied from gs://alkera-world/notes.csv')",
                "SELECT uris, format FROM warehouse.notes WHERE uris = 'gs://x'",
                f"CREATE TABLE `{PROJECT}.warehouse.external` (a INT64)",
                "CREATE OR REPLACE TABLE warehouse.external AS SELECT FORMAT('%d', 1) AS f",
                "DROP TABLE warehouse.external"):
        assert bq.fence(sql, bq.statements(sql), PROJECT, None) is None, sql


def test_query_with_external_table_definition_is_refused_by_name(gcs_less_engine,
                                                                gcs_less_client, audit):
    """A query job's tableDefinitions - a temporary table over gs:// files - would
    send the engine to Cloud Storage; it answered `Table not found` for the name
    it defines. Refused by name, the query still its statement row."""
    from google.cloud import bigquery

    sql = "SELECT COUNT(*) AS n FROM ext_orders"
    defs = {"ext_orders": EXTERNAL_CONFIG}
    row = _refused_at_once(gcs_less_engine, audit, "POST",
                           f"/bigquery/v2/projects/{PROJECT}/jobs",
                           {"jobReference": {"projectId": PROJECT, "jobId": "ext-defs"},
                            "configuration": {"query": {"query": sql, "useLegacySql": False,
                                                        "tableDefinitions": defs}}})
    assert row["tool"] == "bigquery.statement" and row["args"]["sql"] == sql
    assert row["op_class"] == "read"
    # jobs.query does not publish tableDefinitions; sent anyway, it is refused too
    row = _refused_at_once(gcs_less_engine, audit, "POST",
                           f"/bigquery/v2/projects/{PROJECT}/queries",
                           {"query": sql, "useLegacySql": False, "tableDefinitions": defs})
    assert row["tool"] == "bigquery.statement"
    # the real client's QueryJobConfig(table_definitions=...)
    external = bigquery.ExternalConfig("CSV")
    external.source_uris = [GCS_CSV]
    external.schema = [bigquery.SchemaField("order_id", "INT64")]
    row = _client_refused_at_once(gcs_less_engine, audit, lambda: gcs_less_client.query(
        sql, job_config=bigquery.QueryJobConfig(table_definitions={"ext_orders": external})
    ).result(), "external tables")
    assert row["tool"] == "bigquery.statement" and row["args"]["sql"] == sql


def test_tables_insert_with_external_data_configuration_is_refused(gcs_less_engine,
                                                                  gcs_less_client, audit):
    """tables.insert with externalDataConfiguration over gs:// is the measured
    hang: the engine dialled Cloud Storage and every request after it timed out.
    Refused by name on tables.insert / patch / update - and biglakeConfiguration
    too, which the engine answers 200 and files as a plain table."""
    from google.cloud import bigquery

    tables = f"/bigquery/v2/projects/{PROJECT}/datasets/warehouse/tables"
    ref = {"projectId": PROJECT, "datasetId": "warehouse", "tableId": "ext_orders"}
    biglake = {"connectionId": "us.gcs", "storageUri": "gs://alkera-world/iceberg/",
               "fileFormat": "PARQUET", "tableFormat": "ICEBERG"}
    for method, path, body in (
            ("POST", tables, {"tableReference": ref,
                              "externalDataConfiguration": EXTERNAL_CONFIG}),
            ("POST", tables, {"tableReference": ref, "biglakeConfiguration": biglake,
                              "schema": EXTERNAL_CONFIG["schema"]}),
            ("PATCH", f"{tables}/fct_orders", {"externalDataConfiguration": EXTERNAL_CONFIG}),
            ("PUT", f"{tables}/fct_orders", {"tableReference": {**ref, "tableId": "fct_orders"},
                                              "externalDataConfiguration": EXTERNAL_CONFIG})):
        row = _refused_at_once(gcs_less_engine, audit, method, path, body)
        assert row["op_class"] == "ddl"
        name = "ext_orders" if method == "POST" else "fct_orders"
        assert row["targets"] == [f"{PROJECT}.warehouse.{name}"], row["targets"]
        assert row["tool"] == {"POST": "bigquery.table_insert", "PATCH": "bigquery.table_patch",
                               "PUT": "bigquery.table_update"}[method]
    # the real client's create_table with an ExternalConfig
    table = bigquery.Table(f"{PROJECT}.warehouse.ext_orders")
    external = bigquery.ExternalConfig("CSV")
    external.source_uris = [GCS_CSV]
    table.external_data_configuration = external
    row = _client_refused_at_once(gcs_less_engine, audit,
                                  lambda: gcs_less_client.create_table(table),
                                  "external tables")
    assert row["tool"] == "bigquery.table_insert"


def test_federated_dataset_is_refused_as_external_data(gcs_less_engine, audit):
    """datasets.insert with externalDatasetReference (CREATE EXTERNAL SCHEMA's REST
    form): the engine answers 200 and files a plain, empty dataset."""
    row = _refused_at_once(gcs_less_engine, audit, "POST",
                           f"/bigquery/v2/projects/{PROJECT}/datasets",
                           {"datasetReference": {"projectId": PROJECT, "datasetId": "fed"},
                            "externalDatasetReference": {
                                "externalSource": "aws-glue://arn:aws:glue:us-east-1:1:database/x",
                                "connection": "projects/p/locations/aws-us-east-1/connections/c"}})
    assert row["tool"] == "bigquery.dataset_insert" and row["op_class"] == "ddl"


def test_load_and_export_data_statements_are_refused_as_load_and_extract_jobs(
        gcs_less_engine, audit):
    """LOAD DATA ... FROM FILES is a load job in SQL: the engine answers DONE and
    loads nothing. EXPORT DATA is an extract job in SQL: it dials Cloud Storage.
    Refused by name, as their REST forms are."""
    jobs = f"/bigquery/v2/projects/{PROJECT}/jobs"
    for sql, feature in (
            (f"LOAD DATA INTO warehouse.fct_orders FROM FILES (format='CSV', "
             f"uris=['{GCS_CSV}'])", "load_job"),
            (f"load data overwrite warehouse.fresh (a INT64) from files (format='CSV', "
             f"uris=['{GCS_CSV}'])", "load_job"),
            (f"SELECT 1; LOAD DATA INTO warehouse.fct_orders FROM FILES (format='CSV', "
             f"uris=['{GCS_CSV}'])", "load_job"),
            ("EXPORT DATA OPTIONS (uri='gs://alkera-world/out/*.csv', format='CSV') AS "
             "SELECT order_id FROM warehouse.fct_orders", "extract_job")):
        row = _refused_at_once(gcs_less_engine, audit, "POST", jobs, {
            "configuration": {"query": {"query": sql, "useLegacySql": False}}}, feature)
        assert row["tool"] == "bigquery.statement" and row["args"]["sql"] == sql
    assert "LOAD DATA" in bq.REFUSALS["load_job"] and "load jobs" in bq.REFUSALS["load_job"]
    assert "EXPORT DATA" in bq.REFUSALS["extract_job"]


# ---------------------------------------------------------- module contract
def test_no_tool_name_ends_in_an_mcp_tool():
    """blindness.row_tools suffix-matches native rows to MCP tools: a row ending
    in `sql_query` would explain any change to the world's DuckDB file."""
    from alkera_gateway.tools import TOOLS

    ops = {op for _, _, op, _ in bq.ROUTES} | {"statement", "unknown_route", "health",
                                                "request"}
    assert not ops & set(TOOLS)
    assert not any(op.startswith(("bi_", "s3_", "docs_", "chat_", "issues_", "kb_", "logs_"))
                   for op in ops)


def test_import_dials_nothing_without_an_upstream(monkeypatch):
    monkeypatch.delenv(bq.UPSTREAM_ENV, raising=False)
    assert bq.boot() is None
    assert (bq.NAME, bq.HOST, bq.PORT) == ("bigquery", "bigquery.internal", 9050)


def test_boot_seeds_the_world_and_says_so(emulator, world, monkeypatch, capsys):
    monkeypatch.setenv(bq.UPSTREAM_ENV, emulator)
    monkeypatch.setenv("ALKERA_WORLD_ROOT", str(world.parents[2]))
    seeded = bq.boot()
    assert seeded["project"] == PROJECT and seeded["files"] == ["warehouse.duckdb"]
    assert seeded["tables"]["warehouse.fct_orders"] == 400
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed["seed"] == "bigquery" and printed["rows"] == seeded["rows"]


def test_boot_stops_the_process_when_the_seed_fails(world, monkeypatch):
    monkeypatch.setenv(bq.UPSTREAM_ENV, f"http://127.0.0.1:{free_port()}")
    monkeypatch.setenv("ALKERA_WORLD_ROOT", str(world.parents[2]))
    monkeypatch.setattr(bigquery_seed, "WAIT_S", 0.5)
    with pytest.raises(SystemExit, match="could not be seeded"):
        bq.boot()
