"""The Tableau UI clone (native/ui/tableau.py) over a BUILT commerce_analytics world.

The UI is one more client of the fake Tableau Server's published state, so
every check here is against the REST service on the same state:
  - a view page's tables are, cell for cell, the CSV `views/{id}/data` returns
    (and so `render.render_visual` of the published workbook), and `<view>.csv`
    is that CSV byte for byte;
  - a calculation saved in the browser changes the REST API's numbers and the
    published .tds - only its formula attribute - and never the workspace;
  - a formula the front-end refuses shows the front-end's message and
    publishes nothing;
  - a calculation that belongs to a workbook (a workbook-local calculation
    over the published source, or one in a data source the workbook embeds)
    is saved by publishing the workbook, and a packaged file (.tdsx/.twbx) is
    repacked with only its document changed;
  - a view URL's filters (`?region=EMEA`) are the REST API's `vf_` filters;
  - the browser has its own password: it is not a REST credential, and the
    REST token does not open the browser;
  - one audit row per request, named `tableau.ui_*`.
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import replace
from decimal import Decimal
from html import unescape
from html.parser import HTMLParser
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "world") not in sys.path:
    sys.path.insert(0, str(REPO / "world"))

from world import build, fingerprint  # noqa: E402
from world.semantic import render  # noqa: E402
from world.semantic.expr import Agg, Col  # noqa: E402
from world.semantic.formats import tableau as tableau_format  # noqa: E402
from world.semantic.formats.tableau import tds as ttds  # noqa: E402
from world.semantic.formats.tableau import twb as ttwb  # noqa: E402
from world.semantic.formats.tableau import xmlio  # noqa: E402
from world.semantic.model import Page, Visual  # noqa: E402

from alkera_gateway import GatewayConfig  # noqa: E402
from alkera_gateway.native import context as native_context  # noqa: E402
from alkera_gateway.native.services import tableau  # noqa: E402
from alkera_gateway.native.ui import tableau as ui  # noqa: E402

SEED = REPO / "world" / "world" / "themes" / "commerce_analytics" / "seed.example.yaml"
PROFILE = {"name": "da/laptop",
           "slots": ["files", "notebook", "warehouse", "transform", "bi", "human"],
           "plugs": {"bi": ["tableau"]}}
V = "3.25"
NS = {"t": "http://tableau.com/api"}
NET = "Calculation_2962373405968187117"           # [Net Revenue] in Commerce.tds


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> Path:
    tmp = tmp_path_factory.mktemp("ui_world")
    seed = yaml.safe_load(SEED.read_text(encoding="utf-8"))
    path = tmp / "seed.yaml"
    path.write_text(yaml.safe_dump({**seed, "profile": PROFILE}, sort_keys=True),
                    encoding="utf-8")
    return build.build(path, tmp / "world")


@pytest.fixture
def env(world, tmp_path, monkeypatch):
    """The REST service and the standalone UI on ONE Context (one state dir)."""
    audit = tmp_path / "audit"
    cfg = GatewayConfig(
        workspace=world / "workspace",
        warehouse=world / "var" / "warehouse" / "warehouse.duckdb",
        scheduler=world / "var" / "sched" / "state.json",
        dashboards=world / "var" / "bi" / "dashboards",
        observability=world / "var" / "obs" / "definitions.yaml",
        workflow_dir=world / "var" / "workflow",
        audit_dir=audit, profiles_dir=world / "build", episode_id=audit.name,
        initial_world_fp=fingerprint.semantic_hash(world))
    rows = tmp_path / "native-actions.jsonl"
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(rows))
    monkeypatch.setenv("ALKERA_SCHEDULER", str(cfg.scheduler))
    context = native_context.Context(cfg)
    app = ui.standalone()
    native_context.set_context(app, context)
    native_context.set_context(tableau.app, context)
    try:
        yield {"ui": TestClient(app, base_url="http://tableau.internal:8096"),
               "rest": TestClient(tableau.app, base_url="http://tableau.internal:8096"),
               "rows": rows, "config": cfg}
    finally:
        del tableau.app.state.alkera_context


def _signin(client) -> None:
    r = client.post("/signin", data={"username": "alkera",
                                     "password": tableau.UI_PASSWORD,
                                     "redirect": "/workbooks"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/workbooks", r.text
    assert ui.COOKIE in r.cookies


class _Rest:
    def __init__(self, client):
        r = client.post(f"/api/{V}/auth/signin", content=(
            f'<tsRequest><credentials personalAccessTokenName="ci" '
            f'personalAccessTokenSecret="{native_context.DEV_TOKEN}">'
            f'<site contentUrl="" /></credentials></tsRequest>'))
        assert r.status_code == 200, r.text
        creds = ET.fromstring(r.content).find("t:credentials", NS)
        self.client, self.site = client, creds.find("t:site", NS).get("id")
        self.headers = {"X-Tableau-Auth": creds.get("token")}

    def get(self, path: str):
        r = self.client.get(f"/api/{V}/sites/{self.site}/{path}", headers=self.headers)
        assert r.status_code == 200, r.text
        return r

    def views(self) -> list[ET.Element]:
        return list(ET.fromstring(self.get("views").content).iter(f"{{{NS['t']}}}view"))

    def data(self, view_id: str, query: str = "") -> list[list[str]]:
        return list(csv.reader(io.StringIO(self.get(f"views/{view_id}/data{query}").text)))

    def view_id(self, name: str, workbook: str = "Executive Overview") -> str:
        x = ET.fromstring(self.get(f"views?filter=workbookName:eq:{workbook}").content)
        return next(v.get("id") for v in x.iter(f"{{{NS['t']}}}view") if v.get("name") == name)

    def publish(self, kind: str, name: str, data: bytes, filename: str) -> ET.Element:
        """`POST .../{kind}s?overwrite=true` as TSC sends it (multipart/mixed)."""
        boundary = "alkeraBoundary7f9"
        body = b"".join([
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: name="request_payload"\r\nContent-Type: text/xml\r\n\r\n',
            f'<tsRequest><{kind} name="{name}"><project id="" /></{kind}></tsRequest>'.encode(),
            b"\r\n", f"--{boundary}\r\n".encode(),
            f'Content-Disposition: name="tableau_{kind}"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n", data, b"\r\n",
            f"--{boundary}--\r\n".encode()])
        r = self.client.post(
            f"/api/{V}/sites/{self.site}/{kind}s?{kind}Type={filename.rsplit('.', 1)[-1]}"
            f"&overwrite=true", content=body, headers={
                **self.headers, "Content-Type": f"multipart/mixed; boundary={boundary}"})
        assert r.status_code == 201, r.text
        return ET.fromstring(r.content).find(f"t:{kind}", NS)


class _Tables(HTMLParser):
    """`<table class="summary" data-view=...>` -> {view: [[header...], [cell...]...]};
    a listing (`<table data-list=...>`) by its list name."""

    def __init__(self):
        super().__init__()
        self.tables: dict[str, list[list[str]]] = {}
        self._cur = self._row = self._cell = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table" and (a.get("class") == "summary" or "data-list" in a):
            self._cur = self.tables.setdefault(a.get("data-view") or a["data-list"], [])
        elif tag == "tr" and self._cur is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append("".join(self._cell))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self._cur.append(self._row)
            self._row = None
        elif tag == "table":
            self._cur = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def tables(html: str) -> dict[str, list[list[str]]]:
    p = _Tables()
    p.feed(html)
    return p.tables


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line]


def _state(env) -> dict:
    root = Path(env["config"].bi_state_dir) / "tableau"
    return json.loads((root / "manifest.json").read_text("utf-8"))


def _published(env) -> tuple[dict, Path]:
    root = Path(env["config"].bi_state_dir) / "tableau"
    (ds,) = _state(env)["datasources"]
    return ds, root / "content" / f"{ds['id']}.{ds['file_type']}"


def _calc_links(html: str) -> dict[str, str]:
    """The calculated fields a page lists: caption -> editor URL."""
    return {unescape(cap): href for href, cap in re.findall(
        r'<a href="(/(?:workbooks|datasources)/[^"]+/calculations/[^"]+)">([^<]+)</a>', html)}


def _money(text: str) -> Decimal:
    return Decimal(text.replace("$", "").replace(",", ""))


def _zip(members: list[tuple[str, bytes, int]]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, data, method in members:
            info = zipfile.ZipInfo(name, date_time=(2026, 2, 27, 9, 30, 0))
            info.compress_type = method
            info.external_attr = 0o644 << 16
            z.writestr(info, data)
    return out.getvalue()


def _members(data: bytes) -> list[tuple[str, tuple, int, bytes]]:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return [(i.filename, i.date_time, i.compress_type, z.read(i)) for i in z.infolist()]


def _project_model(world):
    return tableau_format.read_project(
        world / "workspace" / "bi" / "tableau",
        warehouse=world / "var" / "warehouse" / "warehouse.duckdb")


# ======================================================================
def test_signed_out_pages_redirect_to_sign_in_and_bad_passwords_are_refused(env):
    c = env["ui"]
    r = c.get("/views/ExecutiveOverview/Overview", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/signin?redirect=/views/ExecutiveOverview/Overview"
    r = c.post("/signin", data={"username": "alkera", "password": "hunter2"})
    assert r.status_code == 401 and "Invalid username or password." in r.text
    assert ui.COOKIE not in c.cookies


def test_every_view_page_shows_the_rest_apis_numbers(env, world):
    c, rest = env["ui"], _Rest(env["rest"])
    _signin(c)
    listing = c.get("/workbooks")
    assert listing.status_code == 200 and "Executive Overview" in listing.text
    published = {v.get("contentUrl"): v for v in rest.views()}
    model, (report,) = tableau_format.read_project(
        world / "workspace" / "bi" / "tableau",
        warehouse=world / "var" / "warehouse" / "warehouse.duckdb")
    sheets = {v.name: v for p in report.pages for v in p.visuals}
    checked = 0
    for content_url, view in published.items():
        wb_url, sheet = content_url.split("/sheets/")
        page = c.get(f"/views/{wb_url}/{sheet}")
        assert page.status_code == 200, page.text
        shown = tables(page.text)
        assert shown, content_url
        if view.get("sheetType") == "worksheet":
            assert shown[view.get("name")] == rest.data(view.get("id")), content_url
            raw = render.render_visual(model, sheets[view.get("name")],
                                       world / "var" / "warehouse" / "warehouse.duckdb")
            assert len(shown[view.get("name")]) == 1 + len(raw["rows"])
            assert shown[view.get("name")][0] == raw["columns"]
            csv_page = c.get(f"/views/{wb_url}/{sheet}.csv")
            assert csv_page.content == rest.get(f"views/{view.get('id')}/data").content
            checked += 1
        else:
            # A dashboard shows each of its sheets, each as its own view shows it.
            for name, cells in shown.items():
                other = next(v for v in published.values() if v.get("name") == name)
                assert cells == rest.data(other.get("id")), (content_url, name)
    assert checked == 5
    month = c.get("/views/ExecutiveOverview/RevenuebyMonth").text
    assert 'data-kind="line"' in month and "<script" not in month
    assert 'data-kind="bar"' in c.get("/views/ExecutiveOverview/RevenuebySegment").text


def test_a_calculation_saved_in_the_browser_is_what_the_rest_api_serves(env, world):
    c, rest = env["ui"], _Rest(env["rest"])
    _signin(c)
    month = next(v for v in rest.views() if v.get("name") == "Revenue by Month")
    before = rest.data(month.get("id"))
    header = before[0]
    net, gross = header.index("Net Revenue"), header.index("Gross Revenue")
    assert any(r[net] != r[gross] for r in before[1:])
    ds, content = _published(env)
    old_bytes = content.read_bytes()
    workspace = (world / "workspace" / "bi" / "tableau" / "datasources" / "Commerce.tds")
    workspace_bytes = workspace.read_bytes()

    editor = c.get(f"/datasources/{ds['id']}/calculations/{NET}")
    assert editor.status_code == 200
    assert "SUM(IF [order_status] = &#39;completed&#39; THEN [order_amount] END)" in editor.text

    r = c.post(f"/datasources/{ds['id']}/calculations/{NET}",
               data={"formula": "SUM([order_amount])"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert "Saved and published." in c.get(r.headers["location"]).text

    after = rest.data(month.get("id"))
    assert [row[net] for row in after[1:]] == [row[gross] for row in after[1:]]
    assert [row[:net] for row in after] == [row[:net] for row in before]
    new_bytes = content.read_bytes()
    assert new_bytes == old_bytes.replace(
        b"formula='SUM(IF [order_status] = &apos;completed&apos; THEN [order_amount] END)'",
        b"formula='SUM([order_amount])'", 1)
    assert workspace.read_bytes() == workspace_bytes           # the repo is not the server

    # A formula typed with captions is stored by field name, as Tableau stores it.
    aov = "Calculation_2962675809468290875"
    r = c.post(f"/datasources/{ds['id']}/calculations/{aov}",
               data={"formula": "[Gross Revenue] / [Completed Orders]"},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    assert (b"formula='[Calculation_2962277896825485931] / "
            b"[Calculation_2962538338428847747]'") in content.read_bytes()


@pytest.mark.parametrize("formula,message", [
    ("SUM([no_such_field])", "unknown field [no_such_field]"),
    ("WINDOW_SUM(SUM([order_amount]))", "unsupported (Tableau)"),
    ("SUM([order_amount]", "The calculation contains errors"),
])
def test_a_refused_formula_shows_the_front_ends_error_and_publishes_nothing(
        env, formula, message):
    c = env["ui"]
    _signin(c)
    ds, content = _published(env)
    old_bytes = content.read_bytes()
    r = c.post(f"/datasources/{ds['id']}/calculations/{NET}", data={"formula": formula})
    assert r.status_code == 400
    assert "The calculation contains errors" in r.text and message in r.text, r.text
    assert 'role="alert"' in r.text and 'id="calc-editor"' in r.text
    ds_after, _ = _published(env)
    assert content.read_bytes() == old_bytes
    assert ds_after["updated_at"] == ds["updated_at"]
    row = _rows(env["rows"])[-1]
    assert row["tool"] == "tableau.ui_calc_save" and row["exit"] == "error"


def test_one_audit_row_per_request(env):
    c, rows = env["ui"], env["rows"]

    def one(tool, call, exit="ok"):
        n = len(_rows(rows))
        call()
        new = _rows(rows)[n:]
        assert [(r["tool"], r["exit"]) for r in new] == [(tool, exit)], new

    one("tableau.ui_view", lambda: c.get("/views/ExecutiveOverview/Overview",
                                         follow_redirects=False), exit="error")
    one("tableau.ui_signin_page", lambda: c.get("/signin"))
    one("tableau.ui_signin", lambda: _signin(c))
    one("tableau.ui_workbooks", lambda: c.get("/workbooks"))
    one("tableau.ui_view", lambda: c.get("/views/ExecutiveOverview/Overview"))
    one("tableau.ui_view_csv", lambda: c.get("/views/ExecutiveOverview/KPIs.csv"))
    one("tableau.ui_datasources", lambda: c.get("/datasources"))
    ds, _ = _published(env)
    one("tableau.ui_datasource", lambda: c.get(f"/datasources/{ds['id']}"))
    one("tableau.ui_calc_edit", lambda: c.get(f"/datasources/{ds['id']}/calculations/{NET}"))
    one("tableau.ui_calc_save", lambda: c.post(
        f"/datasources/{ds['id']}/calculations/{NET}", data={"formula": "SUM([order_amount])"},
        follow_redirects=False))
    save = _rows(rows)[-1]
    assert save["op_class"] == "write" and save["targets"] == ["tableau:datasource:Commerce"]
    assert "overwrite" in save["flags"]
    view = [r for r in _rows(rows) if r["tool"] == "tableau.ui_view" and r["exit"] == "ok"][-1]
    assert view["read_targets"] and all("." in t for t in view["read_targets"])
    one("tableau.ui_view", lambda: c.get("/views/ExecutiveOverview/NoSuchSheet"), exit="error")
    one("tableau.ui_projects", lambda: c.get("/projects"))
    one("tableau.ui_project", lambda: c.get(f"/projects/{tableau.PROJECT_ID}"))
    (wb,) = _state(env)["workbooks"]
    one("tableau.ui_workbook", lambda: c.get(f"/workbooks/{wb['id']}/views"))
    one("tableau.ui_view", lambda: c.get("/views/ExecutiveOverview/Overview?region=EMEA"))
    # a calculation the workbook only caches from its published source
    one("tableau.ui_workbook_calc_edit",
        lambda: c.get(f"/workbooks/{wb['id']}/calculations/{NET}"), exit="error")
    one("tableau.ui_workbook_calc_save", lambda: c.post(
        f"/workbooks/{wb['id']}/calculations/{NET}", data={"formula": "SUM([order_amount])"}),
        exit="error")
    one("tableau.ui_signout", lambda: c.post("/signout", follow_redirects=False))


def test_ui_is_mounted_at_import(env):
    """services/tableau.py mounts the UI when it is imported: nothing calls
    `mount`, every UI route is in the vendor's app exactly once, and the API's
    own routes and refusals are intact beside it."""
    routes = tableau.app.router.routes
    wrapped = [r for r in routes if getattr(r, "original_router", None) is ui.router]
    copied = [r for r in routes
              if getattr(r, "endpoint", None) in {u.endpoint for u in ui.router.routes}]
    # FastAPI >= 0.140 includes a router as one entry; older versions copy
    # its routes. Either way, once.
    assert (len(wrapped), len(copied)) in ((1, 0), (0, len(ui.router.routes)))
    c = env["rest"]
    _signin(c)
    assert "Executive Overview" in c.get("/workbooks").text
    assert c.get("/api/serverinfo").status_code == 200
    refused = c.get(f"/api/{V}/sites")                 # the API's own refusals intact
    assert refused.status_code == 401 and b"<tsResponse" in refused.content


def test_projects_page_lists_the_rest_projects(env):
    c, rest = env["ui"], _Rest(env["rest"])
    _signin(c)
    page = c.get("/projects")
    assert page.status_code == 200, page.text
    listed = tables(page.text)["projects"]
    projects = list(ET.fromstring(rest.get("projects").content).iter(f"{{{NS['t']}}}project"))
    assert projects and listed[0] == ["Name", "Description", "Workbooks", "Data Sources"]
    assert [row[:2] for row in listed[1:]] == [[p.get("name"), p.get("description")]
                                               for p in projects]
    for row, project in zip(listed[1:], projects, strict=True):
        for kind, cell in (("workbooks", row[2]), ("datasources", row[3])):
            x = ET.fromstring(rest.get(f"{kind}?filter=projectName:eq:{project.get('name')}")
                              .content)
            assert cell == x.find("t:pagination", NS).get("totalAvailable"), (kind, row)
    # A project's page lists what the REST API says is in it.
    one = c.get(f"/projects/{projects[0].get('id')}")
    assert one.status_code == 200
    assert [r[0] for r in tables(one.text)["workbooks"][1:]] == ["Executive Overview"]
    assert [r[0] for r in tables(one.text)["datasources"][1:]] == ["Commerce"]
    # The workbooks page names each workbook's project as the REST API does.
    wb = ET.fromstring(rest.get("workbooks").content).find("t:workbooks/t:workbook", NS)
    assert tables(c.get("/workbooks").text)["workbooks"][1][:2] == [
        wb.get("name"), wb.find("t:project", NS).get("name")]
    assert c.get("/projects/no-such-project").status_code == 404


def test_url_filter_in_the_view_matches_the_rest_vf_filter(env):
    """`/views/<wb>/<sheet>?region=EMEA` is Tableau Server's URL filter; the
    table it shows is the REST API's `views/{id}/data?vf_region=EMEA`."""
    c, rest = env["ui"], _Rest(env["rest"])
    _signin(c)
    seg = rest.view_id("Revenue by Segment")
    every, emea = rest.data(seg), rest.data(seg, "?vf_region=EMEA")
    assert emea[0] == every[0] and emea != every
    page = c.get("/views/ExecutiveOverview/RevenuebySegment?region=EMEA")
    assert page.status_code == 200, page.text
    assert tables(page.text)["Revenue by Segment"] == emea
    assert "region = EMEA" in page.text                    # the filter is on the page
    assert 'href="/views/ExecutiveOverview/RevenuebySegment.csv?region=EMEA"' in page.text
    assert c.get("/views/ExecutiveOverview/RevenuebySegment.csv?region=EMEA").content == \
        rest.get(f"views/{seg}/data?vf_region=EMEA").content
    row = [r for r in _rows(env["rows"]) if r["tool"] == "tableau.ui_view_csv"][-1]
    assert row["args"]["filters"] == {"region": "EMEA"}
    # A dashboard filters every sheet on it; Tableau's own `:` parameters are
    # display options, not filters.
    shown = tables(c.get("/views/ExecutiveOverview/Overview?region=EMEA&:embed=y").text)
    assert len(shown) == 4
    for name, cells in shown.items():
        assert cells == rest.data(rest.view_id(name), "?vf_region=EMEA"), name
    # A field the view's data source lacks is not applied, and the page says so.
    page = c.get("/views/ExecutiveOverview/RevenuebySegment?Region=EMEA")
    assert tables(page.text)["Revenue by Segment"] == every
    assert "Region" in page.text and "not applied" in page.text


def _shipping_workbook(world) -> bytes:
    """The executive workbook plus a sheet whose value is a workbook-local
    calculation ("Total Shipping", SUM([shipping_amount])) over the published
    Commerce source - the twb writer makes one for a label that is not a
    measure's."""
    model, (report,) = _project_model(world)
    seg = next(v for p in report.pages for v in p.visuals if v.name == "Revenue by Segment")
    sheet = Visual("Shipping by Segment", group_by=seg.group_by, groups=seg.groups,
                   values=(("Total Shipping", Agg("sum", Col("Orders", "shipping_amount"))),))
    report = replace(report, pages=report.pages + (Page(sheet.name, (sheet,)),))
    return xmlio.serialize(ttwb.write(model, report))


def test_workbook_local_calc_saved_in_browser_is_served_by_rest(env, world):
    c, rest = env["ui"], _Rest(env["rest"])
    _signin(c)
    data = _shipping_workbook(world)
    old = b"formula='SUM([shipping_amount])'"
    assert data.count(old) == 2              # the data source's and the sheet's copy
    wb = rest.publish("workbook", "Shipping", data, "Shipping.twb")
    view = rest.view_id("Shipping by Segment", workbook="Shipping")
    before = rest.data(view)
    published_ds, ds_content = _published(env)
    ds_bytes = ds_content.read_bytes()

    page = c.get(f"/workbooks/{wb.get('id')}/views")
    assert page.status_code == 200, page.text
    links = _calc_links(page.text)
    # The workbook's own calculation; the published source's are the source's.
    assert list(links) == ["Total Shipping"]
    editor = c.get(links["Total Shipping"])
    assert editor.status_code == 200 and "SUM([shipping_amount])" in editor.text
    r = c.post(links["Total Shipping"], data={"formula": "SUM([shipping_amount]) * 2"},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    assert "Saved and published." in c.get(r.headers["location"]).text

    after = rest.data(view)
    assert after[0] == before[0] and [row[0] for row in after] == [row[0] for row in before]
    assert [_money(row[1]) for row in after[1:]] == [2 * _money(row[1]) for row in before[1:]]
    content = rest.get(f"workbooks/{wb.get('id')}/content").content
    assert content == data.replace(old, b"formula='SUM([shipping_amount]) * 2'")
    assert ds_content.read_bytes() == ds_bytes                     # the source untouched
    assert _published(env)[0]["updated_at"] == published_ds["updated_at"]
    save = [row for row in _rows(env["rows"]) if row["tool"] == "tableau.ui_workbook_calc_save"]
    assert len(save) == 1 and save[0]["exit"] == "ok" and save[0]["op_class"] == "write"
    save = save[0]
    assert save["targets"] == ["tableau:workbook:Shipping"] and "overwrite" in save["flags"]

    # A published calculation the workbook caches is the data source's to edit.
    refused = c.get(f"/workbooks/{wb.get('id')}/calculations/{NET}")
    assert refused.status_code == 400
    assert "published data source" in refused.text and "Commerce" in refused.text
    assert f'href="/datasources/{published_ds["id"]}/calculations/{NET}"' in refused.text
    r = c.post(f"/workbooks/{wb.get('id')}/calculations/{NET}",
               data={"formula": "SUM([order_amount])"})
    assert r.status_code == 400
    assert rest.get(f"workbooks/{wb.get('id')}/content").content == content
    # A row-level formula cannot be added to a published source in a workbook.
    r = c.post(links["Total Shipping"], data={"formula": "[shipping_amount] * 2"})
    assert r.status_code == 400 and "The calculation contains errors" in r.text, r.text
    assert rest.get(f"workbooks/{wb.get('id')}/content").content == content


def test_a_calculation_in_an_embedded_data_source_saved_in_the_browser(env, world):
    """A packaged workbook (.twbx) that embeds its data source: the save
    publishes the workbook, repacked with only its .twb changed."""
    c, rest = env["ui"], _Rest(env["rest"])
    _signin(c)
    model, (report,) = _project_model(world)
    twb = xmlio.serialize(ttwb.write(model, report, embed=ttds.CONNECTIONS["duckdb"]))
    old = b"formula='SUM(IF [order_status] = &apos;completed&apos; THEN [order_amount] END)'"
    new = b"formula='SUM([order_amount])'"
    assert twb.count(old) >= 2
    packed = _zip([("Embedded.twb", twb, zipfile.ZIP_DEFLATED),
                   ("Data/Extracts/Embedded.hyper", bytes(range(256)) * 8, zipfile.ZIP_STORED),
                   ("Image/logo.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 64, zipfile.ZIP_DEFLATED)])
    wb = rest.publish("workbook", "Embedded", packed, "Embedded.twbx")
    month, main = (rest.view_id("Revenue by Month", workbook="Embedded"),
                   rest.view_id("Revenue by Month"))
    before, main_before = rest.data(month), rest.data(main)
    net, gross = before[0].index("Net Revenue"), before[0].index("Gross Revenue")
    assert any(r[net] != r[gross] for r in before[1:])
    _, ds_content = _published(env)
    ds_bytes = ds_content.read_bytes()

    links = _calc_links(c.get(f"/workbooks/{wb.get('id')}/views").text)
    assert {"Net Revenue", "Gross Revenue", "AOV"} <= set(links)  # every calc is its own
    r = c.post(links["Net Revenue"], data={"formula": "SUM([order_amount])"},
               follow_redirects=False)
    assert r.status_code == 303, r.text

    after = rest.data(month)
    assert [row[net] for row in after[1:]] == [row[gross] for row in after[1:]]
    assert rest.data(main) == main_before            # another workbook, another source
    assert ds_content.read_bytes() == ds_bytes
    content = rest.get(f"workbooks/{wb.get('id')}/content").content
    was, now = _members(packed), _members(content)
    assert [m[:3] for m in now] == [m[:3] for m in was]
    assert now[1:] == was[1:]                         # every other member byte-equal
    assert now[0][3] == twb.replace(old, new)


def test_tdsx_repack_changes_only_the_tds_member(env, world):
    c, rest = env["ui"], _Rest(env["rest"])
    _signin(c)
    tds = (world / "workspace" / "bi" / "tableau" / "datasources" / "Commerce.tds").read_bytes()
    packed = _zip([("Data/Extracts/Commerce.hyper", bytes(range(256)) * 8, zipfile.ZIP_STORED),
                   ("Commerce.tds", tds, zipfile.ZIP_DEFLATED),
                   ("Image/logo.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 64, zipfile.ZIP_DEFLATED)])
    rest.publish("datasource", "Commerce", packed, "Commerce.tdsx")
    ds, content = _published(env)
    assert ds["file_type"] == "tdsx" and content.read_bytes() == packed
    month = rest.view_id("Revenue by Month")
    page = c.get(f"/datasources/{ds['id']}")
    assert page.status_code == 200 and "Net Revenue" in _calc_links(page.text)

    r = c.post(f"/datasources/{ds['id']}/calculations/{NET}",
               data={"formula": "SUM([order_amount])"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    was, now = _members(packed), _members(content.read_bytes())
    assert [m[:3] for m in now] == [m[:3] for m in was]
    assert now[0] == was[0] and now[2] == was[2]      # every other member byte-equal
    assert now[1][3] == tds.replace(
        b"formula='SUM(IF [order_status] = &apos;completed&apos; THEN [order_amount] END)'",
        b"formula='SUM([order_amount])'", 1)
    after = rest.data(month)
    net, gross = after[0].index("Net Revenue"), after[0].index("Gross Revenue")
    assert [row[net] for row in after[1:]] == [row[gross] for row in after[1:]]


def test_ui_password_is_not_a_rest_token(env):
    """The browser's password signs in to the browser only, and the REST
    token (and a REST session) does not open the browser: an arm can hold one
    without the other."""
    c, api = env["ui"], env["rest"]
    assert tableau.UI_PASSWORD != native_context.DEV_TOKEN
    for creds in (f'personalAccessTokenName="ci" personalAccessTokenSecret='
                  f'"{tableau.UI_PASSWORD}"', f'name="alkera" password="{tableau.UI_PASSWORD}"'):
        r = api.post(f"/api/{V}/auth/signin", content=(
            f'<tsRequest><credentials {creds}><site contentUrl="" /></credentials>'
            f"</tsRequest>"))
        assert r.status_code == 401 and b'code="401001"' in r.content, creds
    r = c.post("/signin", data={"username": "alkera", "password": native_context.DEV_TOKEN})
    assert r.status_code == 401 and "Invalid username or password." in r.text
    assert ui.COOKIE not in c.cookies
    _signin(c)
    # A browser session is not an API session...
    r = api.get(f"/api/{V}/sites", headers={"X-Tableau-Auth": c.cookies[ui.COOKIE]})
    assert r.status_code == 401
    # ...and an API session is not a browser session.
    rest = _Rest(api)
    other = TestClient(c.app, base_url="http://tableau.internal:8096")
    other.cookies.set(ui.COOKIE, rest.headers["X-Tableau-Auth"])
    r = other.get("/workbooks", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/signin")
