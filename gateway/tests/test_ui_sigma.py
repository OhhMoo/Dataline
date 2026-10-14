"""The Sigma UI clone (native/ui/sigma.py), mounted in the fake Sigma app.

The UI is one more client of the fake Sigma service's published state, so
every check here is against the REST API v2 on the same state:
  - a page's element tables are, cell for cell and byte for byte, the CSV an
    element export downloads (`POST /v2/workbooks/{id}/export`), and each
    workbook's page is at the path of the `url` the API returns for it;
  - a column formula published in the browser is what `GET ?includeContents`
    then serves - one formula changed, the version moved - and moves the
    export; a refused or stale one publishes nothing;
  - one audit row per request, named `sigma.ui_*`;
  - unknown API routes are still `sigma.unknown_route` 404s with the UI
    mounted, and the UI password and the API credentials never stand in for
    each other.
"""
from __future__ import annotations

import base64
import copy
import csv
import io
import json
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlsplit

import duckdb
import pytest
from conftest import make_world, world_hash
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "world") not in sys.path:
    sys.path.insert(0, str(REPO / "world"))

from world.semantic import examples as core  # noqa: E402
from world.semantic.expr import Col, MeasureRef  # noqa: E402
from world.semantic.formats import sigma as sigma_format  # noqa: E402
from world.semantic.formats.sigma import _common as common  # noqa: E402
from world.semantic.formats.sigma import examples as sx  # noqa: E402
from world.semantic.model import Page, Report, Visual  # noqa: E402

from alkera_gateway import GatewayConfig  # noqa: E402
from alkera_gateway.native import context as native_context  # noqa: E402
from alkera_gateway.native.services import sigma  # noqa: E402
from alkera_gateway.native.ui import html as H  # noqa: E402
from alkera_gateway.native.ui import sigma as ui  # noqa: E402

SECRET = native_context.DEV_TOKEN
EMAIL = "analyst@alkera.example"
OPS_REVIEW = Report("Ops Review", "Commerce", (Page("Orders", (
    Visual("Orders by Region", group_by=(Col("Customer", "region"),),
           values=(("Orders", MeasureRef("Order Count")),), kind="table", groups="rows"),)),))


class Env:
    """One world served by the Sigma app: a browser and an API client on it."""

    def __init__(self, root: Path, audit_file: Path, warehouse: Path, audit: Path):
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
        self.ui = TestClient(sigma.app, base_url="http://sigma.internal:8097")
        self.api = TestClient(sigma.app, base_url="http://sigma.internal:8097")
        basic = base64.b64encode(f"ci-client:{SECRET}".encode()).decode()
        r = self.api.post("/v2/auth/token", data={"grant_type": "client_credentials"},
                          headers={"Authorization": f"Basic {basic}"})
        assert r.status_code == 200, r.text
        self.token = r.json()["access_token"]

    def call(self, method: str, path: str, **kw):
        headers = {"Authorization": f"Bearer {self.token}", **kw.pop("headers", {})}
        return self.api.request(method, path, headers=headers, **kw)

    def ok(self, method: str, path: str, **kw):
        r = self.call(method, path, **kw)
        assert r.status_code == 200, r.text
        return r.json()

    def workbook(self, workbook_id: str) -> dict:
        return self.ok("GET", f"/v2/workbooks/{workbook_id}", params={"includeContents": "true"})

    def export(self, workbook_id: str, element_id: str) -> str:
        job = self.ok("POST", f"/v2/workbooks/{workbook_id}/export",
                      json={"elementId": element_id, "format": {"type": "csv"}})
        r = self.call("GET", f"/v2/query/{job['queryId']}/download")
        assert r.status_code == 200, r.text
        return r.text

    def rows(self) -> list[dict]:
        if not self.audit_file.exists():
            return []
        return [json.loads(line) for line in self.audit_file.read_text("utf-8").splitlines()
                if line]


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = make_world(tmp_path / "world")
    warehouse = core.write_fixture(root / "var" / "warehouse" / "commerce.duckdb")
    sigma_format.write_project(sx.commerce_model(), (sx.executive_report(), OPS_REVIEW),
                               root / "workspace" / "bi" / "sigma")
    audit_file = tmp_path / "native-actions.jsonl"
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(audit_file))
    try:
        yield Env(root, audit_file, warehouse, tmp_path / "audit")
    finally:
        del sigma.app.state.alkera_context


def repo_doc(env: Env, kind: str, name: str) -> dict:
    return json.loads((env.root / "workspace" / "bi" / "sigma" / kind / f"{name}.json")
                      .read_text("utf-8"))


def overview(env: Env) -> tuple[str, str, dict]:
    """(workbook id, its page in the browser, its published GET body)."""
    doc = repo_doc(env, "workbooks", "executive-overview")
    return doc["workbookId"], urlsplit(doc["url"]).path, env.workbook(doc["workbookId"])


def element(doc: dict, name: str) -> dict:
    return next(e for e in doc["contents"]["elements"] if e["name"] == name)


def column(el: dict, name: str) -> dict:
    return next(c for c in el["columns"] if c.get("name") == name)


def sign_in(client, redirect: str = "/alkera") -> None:
    r = client.post("/alkera/login", data={"email": EMAIL, "password": sigma.UI_PASSWORD,
                                           "redirect": redirect}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == redirect, r.text
    assert ui.COOKIE in r.cookies


class _Tables(HTMLParser):
    """`<table class="summary" data-element=...>` -> {element id: [[cell, ...], ...]}."""

    def __init__(self):
        super().__init__()
        self.tables: dict[str, list[list[str]]] = {}
        self._cur = self._row = self._cell = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table" and a.get("class") == "summary":
            self._cur = self.tables.setdefault(a["data-element"], [])
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


def as_csv(cells: list[list[str]]) -> str:
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows(cells)
    return out.getvalue()


def ui_mounts(routes) -> int:
    """How many times an app's routes hold the UI: as one included router
    (FastAPI 0.141) or as its routes copied in (older FastAPI)."""
    return sum(1 for r in routes if getattr(r, "original_router", None) is ui.router
               or getattr(r, "endpoint", None) is ui.workbook)


def editor_path(base: str, el: dict, col: dict) -> str:
    return f"{base}/element/{el['id']}/column/{col['id']}"


# ======================================================================
def test_signed_out_pages_redirect_to_sign_in(env):
    _wid, base, _doc = overview(env)
    c = env.ui
    for path in (base, "/alkera", "/alkera/data-model/anything"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"] == f"/alkera/login?redirect={quote(path, safe='/')}"
    page = c.get("/alkera/login", params={"redirect": base})
    assert page.status_code == 200 and 'id="login"' in page.text
    r = c.post("/alkera/login", data={"email": EMAIL, "password": "hunter2"})
    assert r.status_code == 401 and "Invalid email or password" in r.text
    assert 'role="alert"' in r.text and ui.COOKIE not in c.cookies
    assert c.get(base, follow_redirects=False).status_code == 303
    sign_in(c, redirect=base)                       # back where the reader started
    assert c.get(base).status_code == 200
    c.post("/alkera/logout", follow_redirects=False)
    assert c.get(base, follow_redirects=False).status_code == 303


def test_element_table_equals_the_api_export_csv(env):
    c = env.ui
    sign_in(c)
    home = c.get("/alkera")
    assert home.status_code == 200
    checked = 0
    for meta in env.ok("GET", "/v2/workbooks")["entries"]:
        wid = meta["workbookId"]
        base = urlsplit(meta["url"]).path                  # the API's own url, on this host
        assert base.startswith("/alkera/workbook/") and f'href="{base}"' in home.text
        pages = env.ok("GET", f"/v2/workbooks/{wid}/pages")["entries"]
        first = c.get(base)
        assert first.status_code == 200, first.text
        for i, p in enumerate(pages):
            page = c.get(f"{base}/page/{p['pageId']}")
            assert page.status_code == 200, page.text
            shown = tables(page.text)
            if i == 0:
                assert tables(first.text) == shown         # the workbook opens on page 1
            placed = env.ok("GET", f"/v2/workbooks/{wid}/pages/{p['pageId']}/elements")
            assert list(shown) == [e["elementId"] for e in placed["entries"]]
            for e in placed["entries"]:
                exported = env.export(wid, e["elementId"])
                assert shown[e["elementId"]] == list(csv.reader(io.StringIO(exported))), e
                assert as_csv(shown[e["elementId"]]) == exported
                checked += 1
    assert checked == 6                                     # 5 + Ops Review's 1
    _wid, base, doc = overview(env)
    page = c.get(base).text
    assert 'data-kind="line"' in page and 'data-kind="bar"' in page
    assert "<script" not in page
    month = element(doc, "Revenue by Month")
    assert f'href="{editor_path(base, month, column(month, "Net Revenue"))}"' in page


def test_a_formula_saved_in_the_browser_is_what_the_api_serves(env):
    c = env.ui
    sign_in(c)
    wid, base, doc = overview(env)
    kpis = element(doc, "KPIs")
    net = column(kpis, "Net Revenue")
    before = list(csv.reader(io.StringIO(env.export(wid, kpis["id"]))))
    world = world_hash(env.root)
    with duckdb.connect() as con:
        con.execute(f"ATTACH '{env.warehouse}' AS wh (READ_ONLY)")
        gross, completed = con.execute(
            "SELECT sum(order_amount), sum(order_amount) FILTER (order_status = 'completed') "
            "FROM wh.warehouse.fct_orders").fetchone()
    assert before[1][0] == f"{completed:.2f}" != f"{gross:.2f}"

    path = editor_path(base, kpis, net)
    editor = c.get(path)
    assert editor.status_code == 200
    assert "[Metrics/Net Revenue]" in editor.text and 'id="formula-editor"' in editor.text
    assert 'name="documentVersion" value="1"' in editor.text
    r = c.post(path, data={"formula": "[Metrics/Gross Revenue]", "documentVersion": "1"},
               follow_redirects=False)
    assert r.status_code == 303, r.text
    assert "Published version 2." in c.get(r.headers["location"]).text

    after = env.workbook(wid)
    assert (after["latestVersion"], after["documentVersion"]) == (2, 2)
    expected = copy.deepcopy(doc["contents"])
    column(element({"contents": expected}, "KPIs"), "Net Revenue")["formula"] = \
        "[Metrics/Gross Revenue]"
    assert after["contents"] == expected                   # that one formula, nothing else
    history = env.ok("GET", f"/v2/workbooks/{wid}/version-history")["entries"]
    assert [h["version"] for h in history] == [2, 1]
    moved = list(csv.reader(io.StringIO(env.export(wid, kpis["id"]))))
    assert moved[0] == before[0] and moved[1][0] == f"{gross:.2f}"
    assert moved[1][1:] == before[1][1:]
    assert tables(c.get(base).text)[kpis["id"]] == moved   # and the page shows it
    assert world_hash(env.root) == world                   # the repo is not the server
    row = [r for r in env.rows() if r["tool"] == "sigma.ui_formula_save"][-1]
    assert (row["op_class"], row["targets"], row["exit"]) == (
        "write", [f"sigma:workbook:{wid}"], "ok")


@pytest.mark.parametrize("formula,message", [
    ("Sum([Orders/no_such_column])", "'Orders' has no column 'no_such_column'"),
    ("CumulativeSum([Orders/order_amount])", "unsupported (Sigma): CumulativeSum"),
    ("Sum([Orders/order_amount]", "expected ')'"),
])
def test_refused_formula_is_not_published(env, formula, message):
    c = env.ui
    sign_in(c)
    wid, base, doc = overview(env)
    kpis = element(doc, "KPIs")
    r = c.post(editor_path(base, kpis, column(kpis, "Net Revenue")),
               data={"formula": formula, "documentVersion": "1"})
    assert r.status_code == 400
    assert H.esc(message) in r.text, r.text
    assert 'role="alert"' in r.text and 'id="formula-editor"' in r.text
    assert H.esc(formula) in r.text                         # what was typed is kept
    after = env.workbook(wid)
    assert after["latestVersion"] == 1 and after["contents"] == doc["contents"]
    row = [r for r in env.rows() if r["tool"] == "sigma.ui_formula_save"][-1]
    assert (row["exit"], row["op_class"]) == ("error", "read")


def test_stale_document_version_is_a_conflict_page(env):
    c = env.ui
    sign_in(c)
    wid, base, doc = overview(env)
    kpis = element(doc, "KPIs")
    path = editor_path(base, kpis, column(kpis, "Net Revenue"))
    assert 'name="documentVersion" value="1"' in c.get(path).text
    assert env.ok("PUT", f"/v2/workbooks/{wid}/contents", json=doc)["latestVersion"] == 2
    r = c.post(path, data={"formula": "[Metrics/Gross Revenue]", "documentVersion": "1"})
    assert r.status_code == 409
    assert "Workbook has changed since version 1" in r.text and 'role="alert"' in r.text
    assert 'id="formula-editor"' in r.text
    after = env.workbook(wid)
    assert after["latestVersion"] == 2
    assert column(element(after, "KPIs"), "Net Revenue")["formula"] == "[Metrics/Net Revenue]"
    row = [r for r in env.rows() if r["tool"] == "sigma.ui_formula_save"][-1]
    assert row["exit"] == "error" and row["error"]["code"] == "conflict"


def test_one_audit_row_per_request(env):
    c = env.ui
    wid, base, doc = overview(env)
    kpis = element(doc, "KPIs")
    path = editor_path(base, kpis, column(kpis, "Net Revenue"))
    page_id = doc["contents"]["pages"][1]["id"]
    data_model = urlsplit(repo_doc(env, "data-models", "commerce")["url"]).path

    def one(tool, call, exit="ok"):
        n = len(env.rows())
        call()
        new = env.rows()[n:]
        assert [(r["tool"], r["exit"]) for r in new] == [(tool, exit)], new
        return new[0]

    one("sigma.ui_workbook", lambda: c.get(base, follow_redirects=False), exit="error")
    one("sigma.ui_login_page", lambda: c.get("/alkera/login"))
    one("sigma.ui_login", lambda: sign_in(c))
    one("sigma.ui_home", lambda: c.get("/alkera"))
    view = one("sigma.ui_workbook", lambda: c.get(base))
    assert view["targets"] == [f"sigma:workbook:{wid}"]
    assert "warehouse.fct_orders" in view["read_targets"]
    assert f"actor:{EMAIL}" in view["flags"]
    one("sigma.ui_workbook", lambda: c.get(f"{base}/page/{page_id}"))
    one("sigma.ui_formula_edit", lambda: c.get(path))
    save = one("sigma.ui_formula_save", lambda: c.post(
        path, data={"formula": "[Metrics/Gross Revenue]", "documentVersion": "1"},
        follow_redirects=False))
    assert (save["op_class"], save["targets"]) == ("write", [f"sigma:workbook:{wid}"])
    assert save["args"]["formula"] == "[Metrics/Gross Revenue]"
    dm = one("sigma.ui_data_model", lambda: c.get(data_model))
    assert dm["targets"][0].startswith("sigma:data-model:")
    one("sigma.ui_workbook", lambda: c.get(f"{base}/page/nope"), exit="error")
    one("sigma.ui_formula_edit", lambda: c.get(f"{base}/element/nope/column/x"), exit="error")
    one("sigma.ui_logout", lambda: c.post("/alkera/logout", follow_redirects=False))
    assert all(r["tool"].startswith("sigma.") and r["schema_version"] == 1
               for r in env.rows())


def test_unknown_api_routes_still_404_after_ui_mount(env):
    """The UI is mounted when the service module is imported, and the
    service's `/{path:path}` fallback is still the last route: a path the UI
    does not serve - under /v2 or not, shaped like a UI page or not - is
    Sigma's 404 with one `sigma.unknown_route` row, as before."""
    routes = sigma.app.router.routes
    assert routes[-1].endpoint is sigma.not_found
    assert [getattr(r, "endpoint", None) for r in routes].count(sigma.not_found) == 1
    assert ui_mounts(routes) == 1
    wid, base, doc = overview(env)
    eid = doc["contents"]["elements"][0]["id"]
    ref = base.rsplit("/", 1)[-1]
    for method, path in (("GET", "/v2/no-such"), ("GET", f"/v2/workbooks/{wid}/contents"),
                         ("GET", f"/v2/workbooks/{wid}/elements/{eid}/export"),
                         ("GET", f"/v2/workbook/{ref}"), ("GET", f"/acme/workbook/{ref}"),
                         ("GET", "/alkera/no-such"), ("DELETE", base),
                         ("PUT", "/alkera/login")):
        n = len(env.rows())
        r = env.call(method, path)
        assert r.status_code == 404, (method, path, r.text)
        assert set(r.json()) == {"message", "code", "requestId"} and \
            r.json()["code"] == "not_found", path
        assert [row["tool"] for row in env.rows()[n:]] == ["sigma.unknown_route"], path
    assert env.ok("GET", f"/v2/workbooks/{wid}")["workbookId"] == wid   # the API is intact


def test_ui_password_is_not_an_api_token(env):
    assert sigma.UI_PASSWORD != SECRET
    form = {"grant_type": "client_credentials", "client_id": "ci",
            "client_secret": sigma.UI_PASSWORD}
    assert env.api.post("/v2/auth/token", data=form).status_code == 401
    basic = base64.b64encode(f"{EMAIL}:{sigma.UI_PASSWORD}".encode()).decode()
    assert env.api.post("/v2/auth/token", data={"grant_type": "client_credentials"},
                        headers={"Authorization": f"Basic {basic}"}).status_code == 401
    c = env.ui
    r = c.post("/alkera/login", data={"email": EMAIL, "password": SECRET})
    assert r.status_code == 401 and ui.COOKIE not in c.cookies   # an API secret is no password
    c.cookies.set(ui.COOKIE, env.token, domain="sigma.internal")  # nor a bearer token a session
    assert c.get("/alkera", follow_redirects=False).status_code == 303
    c.cookies.clear()
    sign_in(c)
    session = c.cookies[ui.COOKIE]
    r = env.api.get("/v2/workbooks", headers={"Authorization": f"Bearer {session}"})
    assert r.status_code == 401 and r.json()["code"] == "unauthorized"
    assert env.api.get("/v2/workbooks",
                       headers={"Cookie": f"{ui.COOKIE}={session}"}).status_code == 401
    rows = json.dumps(env.rows())
    assert sigma.UI_PASSWORD not in rows                 # passwords are never logged


def test_data_model_page_shows_the_published_spec(env):
    c = env.ui
    sign_in(c)
    dm = repo_doc(env, "data-models", "commerce")
    path = urlsplit(dm["url"]).path
    _wid, base, _doc = overview(env)
    assert f'href="{path}"' in c.get(base).text           # linked from the workbook
    page = c.get(path)
    assert page.status_code == 200, page.text
    elements = [el for p in dm["pages"] for el in p["elements"]]
    at = [page.text.index(f"<h2>{H.esc(el['name'])}</h2>") for el in elements]
    assert at == sorted(at) and len(at) == 5
    for el in elements:
        for metric in el.get("metrics", []):
            assert f"<code>{H.esc(metric['formula'])}</code>" in page.text, metric
    assert "ANALYTICS.WAREHOUSE.FCT_ORDERS" in page.text and "Version 1" in page.text
    # A data model published through the API is what the page shows next.
    changed = copy.deepcopy(dm)
    orders = next(el for el in changed["pages"][0]["elements"] if el["name"] == "Orders")
    next(m for m in orders["metrics"] if m["name"] == "Net Revenue")["formula"] = \
        "Sum([order_amount])"
    env.ok("PUT", f"/v2/dataModels/{dm['dataModelId']}/spec", json=changed)
    again = c.get(path).text
    assert "<code>Sum([order_amount])</code>" in again and "Version 2" in again


def test_the_ui_mounts_once_whichever_module_is_imported_first():
    probe = ("import sys; sys.path.insert(0, {world!r})\n"
             "import importlib\n"
             "for name in {order!r}: importlib.import_module(name)\n"
             "from alkera_gateway.native.services import sigma\n"
             "from alkera_gateway.native.ui import sigma as ui\n"
             "routes = sigma.app.router.routes\n"
             "assert routes[-1].endpoint is sigma.not_found, routes[-1]\n"
             "mounted = [r for r in routes if getattr(r, 'original_router', None) is ui.router\n"
             "           or getattr(r, 'endpoint', None) is ui.workbook]\n"
             "assert len(mounted) == 1, routes\n")
    for order in (["alkera_gateway.native.ui.sigma"], ["alkera_gateway.native.services.sigma"]):
        code = probe.format(world=str(REPO / "world"), order=order)
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              timeout=120)
        assert done.returncode == 0, (order, done.stderr)


def test_the_org_in_ui_paths_is_the_documents_org():
    assert ui.ORG == common.ORG_SLUG
    assert common.APP_URL.endswith(f"/{ui.ORG}")
