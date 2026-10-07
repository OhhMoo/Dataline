"""The fake Databricks workspace, tier 1 (native/services/databricks.py).

Claims, each against the shape Databricks publishes (REST API reference: SQL Statement
Execution 2.0, Unity Catalog 2.1, SQL Warehouses 2.0, SCIM 2.0 `Me`, Jobs 2.1):

- STATEMENTS. `POST /api/2.0/sql/statements` runs one statement on the seeded lakehouse
  and answers `{statement_id, status, manifest, result}` with JSON_ARRAY data; the
  answer equals the world's DuckDB; `GET .../{id}`, `/result/chunks/0` and `/cancel`
  work on it; the connector's SEA sessions (`/api/2.0/sql/sessions`) carry catalog and
  schema across statements.
- FENCES ARE LOUD. Delta-only SQL fails the statement with a message naming the
  feature; the connector's Thrift transport is refused with a header the connector
  shows; an unknown route is Databricks' 404 envelope.
- ONE ROW PER REQUEST, before the response - `databricks.statement` per statement,
  `databricks.<op>` for the rest, `databricks.unknown_route` for what is not served.
- JOBS ARE THE WORLD SCHEDULER. Jobs 2.1 lists the world's jobs, and run-now is the
  same `sched_trigger` the agent's `./ops/sched trigger` makes (WorldScheduler.trigger:
  one manual run in the ledger) - never `advance`, which only the grader calls.
- THE WORLD IS NEVER WRITTEN through a statement: the lakehouse is a copy in the
  service's own state.
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import world_hash
from fastapi.testclient import TestClient
from test_databricks_sql import digest, lakehouse_world, truth

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "world"))

from alkera_gateway import GatewayConfig  # noqa: E402
from alkera_gateway.backends import BACKENDS, bundle  # noqa: E402
from alkera_gateway.native import context as native_context  # noqa: E402
from alkera_gateway.native.services import databricks  # noqa: E402

TOKEN = native_context.DEV_TOKEN
SQL = "/api/2.0/sql/statements"


class Workspace:
    """One world served by the Databricks app, with its audit rows."""

    def __init__(self, root: Path, tmp: Path, audit_file: Path):
        self.root, self.audit_file = root, audit_file
        audit = tmp / "audit"
        self.state = tmp / "service-state"
        self.config = GatewayConfig(
            workspace=root / "workspace",
            warehouse=root / "var" / "warehouse" / "warehouse.duckdb",
            scheduler=root / "var" / "sched" / "state.json",
            ingest=root / "var" / "ingest" / "state.json",
            dashboards=root / "var" / "bi" / "dashboards", audit_dir=audit,
            profiles_dir=root / "build", episode_id="dbx", initial_world_fp="fixture",
            observability=root / "var" / "obs" / "observability.yml",
            workflow_dir=root / "var" / "workflow", world_root=root,
            bi_state_dir=self.state / "bi", obs_state_dir=self.state / "obs")
        native_context.set_context(databricks.app, native_context.Context(self.config))
        self.client = TestClient(databricks.app, base_url="https://databricks.internal")
        self.headers = {"Authorization": f"Bearer {TOKEN}"}

    def call(self, method: str, path: str, **kw):
        headers = {**self.headers, **kw.pop("headers", {})}
        return self.client.request(method, path, headers=headers, **kw)

    def ok(self, method: str, path: str, **kw) -> dict:
        r = self.call(method, path, **kw)
        assert r.status_code == 200, r.text
        return r.json()

    def statement(self, sql: str, **body) -> dict:
        return self.ok("POST", SQL, json={"statement": sql, "warehouse_id": "alkera", **body})

    def rows(self) -> list[dict]:
        if not self.audit_file.exists():
            return []
        return [json.loads(line) for line in self.audit_file.read_text("utf-8").splitlines()]


@pytest.fixture
def ws(tmp_path, monkeypatch):
    root = tmp_path / "world"
    lakehouse_world(root)
    audit_file = tmp_path / "native-actions.jsonl"
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(audit_file))
    monkeypatch.setenv("ALKERA_SCHEDULER", str(root / "var" / "sched" / "state.json"))
    workspace = Workspace(root, tmp_path, audit_file)
    yield workspace
    databricks.reset()


WAREHOUSE = lambda ws: ws.root / "var" / "warehouse" / "warehouse.duckdb"  # noqa: E731


# ------------------------------------------------------------------ contract
def test_the_module_is_its_registry_row():
    row = BACKENDS["databricks"]
    assert (databricks.NAME, databricks.HOST, databricks.PORT) == (row.name, row.host, row.port)
    assert row.hosted and row.tls and row.scheme == "https" and row.port == 443
    assert row.statement_tool == databricks.STATEMENT_TOOL == "databricks.statement"
    assert row.env["DATABRICKS_HOST"] == "https://databricks.internal"
    assert row.env["DATABRICKS_SERVER_HOSTNAME"] == row.host
    assert row.env["DATABRICKS_HTTP_PATH"] == f"/sql/1.0/warehouses/{databricks.WAREHOUSE_ID}"
    assert row.env["DATABRICKS_TOKEN"] == TOKEN
    # the connector's trust knob (OpenSSL's default paths), beside REQUESTS_CA_BUNDLE
    assert row.env["SSL_CERT_FILE"] == bundle(row)


# ---------------------------------------------------------------- statements
def test_statement_execution_roundtrip(ws):
    """The request the SDK sends (`w.statement_execution.execute_statement(warehouse_id=
    "alkera", statement=...)`, 0.47.0: trailing slash), answered as the SDK reads it."""
    sql = ("select sum(order_amount) from main.warehouse.fct_orders "
           "where order_status='completed'")
    r = ws.ok("POST", SQL + "/", json={"statement": sql, "warehouse_id": "alkera"})
    assert r["status"]["state"] == "SUCCEEDED"
    manifest = r["manifest"]
    assert manifest["format"] == "JSON_ARRAY" and manifest["total_row_count"] == 1
    column = manifest["schema"]["columns"][0]
    assert (column["type_name"], column["position"]) == ("DECIMAL", 0)
    expected = truth(WAREHOUSE(ws), "SELECT sum(order_amount) FROM warehouse.warehouse."
                                    "fct_orders WHERE order_status = 'completed'")[0][0]
    assert r["result"]["data_array"] == [[str(expected)]]
    again = ws.ok("GET", f"{SQL}/{r['statement_id']}")
    assert again["result"]["data_array"] == [[str(expected)]]
    chunk = ws.ok("GET", f"{SQL}/{r['statement_id']}/result/chunks/0")
    assert chunk["data_array"] == [[str(expected)]] and chunk["chunk_index"] == 0
    assert ws.ok("POST", f"{SQL}/{r['statement_id']}/cancel") == {}


def test_statement_execution_roundtrip_with_the_sdk(ws):
    """The same, through the real databricks-sdk over a real socket. The SDK is in the
    databricks client lock (episode images built with --warehouse-clients databricks),
    not in the gateway venv; tasks/harness/databricks_e2e_check.py runs it in the box."""
    sdk = pytest.importorskip("databricks.sdk")
    import uvicorn

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(databricks.app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        w = sdk.WorkspaceClient(host=f"http://127.0.0.1:{port}", token=TOKEN)
        r = w.statement_execution.execute_statement(
            warehouse_id="alkera",
            statement="select sum(order_amount) from main.warehouse.fct_orders "
                      "where order_status='completed'")
        expected = truth(WAREHOUSE(ws), "SELECT sum(order_amount) FROM warehouse."
                                        "warehouse.fct_orders WHERE order_status = "
                                        "'completed'")[0][0]
        assert r.status.state.value == "SUCCEEDED"
        assert r.result.data_array == [[str(expected)]]
        assert w.current_user.me().user_name == databricks.USER
        assert [c.name for c in w.catalogs.list()] == ["main"]
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_every_statement_writes_one_row_before_response(ws):
    """The row is on file when the response STARTS: an ASGI wrapper reads the action
    log at `http.response.start`."""
    seen: list[int] = []

    async def watching(scope, receive, send):
        async def spy(message):
            if message["type"] == "http.response.start":
                seen.append(len([r for r in ws.rows() if r["tool"] == "databricks.statement"]))
            await send(message)
        await databricks.app(scope, receive, spy)

    client = TestClient(watching, base_url="https://databricks.internal")
    statements = ["select 1", "select count(*) from main.warehouse.fct_orders",
                  "DESCRIBE HISTORY main.warehouse.fct_orders", "selec nope"]
    for n, sql in enumerate(statements, 1):
        r = client.post(SQL, headers=ws.headers,
                        json={"statement": sql, "warehouse_id": "alkera"})
        assert r.status_code == 200, r.text
        assert seen[-1] == n                       # this statement's row, before the answer
    rows = [r for r in ws.rows() if r["tool"] == "databricks.statement"]
    assert [r["args"]["sql"] for r in rows] == statements
    assert [r["exit"] for r in rows] == ["ok", "ok", "error", "error"]
    assert rows[1]["op_class"] == "read"
    assert rows[1]["targets"] == ["main.warehouse.fct_orders"]
    assert all(r["transport"] == "native" for r in rows)


def test_delta_only_sql_refused_by_name(ws):
    before = digest(WAREHOUSE(ws))
    r = ws.statement("DESCRIBE HISTORY main.warehouse.fct_orders")
    assert r["status"]["state"] == "FAILED"
    error = r["status"]["error"]
    assert "DESCRIBE HISTORY" in error["message"]
    assert error["error_code"] == "BAD_REQUEST"
    row = [x for x in ws.rows() if x["tool"] == "databricks.statement"][-1]
    assert row["exit"] == "error" and "DESCRIBE HISTORY" in row["error"]["message"]
    assert digest(WAREHOUSE(ws)) == before


def test_remote_write_leaves_world_fingerprint_unchanged(ws):
    before_file, before_world = digest(WAREHOUSE(ws)), world_hash(ws.root)
    created = ws.statement("CREATE TABLE main.default.scratch (id INT, note STRING)")
    assert created["status"]["state"] == "SUCCEEDED"
    inserted = ws.statement("INSERT INTO main.default.scratch VALUES (1, 'alpha')")
    assert inserted["result"]["data_array"] == [["1", "1"]]
    ws.statement("DELETE FROM main.warehouse.fct_orders")
    assert ws.statement("select count(*) from main.warehouse.fct_orders")[
        "result"]["data_array"] == [["0"]]
    assert digest(WAREHOUSE(ws)) == before_file
    assert world_hash(ws.root) == before_world
    rows = [r for r in ws.rows() if r["tool"] == "databricks.statement"]
    assert [r["op_class"] for r in rows] == ["ddl", "write", "destructive", "read"]
    lakehouse = ws.state / "databricks" / "lakehouse.duckdb"
    assert lakehouse.is_file() and not lakehouse.is_relative_to(ws.root)


def test_sea_sessions_carry_catalog_and_schema(ws):
    session = ws.ok("POST", "/api/2.0/sql/sessions",
                    json={"warehouse_id": "alkera", "catalog": "main",
                          "schema": "warehouse"})["session_id"]
    r = ws.statement("select count(*) from dim_customers", session_id=session)
    assert r["result"]["data_array"] == [["4"]]
    ws.statement("USE SCHEMA raw", session_id=session)
    assert ws.statement("select current_schema()", session_id=session)[
        "result"]["data_array"] == [["raw"]]
    assert ws.ok("DELETE", f"/api/2.0/sql/sessions/{session}",
                 json={"warehouse_id": "alkera"}) == {}
    gone = ws.call("POST", SQL, json={"statement": "select 1", "warehouse_id": "alkera",
                                      "session_id": session})
    assert gone.status_code == 404 and gone.json()["error_code"] == "NOT_FOUND"


def test_the_connectors_bookkeeping_is_served_and_reads(ws):
    """What databricks-sql-connector 4.6 sends besides statements: a feature-flag lookup
    at connect, a close per cursor, telemetry; each one row, none a write."""
    flags = ws.call("GET", "/api/2.0/connector-service/feature-flags/PYTHON/4.6.0")
    assert flags.status_code == 200 and flags.json()["flags"] == []
    session = ws.ok("POST", "/api/2.0/sql/sessions",
                    json={"warehouse_id": "alkera", "schema": "dbt_new"})["session_id"]
    made = ws.statement("CREATE SCHEMA IF NOT EXISTS main.dbt_new", session_id=session)
    assert made["status"]["state"] == "SUCCEEDED"
    statement = ws.statement("select 1", session_id=session)["statement_id"]
    assert ws.ok("DELETE", f"{SQL}/{statement}") == {}
    assert ws.call("GET", f"{SQL}/{statement}").status_code == 404
    sent = ws.call("POST", "/telemetry-ext", json={"protoLogs": ["a", "b"]})
    assert sent.status_code == 200 and sent.json()["numSuccess"] == 2
    rows = ws.rows()
    tools = {r["tool"]: r for r in rows}
    for tool in ("databricks.feature_flags", "databricks.session_create",
                 "databricks.statement_close", "databricks.telemetry"):
        assert tools[tool]["op_class"] == "read", tool
    writes = [r["tool"] for r in rows if r["op_class"] in ("write", "ddl", "destructive")]
    assert writes == ["databricks.statement"]          # the CREATE SCHEMA, nothing else


def test_statement_parameters_and_row_limit(ws):
    r = ws.statement("select order_id from main.warehouse.fct_orders "
                     "where order_status = :status order by order_id",
                     parameters=[{"name": "status", "value": "completed"}], row_limit=2)
    assert r["result"]["data_array"] == [["1"], ["2"]]
    assert r["manifest"]["truncated"] is True


def test_unknown_warehouse_and_bad_body_are_refused(ws):
    r = ws.call("POST", SQL, json={"statement": "select 1", "warehouse_id": "nope"})
    assert r.status_code == 404 and r.json()["error_code"] == "RESOURCE_DOES_NOT_EXIST"
    r = ws.call("POST", SQL, json={"warehouse_id": "alkera"})
    assert r.status_code == 400 and r.json()["error_code"] == "INVALID_PARAMETER_VALUE"


# ------------------------------------------------------------- fences, auth
def test_thrift_transport_refused_loudly(ws):
    r = ws.call("POST", f"/sql/1.0/warehouses/{databricks.WAREHOUSE_ID}",
                content=b"\x80\x01\x00\x01OpenSession",
                headers={"Content-Type": "application/x-thrift"})
    assert r.status_code == 400
    assert "use_sea=True" in r.headers["x-thriftserver-error-message"]
    assert ws.rows()[-1]["tool"] == "databricks.thrift"
    assert ws.rows()[-1]["exit"] == "error"


def test_unknown_route_404_shape_and_audit(ws):
    r = ws.call("GET", "/api/2.0/clusters/list")
    assert r.status_code == 404
    assert r.json() == {"error_code": "ENDPOINT_NOT_FOUND",
                        "message": "No API found for 'GET /api/2.0/clusters/list'"}
    row = ws.rows()[-1]
    assert row["tool"] == "databricks.unknown_route" and row["exit"] == "error"
    assert row["args"]["path"] == "/api/2.0/clusters/list"


def test_a_missing_or_wrong_token_is_401_and_audited(ws):
    for headers in ({"Authorization": ""}, {"Authorization": "Bearer nope"}):
        r = ws.call("GET", "/api/2.0/sql/warehouses", headers=headers)
        assert r.status_code == 401 and r.json()["error_code"] == "UNAUTHENTICATED"
    assert [r["exit"] for r in ws.rows()[-2:]] == ["error", "error"]


# ------------------------------------------------------- catalog, identity
def test_uc_lists_three_level_namespace(ws):
    catalogs = ws.ok("GET", "/api/2.1/unity-catalog/catalogs")["catalogs"]
    assert [c["name"] for c in catalogs] == ["main"]
    schemas = ws.ok("GET", "/api/2.1/unity-catalog/schemas",
                    params={"catalog_name": "main"})["schemas"]
    assert {s["full_name"] for s in schemas} >= {"main.default", "main.warehouse",
                                                  "main.raw", "main.information_schema"}
    tables = ws.ok("GET", "/api/2.1/unity-catalog/tables",
                   params={"catalog_name": "main", "schema_name": "warehouse"})["tables"]
    names = {t["full_name"]: t for t in tables}
    assert "main.warehouse.fct_orders" in names
    fct = ws.ok("GET", "/api/2.1/unity-catalog/tables/main.warehouse.fct_orders")
    assert (fct["catalog_name"], fct["schema_name"], fct["name"]) == \
        ("main", "warehouse", "fct_orders")
    assert fct["table_type"] == "MANAGED" and fct["data_source_format"] == "DELTA"
    assert [(c["name"], c["type_name"]) for c in fct["columns"]][-1] == \
        ("order_amount", "DECIMAL")
    missing = ws.call("GET", "/api/2.1/unity-catalog/tables/main.warehouse.nope")
    assert missing.status_code == 404
    assert missing.json()["error_code"] == "TABLE_DOES_NOT_EXIST"
    # what the agent sees through UC is what SQL sees
    count = ws.statement("select count(*) from main.information_schema.tables "
                         "where table_schema = 'warehouse'")["result"]["data_array"]
    assert int(count[0][0]) == len(tables)


def test_uc_reads_one_row_each(ws):
    ws.ok("GET", "/api/2.1/unity-catalog/tables",
          params={"catalog_name": "main", "schema_name": "warehouse"})
    row = ws.rows()[-1]
    assert row["tool"] == "databricks.tables_list" and row["op_class"] == "read"
    assert "main.warehouse.fct_orders" in row["read_targets"]


def test_warehouses_me_and_host_metadata(ws):
    warehouses = ws.ok("GET", "/api/2.0/sql/warehouses")["warehouses"]
    assert [(w["id"], w["state"]) for w in warehouses] == [("alkera", "RUNNING")]
    assert warehouses[0]["odbc_params"] == {
        "hostname": "databricks.internal", "path": "/sql/1.0/warehouses/alkera",
        "protocol": "https", "port": 443}
    me = ws.ok("GET", "/api/2.0/preview/scim/v2/Me")
    assert me["userName"] == databricks.USER and me["active"] is True
    meta = ws.client.get("/.well-known/databricks-config").json()
    assert meta["workspace_id"] == databricks.WORKSPACE_ID
    assert ws.rows()[-1]["tool"] == "databricks.host_metadata"


# ------------------------------------------------------------------- jobs
def test_jobs_list_is_the_world_scheduler(ws):
    jobs = ws.ok("GET", "/api/2.1/jobs/list")
    assert [j["settings"]["name"] for j in jobs["jobs"]] == ["daily_load"]
    assert jobs["jobs"][0]["job_id"] == 1 and jobs["has_more"] is False
    assert ws.rows()[-1]["tool"] == "databricks.sched_list"


def test_jobs_run_now_is_a_world_scheduler_trigger_never_an_advance(ws, monkeypatch):
    """run-now goes where `./ops/sched trigger` goes: Operations.sched_trigger ->
    WorldScheduler.trigger, ONE manual run in the ledger. The clock moves only by that
    run's own declared duration, exactly as sched_trigger moves it; `advance` - the
    grader's, which lands arrivals and fires scheduled jobs - is never called."""
    from world import scheduler

    def no_advance(*_a, **_k):
        raise AssertionError("run-now must never advance the world's clock")

    monkeypatch.setattr(scheduler, "advance", no_advance)
    state_file = ws.root / "var" / "sched" / "state.json"
    before = json.loads(state_file.read_text())
    started = ws.ok("POST", "/api/2.1/jobs/run-now", json={"job_id": 1})
    after = json.loads(state_file.read_text())
    assert len(after["runs"]) == len(before["runs"]) + 1
    run = after["runs"][-1]
    assert (run["job"], run["trigger"]) == ("daily_load", "manual")
    assert after["vnow"] == run["vts_end"]                 # its own 5 s, nothing more
    assert started["run_id"] == len(after["runs"])
    # the job declares no `exec` block, so nothing ran: the ledger says so
    assert run["executed"] is False and run["effects"] == {}
    got = ws.ok("GET", "/api/2.1/jobs/runs/get", params={"run_id": started["run_id"]})
    assert got["job_id"] == 1 and got["run_id"] == started["run_id"]
    assert got["state"]["life_cycle_state"] == "TERMINATED"
    assert got["state"]["result_state"] == "FAILED"
    assert got["trigger"] == "ONE_TIME"
    listed = ws.ok("GET", "/api/2.1/jobs/runs/list", params={"job_id": 1})
    assert [r["run_id"] for r in listed["runs"]] == [started["run_id"]]
    tools = [r["tool"] for r in ws.rows()]
    assert tools[-3:] == ["databricks.sched_trigger", "databricks.sched_state",
                          "databricks.sched_state"]
    trigger_row = [r for r in ws.rows() if r["tool"] == "databricks.sched_trigger"][0]
    assert trigger_row["op_class"] == "exec"
    assert "scheduler:daily_load" in trigger_row["targets"]


def test_run_now_unknown_job_is_databricks_shaped(ws):
    r = ws.call("POST", "/api/2.1/jobs/run-now", json={"job_id": 99})
    assert r.status_code == 400 and r.json()["error_code"] == "INVALID_PARAMETER_VALUE"
    assert "99" in r.json()["message"]


# ------------------------------------------------------------------- health
def test_health_counts_the_lakehouse(ws):
    health = ws.client.get("/health").json()
    assert health["ok"] is True and health["service"] == "databricks"
    # items, fct_orders, dim_customers, completed_orders (a view, materialised), raw.orders
    assert health["items"] == 5


# ------------------------------------------------------------- serve.py, TLS
def test_serve_mounts_databricks_on_its_row_port_only_when_plugged():
    from alkera_gateway.native import serve, services

    assert "databricks" not in (serve.selected_services("de/laptop", []) or ())
    names = serve.selected_services("de/databricks", [])
    assert names[-1] == "databricks"
    loaded, missing = services.load(("databricks",))
    assert not missing
    item = serve.mount(loaded)[0]
    assert (item.host, item.port, item.tls) == ("databricks.internal", 443, True)
    assert item.certificate is databricks.certificate


def test_serve_refuses_a_tls_row_without_a_certificate():
    from alkera_gateway.native import serve

    item = serve.Mounted("hx", "hx.internal", 1, databricks.app, "module", True, tls=True)
    with pytest.raises(SystemExit, match="no certificate"):
        serve.tls_options(item)


def test_served_over_tls_with_the_published_bundle(ws, tmp_path, monkeypatch):
    """serve.py's own `serve_all`, on a free port: the module mints its certificate
    into the trust directory, the bundle there verifies it, the key is elsewhere, and
    the accepted connection is counted as a destination."""
    import asyncio

    import requests

    from alkera_gateway.native import serve, services

    trust = tmp_path / "trust"
    monkeypatch.setenv(databricks.TRUST_ENV, str(trust))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    loaded, _ = services.load(("databricks",))
    mounted = serve.mount(loaded, offset=port - databricks.PORT)
    log = serve.DestinationLog(tmp_path / "destinations.jsonl")
    log.declare("databricks.internal", port)
    loop = asyncio.new_event_loop()
    task = loop.create_task(serve.serve_all(mounted, "127.0.0.1", log))
    thread = threading.Thread(target=lambda: loop.run_until_complete(
        asyncio.gather(task, return_exceptions=True)), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        bundle_file = trust / "bundle.pem"
        while not bundle_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert bundle_file.is_file() and not list(trust.glob("*.key"))
        url = f"https://localhost:{port}/api/2.0/sql/warehouses"
        while True:
            try:
                r = requests.get(url, headers=ws.headers, verify=str(bundle_file), timeout=5)
                break
            except requests.ConnectionError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        assert r.status_code == 200 and r.json()["warehouses"][0]["id"] == "alkera"
        with pytest.raises(requests.exceptions.SSLError):
            requests.get(url, headers=ws.headers, verify=True, timeout=5)  # certifi alone
    finally:
        loop.call_soon_threadsafe(task.cancel)
        thread.join(timeout=20)
    rows = {(r["host"], r["port"]): r["count"] for r in log.rows()}
    assert rows[("databricks.internal", port)] >= 1
