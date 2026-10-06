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
  - one audit row per request, named `tableau.ui_*`.
"""
from __future__ import annotations

import csv
import io
import json
import sys
import xml.etree.ElementTree as ET
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
from world.semantic.formats import tableau as tableau_format  # noqa: E402

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
                                     "password": native_context.DEV_TOKEN,
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

    def data(self, view_id: str) -> list[list[str]]:
        return list(csv.reader(io.StringIO(self.get(f"views/{view_id}/data").text)))


class _Tables(HTMLParser):
    """`<table class="summary" data-view=...>` -> {view: [[header...], [cell...]...]}."""

    def __init__(self):
        super().__init__()
        self.tables: dict[str, list[list[str]]] = {}
        self._cur = self._row = self._cell = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table" and a.get("class") == "summary":
            self._cur = self.tables.setdefault(a["data-view"], [])
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


def _published(env) -> tuple[dict, Path]:
    root = Path(env["config"].bi_state_dir) / "tableau"
    state = json.loads((root / "manifest.json").read_text("utf-8"))
    (ds,) = state["datasources"]
    return ds, root / "content" / f"{ds['id']}.{ds['file_type']}"


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
    one("tableau.ui_signout", lambda: c.post("/signout", follow_redirects=False))


def test_mounted_in_the_vendor_app_the_ui_and_api_share_one_origin(env):
    app = tableau.app
    routes = list(app.router.routes)
    try:
        ui.mount(app)
        c = env["rest"]
        _signin(c)
        assert "Executive Overview" in c.get("/workbooks").text
        assert c.get("/api/serverinfo").status_code == 200
        refused = c.get(f"/api/{V}/sites")                 # the API's own refusals intact
        assert refused.status_code == 401 and b"<tsResponse" in refused.content
    finally:
        app.router.routes[:] = routes
