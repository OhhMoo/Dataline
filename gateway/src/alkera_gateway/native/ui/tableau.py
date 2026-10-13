"""Tableau Server's browser UI, over the fake Tableau service's PUBLISHED state.

docs/environments/UI_CLONE_PLAN.md. This is another client of the state
`native/services/tableau.py` serves - not a second server. It reads and
writes through that module's `Store` (same `<bi_state_dir>/tableau/` manifest,
same file lock, same in-process `LOCK`), publishes through
`Store.publish_datasource` / `Store.publish_workbook` exactly as
`POST .../datasources|workbooks?overwrite=true` does, renders a view exactly as
`GET .../views/{id}/data` does (the CSV that endpoint returns is the table this
page shows, cell for cell, URL filters included), and writes one 9.2.2 row per
request through the service's own `Row` (`tableau.ui_*`). The service module
mounts this router into its own app when it is imported.

    GET  /signin, POST /signin, POST /signout     cookie `workgroup_session_id`
    GET  /                                        -> /workbooks
    GET  /projects, /projects/{id}                the site's projects, one's content
    GET  /workbooks                               the site's workbooks
    GET  /workbooks/{id}/views                    a workbook's sheets, sources, calcs
    GET  /views/{workbook}/{sheet}                a sheet or dashboard: tables + charts
    GET  /views/{workbook}/{sheet}.csv            its summary data (Tableau's `.csv` URL)
    GET  /datasources, /datasources/{id}          published data sources, their fields
    GET  /datasources/{id}/calculations/{name}    the calculation editor
    POST /datasources/{id}/calculations/{name}    validate, then save and publish
    GET  /workbooks/{id}/calculations/{name}      a workbook's own calculation
    POST /workbooks/{id}/calculations/{name}      validate, then save and publish

URLS. Tableau Server routes its pages client-side (`/#/workbooks/...`,
`/#/views/<Workbook>/<Sheet>`); a fragment never reaches a server, so the same
paths are served here without the `#/`. `/views/<Workbook>/<Sheet>` and its
`.csv` twin are Tableau Server's own server-side URLs, and take its URL
filters: `?region=EMEA` (or `?region=EMEA,NA`) filters every sheet on the page
by the field Tableau shows as `region`, the REST API's `vf_region=EMEA`;
parameters that start with `:` (`:embed=y`) are display options and are
ignored. Only the Default site exists (as in the REST service), so there is no
`/t/<site>/` prefix.

SIGNING IN. The browser has a password of its own, `services.tableau
.UI_PASSWORD`, with any user name. It is not the REST token: the REST sign-in
refuses it, this page refuses the REST token, and a browser session
(`ui_sessions` in the manifest) is not an `X-Tableau-Auth` token, nor the other
way round. No box env or discovery file carries it; a task arm that uses the
browser hands it out in its own brief.

THE CALCULATION EDITOR shows a formula as Tableau's editor does - fields by
caption (`[Net Revenue] / [Completed Orders]`) - and stores it as Tableau
stores it, by field name (`[Calculation_...]`). A formula is checked with the
front-end's `parse_expression` and then by re-reading the edited document as
the server would once it is published; a refusal shows the front-end's message
verbatim (`unsupported (Tableau): ...` for anything outside the subset) and
publishes nothing. Only that calculation's `formula='...'` attribute changes
in the published document: once in a .tds; in a workbook, in its data source
and in every sheet's copy of it. A packaged file (.tdsx/.twbx) is repacked
with every other member as it was.

WHOSE CALCULATION. A published data source's calculations are edited on its
page and published with it. A workbook's own - a workbook-local calculation
over a published source, or any calculation of a source the workbook embeds -
are edited on the workbook's page and published with the workbook. A
published calculation a workbook only caches is refused there, with a link
to the data source that owns it.

NOT IMITATED. Web authoring of sheets (shelves, marks, drag and drop), new
calculations, filter and parameter controls, subscriptions, favourites,
search, users and permissions, nested projects, images/PDF downloads.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import xml.etree.ElementTree as ET
import zipfile
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from world.semantic import render
from world.semantic.errors import ModelError, SemanticError, Unsupported
from world.semantic.formats.tableau import parse_expression, xmlio
from world.semantic.formats.tableau import project as tproject

from ..services import tableau as svc
from . import html as H

COOKIE = "workgroup_session_id"
router = APIRouter()


class _Redirect(Exception):
    def __init__(self, location: str, code: str = "401000",
                 detail: str = "No authentication credentials were provided."):
        super().__init__(detail)
        self.location, self.code, self.detail = location, code, detail


class _Elsewhere(svc.TableauError):
    """A refusal that names the page that can do it (and links to it)."""

    def __init__(self, detail: str, href: str, label: str):
        super().__init__(400, "400000", "Bad Request", detail)
        self.href, self.label = href, label


# ==================================================================== plumbing
def _refused(exc: svc.TableauError, session: dict | None) -> HTMLResponse:
    body = H.error_box(f"{exc.summary}: {exc.detail}")
    if isinstance(exc, _Elsewhere):
        body += f'<p><a href="{H.esc(exc.href)}">{H.esc(exc.label)}</a></p>'
    return _html(exc.summary, body, session, status=exc.status)


def _serve(request: Request, operation: str, call, *, op_class: str = "read",
           args: dict | None = None) -> Response:
    """One audited request: `call(row)` returns a Response or raises; refusals
    become an HTML page (and an error row), never XML."""
    try:
        with svc.Row(request, operation, op_class=op_class, args=args) as row:
            return call(row)
    except _Redirect as r:
        return RedirectResponse(r.location, status_code=303)
    except svc.TableauError as exc:
        return _refused(exc, None)


def _session(request: Request, row) -> tuple[svc.Store, dict, dict]:
    """The signed-in browser session (cookie) and the published state, or a
    redirect to the sign-in page that comes back here."""
    with svc.LOCK:
        store = svc.Store(request)
        with store.lock():
            state = store.load()
    session = state.get("ui_sessions", {}).get(request.cookies.get(COOKIE) or "")
    if session is None:
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise _Redirect(f"/signin?redirect={quote(target, safe='/')}")
    row.args["user"] = session["user"]
    return store, state, session


def _header(session: dict | None) -> str:
    who = ""
    if session is not None:
        who = (f'<form method="post" action="/signout"><span>{H.esc(session["user"])}</span> '
               f'<button type="submit">Sign Out</button></form>')
    return (f'<header><a class="brand" href="/workbooks">Tableau Server (Alkera)</a>'
            f'<a href="/projects">Projects</a><a href="/workbooks">Workbooks</a>'
            f'<a href="/datasources">Data Sources</a>{who}</header>')


def _html(title: str, body: str, session: dict | None, crumbs=(), status: int = 200
          ) -> HTMLResponse:
    return HTMLResponse(H.page(title, body, header=_header(session), crumbs=list(crumbs)),
                        status_code=status)


async def _form(request: Request) -> dict[str, str]:
    raw = await request.body()
    return {k: v[0] for k, v in parse_qs(raw.decode("utf-8"), keep_blank_values=True).items()}


def _safe_redirect(target: str | None) -> str:
    return target if target and target.startswith("/") and not target.startswith("//") \
        else "/workbooks"


# ===================================================================== sign in
def _signin_form(redirect: str, error: str = "") -> str:
    return ("<h1>Sign In</h1>" + (H.error_box(error) if error else "") +
            '<form id="signin" method="post" action="/signin">'
            f'<input type="hidden" name="redirect" value="{H.esc(redirect)}">'
            '<p><label>Username <input name="username" autocomplete="username"></label></p>'
            '<p><label>Password <input name="password" type="password" '
            'autocomplete="current-password"></label></p>'
            '<p><button type="submit">Sign In</button></p></form>')


@router.get("/signin")
def signin_page(request: Request) -> Response:
    redirect = _safe_redirect(request.query_params.get("redirect"))

    def call(row):
        row.targets = [f"{svc.NAME}:session"]
        return _html("Sign In", _signin_form(redirect), None)
    return _serve(request, "ui_signin_page", call)


@router.post("/signin")
async def signin(request: Request) -> Response:
    form = await _form(request)
    user = form.get("username", "").strip() or svc.DEFAULT_USER
    redirect = _safe_redirect(form.get("redirect"))

    def call(row):
        row.targets = [f"{svc.NAME}:session"]
        row.args.update({"user": user, "method_kind": "password"})
        if form.get("password") != svc.UI_PASSWORD:
            raise svc.TableauError(401, "401001", "Signin Error",
                                   "Invalid username or password.")
        with svc.LOCK:
            store = svc.Store(request)
            with store.lock():
                state = store.load()
                n = store.next(state)
                token = hashlib.sha256(f"ui-session/{n}/{user}".encode()).hexdigest()[:32]
                # Browser sessions are kept apart from the REST API's
                # `sessions`, so neither credential opens the other door.
                state.setdefault("ui_sessions", {})[token] = {
                    "user": user, "user_id": svc.uid("user", user), "site_id": svc.SITE_ID}
                store.save(state)
        row.result = {"user": user, "site": svc.SITE_ID}
        resp = RedirectResponse(redirect, status_code=303)
        resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", path="/")
        return resp

    try:
        with svc.Row(request, "ui_signin", op_class="read") as row:
            return call(row)
    except svc.TableauError as exc:
        return _html("Sign In", _signin_form(redirect, exc.detail), None, status=exc.status)


@router.post("/signout")
def signout(request: Request) -> Response:
    def call(row):
        row.targets = [f"{svc.NAME}:session"]
        with svc.LOCK:
            store = svc.Store(request)
            with store.lock():
                state = store.load()
                state.get("ui_sessions", {}).pop(request.cookies.get(COOKIE) or "", None)
                store.save(state)
        resp = RedirectResponse("/signin", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp
    return _serve(request, "ui_signout", call)


# ================================================================== browsing
@router.get("/")
def home(request: Request) -> Response:
    def call(row):
        _session(request, row)
        return RedirectResponse("/workbooks", status_code=303)
    return _serve(request, "ui_home", call)


def _workbook_rows(workbooks: list[dict]) -> str:
    items = "".join(
        f'<tr><td><a href="/workbooks/{w["id"]}/views">{H.esc(w["name"])}</a></td>'
        f'<td>{H.esc(svc.project_of(w)["name"])}</td><td>{len(w["views"])}</td>'
        f'<td>{H.esc(w["updated_at"])}</td></tr>' for w in workbooks)
    return ('<table class="content-list" data-list="workbooks"><thead><tr><th>Name</th>'
            '<th>Project</th><th>Views</th><th>Modified</th></tr></thead>'
            f"<tbody>{items}</tbody></table>")


def _datasource_rows(datasources: list[dict]) -> str:
    items = "".join(
        f'<tr><td><a href="/datasources/{d["id"]}">{H.esc(d["name"])}</a></td>'
        f'<td>{H.esc(d["type"])}</td><td>{"Extract" if d.get("has_extract") else "Live"}'
        f'</td><td>{H.esc(d["updated_at"])}</td></tr>' for d in datasources)
    return ('<table class="content-list" data-list="datasources"><thead><tr><th>Name</th>'
            '<th>Connection</th><th>Live/Extract</th><th>Modified</th></tr></thead>'
            f"<tbody>{items}</tbody></table>")


@router.get("/projects")
def projects(request: Request) -> Response:
    def call(row):
        _, state, session = _session(request, row)
        listed = svc.projects()
        row.targets = [f"{svc.NAME}:project:{p['name']}" for p in listed]
        items = "".join(
            f'<tr><td><a href="/projects/{p["id"]}">{H.esc(p["name"])}</a></td>'
            f'<td>{H.esc(p["description"])}</td>'
            f'<td>{sum(w["project_id"] == p["id"] for w in state["workbooks"])}</td>'
            f'<td>{sum(d["project_id"] == p["id"] for d in state["datasources"])}</td></tr>'
            for p in listed)
        body = ('<h1>Projects</h1><table class="content-list" data-list="projects"><thead><tr>'
                '<th>Name</th><th>Description</th><th>Workbooks</th><th>Data Sources</th>'
                f"</tr></thead><tbody>{items}</tbody></table>")
        return _html("Projects", body, session, [("Default", "/workbooks"), ("Projects", None)])
    return _serve(request, "ui_projects", call)


@router.get("/projects/{project_id}")
def project(project_id: str, request: Request) -> Response:
    def call(row):
        _, state, session = _session(request, row)
        p = svc.find(svc.projects(), project_id, "Project", "404005")
        row.targets = [f"{svc.NAME}:project:{p['name']}"]
        body = (f'<h1>{H.esc(p["name"])}</h1><p>{H.esc(p["description"])}</p>'
                "<h2>Workbooks</h2>" + _workbook_rows(
                    [w for w in state["workbooks"] if w["project_id"] == p["id"]])
                + "<h2>Data Sources</h2>" + _datasource_rows(
                    [d for d in state["datasources"] if d["project_id"] == p["id"]]))
        return _html(p["name"], body, session, [("Default", "/workbooks"),
                                                ("Projects", "/projects"), (p["name"], None)])
    return _serve(request, "ui_project", call)


@router.get("/workbooks")
def workbooks(request: Request) -> Response:
    def call(row):
        _, state, session = _session(request, row)
        row.targets = [f"{svc.NAME}:workbook:{w['content_url']}" for w in state["workbooks"]] \
            or [f"{svc.NAME}:workbooks"]
        body = "<h1>Workbooks</h1>" + _workbook_rows(state["workbooks"])
        return _html("Workbooks", body, session, [("Default", "/workbooks"), ("Workbooks", None)])
    return _serve(request, "ui_workbooks", call)


def _workbook(state: dict, wb_id: str) -> dict:
    return svc.find(state["workbooks"], wb_id, "Workbook", "404006")


def _view_url(view: dict) -> str:
    wb_url, _, sheet = view["content_url"].partition("/sheets/")
    return f"/views/{wb_url}/{sheet}"


@router.get("/workbooks/{wb_id}/views")
def workbook_views(wb_id: str, request: Request) -> Response:
    def call(row):
        store, state, session = _session(request, row)
        wb = _workbook(state, wb_id)
        row.targets = [f"{svc.NAME}:workbook:{wb['content_url']}"]
        items = "".join(
            f'<li><a href="{_view_url(v)}">{H.esc(v["name"])}</a> '
            f'<small>({H.esc(v["sheet_type"])})</small></li>' for v in wb["views"])
        sources = "".join(_connection_html(state, c) for c in wb["connections"])
        body = (f'<h1>{H.esc(wb["name"])}</h1><h2>Views</h2><ul class="views">{items}</ul>'
                f'<h2>Data Sources</h2><ul>{sources}</ul><h2>Calculated Fields</h2>')
        try:
            ds, _, _ = _read_workbook(store, state, wb)
        except svc.TableauError as exc:
            body += H.error_box(f"{exc.summary}: {exc.detail}")
        else:
            own, published = _workbook_calcs(store, state, wb, ds)
            if published is not None:
                body += (f'<p>The calculations of <a href="/datasources/{published["id"]}">'
                         f'{H.esc(published["name"])}</a> are edited there; these are this '
                         f"workbook's own.</p>")
            body += _calc_table(
                own, ds, lambda name: f"/workbooks/{wb['id']}/calculations/{quote(name)}")
        return _html(wb["name"], body, session, [("Default", "/workbooks"),
                                                  (wb["name"], None)])
    return _serve(request, "ui_workbook", call)


def _connection_html(state: dict, conn: dict) -> str:
    """One of a workbook's data sources: the published one it connects to (a
    link), or the one it embeds."""
    published = conn["published"] and next(
        (d for d in state["datasources"] if conn["published"] in (d["content_url"], d["name"])),
        None)
    if published:
        return (f'<li><a href="/datasources/{published["id"]}">{H.esc(published["name"])}</a> '
                f"<small>(published data source)</small></li>")
    return f'<li>{H.esc(conn["name"])} <small>(embedded, {H.esc(conn["class"])})</small></li>'


# ===================================================================== views
def _find_view(state: dict, wb_url: str, sheet_url: str) -> tuple[dict, dict]:
    key = f"{wb_url}/sheets/{sheet_url}"
    for v, wb in svc.all_views(state):
        if v["content_url"] == key:
            return v, wb
    raise svc.not_found("View", "404011", f"{wb_url}/{sheet_url}")


def _read_workbook(store, state, wb, data: bytes | None = None):
    """(data source, report, deferred sheets) of the published workbook - or of
    `data` in its place - as Query View Data reads them."""
    deferred: dict = {}
    try:
        ds, report = store.read_workbook(state, wb, deferred, data=data)
    except (ModelError, SemanticError) as exc:
        raise svc.TableauError(400, "400000", "Bad Request",
                               f"{type(exc).__name__}: {exc}") from None
    return ds, report, deferred


def _sheets_of(report, view: dict) -> list:
    """The worksheets a view shows: itself, or a dashboard's zones in order."""
    if view["sheet_type"] == "worksheet":
        return [svc.visual_for(report, view["name"], "worksheet")]
    for page in report.pages:
        if page.name == view["name"]:
            return list(page.visuals)
    raise svc.TableauError(400, "400000", "Bad Request", f"View '{view['name']}' has no data.")


def _render(store, state, wb, ds, visual):
    """(csv bytes, raw result) exactly as Query View Data computes them."""
    owner = svc.extract_owner(state, wb, ds)
    warehouse, refreshed = store.warehouse_for(state, owner)
    result = render.render_visual(ds.model, visual, warehouse)
    return svc.view_csv(ds.model, visual, result), result, refreshed


def _sheet_html(view_name: str, visual, cells: list[list[str]], result: dict) -> str:
    columns, rows = cells[0], cells[1:]
    k = len(visual.group_by)
    out = [f'<section class="sheet" data-sheet="{H.esc(visual.name)}">'
           f"<h2>{H.esc(visual.name)}</h2>"]
    if visual.kind == "card" or k == 0:
        out.append('<div class="kpis">' + "".join(
            f'<div class="kpi"><div class="l">{H.esc(c)}</div><div class="v">{H.esc(v)}</div>'
            f"</div>" for c, v in zip(columns, rows[0] if rows else [], strict=False))
            + "</div>")
    elif visual.kind in ("barChart", "lineChart", "areaChart") and rows:
        labels = [" / ".join(r[:k]) for r in rows]
        chart = H.line_chart if visual.kind != "barChart" else H.bar_chart
        for j in range(k, min(len(columns), k + 2)):
            out.append(chart(labels, [r[j] for r in result["rows"]], [r[j] for r in rows],
                             title=f"{visual.name}: {columns[j]}"))
    out.append(H.table(columns, rows, numeric_from=k,
                       attrs={"class": "summary", "data-view": visual.name}))
    out.append("</section>")
    return "".join(out)


def _url_filters(request: Request) -> dict[str, str]:
    """A view URL's filters (`?region=EMEA`): every parameter but Tableau's
    own `:`-prefixed display options."""
    return {k: v for k, v in request.query_params.items() if not k.startswith(":")}


def _filters_html(vf: dict[str, str], fields: dict) -> str:
    applied = [f"{k} = {v}" for k, v in vf.items() if k in fields]
    ignored = [k for k in vf if k not in fields]
    out = ""
    if applied:
        out += f'<p class="filters">Filters: {H.esc("; ".join(applied))}</p>'
    if ignored:
        out += (f'<p class="filters-ignored">URL filters not applied (no field of that name '
                f'in this view\'s data source): {H.esc(", ".join(ignored))}</p>')
    return out


@router.get("/views/{wb_url}/{sheet_url}")
def view(wb_url: str, sheet_url: str, request: Request) -> Response:
    as_csv = sheet_url.endswith(".csv")
    sheet_url = sheet_url.removesuffix(".csv")
    vf = _url_filters(request)
    query = f"?{request.url.query}" if request.url.query else ""

    def call(row):
        store, state, session = _session(request, row)
        v, wb = _find_view(state, wb_url, sheet_url)
        row.targets = [f"{svc.NAME}:view:{v['content_url']}"]
        row.args["view"] = v["content_url"]
        ds, report, deferred = _read_workbook(store, state, wb)
        captions = store.field_captions(state, wb, ds) if vf else {}
        if vf:
            row.args["filters"] = vf
        visuals = [svc.with_url_filters(ds, visual, vf, captions)
                   for visual in (_sheets_of(report, v) if v["name"] not in deferred else [])]
        if as_csv:
            # Tableau Server's `<view>.csv`: the first sheet's summary data,
            # byte for byte what the REST endpoint returns.
            if v["name"] in deferred or not visuals:
                raise svc.TableauError(400, "400000", "Bad Request",
                                       str(deferred.get(v["name"], "no data")))
            try:
                body, _, refreshed = _render(store, state, wb, ds, visuals[0])
            except SemanticError as exc:
                raise svc.TableauError(400, "400000", "Bad Request", str(exc)) from None
            row.read_targets = svc.visual_relations(ds.model, visuals[0])
            row.result = {"sha256": hashlib.sha256(body).hexdigest()}
            return Response(body, media_type="text/csv; charset=utf-8")
        sections, read, shown = [], set(), {}
        if v["name"] in deferred:
            sections.append(H.error_box(str(deferred[v["name"]])))
        for visual in visuals:
            try:
                body, result, refreshed = _render(store, state, wb, ds, visual)
            except svc.TableauError as exc:
                sections.append(H.error_box(f"{visual.name}: {exc.detail}"))
                continue
            except (Unsupported, ModelError, SemanticError) as exc:
                sections.append(f'<section class="sheet"><h2>{H.esc(visual.name)}</h2>'
                                f"{H.error_box(str(exc))}</section>")
                continue
            if refreshed:
                row.flags.append(f"extract:{refreshed}")
            read.update(svc.visual_relations(ds.model, visual))
            cells = list(csv.reader(io.StringIO(body.decode("utf-8"))))
            shown[visual.name] = hashlib.sha256(body).hexdigest()
            sections.append(_sheet_html(v["name"], visual, cells, result))
        row.read_targets = sorted(read)
        row.result = {"sheets": shown}
        tabs = ""
        if wb.get("show_tabs", True):
            tabs = '<nav class="tabs">' + "".join(
                f'<a href="{_view_url(o)}"{" class=current" if o is v else ""}>'
                f'{H.esc(o["name"])}</a>' for o in wb["views"]) + "</nav>"
        toolbar = (f'<p class="toolbar"><a href="{H.esc(_view_url(v) + query)}">Refresh</a> | '
                   f'<a href="{H.esc(_view_url(v) + ".csv" + query)}">Download Data (CSV)</a>'
                   "</p>")
        filters = _filters_html(vf, svc.url_filter_fields(ds, captions)) if vf else ""
        return _html(v["name"], f'<h1>{H.esc(v["name"])}</h1>{tabs}{toolbar}{filters}'
                     + "".join(sections), session,
                     [("Default", "/workbooks"), (wb["name"], f"/workbooks/{wb['id']}/views"),
                      (v["name"], None)])
    return _serve(request, "ui_view_csv" if as_csv else "ui_view", call,
                  args={"format": "csv"} if as_csv else None)


# =============================================================== data sources
@router.get("/datasources")
def datasources(request: Request) -> Response:
    def call(row):
        _, state, session = _session(request, row)
        row.targets = [f"{svc.NAME}:datasource:{d['content_url']}"
                       for d in state["datasources"]] or [f"{svc.NAME}:datasources"]
        body = "<h1>Data Sources</h1>" + _datasource_rows(state["datasources"])
        return _html("Data Sources", body, session,
                     [("Default", "/workbooks"), ("Data Sources", None)])
    return _serve(request, "ui_datasources", call)


def _published(state: dict, ds_id: str) -> dict:
    return svc.find(state["datasources"], ds_id, "Datasource", "404009")


def _calc_columns(data: bytes, what: str) -> list[ET.Element]:
    root = xmlio.parse(tproject.document_bytes(data, what), what)
    return [c for c in root.findall("column") if c.find("calculation") is not None]


_TOKEN = re.compile(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|//[^\n]*|\[(?:[^\]]|\]\])*\]")


def _rename_fields(formula: str, mapping: dict[str, str]) -> str:
    """Replace `[a]` field tokens per `mapping` - never inside string literals or
    comments."""
    def sub(m):
        tok = m.group(0)
        if tok.startswith("[") and tok[1:-1] in mapping:
            return f"[{mapping[tok[1:-1]]}]"
        return tok
    return _TOKEN.sub(sub, formula)


def _caption_maps(names) -> tuple[dict[str, str], dict[str, str]]:
    """(field name -> caption, caption -> field name) for unambiguous captions."""
    to_cap, to_name = {}, {}
    for cap, ref in names.captions.items():
        if ref is None:
            continue
        try:
            local = names.name_of(ref)
        except ModelError:
            continue
        if local != cap:
            to_cap[local] = cap
            if cap not in names.refs:
                to_name[cap] = local
    return to_cap, to_name


def _calc_table(columns: list[ET.Element], ds, href) -> str:
    """The Calculated Fields table: caption (a link to its editor), formula as
    the editor shows it, and the reason when the server cannot compute it."""
    to_cap, _ = _caption_maps(ds.names)
    rows = []
    for c in columns:
        name = c.get("name")[1:-1]
        formula = _rename_fields(c.find("calculation").get("formula") or "", to_cap)
        status = H.error_box(str(ds.deferred[name])) if name in ds.deferred else ""
        rows.append(f'<tr><td><a href="{href(name)}">{H.esc(c.get("caption") or name)}</a></td>'
                    f"<td><code>{H.esc(formula)}</code>{status}</td></tr>")
    return ('<table class="calculations"><thead><tr><th>Name</th><th>Formula</th></tr>'
            f'</thead><tbody>{"".join(rows)}</tbody></table>')


@router.get("/datasources/{ds_id}")
def datasource(ds_id: str, request: Request) -> Response:
    def call(row):
        store, state, session = _session(request, row)
        item = _published(state, ds_id)
        row.targets = [f"{svc.NAME}:datasource:{item['content_url']}"]
        ds = store.read_datasource(item)
        calcs = _calc_columns(store.content_path(item).read_bytes(),
                              f"{item['name']}.{item['file_type']}")
        fields = "".join(
            f"<tr><td>{H.esc(t.name)}</td><td>{H.esc(col.name)}</td>"
            f"<td>{H.esc(col.data_type)}</td></tr>"
            for t in ds.model.tables for col in t.columns if col.expr is None)
        body = (f'<h1>{H.esc(item["name"])}</h1>'
                f'<p>{"Extract" if item.get("has_extract") else "Live"} connection '
                f'({H.esc(item["type"])})</p><h2>Calculated Fields</h2>'
                + _calc_table(calcs, ds, lambda name: f"/datasources/{item['id']}/calculations/"
                              f"{quote(name)}")
                + '<h2>Fields</h2><table class="fields"><thead><tr><th>Table</th><th>Field</th>'
                f"<th>Type</th></tr></thead><tbody>{fields}</tbody></table>")
        return _html(item["name"], body, session, [("Default", "/workbooks"),
                                                    ("Data Sources", "/datasources"),
                                                    (item["name"], None)])
    return _serve(request, "ui_datasource", call)


def _editor(action: str, back: str, caption: str, formula: str, *, message: str = "",
            error: str = "") -> str:
    return (f'<h1>{H.esc(caption)}</h1>'
            + (f'<div class="ok" role="status">{H.esc(message)}</div>' if message else "")
            + (H.error_box(error) if error else "")
            + f'<form id="calc-editor" method="post" action="{H.esc(action)}">'
            f'<p><label for="formula">Formula</label></p>'
            f'<textarea id="formula" name="formula" rows="6">{H.esc(formula)}</textarea>'
            '<p><button type="submit">Save and Publish</button> '
            f'<a href="{H.esc(back)}">Cancel</a></p></form>')


def _calc(store, item: dict, name: str) -> tuple[ET.Element, object]:
    ds = store.read_datasource(item)
    for c in _calc_columns(store.content_path(item).read_bytes(),
                           f"{item['name']}.{item['file_type']}"):
        if c.get("name") == f"[{name}]":
            return c, ds
    raise svc.not_found("Calculated field", "404000", name)


def _ds_editor(item: dict, name: str, caption: str) -> dict:
    """Where a data source calculation's editor posts, returns, and sits."""
    return {"action": f"/datasources/{item['id']}/calculations/{quote(name)}",
            "back": f"/datasources/{item['id']}", "caption": caption,
            "crumbs": [("Default", "/workbooks"), ("Data Sources", "/datasources"),
                       (item["name"], f"/datasources/{item['id']}"), (caption, None)]}


def _show_editor(editor: dict, formula: str, session: dict, *, message: str = "",
                 error: str = "", status: int = 200) -> HTMLResponse:
    return _html(editor["caption"], _editor(editor["action"], editor["back"],
                                            editor["caption"], formula, message=message,
                                            error=error),
                 session, editor["crumbs"], status=status)


@router.get("/datasources/{ds_id}/calculations/{name}")
def calc_edit(ds_id: str, name: str, request: Request) -> Response:
    def call(row):
        store, state, session = _session(request, row)
        item = _published(state, ds_id)
        row.targets = [f"{svc.NAME}:datasource:{item['content_url']}"]
        row.args["calculation"] = name
        c, ds = _calc(store, item, name)
        to_cap, _ = _caption_maps(ds.names)
        formula = _rename_fields(c.find("calculation").get("formula") or "", to_cap)
        return _show_editor(
            _ds_editor(item, name, c.get("caption") or name), formula, session,
            message="Saved and published." if request.query_params.get("saved") else "",
            error=str(ds.deferred[name]) if name in ds.deferred else "")
    return _serve(request, "ui_calc_edit", call)


# ------------------------------------------------------------- saving a calc
_DOCUMENT_SUFFIX = {"tds": ".tds", "tdsx": ".tds", "twb": ".twb", "twbx": ".twb"}


def _repack(data: bytes, file_type: str, document: bytes) -> bytes:
    """`data` with its document replaced by `document`. A .tds/.twb IS its
    document; a packaged .tdsx/.twbx is rewritten member by member, the one
    document at its top level replaced and every other member as it was (name,
    order, timestamp, attributes, compression and content), as a save of the
    packaged file in Desktop leaves its extracts and images alone."""
    if not data.startswith(b"PK"):
        return document
    want = _DOCUMENT_SUFFIX[file_type]
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w") as dst:
        dst.comment = src.comment
        top = [i for i in src.infolist() if "/" not in i.filename
               and i.filename.lower().endswith(want)]
        if len(top) != 1:           # publishing refused such a file; never guess
            raise svc.TableauError(400, "400000", "Bad Request",
                                   f"a packaged file holds exactly one {want} at its top "
                                   f"level, found {[i.filename for i in top]}")
        for info in src.infolist():
            dst.writestr(info, document if info is top[0] else src.read(info))
    return out.getvalue()


_FORMULA = r"'[^>]*>\s*<calculation\b[^>]*?\bformula='(?P<f>[^']*)'"


def _spans(text: str, ds_name: str | None) -> list[tuple[int, int]]:
    """Where a calculation may be edited: the whole .tds (`ds_name` None); in a
    workbook, its data source `ds_name` and every sheet's copy of it
    (`<datasource-dependencies datasource=...>`)."""
    if ds_name is None:
        return [(0, len(text))]
    attr = re.escape(xmlio._escape_attr(ds_name))
    out = []
    for open_tag, close_tag in ((r"<datasource\b[^>]*\bname='" + attr + r"'[^>]*(?<!/)>",
                                 "</datasource>"),
                                (r"<datasource-dependencies\b[^>]*\bdatasource='" + attr
                                 + r"'[^>]*(?<!/)>", "</datasource-dependencies>")):
        for m in re.finditer(open_tag, text):
            out.append((m.start(), text.index(close_tag, m.end())))
            if close_tag == "</datasource>":
                break                   # the workbook's own <datasources> entry
    return sorted(out)


def _with_formula(data: bytes, name: str, formula: str, ds_name: str | None = None) -> bytes:
    """The document with one calculation's formula attribute replaced - every
    other byte as published (a minimal diff, as Desktop would save it). In a
    workbook every copy of the calculation in data source `ds_name` changes."""
    text = data.decode("utf-8")
    col = re.compile(r"<column\b[^>]*\bname='" + re.escape(xmlio._escape_attr(f"[{name}]"))
                     + _FORMULA)
    hits = [m for start, end in _spans(text, ds_name) for m in col.finditer(text, start, end)]
    if not hits:
        raise svc.TableauError(400, "400000", "Bad Request",
                               f"The calculation [{name}] is not in a form this editor can "
                               f"save (a <column> holding a <calculation formula=...>).")
    for m in sorted(hits, key=lambda m: m.start("f"), reverse=True):
        text = text[:m.start("f")] + xmlio._escape_attr(formula) + text[m.end("f"):]
    return text.encode()


def _edited(data: bytes, file_type: str, what: str, name: str, stored: str,
            ds_name: str | None = None) -> bytes:
    """`data` (a .tds/.twb or its packaged form) with calculation `name`'s
    formula replaced by `stored`, scoped to workbook data source `ds_name`."""
    document = tproject.document_bytes(data, what)
    return _repack(data, file_type, _with_formula(document, name, stored, ds_name))


def _checked(typed: str, ds, name: str, edit, reread) -> tuple[str, bytes]:
    """(the formula as stored, the edited file `edit(stored)`) once the formula
    passes: the front-end's own check of the text as the author sees it, then
    the edited file as the server will read it once published (`reread`) - the
    calculation, every field that used to lower and every sheet that used to
    render still do."""
    to_cap, to_name = _caption_maps(ds.names)
    stored = _rename_fields(typed, to_name)
    shown = _rename_fields(stored, to_cap)
    try:
        parse_expression(shown, ds.model)
        candidate = edit(stored)
        after, sheets = reread(candidate)
    except (Unsupported, ModelError, SemanticError) as exc:
        raise svc.TableauError(400, "400000", "The calculation contains errors",
                               str(exc)) from None
    broken = sorted((set(after.deferred) - set(ds.deferred)) | ({name} & set(after.deferred)))
    reasons = [str(after.deferred[b]) for b in broken] + [f"{s}: {e}" for s, e in sheets]
    if reasons:
        raise svc.TableauError(400, "400000", "The calculation contains errors",
                               "; ".join(reasons))
    return stored, candidate


def _save(request: Request, operation: str, typed: str, call) -> Response:
    """One audited save: `call(row, holder)` publishes and redirects, or
    raises. A formula the checks refuse comes back in its editor with the
    message (`holder["editor"]`); any other refusal is an error page."""
    holder: dict = {}
    try:
        with svc.Row(request, operation, op_class="write") as row:
            return call(row, holder)
    except _Redirect as r:
        return RedirectResponse(r.location, status_code=303)
    except svc.TableauError as exc:
        editor = holder.get("editor")
        if editor is None or exc.summary != "The calculation contains errors":
            return _refused(exc, holder.get("session"))
        return _show_editor(editor, typed, holder.get("session"),
                            error=f"{exc.summary}: {exc.detail}", status=400)


@router.post("/datasources/{ds_id}/calculations/{name}")
async def calc_save(ds_id: str, name: str, request: Request) -> Response:
    form = await _form(request)
    typed = form.get("formula", "").replace("\r\n", "\n")

    def call(row, holder):
        store, state, session = _session(request, row)
        holder["session"] = session
        item = _published(state, ds_id)
        row.targets = [f"{svc.NAME}:datasource:{item['content_url']}"]
        row.args.update({"calculation": name, "formula": typed})
        c, ds = _calc(store, item, name)
        holder["editor"] = _ds_editor(item, name, c.get("caption") or name)
        what = f"{item['name']}.{item['file_type']}"
        data = store.content_path(item).read_bytes()
        stored, candidate = _checked(
            typed, ds, name, lambda f: _edited(data, item["file_type"], what, name, f),
            lambda edited: (tproject.read_datasource_bytes(edited, what, strict=False), []))
        with svc.LOCK, store.lock():
            state = store.load()
            item = store.publish_datasource(state, item["name"], candidate, item["file_type"],
                                            overwrite=True)
            store.save(state)
        row.flags.append("overwrite")
        row.result = {"datasource": item["name"], "id": item["id"], "calculation": name,
                      "formula": stored, "sha256": hashlib.sha256(candidate).hexdigest(),
                      "extract": item.get("has_extract", False)}
        return RedirectResponse(
            f"/datasources/{item['id']}/calculations/{quote(name)}?saved=1", status_code=303)

    return _save(request, "ui_calc_save", typed, call)


# ================================================== a workbook's own calculations
def _workbook_calcs(store, state, wb: dict, ds) -> tuple[list[ET.Element], dict | None]:
    """(the calculated fields that are the workbook's own - published with the
    workbook - and the published data source it connects to, if any). Over a
    published source those are its workbook-local calculations: Desktop also
    caches the source's own in the workbook, and the published definition
    wins (tds._read_sqlproxy). In an embedded source, every calculation."""
    what = f"{wb['name']}.{wb['file_type']}"
    root = xmlio.parse(tproject.document_bytes(store.content_path(wb).read_bytes(), what),
                       what)
    elem = next((d for d in root.findall("datasources/datasource")
                 if d.get("name") == ds.name), None)
    calcs = [] if elem is None else [c for c in elem.findall("column")
                                     if c.find("calculation") is not None]
    if not ds.published:
        return calcs, None
    published = store.datasource_by_url(state, ds.published)
    base = store.read_datasource(published)
    return [c for c in calcs if c.get("name")[1:-1] not in base.names.refs
            and c.get("name")[1:-1] not in base.deferred], published


def _workbook_calc(store, state, wb: dict, ds, name: str) -> ET.Element:
    own, published = _workbook_calcs(store, state, wb, ds)
    for c in own:
        if c.get("name") == f"[{name}]":
            return c
    theirs = [] if published is None else [
        c for c in _calc_columns(store.content_path(published).read_bytes(),
                                 f"{published['name']}.{published['file_type']}")
        if c.get("name") == f"[{name}]"]
    if theirs:
        caption = theirs[0].get("caption") or name
        raise _Elsewhere(
            f"The calculation [{caption}] belongs to the published data source "
            f"'{published['name']}'; workbook '{wb['name']}' only shows it. Edit it in the "
            f"data source, and every workbook connected to it shows the change.",
            f"/datasources/{published['id']}/calculations/{quote(name)}",
            f"Edit {caption} in {published['name']}")
    raise svc.not_found("Calculated field", "404000", name)


def _wb_editor(wb: dict, name: str, caption: str) -> dict:
    return {"action": f"/workbooks/{wb['id']}/calculations/{quote(name)}",
            "back": f"/workbooks/{wb['id']}/views", "caption": caption,
            "crumbs": [("Default", "/workbooks"), (wb["name"], f"/workbooks/{wb['id']}/views"),
                       (caption, None)]}


@router.get("/workbooks/{wb_id}/calculations/{name}")
def workbook_calc_edit(wb_id: str, name: str, request: Request) -> Response:
    def call(row):
        store, state, session = _session(request, row)
        wb = _workbook(state, wb_id)
        row.targets = [f"{svc.NAME}:workbook:{wb['content_url']}"]
        row.args["calculation"] = name
        ds, _, _ = _read_workbook(store, state, wb)
        c = _workbook_calc(store, state, wb, ds, name)
        to_cap, _ = _caption_maps(ds.names)
        formula = _rename_fields(c.find("calculation").get("formula") or "", to_cap)
        return _show_editor(
            _wb_editor(wb, name, c.get("caption") or name), formula, session,
            message="Saved and published." if request.query_params.get("saved") else "",
            error=str(ds.deferred[name]) if name in ds.deferred else "")
    return _serve(request, "ui_workbook_calc_edit", call)


@router.post("/workbooks/{wb_id}/calculations/{name}")
async def workbook_calc_save(wb_id: str, name: str, request: Request) -> Response:
    form = await _form(request)
    typed = form.get("formula", "").replace("\r\n", "\n")

    def call(row, holder):
        store, state, session = _session(request, row)
        holder["session"] = session
        wb = _workbook(state, wb_id)
        row.targets = [f"{svc.NAME}:workbook:{wb['content_url']}"]
        row.args.update({"calculation": name, "formula": typed})
        ds, _, deferred = _read_workbook(store, state, wb)
        c = _workbook_calc(store, state, wb, ds, name)
        holder["editor"] = _wb_editor(wb, name, c.get("caption") or name)

        def reread(edited):
            sheets: dict = {}
            after, _ = store.read_workbook(state, wb, sheets, data=edited)
            return after, [(s, e) for s, e in sheets.items() if s not in deferred]

        data = store.content_path(wb).read_bytes()
        what = f"{wb['name']}.{wb['file_type']}"
        stored, candidate = _checked(
            typed, ds, name, lambda f: _edited(data, wb["file_type"], what, name, f, ds.name),
            reread)
        with svc.LOCK, store.lock():
            state = store.load()
            wb = store.publish_workbook(state, wb["name"], candidate, wb["file_type"],
                                        overwrite=True, show_tabs=wb.get("show_tabs", True))
            store.save(state)
        row.flags.append("overwrite")
        row.result = {"workbook": wb["name"], "id": wb["id"], "calculation": name,
                      "formula": stored, "sha256": hashlib.sha256(candidate).hexdigest(),
                      "extract": wb.get("has_extract", False)}
        return RedirectResponse(
            f"/workbooks/{wb['id']}/calculations/{quote(name)}?saved=1", status_code=303)

    return _save(request, "ui_workbook_calc_save", typed, call)


# ==================================================================== wiring
def mount(app: FastAPI) -> FastAPI:
    """Serve the UI from the vendor's own app (one process, one writer, one
    audit stream). services/tableau.py calls this once, at the end of its
    module, after its routes are declared: the UI paths do not overlap
    `/api/...` or `/health`."""
    app.include_router(router)
    return app


def standalone() -> FastAPI:
    """The UI alone (tests, or a separate port); give it the same Context as
    the service and both read and write the same published state."""
    return mount(FastAPI(title="Alkera Tableau Server UI (fake)"))
