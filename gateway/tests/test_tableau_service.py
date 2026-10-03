"""The fake Tableau Server: REST shapes, published-vs-workspace truth, extracts, audit.

THREE CLAIMS.

1. SHAPES. Every endpoint answers in the Tableau REST API's XML (tsResponse,
   namespace http://tableau.com/api, `<error code=..>` refusals with Tableau's
   codes), checked over FastAPI's TestClient - and, when the real clients are
   importable, driven end to end by `tableauserverclient` in-process and by
   `tabcmd` as a subprocess against the app served by uvicorn on a loopback
   port (skipped otherwise; run them with
   `uv run --with tableauserverclient --with tabcmd pytest tests/test_tableau_service.py -q`).
2. THE NUMBERS ARE THE PUBLISHED DEFINITION ON THE WAREHOUSE. A view's CSV is the
   semantic engine's answer for the PUBLISHED workbook: it agrees with SQL on the
   warehouse, changes when a different definition is published, and does not
   change when the workspace copy is edited without publishing.
3. EXTRACTS GO STALE. An extract-enabled data source serves the copy taken at
   its last refresh; a live one refuses a refresh (403104) as Tableau does.

Every request writes exactly one 9.2.2 row (failures included).
"""
from __future__ import annotations

import csv
import io
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from conftest import config
from fastapi.testclient import TestClient

WORLD = Path(__file__).resolve().parents[2] / "world"
if str(WORLD) not in sys.path:
    sys.path.insert(0, str(WORLD))

from world.semantic import examples  # noqa: E402
from world.semantic.formats import tableau as tformat  # noqa: E402
from world.semantic.formats.tableau import tds as ttds  # noqa: E402
from world.semantic.formats.tableau import twb as ttwb  # noqa: E402
from world.semantic.formats.tableau import xmlio  # noqa: E402
from world.semantic.formats.tableau.examples import (  # noqa: E402
    tableau_commerce_model,
    tableau_executive_report,
)

from alkera_gateway.native import context as native_context  # noqa: E402
from alkera_gateway.native.context import DEV_TOKEN  # noqa: E402
from alkera_gateway.native.services import tableau  # noqa: E402

NS = {"t": "http://tableau.com/api"}
V = "3.25"


def seed_commerce(root: Path) -> Path:
    """The Commerce fixture's tables, copied into the world's warehouse file."""
    fixture = examples.write_fixture(root.parent / "commerce_fixture.duckdb")
    warehouse = root / "var" / "warehouse" / "warehouse.duckdb"
    with duckdb.connect(str(warehouse)) as con:
        con.execute(f"ATTACH '{fixture}' AS fx (READ_ONLY)")
        for table in examples.DDL:
            con.execute(f"CREATE TABLE warehouse.warehouse.{table} AS "
                        f"SELECT * FROM fx.warehouse.{table}")
    return warehouse


class Server:
    """One world with a Tableau project in its workspace, served by `tableau.app`."""

    def __init__(self, root: Path, audit: Path, audit_file: Path):
        self.root, self.audit_file = root, audit_file
        self.warehouse = root / "var" / "warehouse" / "warehouse.duckdb"
        self.project = root / "workspace" / "bi" / "tableau"
        self.context = native_context.Context(config(root, audit))
        native_context.set_context(tableau.app, self.context)
        self.client = TestClient(tableau.app, base_url="http://tableau.internal:8096")
        self.token = None
        self.site = None

    # -- requests
    def signin(self, secret=DEV_TOKEN, **kw) -> ET.Element:
        creds = kw.get("creds") or (f'<credentials personalAccessTokenName="ci" '
                                    f'personalAccessTokenSecret="{secret}">'
                                    f'<site contentUrl="" /></credentials>')
        r = self.client.post(f"/api/{V}/auth/signin", content=f"<tsRequest>{creds}</tsRequest>")
        if r.status_code == 200:
            x = ET.fromstring(r.content)
            self.token = x.find("t:credentials", NS).get("token")
            self.site = x.find("t:credentials/t:site", NS).get("id")
        return r

    def get(self, path, **kw):
        return self.client.get(self.url(path), headers=self.headers(), **kw)

    def post(self, path, **kw):
        return self.client.post(self.url(path), headers={**self.headers(),
                                                         **kw.pop("headers", {})}, **kw)

    def url(self, path):
        return path if path.startswith("/") else f"/api/{V}/sites/{self.site}/{path}"

    def headers(self):
        return {"X-Tableau-Auth": self.token} if self.token else {}

    def xml(self, response, status=200) -> ET.Element:
        assert response.status_code == status, response.text
        return ET.fromstring(response.content)

    def publish(self, kind, name, data: bytes, filename, *, overwrite=True, extra=""):
        payload = (f'<tsRequest><{kind} name="{name}"{extra}><project id="" />'
                   f"</{kind}></tsRequest>").encode()
        boundary = "alkeraBoundary7f9"
        body = b"".join([
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: name="request_payload"\r\nContent-Type: text/xml\r\n\r\n',
            payload, b"\r\n",
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: name="tableau_{kind}"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n", data, b"\r\n",
            f"--{boundary}--\r\n".encode()])
        ext = filename.rsplit(".", 1)[-1]
        q = f"?{kind}Type={ext}" + ("&overwrite=true" if overwrite else "")
        return self.post(f"{kind}s{q}", content=body,
                         headers={"Content-Type": f"multipart/mixed; boundary={boundary}"})

    # -- lookups
    def workbook(self, name="Executive Overview") -> ET.Element:
        x = self.xml(self.get(f"workbooks?filter=name:eq:{name}"))
        return x.find("t:workbooks/t:workbook", NS)

    def datasource(self, name="Commerce") -> ET.Element:
        x = self.xml(self.get(f"datasources?filter=name:eq:{name}"))
        return x.find("t:datasources/t:datasource", NS)

    def view_csv(self, sheet, workbook="ExecutiveOverview", query=""):
        url = f"{workbook}/sheets/{sheet.replace(' ', '')}"
        x = self.xml(self.get(f"views?filter=contentUrl:eq:{url}"))
        view = x.find("t:views/t:view", NS)
        r = self.get(f"views/{view.get('id')}/data{query}")
        return r

    def rows(self, tool=None) -> list[dict]:
        import json
        if not self.audit_file.exists():
            return []
        rows = [json.loads(line) for line in self.audit_file.read_text("utf-8").splitlines()]
        return [r for r in rows if tool is None or r["tool"] == tool]


def write_tableau_project(root: Path, **kw) -> Path:
    dest = root / "workspace" / "bi" / "tableau"
    tformat.write_project(tableau_commerce_model(), (tableau_executive_report(),), dest, **kw)
    return dest


@pytest.fixture
def server(world, tmp_path, monkeypatch) -> Server:
    seed_commerce(world)
    write_tableau_project(world)
    audit_file = tmp_path / "tableau-actions.jsonl"
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(audit_file))
    return Server(world, tmp_path / "audit", audit_file)


def parse_csv(response) -> list[list[str]]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    return list(csv.reader(io.StringIO(response.text)))


def money(text: str) -> Decimal:
    return Decimal(text.replace("$", "").replace(",", ""))


# ============================================================== 1. shapes
def test_serverinfo_needs_no_sign_in(server):
    for path in ("/api/2.4/serverInfo", f"/api/{V}/serverinfo", "/api/serverinfo"):
        x = server.xml(server.client.get(path))
        info = x.find("t:serverInfo", NS)
        assert info.findtext("t:restApiVersion", namespaces=NS) == "3.25"
        version = info.find("t:productVersion", NS)
        assert version.text == xmlio.PRODUCT_VERSION and version.get("build") == xmlio.BUILD
    assert len(server.rows("tableau.server_info")) == 3


def test_signin_with_token_or_password_and_refusals(server):
    r = server.signin("wrong")
    x = server.xml(r, 401)
    assert x.find("t:error", NS).get("code") == "401001"
    assert x.findtext("t:error/t:summary", namespaces=NS) == "Signin Error"
    server.xml(server.signin(creds=f'<credentials name="alkera" password="{DEV_TOKEN}">'
                                   f'<site contentUrl="" /></credentials>'))
    server.xml(server.signin())
    assert server.site == tableau.SITE_ID
    assert server.xml(server.get("workbooks")).find("t:pagination", NS) is not None
    # A site other than Default does not exist here.
    r = server.signin(creds=f'<credentials name="a" password="{DEV_TOKEN}">'
                            f'<site contentUrl="Marketing" /></credentials>')
    assert server.xml(r, 401).find("t:error", NS).get("code") == "401001"
    token = server.token
    assert server.post("/api/3.25/auth/signout").status_code == 204
    server.token = token
    assert server.xml(server.get("workbooks"), 401).find("t:error", NS).get("code") == "401002"
    server.token = None
    assert server.xml(server.client.get(f"/api/{V}/sites/x/workbooks"), 401) \
        .find("t:error", NS).get("code") == "401000"


def test_unknown_resources_answer_in_tableau_shape(server):
    server.signin()
    x = server.xml(server.get("/api/3.25/no/such/thing"), 404)
    assert x.find("t:error", NS).get("code") == "404003"
    assert server.xml(server.get("workbooks/nope"), 404).find("t:error", NS).get("code") \
        == "404006"
    assert server.xml(server.get("datasources/nope"), 404).find("t:error", NS).get("code") \
        == "404004"
    assert server.xml(server.get("jobs/nope"), 404).find("t:error", NS).get("code") == "404018"
    assert server.xml(server.get("views/nope/data"), 400).find("t:error", NS).get("code") \
        == "400080"
    assert server.xml(server.get("workbooks?pageSize=0"), 400).find("t:error", NS) \
        .get("code") == "400006"


def test_published_state_starts_as_the_workspace_project(server):
    server.signin()
    wb = server.workbook()
    assert wb.get("contentUrl") == "ExecutiveOverview"
    assert wb.find("t:project", NS).get("name") == "Default"
    x = server.xml(server.get(f"workbooks/{wb.get('id')}/views"))
    views = [(v.get("name"), v.get("contentUrl")) for v in x.findall("t:views/t:view", NS)]
    assert ("Revenue by Segment", "ExecutiveOverview/sheets/RevenuebySegment") in views
    assert ("Overview", "ExecutiveOverview/sheets/Overview") in views
    ds = server.datasource()
    assert (ds.get("type"), ds.get("hasExtracts")) == ("genericodbc", "false")
    x = server.xml(server.get(f"workbooks/{wb.get('id')}/connections"))
    conn = x.find("t:connections/t:connection", NS)
    assert conn.get("type") == "sqlproxy"
    assert conn.find("t:datasource", NS).get("id") == ds.get("id")
    assert server.client.get("/health").json() == {"ok": True, "service": "tableau", "items": 2}


def test_filters_paging_and_case_insensitive_names(server):
    server.signin()
    # tabcmd looks the default project up as "Default"; Tableau matches names
    # without regard to case.
    for name in ("Default", "default"):
        x = server.xml(server.get(f"projects?filter=name:eq:{name}"))
        assert len(x.findall("t:projects/t:project", NS)) == 1
    x = server.xml(server.get("views?pageSize=2&pageNumber=2"))
    assert x.find("t:pagination", NS).attrib == {"pageNumber": "2", "pageSize": "2",
                                                 "totalAvailable": "7"}
    assert len(x.findall("t:views/t:view", NS)) == 2
    x = server.xml(server.get("workbooks?filter=name:eq:Nope"))
    assert x.find("t:pagination", NS).get("totalAvailable") == "0"
    assert server.xml(server.get("workbooks?filter=color:eq:red"), 400) \
        .find("t:error", NS).get("code") == "400065"


def test_download_returns_the_published_bytes(server):
    server.signin()
    wb = server.workbook()
    r = server.get(f"workbooks/{wb.get('id')}/content")
    assert r.status_code == 200
    assert r.content == (server.project / "workbooks" / "Executive Overview.twb").read_bytes()
    assert r.headers["content-disposition"] == \
        'name="tableau_workbook"; filename="Executive Overview.twb"'
    ds = server.datasource()
    r = server.get(f"datasources/{ds.get('id')}/content")
    assert r.content == (server.project / "datasources" / "Commerce.tds").read_bytes()


# ============================================= 2. the published definition
def test_view_data_agrees_with_the_warehouse(server):
    server.signin()
    rows = parse_csv(server.view_csv("KPIs"))
    assert rows[0] == ["Net Revenue", "Completed Orders", "AOV"]
    with duckdb.connect(str(server.warehouse), read_only=True) as con:
        net, n = con.execute("SELECT SUM(order_amount), COUNT(*) FROM warehouse.warehouse."
                             "fct_orders WHERE order_status = 'completed'").fetchone()
    assert money(rows[1][0]) == net and int(rows[1][1]) == n
    assert rows[1][2] == f"${net / n:,.2f}"
    seg = parse_csv(server.view_csv("Revenue by Segment"))
    assert seg[0] == ["segment", "Net Revenue", "Segment Share"]
    assert [r[0] for r in seg[1:]] == ["Consumer", "SMB", "Enterprise"]   # computed sort
    assert sum(money(r[1]) for r in seg[1:]) == net
    assert all(re.fullmatch(r"\d+\.\d%", r[2]) for r in seg[1:])
    top = parse_csv(server.view_csv("Top Products"))
    assert len(top) == 4                                                  # top 3
    # A dashboard's data is its first sheet's.
    assert parse_csv(server.view_csv("Overview")) == rows


def test_url_filters_narrow_the_view(server):
    server.signin()
    rows = parse_csv(server.view_csv("Revenue by Segment", query="?vf_region=NA"))
    every = parse_csv(server.view_csv("Revenue by Segment"))
    assert sum(money(r[1]) for r in rows[1:]) < sum(money(r[1]) for r in every[1:])
    with duckdb.connect(str(server.warehouse), read_only=True) as con:
        na = con.execute("SELECT SUM(o.order_amount) FROM warehouse.warehouse.fct_orders o "
                         "JOIN warehouse.warehouse.dim_customer c USING (customer_id) "
                         "WHERE c.region = 'NA' AND o.order_status = 'completed'").fetchone()[0]
    assert sum(money(r[1]) for r in rows[1:]) == na
    assert parse_csv(server.view_csv("Revenue by Segment", query="?vf_nonexistent=1")) == every


def test_publishing_changes_the_numbers_editing_the_workspace_does_not(server):
    server.signin()
    before = parse_csv(server.view_csv("KPIs"))
    tds_path = server.project / "datasources" / "Commerce.tds"
    original = tds_path.read_bytes()
    # The engineer "fixes" Net Revenue to include every status, in the repo only.
    edited = original.replace(b"SUM(IF [order_status] = &apos;completed&apos; THEN "
                              b"[order_amount] END)", b"SUM([order_amount])")
    assert edited != original
    tds_path.write_bytes(edited)
    assert parse_csv(server.view_csv("KPIs")) == before            # not published yet
    r = server.publish("datasource", "Commerce", edited, "Commerce.tds")
    x = server.xml(r, 201)
    assert x.find("t:datasource", NS).get("name") == "Commerce"
    after = parse_csv(server.view_csv("KPIs"))
    with duckdb.connect(str(server.warehouse), read_only=True) as con:
        gross = con.execute("SELECT SUM(order_amount) FROM warehouse.warehouse.fct_orders") \
            .fetchone()[0]
    assert money(after[1][0]) == gross != money(before[1][0])
    row = server.rows("tableau.datasources_publish")[-1]
    assert (row["op_class"], row["targets"], row["flags"]) == \
        ("write", ["tableau:datasource:Commerce"], ["overwrite"])


def test_publish_workbook_twb_and_twbx(server):
    server.signin()
    model, report = tableau_commerce_model(), tableau_executive_report()
    data = xmlio.serialize(ttwb.write(model, report))
    x = server.xml(server.publish("workbook", "Exec Copy", data, "Exec Copy.twb"), 201)
    wb = x.find("t:workbook", NS)
    assert wb.get("contentUrl") == "ExecCopy"
    assert len(wb.findall("t:views/t:view", NS)) == 7
    packed = io.BytesIO()
    import zipfile
    with zipfile.ZipFile(packed, "w") as z:
        z.writestr("Exec Copy.twb", data)
    x = server.xml(server.publish("workbook", "Exec Packed", packed.getvalue(),
                                  "Exec Packed.twbx"), 201)
    assert parse_csv(server.view_csv("KPIs", workbook="ExecPacked")) == \
        parse_csv(server.view_csv("KPIs"))
    r = server.get(f"workbooks/{x.find('t:workbook', NS).get('id')}/content")
    assert r.headers["content-type"] == "application/octet-stream"


def test_publish_refusals(server):
    server.signin()
    data = (server.project / "workbooks" / "Executive Overview.twb").read_bytes()
    r = server.publish("workbook", "Executive Overview", data, "Executive Overview.twb",
                       overwrite=False)
    assert server.xml(r, 403).find("t:error", NS).get("code") == "403007"
    r = server.publish("workbook", "W", data, "W.pdf")
    assert server.xml(r, 400).find("t:error", NS).get("code") == "400035"
    r = server.publish("workbook", "W", b"<workbook", "W.twb")
    assert server.xml(r, 400).find("t:error", NS).get("code") == "400011"
    r = server.publish("workbook", "W", (server.project / "datasources" / "Commerce.tds")
                       .read_bytes(), "W.twb")
    assert server.xml(r, 400).find("t:error", NS).get("code") == "400011"
    r = server.publish("datasource", "Commerce", (server.project / "datasources" /
                                                  "Commerce.tds").read_bytes(),
                       "Commerce.tds", overwrite=False)
    assert server.xml(r, 409).find("t:error", NS).get("code") == "409005"
    r = server.post("workbooks", content=b"x", headers={"Content-Type": "text/xml"})
    assert server.xml(r, 400).find("t:error", NS).get("code") == "400000"


def test_unsupported_sheet_is_refused_by_name_the_rest_still_render(server):
    server.signin()
    text = (server.project / "workbooks" / "Executive Overview.twb").read_text("utf-8")
    start = text.index("<worksheet name='Top Products'>")
    end = text.index("</worksheet>", start)
    sheet = re.sub(r"(<column-instance column='\[Calculation_\d+\]' derivation='User' "
                   r"name='\[usr:Calculation_\d+:qk\]' pivot='key' type='quantitative') />",
                   r"\1>\n<table-calc ordering-type='Rows' type='PctTotal' />"
                   r"</column-instance>", text[start:end])
    patched = text[:start] + sheet + text[end:]
    assert patched != text
    server.xml(server.publish("workbook", "Executive Overview", patched.encode(),
                              "Executive Overview.twb"), 201)
    r = server.view_csv("Top Products")
    x = server.xml(r, 400)
    assert "table calculation (quick table calculation)" in \
        x.findtext("t:error/t:detail", namespaces=NS)
    assert parse_csv(server.view_csv("KPIs"))[0] == ["Net Revenue", "Completed Orders", "AOV"]


# ================================================================ 3. extracts
def test_refreshing_a_live_connection_is_refused(server):
    server.signin()
    ds = server.datasource()
    x = server.xml(server.post(f"datasources/{ds.get('id')}/refresh",
                               content=b"<tsRequest />"), 403)
    assert x.find("t:error", NS).get("code") == "403104"
    wb = server.workbook()
    x = server.xml(server.post(f"workbooks/{wb.get('id')}/refresh",
                               content=b"<tsRequest />"), 403)
    assert x.find("t:error", NS).get("code") == "403104"


def _add_order(warehouse: Path, order_id: int, amount: str) -> None:
    with duckdb.connect(str(warehouse)) as con:
        con.execute("INSERT INTO warehouse.warehouse.fct_orders VALUES "
                    f"({order_id}, DATE '2026-02-28', 1, 'completed', {amount}, 0)")


def test_an_extract_serves_its_last_refresh(server):
    server.signin()
    model = tableau_commerce_model()
    data = xmlio.serialize(ttds.write(model, ttds.CONNECTIONS["duckdb"], root=True,
                                      extract=True))
    x = server.xml(server.publish("datasource", "Commerce", data, "Commerce.tds"), 201)
    assert x.find("t:datasource", NS).get("hasExtracts") == "true"
    before = parse_csv(server.view_csv("KPIs"))
    _add_order(server.warehouse, 99_001, "1000.00")
    # The late order is in the warehouse, not in the extract.
    assert parse_csv(server.view_csv("KPIs")) == before
    ds = server.datasource()
    x = server.xml(server.post(f"datasources/{ds.get('id')}/refresh",
                               content=b"<tsRequest><extractRefresh /></tsRequest>"), 202)
    job = x.find("t:job", NS)
    assert (job.get("type"), job.get("mode")) == ("RefreshExtract", "Asynchronous")
    assert job.find("t:extractRefreshJob/t:datasource", NS).get("name") == "Commerce"
    done = server.xml(server.get(f"jobs/{job.get('id')}")).find("t:job", NS)
    assert (done.get("finishCode"), done.get("progress")) == ("0", "100")
    assert done.get("completedAt")
    after = parse_csv(server.view_csv("KPIs"))
    assert money(after[1][0]) - money(before[1][0]) == Decimal("1000.00")
    row = server.rows("tableau.datasources_refresh")[-1]
    assert row["op_class"] == "write" and "warehouse.fct_orders" in row["read_targets"]
    view_row = server.rows("tableau.views_data")[-1]
    assert any(f.startswith("extract:") for f in view_row["flags"])


def test_a_workbook_embedding_an_extract_refreshes_itself(server):
    server.signin()
    data = xmlio.serialize(ttwb.write(tableau_commerce_model(), tableau_executive_report(),
                                      embed=ttds.CONNECTIONS["duckdb"], extract=True))
    x = server.xml(server.publish("workbook", "Embedded", data, "Embedded.twb"), 201)
    wb_id = x.find("t:workbook", NS).get("id")
    before = parse_csv(server.view_csv("KPIs", workbook="Embedded"))
    _add_order(server.warehouse, 99_002, "50.00")
    assert parse_csv(server.view_csv("KPIs", workbook="Embedded")) == before
    x = server.xml(server.post(f"workbooks/{wb_id}/refresh", content=b"<tsRequest />"), 202)
    assert x.find("t:job/t:extractRefreshJob/t:workbook", NS).get("name") == "Embedded"
    after = parse_csv(server.view_csv("KPIs", workbook="Embedded"))
    assert money(after[1][0]) - money(before[1][0]) == Decimal("50.00")
    # The live, published-source workbook saw the late order at once.
    assert money(parse_csv(server.view_csv("KPIs"))[1][0]) == money(after[1][0])


def test_an_extract_that_cannot_be_taken_publishes_and_says_why(server):
    """Tableau accepts the file; the reason surfaces where the numbers would."""
    server.signin()
    model = tableau_commerce_model()
    broken = replace(model, tables=tuple(
        replace(t, source="warehouse.dim_missing") if t.name == "Customer" else t
        for t in model.tables))
    data = xmlio.serialize(ttds.write(broken, ttds.CONNECTIONS["duckdb"], root=True,
                                      extract=True))
    server.xml(server.publish("datasource", "Commerce", data, "Commerce.tds"), 201)
    x = server.xml(server.view_csv("KPIs"), 400)
    assert "The extract could not be created" in x.findtext("t:error/t:detail", namespaces=NS)
    ds = server.datasource()
    x = server.xml(server.post(f"datasources/{ds.get('id')}/refresh",
                               content=b"<tsRequest />"), 202)
    job = server.xml(server.get(f"jobs/{x.find('t:job', NS).get('id')}")).find("t:job", NS)
    assert job.get("finishCode") == "1"
    assert "dim_missing" in job.findtext("t:extractRefreshJob/t:notes", namespaces=NS)


# ==================================================================== audit
def test_every_request_writes_exactly_one_row(server):
    calls = [lambda: server.client.get("/api/serverinfo"),
             lambda: server.signin("bad"),
             lambda: server.signin(),
             lambda: server.get("workbooks"),
             lambda: server.get("workbooks/nope"),
             lambda: server.view_csv("KPIs"),     # two requests: lookup + data
             lambda: server.client.get("/health"),
             lambda: server.get("/api/3.25/no/such/thing"),
             lambda: server.get("/api/x.y/serverinfo")]
    for call in calls:
        call()
    rows = server.rows()
    assert [r["tool"] for r in rows] == [
        "tableau.server_info", "tableau.auth_signin", "tableau.auth_signin",
        "tableau.workbooks_list", "tableau.workbooks_get", "tableau.views_list",
        "tableau.views_data", "tableau.health", "tableau.request", "tableau.request"]
    assert [r["exit"] for r in rows] == ["ok", "error", "ok", "ok", "error", "ok", "ok", "ok",
                                         "error", "error"]
    assert rows[8]["error"]["code"] == rows[9]["error"]["code"] == "404003"
    assert rows[1]["error"]["code"] == "401001"
    assert "alkera-local-dev" not in str(rows)          # no secret in the log
    data = rows[6]
    assert data["targets"] == ["tableau:view:ExecutiveOverview/sheets/KPIs"]
    assert "warehouse.fct_orders" in data["read_targets"]
    assert rows[7]["targets"] == ["health:tableau"]
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))


def test_state_lives_outside_the_world_tree(server):
    server.signin()
    state = Path(server.context.config.bi_state_dir) / "tableau"
    assert (state / "manifest.json").exists()
    assert not state.is_relative_to(server.root)


def test_a_restarted_service_keeps_published_content_and_sessions(server, tmp_path):
    server.signin()
    data = xmlio.serialize(ttwb.write(tableau_commerce_model(), tableau_executive_report()))
    server.xml(server.publish("workbook", "Kept", data, "Kept.twb"), 201)
    token, site = server.token, server.site
    again = Server(server.root, Path(server.context.config.audit_dir), server.audit_file)
    again.token, again.site = token, site
    assert again.workbook("Kept").get("contentUrl") == "Kept"


# ========================================================== real clients
try:
    import tableauserverclient as TSC
except ImportError:                                       # pragma: no cover
    TSC = None
try:
    import tabcmd as _tabcmd  # noqa: F401
    HAS_TABCMD = True
except ImportError:                                       # pragma: no cover
    HAS_TABCMD = False
CLIENTS = "run with `uv run --with tableauserverclient --with tabcmd pytest " \
          "tests/test_tableau_service.py -q`"


@pytest.fixture
def live(server):
    """The app under uvicorn on a free loopback port, for the real clients."""
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    srv = uvicorn.Server(uvicorn.Config(tableau.app, host="127.0.0.1", port=port,
                                        log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not srv.started:
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    yield server, f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=10)


@pytest.mark.skipif(TSC is None, reason=f"tableauserverclient not installed; {CLIENTS}")
def test_tableau_server_client_end_to_end(live, tmp_path):
    server, url = live
    tsc = TSC.Server(url, use_server_version=True)
    tsc.auth.sign_in(TSC.PersonalAccessTokenAuth("ci", DEV_TOKEN, site_id=""))
    assert tsc.version == "3.25"
    workbooks, pagination = tsc.workbooks.get()
    assert [w.name for w in workbooks] == ["Executive Overview"]
    wb = workbooks[0]
    tsc.workbooks.populate_views(wb)
    kpis = next(v for v in wb.views if v.name == "KPIs")
    tsc.views.populate_csv(kpis)
    rows = list(csv.reader(io.StringIO(b"".join(kpis.csv).decode())))
    assert rows[0] == ["Net Revenue", "Completed Orders", "AOV"]
    tsc.workbooks.populate_connections(wb)
    assert [c.connection_type for c in wb.connections] == ["sqlproxy"]
    project = next(p for p in TSC.Pager(tsc.projects) if p.name == "Default")
    # Publish an extract-enabled data source and a new workbook, refresh, wait.
    tds_file = tmp_path / "Commerce.tds"
    tds_file.write_bytes(xmlio.serialize(ttds.write(tableau_commerce_model(),
                                                    ttds.CONNECTIONS["duckdb"], root=True,
                                                    extract=True)))
    ds = tsc.datasources.publish(TSC.DatasourceItem(project.id), str(tds_file),
                                 TSC.Server.PublishMode.Overwrite)
    assert ds.name == "Commerce" and ds.has_extracts
    job = tsc.datasources.refresh(ds)
    done = tsc.jobs.wait_for_job(job, timeout=30)
    assert done.finish_code == 0
    twb_file = tmp_path / "Copy.twb"
    twb_file.write_bytes(xmlio.serialize(ttwb.write(tableau_commerce_model(),
                                                   tableau_executive_report())))
    new = tsc.workbooks.publish(TSC.WorkbookItem(project.id), str(twb_file),
                                TSC.Server.PublishMode.Overwrite)
    assert new.name == "Copy" and len(new.views) == 7
    path = tsc.workbooks.download(new.id, filepath=str(tmp_path / "dl"))
    assert Path(path).read_bytes() == twb_file.read_bytes()
    tsc.auth.sign_out()


def _tabcmd(home: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "tabcmd", *args], capture_output=True,
                          text=True, timeout=120, stdin=subprocess.DEVNULL,
                          env={**os.environ, "HOME": str(home)})


@pytest.mark.skipif(not HAS_TABCMD, reason=f"tabcmd not installed; {CLIENTS}")
def test_tabcmd_end_to_end(live, tmp_path):
    server, url = live
    home = tmp_path / "home"
    home.mkdir()
    r = _tabcmd(home, "login", "-s", url, "--token-name", "ci", "--token-value", DEV_TOKEN)
    assert r.returncode == 0, r.stdout + r.stderr
    tds_file = tmp_path / "Commerce.tds"
    tds_file.write_bytes(xmlio.serialize(ttds.write(tableau_commerce_model(),
                                                    ttds.CONNECTIONS["duckdb"], root=True,
                                                    extract=True)))
    r = _tabcmd(home, "publish", str(tds_file), "-o")
    assert r.returncode == 0, r.stdout + r.stderr
    _add_order(server.warehouse, 99_003, "250.00")
    out = tmp_path / "before.csv"
    r = _tabcmd(home, "export", "/ExecutiveOverview/KPIs", "--csv", "-f", str(out))
    assert r.returncode == 0, r.stdout + r.stderr
    r = _tabcmd(home, "refreshextracts", "--datasource", "Commerce", "--synchronous")
    assert r.returncode == 0, r.stdout + r.stderr
    after = tmp_path / "after.csv"
    r = _tabcmd(home, "get", "/views/ExecutiveOverview/KPIs.csv", "-f", str(after))
    assert r.returncode == 0, r.stdout + r.stderr
    before_rows = list(csv.reader(out.open()))
    after_rows = list(csv.reader(after.open()))
    assert money(after_rows[1][0]) - money(before_rows[1][0]) == Decimal("250.00")
    twb_file = tmp_path / "Exec Copy.twb"
    shutil.copy(server.project / "workbooks" / "Executive Overview.twb", twb_file)
    r = _tabcmd(home, "publish", str(twb_file), "-n", "Exec Copy", "-o")
    assert r.returncode == 0, r.stdout + r.stderr
    assert server.rows("tableau.workbooks_publish")[-1]["args"]["name"] == "Exec Copy"
    r = _tabcmd(home, "logout")
    assert r.returncode == 0
