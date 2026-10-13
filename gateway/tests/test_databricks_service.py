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
- JOBS ARE THE WORLD SCHEDULER. Jobs 2.1 lists the world's jobs, and run-now QUEUES
  one run (WorldScheduler.enqueue): nothing executes and the clock does not move until
  the grader's `advance`, which runs it; the run id run-now returns shows QUEUED until
  then and the executed ledger run after. Blindness attributes the queue entry to the
  run-now row (`databricks.sched_trigger`).
- THE WORLD IS NEVER WRITTEN through a statement: the lakehouse is a copy in the
  service's own state.
"""
from __future__ import annotations

import hashlib
import json
import socket
import sys
import threading
import time
from datetime import datetime
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


def test_a_session_schema_is_an_identifier_never_sql(ws):
    """The reviewer's repro: a schema name carrying `"; drop view ...; --` reaches the
    engine only as a quoted identifier - nothing is dropped, the rows say what was
    sent."""
    evil = 'x"; DROP VIEW memory.dbx_information_schema.tables; --'
    session = ws.ok("POST", "/api/2.0/sql/sessions",
                    json={"warehouse_id": "alkera", "schema": evil})["session_id"]
    assert ws.statement("select 1", session_id=session)["status"]["state"] == "SUCCEEDED"
    made = ws.statement(f"CREATE SCHEMA main.`{evil}`", session_id=session)
    assert made["status"]["state"] == "SUCCEEDED", made
    # the schema now exists, so the session's USE names it - as an identifier
    created = ws.statement("CREATE TABLE t AS SELECT 1 AS id", session_id=session)
    assert created["status"]["state"] == "SUCCEEDED", created
    assert ws.statement("select id from t", session_id=session)[
        "result"]["data_array"] == [["1"]]
    tables = ws.statement("select count(*) from system.information_schema.tables "
                          "where table_schema = 'warehouse'")
    assert tables["status"]["state"] == "SUCCEEDED" and int(
        tables["result"]["data_array"][0][0]) == 4     # the views were not dropped
    rows = [r for r in ws.rows() if r["tool"] == "databricks.statement"]
    assert [r["args"]["sql"] for r in rows][-2:] == [
        "select id from t", "select count(*) from system.information_schema.tables "
        "where table_schema = 'warehouse'"]
    assert rows[-2]["args"]["session_id"] == session


@pytest.mark.parametrize("method,path", [
    ("POST", SQL), ("POST", "/api/2.0/sql/sessions"), ("POST", "/api/2.1/jobs/run-now"),
    ("POST", "/telemetry-ext")])
def test_a_malformed_body_is_refused_with_exactly_one_row(ws, method, path):
    before = len(ws.rows())
    r = ws.call(method, path, content=b"{not json", headers={"Content-Type":
                                                               "application/json"})
    assert r.status_code == 400 and r.json()["error_code"] == "MALFORMED_REQUEST"
    rows = ws.rows()[before:]
    assert len(rows) == 1 and rows[0]["exit"] == "error", rows
    assert rows[0]["error"]["code"] == "MALFORMED_REQUEST"


def test_run_now_job_parameters_must_be_an_object(ws):
    before = len(ws.rows())
    r = ws.call("POST", "/api/2.1/jobs/run-now",
                json={"job_id": 1, "job_parameters": ["not", "an", "object"]})
    assert r.status_code == 400 and r.json()["error_code"] == "INVALID_PARAMETER_VALUE"
    rows = ws.rows()[before:]
    assert [(x["tool"], x["exit"]) for x in rows] == [("databricks.sched_trigger", "error")]
    assert rows[0]["args"]["job_parameters"] == ["not", "an", "object"]


@pytest.mark.parametrize("params", [
    [{"name": "n", "value": "abc", "type": "INT"}],
    [{"name": "d", "value": "2026-13-45", "type": "DATE"}],
    [{"name": "b", "value": "maybe", "type": "BOOLEAN"}],
    {"name": "n"}, 7])
def test_a_bad_parameter_is_a_failed_statement_not_a_500(ws, params):
    r = ws.statement("select :n", parameters=params)
    assert r["status"]["state"] == "FAILED"
    assert "INVALID_PARAMETER_VALUE" in r["status"]["error"]["message"]
    assert ws.rows()[-1]["exit"] == "error"


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


def _ledger(ws) -> dict:
    return json.loads((ws.root / "var" / "sched" / "state.json").read_text())


def _ms(vts: str) -> int:
    return int(datetime.fromisoformat(vts.replace("Z", "+00:00")).timestamp() * 1000)


def test_jobs_run_now_queues_without_advancing(ws, monkeypatch):
    """run-now QUEUES (WorldScheduler.enqueue): one entry under state.json's `queue`,
    `runs` and `vnow` untouched, no job's work run - `trigger`, `advance` and the
    executor are never reached. runs/get and runs/list (2.1 and 2.2) show it QUEUED
    under the id run-now returned, with no result_state; one exec row, sched_trigger."""
    from world import scheduler

    def never(*_a, **_k):
        raise AssertionError("run-now must neither run a job nor move the world's clock")

    for name in ("advance", "trigger", "_execute"):
        monkeypatch.setattr(scheduler, name, never)
    before = _ledger(ws)
    started = ws.ok("POST", "/api/2.1/jobs/run-now",
                    json={"job_id": 1, "job_parameters": {"day": "2026-03-04"}})
    run_id = databricks.RUN_NOW_IDS + 1
    assert started == {"run_id": run_id, "number_in_job": run_id}
    after = _ledger(ws)
    assert after["vnow"] == before["vnow"] and after["runs"] == before["runs"] == []
    assert after["queue"] == [{
        "job": "daily_load", "queued_at": before["vnow"],
        "execution_date": before["vnow"][:10], "variables": {"day": "2026-03-04"},
        "origin": {"service": "databricks", "run_id": run_id, "job_id": 1}}]
    for version in ("2.1", "2.2"):
        got = ws.ok("GET", f"/api/{version}/jobs/runs/get", params={"run_id": run_id})
        assert (got["job_id"], got["run_id"], got["run_name"]) == (1, run_id, "daily_load")
        assert got["state"]["life_cycle_state"] == "QUEUED"
        assert "result_state" not in got["state"]
        assert got["start_time"] == _ms(before["vnow"]) and got["end_time"] == 0
        assert got["queue_duration"] == 0 and got["trigger"] == "ONE_TIME"
        listed = ws.ok("GET", f"/api/{version}/jobs/runs/list", params={"job_id": 1})
        assert [r["run_id"] for r in listed["runs"]] == [run_id]
    active = ws.ok("GET", "/api/2.1/jobs/runs/list", params={"active_only": "true"})
    assert [r["run_id"] for r in active["runs"]] == [run_id]
    assert "runs" not in ws.ok("GET", "/api/2.1/jobs/runs/list",
                               params={"completed_only": "true"})
    assert _ledger(ws) == after                        # reading it changes nothing
    triggers = [r for r in ws.rows() if r["tool"] == "databricks.sched_trigger"]
    assert len(triggers) == 1 and triggers[0]["exit"] == "ok"
    assert triggers[0]["op_class"] == "exec"
    assert triggers[0]["targets"] == ["scheduler:daily_load", "dbt_project"]


def test_queued_run_resolves_after_advance(ws):
    """The grader's advance drains the queue first; the SAME run id then answers with
    the executed ledger run - TERMINATED and its result - even though a sched_trigger in
    between took the next ledger position (why run-now's ids are its own)."""
    from world import scheduler
    from world.gateway_scheduler import WorldScheduler

    run_id = ws.ok("POST", "/api/2.1/jobs/run-now", json={"job_id": 1})["run_id"]
    queued_at = _ledger(ws)["vnow"]
    WorldScheduler(ws.root).trigger("daily_load", queued_at[:10], {})  # ./ops/sched trigger
    scheduler.advance(ws.root, 1)
    ledger = _ledger(ws)
    assert "queue" not in ledger and len(ledger["runs"]) == 2
    manual, drained = ledger["runs"]
    assert "origin" not in manual
    assert drained["origin"] == {"service": "databricks", "run_id": run_id, "job_id": 1}
    assert drained["queued_at"] == queued_at and drained["vts_start"] == manual["vts_end"]
    got = ws.ok("GET", "/api/2.1/jobs/runs/get", params={"run_id": run_id})
    assert got["run_id"] == run_id and got["state"]["life_cycle_state"] == "TERMINATED"
    assert got["state"]["result_state"] == {"success": "SUCCESS",
                                            "failed": "FAILED"}[drained["status"]]
    assert (got["start_time"], got["end_time"]) == (_ms(drained["vts_start"]),
                                                    _ms(drained["vts_end"]))
    assert got["queue_duration"] == _ms(drained["vts_start"]) - _ms(queued_at) == 5000
    assert ws.ok("GET", "/api/2.2/jobs/runs/get", params={"run_id": 1})["run_id"] == 1
    # one id per run: the drained run is not also reachable at its ledger position
    r = ws.call("GET", "/api/2.1/jobs/runs/get", params={"run_id": 2})
    assert r.status_code == 400 and r.json()["error_code"] == "INVALID_PARAMETER_VALUE"
    listed = ws.ok("GET", "/api/2.1/jobs/runs/list")["runs"]
    assert sorted(r["run_id"] for r in listed) == [1, run_id]
    assert "runs" not in ws.ok("GET", "/api/2.1/jobs/runs/list", params={"active_only": "true"})
    # the next run-now gets the next id, never one already handed out
    assert ws.ok("POST", "/api/2.1/jobs/run-now", json={"job_id": 1})["run_id"] == run_id + 1


def test_run_now_queue_full_is_named(ws, monkeypatch):
    """Past world.scheduler.MAX_QUEUED a run-now is refused in Databricks' envelope,
    400 RESOURCE_EXHAUSTED naming the limit (not a 429, which the SDK retries for
    minutes), with exactly one row, and the ledger unchanged."""
    from world import scheduler

    monkeypatch.setattr(scheduler, "MAX_QUEUED", 2)
    for _ in range(2):
        ws.ok("POST", "/api/2.1/jobs/run-now", json={"job_id": 1})
    state_file = ws.root / "var" / "sched" / "state.json"
    before, rows = state_file.read_bytes(), len(ws.rows())
    r = ws.call("POST", "/api/2.2/jobs/run-now", json={"job_id": 1})
    assert r.status_code == 400 and r.json()["error_code"] == "RESOURCE_EXHAUSTED"
    assert "MAX_QUEUED" in r.json()["message"] and "2 runs" in r.json()["message"]
    assert state_file.read_bytes() == before
    new = ws.rows()[rows:]
    assert [(x["tool"], x["exit"], x["error"]["code"]) for x in new] == [
        ("databricks.sched_trigger", "error", "RESOURCE_EXHAUSTED")]


def test_blindness_attributes_the_queued_change_to_run_now(ws):
    """The queue entry is the one world change a run-now makes, and the harness's
    blindness detector attributes it to the `databricks.sched_trigger` row (suffix
    `sched_trigger`, blindness.SCHEDULER_TOOLS); with only the reads it is off-channel."""
    sys.path.insert(0, str(REPO / "tasks" / "harness"))
    import blindness

    def tree() -> dict:
        return {p.relative_to(ws.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(ws.root.rglob("*")) if p.is_file()}

    ws.ok("GET", "/api/2.1/jobs/list")
    before, rows = tree(), len(ws.rows())
    run_id = ws.ok("POST", "/api/2.1/jobs/run-now", json={"job_id": 1})["run_id"]
    ws.ok("GET", "/api/2.1/jobs/runs/get", params={"run_id": run_id})
    after, log = tree(), ws.rows()[rows:]
    assert sorted(k for k in set(before) | set(after)
                  if before.get(k) != after.get(k)) == ["var/sched/state.json"]
    assert [r["tool"] for r in log] == ["databricks.sched_trigger", "databricks.sched_state"]
    assert blindness.row_tools(log) & blindness.SCHEDULER_TOOLS == {"sched_trigger"}
    layout = {"world_root": str(ws.root), "workspace": str(ws.root / "workspace"),
              "warehouse": str(WAREHOUSE(ws)), "scheduler": str(ws.config.scheduler),
              "profiles_dir": str(ws.root / "build")}
    assert blindness._unattributed(before, after, log, layout) == []
    reads = [r for r in log if r["tool"] != "databricks.sched_trigger"]
    assert blindness._unattributed(before, after, reads, layout) == ["var/sched/state.json"]


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
