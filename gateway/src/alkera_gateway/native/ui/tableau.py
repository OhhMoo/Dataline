"""Tableau Server's browser UI, over the fake Tableau service's PUBLISHED state.

docs/environments/UI_CLONE_PLAN.md. This is another client of the state
`native/services/tableau.py` serves - not a second server. It reads and
writes through that module's `Store` (same `<bi_state_dir>/tableau/` manifest,
same file lock, same in-process `_LOCK`), publishes through
`Store.publish_datasource` exactly as `POST .../datasources?overwrite=true`
does, renders a view exactly as `GET .../views/{id}/data` does (the CSV that
endpoint returns is the table this page shows, cell for cell), and writes one
9.2.2 row per request through the service's own `_Row` (`tableau.ui_*`).

    GET  /signin, POST /signin, POST /signout     cookie `workgroup_session_id`
    GET  /                                        -> /workbooks
    GET  /workbooks                               the site's workbooks
    GET  /workbooks/{id}/views                    a workbook's sheets
    GET  /views/{workbook}/{sheet}                a sheet or dashboard: tables + charts
    GET  /views/{workbook}/{sheet}.csv            its summary data (Tableau's `.csv` URL)
    GET  /datasources, /datasources/{id}          published data sources, their fields
    GET  /datasources/{id}/calculations/{name}    the calculation editor
    POST /datasources/{id}/calculations/{name}    validate, then save and publish

URLS. Tableau Server routes its pages client-side (`/#/workbooks/...`,
`/#/views/<Workbook>/<Sheet>`); a fragment never reaches a server, so the same
paths are served here without the `#/`. `/views/<Workbook>/<Sheet>` and its
`.csv` twin are Tableau Server's own server-side URLs. Only the Default site
exists (as in the REST service), so there is no `/t/<site>/` prefix.

THE CALCULATION EDITOR shows a formula as Tableau's editor does - fields by
caption (`[Net Revenue] / [Completed Orders]`) - and stores it as Tableau
stores it, by field name (`[Calculation_...]`). A formula is checked with the
front-end's `parse_expression` and then by re-reading the edited data source;
a refusal shows the front-end's message verbatim (`unsupported (Tableau): ...`
for anything outside the subset) and publishes nothing. Only the one
`formula='...'` attribute changes in the published document.

NOT IMITATED. Web authoring of sheets (shelves, marks, drag and drop), new
calculations, workbook-local calculations, packaged (.tdsx) data sources,
filters/parameters controls, subscriptions, favourites, search, users and
permissions, images/PDF downloads.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
import xml.etree.ElementTree as ET
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


# ==================================================================== plumbing
def _serve(request: Request, operation: str, call, *, op_class: str = "read",
           args: dict | None = None) -> Response:
    """One audited request: `call(row)` returns a Response or raises; refusals
    become an HTML page (and an error row), never XML."""
    try:
        with svc._Row(request, operation, op_class=op_class, args=args) as row:
            return call(row)
    except _Redirect as r:
        return RedirectResponse(r.location, status_code=303)
    except svc.TableauError as exc:
        body = H.error_box(f"{exc.summary}: {exc.detail}")
        return HTMLResponse(H.page(exc.summary, body, header=_header(None)),
                            status_code=exc.status)


def _session(request: Request, row) -> tuple[svc.Store, dict, dict]:
    """The signed-in session (cookie) and the published state, or a redirect to
    the sign-in page that comes back here."""
    with svc._LOCK:
        store = svc.Store(request)
        with store.lock():
            state = store.load()
    session = state["sessions"].get(request.cookies.get(COOKIE) or "")
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
            f'<a href="/workbooks">Workbooks</a><a href="/datasources">Data Sources</a>'
            f"{who}</header>")


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
        if form.get("password") != svc.DEV_TOKEN:
            raise svc.TableauError(401, "401001", "Signin Error",
                                   "Invalid username or password.")
        with svc._LOCK:
            store = svc.Store(request)
            with store.lock():
                state = store.load()
                n = store.next(state)
                token = hashlib.sha256(f"session/{n}/{user}".encode()).hexdigest()[:32]
                state["sessions"][token] = {"user": user, "user_id": svc._uid("user", user),
                                            "site_id": svc.SITE_ID}
                store.save(state)
        row.result = {"user": user, "site": svc.SITE_ID}
        resp = RedirectResponse(redirect, status_code=303)
        resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", path="/")
        return resp

    try:
        with svc._Row(request, "ui_signin", op_class="read") as row:
            return call(row)
    except svc.TableauError as exc:
        return _html("Sign In", _signin_form(redirect, exc.detail), None, status=exc.status)


@router.post("/signout")
def signout(request: Request) -> Response:
    def call(row):
        row.targets = [f"{svc.NAME}:session"]
        with svc._LOCK:
            store = svc.Store(request)
            with store.lock():
                state = store.load()
                state["sessions"].pop(request.cookies.get(COOKIE) or "", None)
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


@router.get("/workbooks")
def workbooks(request: Request) -> Response:
    def call(row):
        _, state, session = _session(request, row)
        row.targets = [f"{svc.NAME}:workbook:{w['content_url']}" for w in state["workbooks"]] \
            or [f"{svc.NAME}:workbooks"]
        items = "".join(
            f'<tr><td><a href="/workbooks/{w["id"]}/views">{H.esc(w["name"])}</a></td>'
            f'<td>{H.esc(svc.PROJECT_NAME)}</td><td>{len(w["views"])}</td>'
            f'<td>{H.esc(w["updated_at"])}</td></tr>' for w in state["workbooks"])
        body = ('<h1>Workbooks</h1><table class="content-list"><thead><tr><th>Name</th>'
                '<th>Project</th><th>Views</th><th>Modified</th></tr></thead>'
                f"<tbody>{items}</tbody></table>")
        return _html("Workbooks", body, session, [("Default", "/workbooks"), ("Workbooks", None)])
    return _serve(request, "ui_workbooks", call)


def _workbook(state: dict, wb_id: str) -> dict:
    return svc._find(state["workbooks"], wb_id, "Workbook", "404006")


def _view_url(view: dict) -> str:
    wb_url, _, sheet = view["content_url"].partition("/sheets/")
    return f"/views/{wb_url}/{sheet}"


@router.get("/workbooks/{wb_id}/views")
def workbook_views(wb_id: str, request: Request) -> Response:
    def call(row):
        _, state, session = _session(request, row)
        wb = _workbook(state, wb_id)
        row.targets = [f"{svc.NAME}:workbook:{wb['content_url']}"]
        items = "".join(
            f'<li><a href="{_view_url(v)}">{H.esc(v["name"])}</a> '
            f'<small>({H.esc(v["sheet_type"])})</small></li>' for v in wb["views"])
        sources = "".join(
            f'<li>{H.esc(c.get("caption") or c.get("dbname") or c["class"])}</li>'
            for c in wb["connections"])
        body = (f'<h1>{H.esc(wb["name"])}</h1><h2>Views</h2><ul class="views">{items}</ul>'
                f'<h2>Data Sources</h2><ul>{sources}</ul>')
        return _html(wb["name"], body, session, [("Default", "/workbooks"),
                                                  (wb["name"], None)])
    return _serve(request, "ui_workbook", call)


# ===================================================================== views
def _find_view(state: dict, wb_url: str, sheet_url: str) -> tuple[dict, dict]:
    key = f"{wb_url}/sheets/{sheet_url}"
    for v, wb in svc._all_views(state):
        if v["content_url"] == key:
            return v, wb
    raise svc._not_found("View", "404011", f"{wb_url}/{sheet_url}")


def _sheets_of(report, view: dict) -> list:
    """The worksheets a view shows: itself, or a dashboard's zones in order."""
    if view["sheet_type"] == "worksheet":
        return [svc._visual_for(report, view["name"], "worksheet")]
    for page in report.pages:
        if page.name == view["name"]:
            return list(page.visuals)
    raise svc.TableauError(400, "400000", "Bad Request", f"View '{view['name']}' has no data.")


def _render(store, state, wb, ds, visual):
    """(csv bytes, raw result) exactly as Query View Data computes them."""
    owner = svc._owner(state, wb, ds)
    warehouse, refreshed = store.warehouse_for(state, owner)
    result = render.render_visual(ds.model, visual, warehouse)
    return svc._csv(ds.model, visual, result), result, refreshed


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


@router.get("/views/{wb_url}/{sheet_url}")
def view(wb_url: str, sheet_url: str, request: Request) -> Response:
    as_csv = sheet_url.endswith(".csv")
    sheet_url = sheet_url.removesuffix(".csv")

    def call(row):
        store, state, session = _session(request, row)
        v, wb = _find_view(state, wb_url, sheet_url)
        row.targets = [f"{svc.NAME}:view:{v['content_url']}"]
        row.args["view"] = v["content_url"]
        deferred: dict = {}
        try:
            ds, report = store.read_workbook(state, wb, deferred)
        except (ModelError, SemanticError) as exc:
            raise svc.TableauError(400, "400000", "Bad Request",
                                   f"{type(exc).__name__}: {exc}") from None
        visuals = _sheets_of(report, v) if v["name"] not in deferred else []
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
            row.read_targets = svc._visual_relations(ds.model, visuals[0])
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
            read.update(svc._visual_relations(ds.model, visual))
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
        toolbar = (f'<p class="toolbar"><a href="{_view_url(v)}">Refresh</a> | '
                   f'<a href="{_view_url(v)}.csv">Download Data (CSV)</a></p>')
        return _html(v["name"], f'<h1>{H.esc(v["name"])}</h1>{tabs}{toolbar}'
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
        items = "".join(
            f'<tr><td><a href="/datasources/{d["id"]}">{H.esc(d["name"])}</a></td>'
            f'<td>{H.esc(d["type"])}</td><td>{"Extract" if d.get("has_extract") else "Live"}'
            f'</td><td>{H.esc(d["updated_at"])}</td></tr>' for d in state["datasources"])
        body = ('<h1>Data Sources</h1><table class="content-list"><thead><tr><th>Name</th>'
                '<th>Connection</th><th>Live/Extract</th><th>Modified</th></tr></thead>'
                f"<tbody>{items}</tbody></table>")
        return _html("Data Sources", body, session,
                     [("Default", "/workbooks"), ("Data Sources", None)])
    return _serve(request, "ui_datasources", call)


def _published(state: dict, ds_id: str) -> dict:
    return svc._find(state["datasources"], ds_id, "Datasource", "404009")


def _document(store, item: dict) -> bytes:
    data = store.content_path(item).read_bytes()
    if item["file_type"] != "tds":
        raise svc.TableauError(400, "400000", "Bad Request", str(Unsupported(
            f"editing a packaged data source (.{item['file_type']}) in the browser", "Tableau",
            "download it, edit the .tds inside, and publish it")))
    return data


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


@router.get("/datasources/{ds_id}")
def datasource(ds_id: str, request: Request) -> Response:
    def call(row):
        store, state, session = _session(request, row)
        item = _published(state, ds_id)
        row.targets = [f"{svc.NAME}:datasource:{item['content_url']}"]
        ds = store.read_datasource(item)
        to_cap, _ = _caption_maps(ds.names)
        calcs = []
        for c in _calc_columns(store.content_path(item).read_bytes(),
                               f"{item['name']}.{item['file_type']}"):
            name = c.get("name")[1:-1]
            formula = _rename_fields(c.find("calculation").get("formula") or "", to_cap)
            status = H.error_box(str(ds.deferred[name])) if name in ds.deferred else ""
            calcs.append(
                f'<tr><td><a href="/datasources/{item["id"]}/calculations/{quote(name)}">'
                f'{H.esc(c.get("caption") or name)}</a></td><td><code>{H.esc(formula)}</code>'
                f"{status}</td></tr>")
        fields = "".join(
            f"<tr><td>{H.esc(t.name)}</td><td>{H.esc(col.name)}</td>"
            f"<td>{H.esc(col.data_type)}</td></tr>"
            for t in ds.model.tables for col in t.columns if col.expr is None)
        body = (f'<h1>{H.esc(item["name"])}</h1>'
                f'<p>{"Extract" if item.get("has_extract") else "Live"} connection '
                f'({H.esc(item["type"])})</p><h2>Calculated Fields</h2>'
                '<table class="calculations"><thead><tr><th>Name</th><th>Formula</th></tr>'
                f'</thead><tbody>{"".join(calcs)}</tbody></table><h2>Fields</h2>'
                '<table class="fields"><thead><tr><th>Table</th><th>Field</th><th>Type</th>'
                f"</tr></thead><tbody>{fields}</tbody></table>")
        return _html(item["name"], body, session, [("Default", "/workbooks"),
                                                    ("Data Sources", "/datasources"),
                                                    (item["name"], None)])
    return _serve(request, "ui_datasource", call)


def _editor(item: dict, name: str, caption: str, formula: str, *, message: str = "",
            error: str = "") -> str:
    return (f'<h1>{H.esc(caption)}</h1>'
            + (f'<div class="ok" role="status">{H.esc(message)}</div>' if message else "")
            + (H.error_box(error) if error else "")
            + f'<form id="calc-editor" method="post" '
            f'action="/datasources/{item["id"]}/calculations/{quote(name)}">'
            f'<p><label for="formula">Formula</label></p>'
            f'<textarea id="formula" name="formula" rows="6">{H.esc(formula)}</textarea>'
            '<p><button type="submit">Save and Publish</button> '
            f'<a href="/datasources/{item["id"]}">Cancel</a></p></form>')


def _calc(store, item: dict, name: str) -> tuple[ET.Element, object]:
    ds = store.read_datasource(item)
    for c in _calc_columns(store.content_path(item).read_bytes(),
                           f"{item['name']}.{item['file_type']}"):
        if c.get("name") == f"[{name}]":
            return c, ds
    raise svc._not_found("Calculated field", "404000", name)


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
        saved = "Saved and published." if request.query_params.get("saved") else ""
        error = str(ds.deferred[name]) if name in ds.deferred else ""
        return _html(c.get("caption") or name,
                     _editor(item, name, c.get("caption") or name, formula, message=saved,
                             error=error), session, _calc_crumbs(item, c.get("caption") or name))
    return _serve(request, "ui_calc_edit", call)


def _calc_crumbs(item: dict, caption: str):
    return [("Default", "/workbooks"), ("Data Sources", "/datasources"),
            (item["name"], f"/datasources/{item['id']}"), (caption, None)]


_FORMULA_ATTR = "formula='{}'"


def _with_formula(data: bytes, name: str, formula: str) -> bytes:
    """The document with one calculation's formula attribute replaced - every
    other byte as published (a minimal diff, as Desktop would save it)."""
    text = data.decode("utf-8")
    col = re.compile(r"<column\b[^>]*\bname='" + re.escape(xmlio._escape_attr(f"[{name}]"))
                     + r"'[^>]*>\s*<calculation\b[^>]*?\bformula='(?P<f>[^']*)'")
    m = col.search(text)
    if m is None:
        raise svc.TableauError(400, "400000", "Bad Request",
                               f"The calculation [{name}] is not in a form this editor can "
                               f"save (a <column> holding a <calculation formula=...>).")
    return (text[:m.start("f")] + xmlio._escape_attr(formula) + text[m.end("f"):]).encode()


@router.post("/datasources/{ds_id}/calculations/{name}")
async def calc_save(ds_id: str, name: str, request: Request) -> Response:
    form = await _form(request)
    typed = form.get("formula", "").replace("\r\n", "\n")
    holder: dict = {}

    def call(row):
        store, state, session = _session(request, row)
        holder["session"] = session
        item = _published(state, ds_id)
        holder["item"] = item
        row.targets = [f"{svc.NAME}:datasource:{item['content_url']}"]
        row.args.update({"calculation": name, "formula": typed})
        data = _document(store, item)
        c, ds = _calc(store, item, name)
        holder["caption"] = c.get("caption") or name
        to_cap, to_name = _caption_maps(ds.names)
        stored = _rename_fields(typed, to_name)
        shown = _rename_fields(stored, to_cap)
        what = f"{item['name']}.{item['file_type']}"
        try:
            # The front-end's own check of the text as the author sees it...
            parse_expression(shown, ds.model)
            # ...and the document as the server will read it once published:
            # the calculation and every field that used to lower still lower.
            candidate = _with_formula(data, name, stored)
            after = tproject.read_datasource_bytes(candidate, what, strict=False)
        except (Unsupported, ModelError, SemanticError) as exc:
            raise svc.TableauError(400, "400000", "The calculation contains errors",
                                   str(exc)) from None
        broken = sorted((set(after.deferred) - set(ds.deferred)) | (
            {name} & set(after.deferred)))
        if broken:
            raise svc.TableauError(400, "400000", "The calculation contains errors",
                                   "; ".join(str(after.deferred[b]) for b in broken))
        with svc._LOCK, store.lock():
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

    try:
        with svc._Row(request, "ui_calc_save", op_class="write") as row:
            return call(row)
    except _Redirect as r:
        return RedirectResponse(r.location, status_code=303)
    except svc.TableauError as exc:
        item = holder.get("item")
        if item is None or exc.summary != "The calculation contains errors":
            return _html(exc.summary, H.error_box(f"{exc.summary}: {exc.detail}"),
                         holder.get("session"), status=exc.status)
        caption = holder.get("caption", name)
        return _html(caption, _editor(item, name, caption, typed,
                                      error=f"{exc.summary}: {exc.detail}"),
                     holder.get("session"), _calc_crumbs(item, caption), status=400)


# ==================================================================== wiring
def mount(app: FastAPI) -> FastAPI:
    """Serve the UI from the vendor's own app (one process, one writer, one
    audit stream). Call once, after the service's routes are declared: the UI
    paths do not overlap `/api/...` or `/health`."""
    app.include_router(router)
    return app


def standalone() -> FastAPI:
    """The UI alone (tests, or a separate port); give it the same Context as
    the service and both read and write the same published state."""
    return mount(FastAPI(title="Alkera Tableau Server UI (fake)"))
