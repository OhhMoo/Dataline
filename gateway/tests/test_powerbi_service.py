"""The Power BI / Fabric fake: vendor shapes, import-mode truth, one row per request.

What is proved here, against a world whose warehouse is the Commerce fixture
and whose workspace holds the Commerce PBIP project under bi/powerbi/:

1. The documented REST shapes answer (Entra token, Power BI groups / datasets /
   refreshes / executeQueries / reports, Fabric workspaces / semantic models /
   getDefinition / updateDefinition), driven in-process and - once - by the
   `requests` library over a real socket, which is what a DE's script uses
   (there is no official Python SDK; BI_BUILD_PLAN.md).
2. The numbers are the warehouse's: executeQueries agrees with a hand-written
   SQL query over the same fixture and with world.semantic.render.
3. Import mode: the dataset answers from its last refresh, a refresh moves it
   to the warehouse's present, a measure-only deployment is live at once, a
   structural one waits for a refresh, and a local TMDL edit changes nothing.
4. Every request - refused credentials and refused DAX included - writes
   exactly one 9.2.2 row, on the virtual clock.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pytest
import uvicorn
from conftest import config
from fastapi.testclient import TestClient

from alkera_gateway.native import context as native_context
from alkera_gateway.native.services import powerbi

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "world"))

from world.semantic.examples import commerce_model, executive_report, write_fixture  # noqa: E402
from world.semantic.formats import powerbi as pbip  # noqa: E402
from world.semantic.formats.powerbi import daxquery, tmdl  # noqa: E402

TENANT = "7f3c2a10-5b8e-4d6f-9a1c-2e4b6d8f0a12"
CLIENT = "3b9d6e2f-0c4a-4f1e-8b7d-5a6c9e1f2d34"
SCOPE = "https://analysis.windows.net/powerbi/api/.default"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
GIT_ENV = {"GIT_AUTHOR_NAME": "Fixture", "GIT_AUTHOR_EMAIL": "fixture@alkera.local",
           "GIT_COMMITTER_NAME": "Fixture", "GIT_COMMITTER_EMAIL": "fixture@alkera.local",
           "GIT_AUTHOR_DATE": "2026-03-02T00:00:00Z", "GIT_COMMITTER_DATE": "2026-03-02T00:00:00Z"}
NET_REVENUE_SQL = ("SELECT SUM(order_amount) FROM wh.warehouse.fct_orders "
                   "WHERE order_status = 'completed'")


class Plane:
    def __init__(self, root: Path, settings, rows: Path):
        self.root, self.settings, self.rows_file = root, settings, rows
        self.client = TestClient(powerbi.app, base_url="http://powerbi.internal:8095")
        self._token = None

    @property
    def warehouse(self) -> Path:
        return self.root / "var" / "warehouse" / "warehouse.duckdb"

    def rows(self, tool: str | None = None) -> list[dict]:
        if not self.rows_file.exists():
            return []
        rows = [json.loads(line) for line in self.rows_file.read_text("utf-8").splitlines()]
        return [r for r in rows if tool is None or r["tool"] == f"powerbi.{tool}"]

    def token(self, scope: str = SCOPE) -> str:
        response = self.client.post(f"/{TENANT}/oauth2/v2.0/token", data={
            "grant_type": "client_credentials", "client_id": CLIENT,
            "client_secret": native_context.DEV_TOKEN, "scope": scope})
        assert response.status_code == 200, response.text
        return response.json()["access_token"]

    def auth(self) -> dict:
        if self._token is None:
            self._token = self.token()
        return {"Authorization": f"Bearer {self._token}"}

    def get(self, path: str, **kw):
        return self.client.get(path, headers=self.auth(), **kw)

    def post(self, path: str, **kw):
        return self.client.post(path, headers=self.auth(), **kw)

    def ok(self, response):
        assert response.status_code < 300, response.text
        return response.json() if response.content else None

    def group(self) -> str:
        return self.ok(self.get("/v1.0/myorg/groups"))["value"][0]["id"]

    def dataset(self) -> str:
        return self.ok(self.get(f"/v1.0/myorg/groups/{self.group()}/datasets"))["value"][0]["id"]

    def query(self, dax: str, **settings):
        body = {"queries": [{"query": dax}]}
        if settings:
            body["serializerSettings"] = settings
        return self.post(f"/v1.0/myorg/groups/{self.group()}/datasets/{self.dataset()}"
                         f"/executeQueries", json=body)

    def table(self, dax: str, **settings) -> list[dict]:
        return self.ok(self.query(dax, **settings))["results"][0]["tables"][0]["rows"]


def commit(workspace: Path, message: str) -> None:
    env = {**os.environ, **GIT_ENV}
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=workspace, env=env, check=True)


@pytest.fixture
def plane(world, tmp_path, monkeypatch):
    shutil.copy(write_fixture(tmp_path / "commerce.duckdb"),
                world / "var" / "warehouse" / "warehouse.duckdb")
    pbip.write_project(commerce_model("dax"), (executive_report(),),
                       world / "workspace" / "bi" / "powerbi")
    commit(world / "workspace", "Commerce semantic model and executive report")
    audit = tmp_path / "audit"
    settings = config(world, audit)
    rows = audit / "actions.jsonl"
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(rows))
    monkeypatch.setenv("ALKERA_SCHEDULER", str(settings.scheduler))
    native_context.set_context(powerbi.app, native_context.Context(settings))
    yield Plane(world, settings, rows)
    del powerbi.app.state.alkera_context


def warehouse_value(plane: Plane, sql: str):
    with duckdb.connect(":memory:") as con:
        con.execute(f"ATTACH '{plane.warehouse}' AS wh (READ_ONLY)")
        return con.execute(sql).fetchone()[0]


# --------------------------------------------------------------------------
# Registry and health
# --------------------------------------------------------------------------
def test_the_module_satisfies_the_registry_contract():
    from alkera_gateway.native.services import NAMES, load

    assert (powerbi.NAME, powerbi.HOST, powerbi.PORT) == ("powerbi", "powerbi.internal", 8095)
    others, _ = load([n for n in NAMES])
    assert powerbi.PORT not in {m.PORT for m in others.values()}


def test_health_counts_published_semantic_models(plane):
    body = plane.ok(plane.client.get("/health"))
    assert body == {"ok": True, "service": "powerbi", "items": 1}
    row = plane.rows("health")[0]
    assert row["targets"] == ["health:powerbi"]
    assert row["read_targets"] == ["powerbi:semantic_model:Commerce"]


def test_a_world_without_a_project_says_so(world, tmp_path, monkeypatch):
    settings = config(world, tmp_path / "audit")
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(tmp_path / "audit" / "actions.jsonl"))
    native_context.set_context(powerbi.app, native_context.Context(settings))
    try:
        response = TestClient(powerbi.app).get("/health")
    finally:
        del powerbi.app.state.alkera_context
    assert response.status_code == 503
    assert response.json() == {"ok": False, "service": "powerbi", "items": 0,
                               "error": "no_semantic_models"}


# --------------------------------------------------------------------------
# Entra ID
# --------------------------------------------------------------------------
def test_client_credentials_issue_a_bearer_token_on_the_virtual_clock(plane):
    response = plane.client.post(f"/{TENANT}/oauth2/v2.0/token", data={
        "grant_type": "client_credentials", "client_id": CLIENT,
        "client_secret": native_context.DEV_TOKEN, "scope": SCOPE})
    body = plane.ok(response)
    assert body["token_type"] == "Bearer" and body["expires_in"] == 3599
    claims = powerbi.verify(body["access_token"])
    assert claims["aud"] == "https://analysis.windows.net/powerbi/api"
    assert claims["tid"] == TENANT and claims["appid"] == CLIENT
    assert claims["iat"] == powerbi.epoch("2026-03-05T09:00:00Z")
    row = plane.rows("token")[0]
    assert row["targets"] == ["powerbi:token"] and row["exit"] == "ok"
    assert "client_secret" not in json.dumps(row["args"])
    assert row["vts"] == "2026-03-05T09:00:00Z"
    v1 = plane.ok(plane.client.post(f"/{TENANT}/oauth2/token", data={
        "grant_type": "client_credentials", "client_id": CLIENT,
        "client_secret": native_context.DEV_TOKEN,
        "resource": "https://analysis.windows.net/powerbi/api"}))
    assert v1["resource"] == "https://analysis.windows.net/powerbi/api"


@pytest.mark.parametrize("form, status, error, code", [
    ({"client_secret": "wrong"}, 401, "invalid_client", 7000215),
    ({"grant_type": "password"}, 400, "unsupported_grant_type", 70003),
    ({"scope": "https://analysis.windows.net/powerbi/api/Dataset.Read.All"}, 400,
     "invalid_scope", 1002012),
    ({"scope": "https://graph.microsoft.com/.default"}, 400, "invalid_resource", 500011),
])
def test_bad_credentials_are_refused_in_entra_shape(plane, form, status, error, code):
    data = {"grant_type": "client_credentials", "client_id": CLIENT,
            "client_secret": native_context.DEV_TOKEN, "scope": SCOPE, **form}
    response = plane.client.post(f"/{TENANT}/oauth2/v2.0/token", data=data)
    assert response.status_code == status
    body = response.json()
    assert body["error"] == error and body["error_codes"] == [code]
    assert body["error_description"].startswith(f"AADSTS{code}: ")
    row = plane.rows("token")[-1]
    assert row["exit"] == "error" and row["error"]["code"] == error


def test_api_calls_without_a_valid_token_are_refused_and_audited(plane):
    no_auth = plane.client.get("/v1.0/myorg/groups")
    assert no_auth.status_code == 401
    assert no_auth.json()["error"]["code"] == "PowerBINotAuthorizedException"
    assert no_auth.headers["WWW-Authenticate"].startswith("Bearer ")
    forged = plane.client.get("/v1/workspaces", headers={"Authorization": "Bearer abc.def.ghi"})
    assert forged.status_code == 401 and forged.json()["errorCode"] == "InvalidToken"
    missing = plane.client.get("/v1/workspaces")
    assert missing.json()["errorCode"] == "TokenNotProvided"
    rows = plane.rows()
    assert [r["tool"] for r in rows] == ["powerbi.groups_list", "powerbi.workspaces_list",
                                         "powerbi.workspaces_list"]
    assert all(r["exit"] == "error" and r["targets"] == [] for r in rows)


def test_an_expired_token_is_refused(plane):
    token = plane.token()
    state = json.loads(plane.settings.scheduler.read_text())
    state["vnow"] = "2026-03-05T10:00:00Z"
    plane.settings.scheduler.write_text(json.dumps(state))
    response = plane.client.get("/v1.0/myorg/groups",
                                headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TokenExpired"


# --------------------------------------------------------------------------
# Power BI REST: discovery
# --------------------------------------------------------------------------
def test_groups_datasets_and_reports(plane):
    groups = plane.ok(plane.get("/v1.0/myorg/groups"))
    assert groups["@odata.count"] == 1
    group = groups["value"][0]
    assert group["name"] == "Analytics" and group["type"] == "Workspace"
    assert group["isOnDedicatedCapacity"] is True
    filtered = plane.ok(plane.get("/v1.0/myorg/groups", params={"$filter": "name eq 'Nope'"}))
    assert filtered["value"] == []
    datasets = plane.ok(plane.get(f"/v1.0/myorg/groups/{group['id']}/datasets"))["value"]
    assert [d["name"] for d in datasets] == ["Commerce"]
    dataset = datasets[0]
    assert dataset["isRefreshable"] is True and dataset["targetStorageMode"] == "Abf"
    assert dataset["webUrl"] == (f"https://app.powerbi.com/groups/{group['id']}/datasets/"
                                 f"{dataset['id']}")
    one = plane.ok(plane.get(f"/v1.0/myorg/groups/{group['id']}/datasets/{dataset['id']}"))
    assert one["name"] == "Commerce" and one["@odata.context"].endswith("#datasets/$entity")
    reports = plane.ok(plane.get(f"/v1.0/myorg/groups/{group['id']}/reports"))["value"]
    assert [(r["name"], r["datasetId"], r["reportType"]) for r in reports] == [
        ("Executive Overview", dataset["id"], "PowerBIReport")]
    missing = plane.get(f"/v1.0/myorg/groups/{group['id']}/datasets/"
                        f"00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "ItemNotFound"
    wrong_group = plane.get("/v1.0/myorg/groups/00000000-0000-0000-0000-000000000000/datasets")
    assert wrong_group.status_code == 404
    assert wrong_group.json()["error"]["pbi.error"]["code"] == "PowerBIEntityNotFound"
    assert plane.rows("dataset_get")[0]["targets"] == ["powerbi:semantic_model:Commerce"]


# --------------------------------------------------------------------------
# Refresh and history
# --------------------------------------------------------------------------
def test_refresh_is_accepted_and_recorded_on_the_virtual_clock(plane):
    group, dataset = plane.group(), plane.dataset()
    base = f"/v1.0/myorg/groups/{group}/datasets/{dataset}/refreshes"
    history = plane.ok(plane.get(base))["value"]
    assert [(h["refreshType"], h["status"]) for h in history] == [("Scheduled", "Completed")]
    accepted = plane.post(base, json={"notifyOption": "NoNotification"})
    assert accepted.status_code == 202 and accepted.content == b""
    request_id = accepted.headers["x-ms-request-id"]
    assert accepted.headers["Location"].endswith(f"{base}/{request_id}")
    history = plane.ok(plane.get(base))["value"]
    assert [(h["refreshType"], h["status"]) for h in history] == [
        ("ViaApi", "Completed"), ("Scheduled", "Completed")]
    newest = history[0]
    assert newest["requestId"] == request_id
    assert newest["startTime"] == newest["endTime"] == "2026-03-05T09:00:00Z"
    assert [a["type"] for a in newest["refreshAttempts"]] == ["Data", "Query"]
    assert plane.ok(plane.get(base, params={"$top": "1"}))["value"] == [newest]
    detail = plane.ok(plane.get(f"{base}/{request_id}"))
    assert detail["status"] == "Completed" and detail["type"] == "Full"
    row = plane.rows("refresh")[0]
    assert row["op_class"] == "write"
    assert row["targets"] == ["powerbi:semantic_model:Commerce"]
    assert row["read_targets"] == ["warehouse.dim_customer", "warehouse.dim_date",
                                   "warehouse.dim_product", "warehouse.fct_order_lines",
                                   "warehouse.fct_orders"]
    enhanced = plane.post(base, json={"type": "Full", "commitMode": "transactional"})
    assert enhanced.status_code == 202
    assert plane.ok(plane.get(base))["value"][0]["refreshType"] == "ViaEnhancedApi"
    partial = plane.post(base, json={"type": "Full", "objects": [{"table": "Orders"}]})
    assert partial.status_code == 400 and "partial refresh" in partial.json()["error"]["message"]


def test_a_refresh_records_what_the_report_showed(plane):
    pytest.importorskip("world.semantic.render", reason="engine not yet available")
    group, dataset = plane.group(), plane.dataset()
    plane.post(f"/v1.0/myorg/groups/{group}/datasets/{dataset}/refreshes", json={})
    values = plane.rows("refresh")[0]["result"]["values"]["Executive Overview"]
    kpis = values["Overview"]["KPIs"]
    assert kpis["columns"] == ["Net Revenue", "Completed Orders", "AOV"]
    assert kpis["rows"][0][0] == str(warehouse_value(plane, NET_REVENUE_SQL))


def test_a_refresh_that_cannot_read_the_warehouse_fails_and_keeps_the_old_data(plane):
    pytest.importorskip("world.semantic.render", reason="engine not yet available")
    before = plane.table('EVALUATE ROW("Net", [Net Revenue])')
    with duckdb.connect(str(plane.warehouse)) as con:
        con.execute("ALTER TABLE warehouse.warehouse.fct_orders RENAME TO fct_orders_v2")
    group, dataset = plane.group(), plane.dataset()
    base = f"/v1.0/myorg/groups/{group}/datasets/{dataset}/refreshes"
    assert plane.post(base, json={}).status_code == 202
    newest = plane.ok(plane.get(base))["value"][0]
    assert newest["status"] == "Failed"
    assert "ModelRefresh_ShortMessage_ProcessingError" in newest["serviceExceptionJson"]
    assert plane.table('EVALUATE ROW("Net", [Net Revenue])') == before


# --------------------------------------------------------------------------
# executeQueries: the warehouse's numbers, import-mode truth
# --------------------------------------------------------------------------
def test_execute_queries_returns_the_warehouses_numbers(plane):
    pytest.importorskip("world.semantic.render", reason="engine not yet available")
    rows = plane.table('EVALUATE ROW("Net Revenue", [Net Revenue], "Orders", [Order Count])')
    total = warehouse_value(plane, NET_REVENUE_SQL)
    count = warehouse_value(plane, "SELECT COUNT(*) FROM wh.warehouse.fct_orders")
    assert rows == [{"[Net Revenue]": float(total), "[Orders]": count}]
    by_segment = plane.table(
        "EVALUATE SUMMARIZECOLUMNS ( Customer[segment], \"Net Revenue\", [Net Revenue] ) "
        "ORDER BY [Net Revenue] DESC")
    expected = dict(duckdb.connect(":memory:").execute(
        f"ATTACH '{plane.warehouse}' AS wh (READ_ONLY); "
        "SELECT c.segment, SUM(o.order_amount) FROM wh.warehouse.fct_orders o "
        "JOIN wh.warehouse.dim_customer c USING (customer_id) "
        "WHERE o.order_status = 'completed' GROUP BY 1").fetchall())
    assert {r["Customer[segment]"]: r["[Net Revenue]"] for r in by_segment} == {
        k: float(v) for k, v in expected.items()}
    assert [r["[Net Revenue]"] for r in by_segment] == sorted(
        (r["[Net Revenue]"] for r in by_segment), reverse=True)
    row = plane.rows("execute_queries")[-1]
    assert row["targets"] == row["read_targets"] == ["powerbi:semantic_model:Commerce"]
    assert row["op_class"] == "read"


def test_execute_queries_agrees_with_the_engine(plane):
    render = pytest.importorskip("world.semantic.render", reason="engine not yet available")
    text = ('EVALUATE SUMMARIZECOLUMNS ( \'Date\'[year], \'Date\'[month], '
            'TREATAS ( { 2026 }, \'Date\'[year] ), "Net Revenue", [Net Revenue], '
            '"Revenue LY", [Revenue LY], "YoY Growth", [YoY Growth] )')
    query = daxquery.parse_query(text, commerce_model("dax"))
    expected = daxquery.execute_queries_result(
        query, render.render_visual(query.model, query.visual, plane.warehouse))
    assert plane.ok(plane.query(text)) == expected
    first = expected["results"][0]["tables"][0]["rows"][0]
    assert set(first) == {"Date[year]", "Date[month]", "[Net Revenue]", "[Revenue LY]",
                          "[YoY Growth]"}


def test_include_nulls_and_query_errors(plane):
    pytest.importorskip("world.semantic.render", reason="engine not yet available")
    text = ('DEFINE MEASURE Orders[Nothing] = CALCULATE ( [Gross Revenue], '
            'Orders[order_status] = "lost" ) EVALUATE ROW ( "Nothing", [Nothing] )')
    assert plane.table(text) == [{}]
    # [Net Revenue]'s own CALCULATE overwrites the outer status filter, as in DAX.
    overwritten = plane.table('EVALUATE ROW ( "Net", CALCULATE ( [Net Revenue], '
                              'Orders[order_status] = "lost" ) )')
    assert overwritten == [{"[Net]": float(warehouse_value(plane, NET_REVENUE_SQL))}]
    assert plane.table(text, includeNulls=True) == [{"[Nothing]": None}]
    refused = plane.query("EVALUATE ROW(\"x\", CALCULATE([Net Revenue], "
                          "USERELATIONSHIP(Orders[order_date], 'Date'[date_day])))")
    assert refused.status_code == 400
    error = refused.json()["error"]
    assert error["code"] == "DatasetExecuteQueriesError"
    detail = error["pbi.error"]["details"][0]["detail"]["value"]
    assert detail == "unsupported (Power BI): USERELATIONSHIP"
    syntax = plane.query("EVALUATE ROW(\"x\", [Net Revenue]")
    assert syntax.status_code == 400
    assert "The syntax for" in syntax.json()["error"]["pbi.error"]["details"][0]["detail"]["value"]
    two = plane.post(f"/v1.0/myorg/groups/{plane.group()}/datasets/{plane.dataset()}"
                     f"/executeQueries",
                     json={"queries": [{"query": "EVALUATE ROW(\"a\", 1)"}] * 2})
    assert two.status_code == 400
    assert [r["exit"] for r in plane.rows("execute_queries")] == [
        "ok", "ok", "ok", "error", "error", "error"]


def test_the_dataset_answers_from_its_last_refresh(plane):
    pytest.importorskip("world.semantic.render", reason="engine not yet available")
    before = plane.table('EVALUATE ROW("Net", [Net Revenue])')[0]["[Net]"]
    with duckdb.connect(str(plane.warehouse)) as con:
        con.execute("UPDATE warehouse.warehouse.fct_orders SET order_amount = order_amount + 1 "
                    "WHERE order_status = 'completed'")
    assert plane.table('EVALUATE ROW("Net", [Net Revenue])')[0]["[Net]"] == before
    plane.post(f"/v1.0/myorg/groups/{plane.group()}/datasets/{plane.dataset()}/refreshes",
               json={})
    after = plane.table('EVALUATE ROW("Net", [Net Revenue])')[0]["[Net]"]
    assert after == float(warehouse_value(plane, NET_REVENUE_SQL)) != before


# --------------------------------------------------------------------------
# The world's import: a dataset provisioned from what the world says its last
# scheduled refresh loaded (<var/bi>/powerbi/datasets.json; world/world/
# bi_projects.py IMPORTS), not from the warehouse at the first request
# --------------------------------------------------------------------------
RELATIONS = ("warehouse.dim_customer", "warehouse.dim_date", "warehouse.dim_product",
             "warehouse.fct_order_lines", "warehouse.fct_orders")
LATE_DAY = "2026-02-28"


def stale_copy(plane: Plane, tmp_path: Path) -> Path:
    """The warehouse as a refresh saw it before LATE_DAY's orders landed."""
    stale = tmp_path / "stale.duckdb"
    shutil.copy(plane.warehouse, stale)
    with duckdb.connect(str(stale)) as con:
        for table in ("fct_order_lines", "fct_orders"):
            con.execute(f"DELETE FROM stale.warehouse.{table} "
                        f"WHERE order_date = DATE '{LATE_DAY}'")
    return stale


def write_world_import(plane: Plane, source: Path, relations=RELATIONS,
                       miscount: int = 0) -> dict:
    """What gen.bi_imports writes: each relation as CSV in canonical order, its
    declared column types and row count, and the scheduled refresh history."""
    folder = plane.root / "var" / "bi" / "powerbi"
    listed = []
    with duckdb.connect(":memory:") as con:
        con.execute(f"ATTACH '{source}' AS wh (READ_ONLY)")
        for relation in relations:
            schema, table = relation.split(".")
            columns = [list(c) for c in con.execute(
                "SELECT column_name, data_type FROM duckdb_columns() WHERE database_name = "
                "'wh' AND schema_name = ? AND table_name = ? ORDER BY column_index",
                [schema, table]).fetchall()]
            path = folder / "import" / "Commerce" / f"{relation}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            con.execute(f"COPY (SELECT * FROM wh.{schema}.{table} ORDER BY ALL) TO '{path}' "
                        f"(FORMAT csv, HEADER true, DELIMITER ',')")
            rows = con.execute(f"SELECT count(*) FROM wh.{schema}.{table}").fetchone()[0]
            listed.append({"relation": relation,
                           "path": path.relative_to(folder).as_posix(),
                           "columns": columns, "rows": rows + miscount})
    record = {"datasets": {"Commerce": {
        "refreshes": [{"refreshType": "Scheduled", "startTime": f"2026-03-0{d}T06:00:00Z",
                       "endTime": f"2026-03-0{d}T06:07:00Z", "status": "Completed"}
                      for d in (4, 5)],
        "import": {"refreshed": "2026-03-05T06:00:00Z", "relations": listed}}}}
    (folder / "datasets.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return record


def snapshot_rows(plane: Plane, relation: str) -> list:
    state = plane.settings.bi_state_dir / "powerbi"
    (snapshot,) = sorted((state / "snapshots").glob("*.duckdb"))
    with duckdb.connect(str(snapshot), read_only=True) as con:
        return con.execute(f"SELECT * FROM {relation} ORDER BY ALL").fetchall()


def test_a_world_import_is_what_the_dataset_answers_until_a_refresh(plane, tmp_path):
    pytest.importorskip("world.semantic.render", reason="engine not yet available")
    stale = stale_copy(plane, tmp_path)
    write_world_import(plane, stale)
    net = plane.table('EVALUATE ROW("Net", [Net Revenue])')[0]["[Net]"]   # provisions
    with duckdb.connect(":memory:") as con:
        con.execute(f"ATTACH '{stale}' AS wh (READ_ONLY)")
        want = con.execute(NET_REVENUE_SQL).fetchone()[0]
        for relation in RELATIONS:                       # the CSV round trip is exact
            assert snapshot_rows(plane, relation) == con.execute(
                f"SELECT * FROM wh.{relation} ORDER BY ALL").fetchall(), relation
    live = warehouse_value(plane, NET_REVENUE_SQL)
    assert net == float(want) and want != live
    base = f"/v1.0/myorg/groups/{plane.group()}/datasets/{plane.dataset()}/refreshes"
    history = plane.ok(plane.get(base))["value"]
    assert [(h["refreshType"], h["startTime"], h["status"]) for h in history] == [
        ("Scheduled", "2026-03-05T06:00:00Z", "Completed"),
        ("Scheduled", "2026-03-04T06:00:00Z", "Completed")]
    state = json.loads((plane.settings.bi_state_dir / "powerbi" / "state.json").read_text())
    (dataset,) = state["datasets"].values()
    kpis = dataset["refreshes"][-1]["values"]["Executive Overview"]["Overview"]["KPIs"]
    assert kpis["rows"][0][0] == str(want)              # what that refresh rendered
    assert "values" not in dataset["refreshes"][0]
    assert plane.post(base, json={}).status_code == 202
    assert plane.table('EVALUATE ROW("Net", [Net Revenue])')[0]["[Net]"] == float(live)
    assert plane.ok(plane.get(base))["value"][0]["refreshType"] == "ViaApi"


@pytest.mark.parametrize("relations, miscount, code", [
    (RELATIONS[:-1], 0, "powerbi_import_incomplete"),    # the model reads fct_orders too
    (RELATIONS, 1, "powerbi_import_unreadable"),         # a row count the CSV does not hold
])
def test_a_world_import_that_is_not_the_models_is_refused(plane, tmp_path, relations,
                                                          miscount, code):
    write_world_import(plane, stale_copy(plane, tmp_path), relations, miscount)
    response = plane.client.get("/health")
    assert response.status_code == 503
    assert response.json()["error"] == code


def test_without_a_world_import_the_warehouse_is_the_import(plane):
    pytest.importorskip("world.semantic.render", reason="engine not yet available")
    assert not (plane.root / "var" / "bi" / "powerbi").exists()
    net = plane.table('EVALUATE ROW("Net", [Net Revenue])')[0]["[Net]"]
    assert net == float(warehouse_value(plane, NET_REVENUE_SQL))


# --------------------------------------------------------------------------
# Fabric: the CI/CD deploy path
# --------------------------------------------------------------------------
def definition(plane: Plane) -> dict[str, bytes]:
    workspace = plane.ok(plane.get("/v1/workspaces"))["value"][0]["id"]
    model = plane.ok(plane.get(f"/v1/workspaces/{workspace}/semanticModels"))["value"][0]["id"]
    body = plane.ok(plane.post(f"/v1/workspaces/{workspace}/semanticModels/{model}"
                               f"/getDefinition"))
    return {p["path"]: base64.b64decode(p["payload"]) for p in body["definition"]["parts"]}


def deploy(plane: Plane, files: dict[str, bytes], **params):
    workspace = plane.ok(plane.get("/v1/workspaces"))["value"][0]["id"]
    model = plane.ok(plane.get(f"/v1/workspaces/{workspace}/semanticModels"))["value"][0]["id"]
    parts = [{"path": p, "payload": base64.b64encode(d).decode(), "payloadType": "InlineBase64"}
             for p, d in sorted(files.items())]
    return plane.post(f"/v1/workspaces/{workspace}/semanticModels/{model}/updateDefinition",
                      json={"definition": {"parts": parts}}, params=params)


def test_get_definition_returns_the_published_tmdl(plane):
    workspace = plane.ok(plane.get("/v1/workspaces"))["value"][0]
    assert workspace["displayName"] == "Analytics" and workspace["type"] == "Workspace"
    files = definition(plane)
    local = plane.root / "workspace/bi/powerbi/Commerce.SemanticModel"
    assert files == {p.relative_to(local).as_posix(): p.read_bytes()
                     for p in local.rglob("*") if p.is_file()}
    model, _ = tmdl.read_semantic_model(files, "Commerce")
    assert model.name == "Commerce"
    items = plane.ok(plane.get(f"/v1/workspaces/{workspace['id']}/semanticModels"))["value"]
    assert items[0]["type"] == "SemanticModel" and items[0]["displayName"] == "Commerce"
    tmsl = plane.post(f"/v1/workspaces/{workspace['id']}/semanticModels/{items[0]['id']}"
                      f"/getDefinition", params={"format": "TMSL"})
    assert tmsl.status_code == 400 and tmsl.json()["errorCode"] == "OperationNotSupportedForItem"


def test_update_definition_deploys_a_measure_change_at_once(plane):
    pytest.importorskip("world.semantic.render", reason="engine not yet available")
    files = definition(plane)
    orders = files["definition/tables/Orders.tmdl"].decode()
    fixed = orders.replace("Orders[order_status] = \"completed\" )\n\t\tformatString",
                           "Orders[order_status] IN { \"completed\", \"refunded\" } )\n"
                           "\t\tformatString", 1)
    assert fixed != orders
    response = deploy(plane, {**files, "definition/tables/Orders.tmdl": fixed.encode()})
    assert response.status_code == 200 and response.content == b""
    assert definition(plane)["definition/tables/Orders.tmdl"] == fixed.encode()
    expected = warehouse_value(plane, "SELECT SUM(order_amount) FROM wh.warehouse.fct_orders "
                                      "WHERE order_status IN ('completed', 'refunded')")
    assert plane.table('EVALUATE ROW("Net", [Net Revenue])') == [{"[Net]": float(expected)}]
    row = plane.rows("update_definition")[0]
    assert row["op_class"] == "write" and row["targets"] == ["powerbi:semantic_model:Commerce"]
    platform = json.loads(files[".platform"])
    platform["metadata"]["displayName"] = "Commerce (certified)"
    renamed = {**files, ".platform": json.dumps(platform).encode()}
    assert deploy(plane, renamed).status_code == 200
    assert plane.ok(plane.get(f"/v1.0/myorg/groups/{plane.group()}/datasets"))["value"][0][
        "name"] == "Commerce"
    assert deploy(plane, renamed, updateMetadata="True").status_code == 200
    assert plane.ok(plane.get(f"/v1.0/myorg/groups/{plane.group()}/datasets"))["value"][0][
        "name"] == "Commerce (certified)"


def test_a_structural_deployment_waits_for_a_refresh(plane):
    pytest.importorskip("world.semantic.render", reason="engine not yet available")
    files = definition(plane)
    customer = files["definition/tables/Customer.tmdl"].decode()
    renamed = customer.replace("sourceColumn: region", "sourceColumn: segment")
    assert deploy(plane, {**files, "definition/tables/Customer.tmdl": renamed.encode()}
                  ).status_code == 200
    stale = plane.query('EVALUATE ROW("Net", [Net Revenue])')
    assert stale.status_code == 400
    assert "has not been processed" in json.dumps(stale.json())
    plane.post(f"/v1.0/myorg/groups/{plane.group()}/datasets/{plane.dataset()}/refreshes",
               json={})
    assert plane.query('EVALUATE ROW("Net", [Net Revenue])').status_code == 200


@pytest.mark.parametrize("edit, message", [
    (lambda t: t.replace("measure 'Gross Revenue' = SUM ( Orders[order_amount] )",
                         "measure 'Gross Revenue' = SUM ( Orders[order_amount]"),
     "The syntax for"),
    (lambda t: t.replace("SUM ( Orders[order_amount] )",
                         "CALCULATE ( SUM ( Orders[order_amount] ), USERELATIONSHIP ( "
                         "Orders[order_date], 'Date'[date_day] ) )"),
     "unsupported (Power BI): USERELATIONSHIP"),
    (lambda t: t.replace("SUM ( Orders[order_amount] )", "SUM ( Orders[amount] )"),
     "Column 'amount' in table 'Orders' cannot be found"),
])
def test_update_definition_refuses_what_does_not_parse(plane, edit, message):
    files = definition(plane)
    orders = files["definition/tables/Orders.tmdl"].decode()
    response = deploy(plane, {**files, "definition/tables/Orders.tmdl": edit(orders).encode()})
    assert response.status_code == 400
    body = response.json()
    assert body["errorCode"] == "Workload_FailedToParseFile"
    assert message in body["message"]
    assert definition(plane)["definition/tables/Orders.tmdl"] == orders.encode()
    assert plane.rows("update_definition")[-1]["exit"] == "error"


def test_a_local_edit_is_not_published_until_deployed(plane):
    pytest.importorskip("world.semantic.render", reason="engine not yet available")
    before = plane.table('EVALUATE ROW("Net", [Net Revenue])')
    local = plane.root / "workspace/bi/powerbi/Commerce.SemanticModel/definition/tables"
    text = (local / "Orders.tmdl").read_text()
    (local / "Orders.tmdl").write_text(text.replace('= "completed"', '= "refunded"', 1))
    assert plane.table('EVALUATE ROW("Net", [Net Revenue])') == before


def test_published_state_lives_outside_the_world_tree(plane):
    plane.ok(plane.client.get("/health"))
    state = plane.settings.bi_state_dir / "powerbi"
    assert (state / "state.json").is_file()
    assert not state.is_relative_to(plane.root)
    assert len(list((state / "snapshots").glob("*.duckdb"))) == 1


# --------------------------------------------------------------------------
# The action log
# --------------------------------------------------------------------------
def test_every_request_writes_exactly_one_row(plane):
    plane.client.get("/health")
    token = plane.token()
    headers = {"Authorization": f"Bearer {token}"}
    group = plane.client.get("/v1.0/myorg/groups", headers=headers).json()["value"][0]["id"]
    dataset = plane.client.get(f"/v1.0/myorg/groups/{group}/datasets",
                               headers=headers).json()["value"][0]["id"]
    plane.client.get(f"/v1.0/myorg/groups/{group}/datasets/{dataset}/refreshes",
                     headers=headers)
    plane.client.get(f"/v1.0/myorg/groups/{group}/reports", headers=headers)
    plane.client.get("/v1/workspaces", headers=headers)
    plane.client.get("/v1.0/myorg/groups")                     # refused
    rows = plane.rows()
    assert [r["tool"] for r in rows] == [
        "powerbi.health", "powerbi.token", "powerbi.groups_list", "powerbi.datasets_list",
        "powerbi.refreshes_list", "powerbi.reports_list", "powerbi.workspaces_list",
        "powerbi.groups_list"]
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))
    assert all(r["transport"] == "native" for r in rows)
    assert all(f"actor:{CLIENT}" in r["flags"] for r in rows[1:-1])
    assert rows[2]["args"]["path"] == "/v1.0/myorg/groups"


def test_a_read_row_names_what_it_read(plane):
    """Regression, measured 2026-10-04 by tasks/harness/bi_e2e_check.py: every
    Power BI read but executeQueries wrote `read_targets: []` (the Ledger writes
    the observation verbatim, and these handlers only set `targets`), while the
    other services' reads name the object they read. A read row's
    `read_targets` is now what it targeted; a write's are left to the call."""
    group = plane.group()
    dataset = plane.dataset()
    plane.get(f"/v1.0/myorg/groups/{group}/reports")
    plane.get(f"/v1.0/myorg/groups/{group}/datasets/{dataset}/refreshes")
    plane.post(f"/v1/workspaces/{group}/semanticModels/{dataset}/getDefinition")
    plane.get("/v1.0/myorg/groups/00000000-0000-0000-0000-000000000000/datasets")  # 404
    rows = [r for r in plane.rows() if r["tool"] != "powerbi.health"]
    assert [r["tool"].split(".", 1)[1] for r in rows] == [
        "token", "groups_list", "groups_list", "datasets_list", "reports_list",
        "refreshes_list", "get_definition", "datasets_list"]
    for row in rows:
        assert row["op_class"] == "read"
        assert row["targets"], row["tool"]
        assert row["read_targets"] == row["targets"], row["tool"]
    assert rows[0]["read_targets"] == ["powerbi:token"]
    assert rows[-1]["exit"] == "error"
    # A write still reports what IT read (a refresh: the warehouse relations).
    plane.post(f"/v1.0/myorg/groups/{group}/datasets/{dataset}/refreshes", json={})
    refresh = plane.rows("refresh")[-1]
    assert refresh["op_class"] == "write"
    assert refresh["targets"] == ["powerbi:semantic_model:Commerce"]
    assert refresh["read_targets"] and all(t.startswith("warehouse.")
                                           for t in refresh["read_targets"])


# --------------------------------------------------------------------------
# A real client over a real socket
# --------------------------------------------------------------------------
@contextmanager
def serving(app):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def test_requests_drives_the_service_like_a_deployment_script(plane):
    requests = pytest.importorskip("requests")
    pytest.importorskip("world.semantic.render", reason="engine not yet available")
    with serving(powerbi.app) as base:
        token = requests.post(f"{base}/{TENANT}/oauth2/v2.0/token", data={
            "grant_type": "client_credentials", "client_id": CLIENT,
            "client_secret": native_context.DEV_TOKEN, "scope": FABRIC_SCOPE},
            timeout=10).json()["access_token"]
        session = requests.Session()
        session.headers["Authorization"] = f"Bearer {token}"
        group = session.get(f"{base}/v1.0/myorg/groups",
                            params={"$filter": "name eq 'Analytics'"}, timeout=10
                            ).json()["value"][0]["id"]
        dataset = session.get(f"{base}/v1.0/myorg/groups/{group}/datasets",
                              timeout=10).json()["value"][0]["id"]
        refresh = session.post(f"{base}/v1.0/myorg/groups/{group}/datasets/{dataset}/refreshes",
                               json={"notifyOption": "NoNotification"}, timeout=30)
        assert refresh.status_code == 202
        status = session.get(refresh.headers["Location"], timeout=10).json()["status"]
        assert status == "Completed"
        result = session.post(
            f"{base}/v1.0/myorg/groups/{group}/datasets/{dataset}/executeQueries",
            json={"queries": [{"query": 'EVALUATE ROW("Net", [Net Revenue])'}]}, timeout=30)
        assert result.status_code == 200
        assert result.headers["RequestId"]
        net = result.json()["results"][0]["tables"][0]["rows"][0]["[Net]"]
        assert net == float(warehouse_value(plane, NET_REVENUE_SQL))
