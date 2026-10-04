"""The fake Sigma REST API v2 (native/services/sigma.py).

Claims, each tested against the documented shape (Sigma Computing Public REST
API 2.0.0+20260930) rather than against the service's own output:

- AUTH. `POST /v2/auth/token` is form-encoded client credentials (HTTP Basic or
  form fields), the secret is the workspace's obviously-fake token, and every
  other route answers 401 `{message, code, requestId}` without the bearer.
- PAGINATION. `{entries, nextPage, total, hasMore}`; `nextPage` fed back as
  `page` walks the list; the last page says `nextPage: null`.
- SPEC IN, SPEC OUT. `GET ?includeContents=true` returns the repo's file;
  `PUT .../contents` takes that GET body unchanged; a representation the
  semantic core refuses is a 400 naming the construct and publishes nothing.
- EXPORTS AGREE WITH THE WAREHOUSE. Every element's CSV is what
  `world.semantic.render` computes from the published documents on the
  world's warehouse - and a published fix moves it.
- ONE ROW PER REQUEST, refusals and unknown routes included; Sigma's state
  lives outside the world tree, so serving never changes the world.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import sys
from pathlib import Path

import duckdb
import pytest
import yaml
from conftest import make_world, world_hash
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "world"))

from world.semantic import examples as core  # noqa: E402
from world.semantic.expr import Col, MeasureRef  # noqa: E402
from world.semantic.formats import sigma as sigma_format  # noqa: E402
from world.semantic.formats.sigma import examples as sx  # noqa: E402
from world.semantic.model import Page, Report, Visual  # noqa: E402

from alkera_gateway import GatewayConfig  # noqa: E402
from alkera_gateway.native import context as native_context  # noqa: E402
from alkera_gateway.native.services import sigma  # noqa: E402

try:
    from world.semantic import render
except ImportError:  # pragma: no cover - the engine is written concurrently
    render = None
needs_engine = pytest.mark.skipif(render is None, reason="world.semantic.render not present")

SECRET = native_context.DEV_TOKEN
OPS_REVIEW = Report("Ops Review", "Commerce", (Page("Orders", (
    Visual("Orders by Region", group_by=(Col("Customer", "region"),),
           values=(("Orders", MeasureRef("Order Count")),), kind="table", groups="rows"),)),))


class Sigma:
    """One world served by the Sigma app, with its audit rows."""

    def __init__(self, root: Path, audit: Path, audit_file: Path, warehouse: Path):
        self.root, self.audit_file, self.warehouse = root, audit_file, warehouse
        self.config = GatewayConfig(
            workspace=root / "workspace", warehouse=warehouse,
            scheduler=root / "var" / "sched" / "state.json",
            ingest=root / "var" / "ingest" / "state.json",
            dashboards=root / "var" / "bi" / "dashboards", audit_dir=audit,
            profiles_dir=root / "build", episode_id=audit.name, initial_world_fp="fixture",
            observability=root / "var" / "obs" / "observability.yml",
            workflow_dir=root / "var" / "workflow")
        native_context.set_context(sigma.app, native_context.Context(self.config))
        self.client = TestClient(sigma.app, base_url="http://sigma.internal")
        self.token = None

    def login(self) -> str:
        basic = base64.b64encode(f"ci-client:{SECRET}".encode()).decode()
        r = self.client.post("/v2/auth/token", data={"grant_type": "client_credentials"},
                             headers={"Authorization": f"Basic {basic}"})
        assert r.status_code == 200, r.text
        self.token = r.json()["access_token"]
        return self.token

    def call(self, method: str, path: str, **kw):
        headers = kw.pop("headers", {})
        if self.token:
            headers.setdefault("Authorization", f"Bearer {self.token}")
        return self.client.request(method, path, headers=headers, **kw)

    def ok(self, method: str, path: str, **kw):
        r = self.call(method, path, **kw)
        assert r.status_code == 200, r.text
        return r.json()

    def rows(self) -> list[dict]:
        if not self.audit_file.exists():
            return []
        return [json.loads(line) for line in self.audit_file.read_text("utf-8").splitlines()]


@pytest.fixture
def served(tmp_path, monkeypatch):
    root = make_world(tmp_path / "world")
    warehouse = core.write_fixture(root / "var" / "warehouse" / "commerce.duckdb")
    sigma_format.write_project(sx.commerce_model(), (sx.executive_report(), OPS_REVIEW),
                               root / "workspace" / "bi" / "sigma")
    audit_file = tmp_path / "native-actions.jsonl"
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(audit_file))
    s = Sigma(root, tmp_path / "audit", audit_file, warehouse)
    s.login()
    return s


def repo_file(s: Sigma, kind: str, name: str) -> dict:
    return json.loads((s.root / "workspace" / "bi" / "sigma" / kind / f"{name}.json")
                      .read_text("utf-8"))


def workbook_id(s: Sigma, name: str) -> str:
    return repo_file(s, "workbooks", name)["workbookId"]


def data_model(s: Sigma) -> dict:
    return repo_file(s, "data-models", "commerce")


# ----------------------------------------------------------------- contract
def test_the_module_satisfies_the_registry_contract():
    from alkera_gateway.native.services import (
        airbyte,
        alertmanager,
        confluence,
        deploy,
        jira,
        loki,
        metabase,
        prometheus,
        slack,
    )

    assert (sigma.NAME, sigma.HOST, sigma.PORT) == ("sigma", "sigma.internal", 8097)
    ports = {m.PORT for m in (airbyte, alertmanager, confluence, deploy, jira, loki, metabase,
                              prometheus, slack)}
    assert sigma.PORT not in ports


def test_health_counts_the_published_documents(served):
    body = served.client.get("/health").json()
    assert body == {"ok": True, "service": "sigma", "items": 3}   # 1 data model, 2 workbooks
    row = served.rows()[-1]
    assert row["tool"] == "sigma.health" and row["targets"] == ["health:sigma"]
    assert row["read_targets"]


# --------------------------------------------------------------------- auth
def test_every_route_but_the_token_needs_the_bearer(served):
    for path in ("/v2/whoami", "/v2/workbooks", "/v2/dataModels", "/v2/connections"):
        r = served.client.get(path)
        assert r.status_code == 401
        assert set(r.json()) == {"message", "code", "requestId"}
        assert r.json()["code"] == "unauthorized"
    r = served.client.get("/v2/workbooks", headers={"Authorization": "Bearer forged"})
    assert r.status_code == 401
    assert served.rows()[-1]["exit"] == "error"


def test_the_token_endpoint_speaks_client_credentials(served):
    form = {"grant_type": "client_credentials", "client_id": "ci", "client_secret": SECRET}
    r = served.client.post("/v2/auth/token", data=form)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"access_token", "refresh_token", "token_type", "expires_in"}
    assert body["token_type"] == "bearer" and body["expires_in"] == 3599
    me = served.client.get("/v2/whoami",
                           headers={"Authorization": f"Bearer {body['access_token']}"}).json()
    assert set(me) == {"userId", "organizationId"}
    again = served.client.post("/v2/auth/token", data={"grant_type": "refresh_token",
                                                       "refresh_token": body["refresh_token"]})
    assert again.status_code == 200 and again.json()["access_token"] != body["access_token"]
    assert served.client.post("/v2/auth/token", data={**form, "client_secret": "nope"}
                              ).status_code == 401
    assert served.client.post("/v2/auth/token", json=form).status_code == 400
    assert served.client.post("/v2/auth/token", data={"grant_type": "password"}
                              ).status_code == 400
    assert "nope" not in json.dumps(served.rows())               # secrets never logged


# --------------------------------------------------------------- pagination
def test_lists_page_with_next_page(served):
    first = served.ok("GET", "/v2/workbooks", params={"limit": 1})
    assert set(first) == {"entries", "nextPage", "total", "hasMore"}
    assert first["total"] == 2 and first["hasMore"] and first["nextPage"] == "1"
    second = served.ok("GET", "/v2/workbooks", params={"limit": 1, "page": first["nextPage"]})
    assert second["nextPage"] is None and not second["hasMore"]
    names = [first["entries"][0]["name"], second["entries"][0]["name"]]
    assert names == ["Executive Overview", "Ops Review"]
    entry = first["entries"][0]
    assert {"workbookId", "workbookUrlId", "name", "url", "path", "latestVersion",
            "ownerId", "createdBy", "updatedBy", "createdAt", "updatedAt"} <= set(entry)
    assert "contents" not in entry
    for bad in ({"limit": 0}, {"limit": 1001}, {"page": "x"}):
        assert served.call("GET", "/v2/workbooks", params=bad).status_code == 400


# ------------------------------------------------------------- spec in, out
def test_the_workbook_representation_is_the_repo_file(served):
    wid = workbook_id(served, "executive-overview")
    full = served.ok("GET", f"/v2/workbooks/{wid}", params={"includeContents": "true"})
    assert full == repo_file(served, "workbooks", "executive-overview")
    meta = served.ok("GET", f"/v2/workbooks/{wid}")
    assert "contents" not in meta and meta["workbookId"] == wid
    as_yaml = served.call("GET", f"/v2/workbooks/{wid}",
                          params={"includeContents": "true", "format": "yaml"})
    assert as_yaml.headers["content-type"].startswith("application/yaml")
    assert yaml.safe_load(as_yaml.text) == full
    spec = served.ok("GET", f"/v2/workbooks/{wid}/spec")             # deprecated spelling
    assert spec["document"] == full["contents"]
    pages = served.ok("GET", f"/v2/workbooks/{wid}/pages")["entries"]
    assert [p["name"] for p in pages] == ["Overview", "Regions"]
    elements = served.ok("GET", f"/v2/workbooks/{wid}/pages/{pages[0]['pageId']}/elements")
    assert [e["name"] for e in elements["entries"]] == ["KPIs", "Revenue by Month",
                                                        "Revenue by Segment", "Top Products"]
    bar = elements["entries"][2]
    assert (bar["type"], bar["vizualizationType"]) == ("visualization", "bar")
    assert served.call("GET", "/v2/workbooks/nope").status_code == 404


def test_a_get_response_is_accepted_unchanged_and_versions_move(served):
    wid = workbook_id(served, "executive-overview")
    full = served.ok("GET", f"/v2/workbooks/{wid}", params={"includeContents": "true"})
    meta = served.ok("PUT", f"/v2/workbooks/{wid}/contents", json=full)
    assert meta["latestVersion"] == 2
    assert served.call("PUT", f"/v2/workbooks/{wid}/contents",
                       json={**full, "documentVersion": 1}).status_code == 409
    history = served.ok("GET", f"/v2/workbooks/{wid}/version-history")["entries"]
    assert [h["version"] for h in history] == [2, 1]
    old = served.ok("GET", f"/v2/workbooks/{wid}",
                    params={"includeContents": "true", "documentVersion": 1})
    assert old["latestVersion"] == 1


def test_a_workbook_sigma_cannot_compute_is_refused_and_not_published(served):
    wid = workbook_id(served, "executive-overview")
    full = served.ok("GET", f"/v2/workbooks/{wid}", params={"includeContents": "true"})
    chart = next(e for e in full["contents"]["elements"] if e["name"] == "Revenue by Segment")
    chart["trellis"] = {"columnsBy": [{"columnId": chart["xAxis"]["columnId"]}]}
    r = served.call("PUT", f"/v2/workbooks/{wid}/contents", json=full)
    assert r.status_code == 400 and "trellis" in r.json()["message"]
    assert served.rows()[-1]["exit"] == "error"
    assert served.ok("GET", f"/v2/workbooks/{wid}")["latestVersion"] == 1
    assert served.call("PUT", f"/v2/workbooks/{wid}/contents", json={}).status_code == 400


def test_a_dry_run_validates_without_creating(served):
    full = repo_file(served, "workbooks", "ops-review")
    body = {"name": "Ops Copy", "folderId": "f", "contents": full["contents"], "dryRun": True}
    ok = served.ok("POST", "/v2/workbooks", json=body)
    assert ok["valid"] is True and ok["workbookId"] is None
    bad = json.loads(json.dumps(full["contents"]))
    bad["elements"][0]["groupings"][0]["calculations"] = []
    r = served.call("POST", "/v2/workbooks", json={**body, "contents": bad})
    assert r.status_code == 400
    assert r.json()["valid"] is False and r.json()["errors"][0]["summary"]
    assert served.ok("GET", "/v2/workbooks")["total"] == 2
    created = served.ok("POST", "/v2/workbooks", json={**body, "dryRun": False})
    assert served.ok("GET", "/v2/workbooks")["total"] == 3 and created["name"] == "Ops Copy"


def test_the_data_model_spec_round_trips_and_refuses_what_sigma_cannot_say(served):
    dm = data_model(served)
    dmid = dm["dataModelId"]
    assert served.ok("GET", f"/v2/dataModels/{dmid}/spec") == dm
    listed = served.ok("GET", "/v2/dataModels")["entries"]
    assert [e["dataModelId"] for e in listed] == [dmid]
    elements = served.ok("GET", f"/v2/dataModels/{dmid}/elements")["entries"]
    assert [e["name"] for e in elements] == ["Orders", "Order Lines", "Customer", "Product",
                                             "Date"]
    broken = json.loads(json.dumps(dm))
    orders = broken["pages"][0]["elements"][0]
    orders["metrics"][0]["formula"] = "CumulativeSum([order_amount])"
    r = served.call("PUT", f"/v2/dataModels/{dmid}/spec", json=broken)
    assert r.status_code == 400 and "CumulativeSum" in r.json()["message"]
    assert served.ok("GET", f"/v2/dataModels/{dmid}/spec") == dm   # nothing published
    assert served.ok("PUT", f"/v2/dataModels/{dmid}/spec", json=dm) == \
        {"success": True, "dataModelId": dmid}
    assert served.ok("GET", f"/v2/dataModels/{dmid}/spec")["documentVersion"] == 2
    # YAML both ways, as the code-representation endpoints accept it
    as_yaml = served.call("GET", f"/v2/dataModels/{dmid}/spec",
                          headers={"Accept": "application/yaml"}).text
    r = served.call("PUT", f"/v2/dataModels/{dmid}/spec", content=as_yaml,
                    headers={"Content-Type": "application/yaml"})
    assert r.status_code == 200, r.text
    assert served.ok("GET", f"/v2/dataModels/{dmid}/spec")["documentVersion"] == 3


# -------------------------------------------------------------------- export
def _export(s: Sigma, wid: str, element_id: str, kind: str = "csv"):
    job = s.ok("POST", f"/v2/workbooks/{wid}/export",
               json={"elementId": element_id, "format": {"type": kind}})
    assert set(job) == {"jobComplete", "queryId"} and job["jobComplete"] is True
    r = s.call("GET", f"/v2/query/{job['queryId']}/download")
    assert r.status_code == 200
    return r


def _csv(rendered: dict) -> list[list[str]]:
    rows = [rendered["columns"]] + [["" if v is None else str(v) for v in row]
                                    for row in rendered["rows"]]
    return rows


@needs_engine
def test_every_export_is_what_the_engine_computes_on_the_warehouse(served):
    wid = workbook_id(served, "executive-overview")
    contents = repo_file(served, "workbooks", "executive-overview")["contents"]
    model, reports = sigma_format.read_project(served.root / "workspace" / "bi" / "sigma")
    assert [r.name for r in reports] == ["Executive Overview", "Ops Review"]
    visuals = {v.name: v for p in reports[0].pages for v in p.visuals}
    for el in contents["elements"]:
        got = list(csv.reader(io.StringIO(_export(served, wid, el["id"]).text)))
        want = render.render_visual(model, visuals[el["name"]], served.warehouse)
        assert got == _csv(want), el["name"]
    row = served.rows()[-2]
    assert row["tool"] == "sigma.workbook_export"
    assert "warehouse.fct_orders" in row["read_targets"]


@needs_engine
def test_a_published_fix_moves_the_export(served):
    wid = workbook_id(served, "executive-overview")
    kpis = next(e for e in repo_file(served, "workbooks", "executive-overview")["contents"]
                ["elements"] if e["name"] == "KPIs")
    before = list(csv.reader(io.StringIO(_export(served, wid, kpis["id"]).text)))
    with duckdb.connect() as con:
        con.execute(f"ATTACH '{served.warehouse}' AS wh (READ_ONLY)")
        gross, completed = con.execute(
            "SELECT sum(order_amount), sum(order_amount) FILTER (order_status = 'completed') "
            "FROM wh.warehouse.fct_orders").fetchone()
    assert before[1][0] == f"{completed:.2f}"
    dm = data_model(served)
    net = next(m for m in dm["pages"][0]["elements"][0]["metrics"] if m["name"] == "Net Revenue")
    net["formula"] = "Sum([order_amount])"                       # drop the condition
    served.ok("PUT", f"/v2/dataModels/{dm['dataModelId']}/spec", json=dm)
    after = list(csv.reader(io.StringIO(_export(served, wid, kpis["id"]).text)))
    assert after[1][0] == f"{gross:.2f}"


@needs_engine
def test_export_formats_and_slicing(served):
    wid = workbook_id(served, "executive-overview")
    top = next(e for e in repo_file(served, "workbooks", "executive-overview")["contents"]
               ["elements"] if e["name"] == "Top Products")
    lines = _export(served, wid, top["id"], "jsonl").text.splitlines()
    assert len(lines) == 3 and set(json.loads(lines[0])) == {"product_name", "Units"}
    job = served.ok("POST", f"/v2/workbooks/{wid}/export",
                    json={"elementId": top["id"], "format": {"type": "csv"}, "rowLimit": 1,
                          "offset": 1})
    sliced = served.call("GET", f"/v2/query/{job['queryId']}/download").text.splitlines()
    assert len(sliced) == 2
    assert served.call("POST", f"/v2/workbooks/{wid}/export",
                       json={"elementId": top["id"], "format": {"type": "pdf",
                                                                "layout": "portrait"}}
                       ).status_code == 400
    assert served.call("POST", f"/v2/workbooks/{wid}/export",
                       json={"elementId": "missing", "format": {"type": "csv"}}
                       ).status_code == 404
    assert served.call("GET", "/v2/query/unknown/download").status_code == 404


# --------------------------------------------------------------- connections
def test_a_new_warehouse_column_is_unknown_until_the_connection_syncs(served):
    conns = served.ok("GET", "/v2/connections")
    (conn,) = conns["entries"]
    assert conn["type"] == "snowflake"
    assert served.ok("GET", f"/v2/connections/{conn['connectionId']}") == conn
    with duckdb.connect(str(served.warehouse)) as con:
        con.execute("ALTER TABLE warehouse.fct_orders ADD COLUMN discount_amount DECIMAL(12,2)")
    dm = data_model(served)
    orders = dm["pages"][0]["elements"][0]
    column_id = orders["columns"][0]["id"].split("/")[0] + "/DISCOUNT_AMOUNT"
    orders["columns"].append({"id": column_id, "formula": "[FCT_ORDERS/Discount Amount]"})
    orders["order"].append(column_id)
    r = served.call("PUT", f"/v2/dataModels/{dm['dataModelId']}/spec", json=dm)
    assert r.status_code == 400 and "sync the connection" in r.json()["message"]
    assert served.ok("POST", f"/v2/connections/{conn['connectionId']}/sync",
                     json={"path": ["ANALYTICS", "WAREHOUSE", "FCT_ORDERS"]}) == {}
    sync_row = served.rows()[-1]                     # the sync rewrites Sigma's catalog
    assert sync_row["op_class"] == "write"
    assert sync_row["targets"] == [f"sigma:connection:{conn['connectionId']}"]
    served.ok("PUT", f"/v2/dataModels/{dm['dataModelId']}/spec", json=dm)
    assert served.call("POST", f"/v2/connections/{conn['connectionId']}/sync",
                       json={"path": ["ANALYTICS", "WAREHOUSE", "NOPE"]}).status_code == 404


# ------------------------------------------------------------ audit and state
def test_one_row_per_request_including_refusals(served):
    wid = workbook_id(served, "executive-overview")
    requests = [("GET", "/v2/whoami"), ("GET", "/v2/workbooks"), ("GET", f"/v2/workbooks/{wid}"),
                ("GET", "/v2/workbooks/missing"), ("GET", "/v2/members"),
                ("GET", "/v2/dataModels"), ("POST", "/v2/teams")]
    for method, path in requests:
        before = len(served.rows())
        served.call(method, path)
        rows = served.rows()
        assert len(rows) == before + 1, path
        assert rows[-1]["tool"].startswith("sigma.")
        assert rows[-1]["args"]["path"] == path
    assert served.rows()[-1]["tool"] == "sigma.unknown_route"
    assert served.call("GET", "/v2/members").json()["code"] == "not_found"
    dm = data_model(served)
    served.ok("PUT", f"/v2/dataModels/{dm['dataModelId']}/spec", json=dm)
    write = served.rows()[-1]
    assert (write["tool"], write["op_class"], write["targets"]) == (
        "sigma.data_model_update", "write", [f"sigma:data-model:{dm['dataModelId']}"])
    assert all(row["schema_version"] == 1 for row in served.rows())
    assert any("actor:ci-client" in row["flags"] for row in served.rows())


def test_published_state_lives_outside_the_world_and_survives_a_restart(served):
    before = world_hash(served.root)
    dm = data_model(served)
    served.ok("PUT", f"/v2/dataModels/{dm['dataModelId']}/spec", json=dm)
    assert world_hash(served.root) == before
    state = served.config.bi_state_dir / "sigma" / "state.json"
    assert state.exists() and not state.is_relative_to(served.root)
    restarted = Sigma(served.root, served.config.audit_dir, served.audit_file, served.warehouse)
    restarted.token = served.token
    spec = restarted.ok("GET", f"/v2/dataModels/{dm['dataModelId']}/spec")
    assert spec["documentVersion"] == 2
