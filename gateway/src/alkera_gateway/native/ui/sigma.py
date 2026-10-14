"""Sigma's browser UI, over the fake Sigma service's PUBLISHED state.

docs/environments/UI_CLONE_PLAN.md, phase 3. This is another client of the
state `native/services/sigma.py` serves, mounted in that service's own app (one
process, one writer, one audit stream) - not a second server. It reads through
the service's `SigmaStore` (the same `<bi_state_dir>/sigma/state.json`),
computes an element exactly as `POST /v2/workbooks/{id}/export` does
(`read_data_model`, `element_visual`, `render.render_visual`, written out by the
service's own `_file`: the table on the page is the export's CSV, cell for
cell), publishes a column formula through `_publish_workbook` exactly as
`PUT /v2/workbooks/{id}/contents` does, and writes one 9.2.2 row per request
through the service's `Ledger` (`sigma.ui_*`).

    GET  /alkera/login, POST /alkera/login, POST /alkera/logout
    GET  /alkera                                        workbooks and data models
    GET  /alkera/workbook/<slug>-<urlId>                the workbook's first page
    GET  /alkera/workbook/<slug>-<urlId>/page/<pageId>  a page: each element's table
                                                        (+ chart or KPIs) and columns
    GET  .../element/<elementId>/column/<columnId>      a column's formula editor
    POST .../element/<elementId>/column/<columnId>      publish it
    GET  /alkera/data-model/<slug>-<urlId>              a data model: elements, columns,
                                                        metrics, relationships

URLS. A document's page is the path of the `url` the API returns for it
(`https://app.sigmacomputing.com/alkera/workbook/<slug>-<urlId>`,
formats/sigma/_common.document_url) on this host. The url id resolves it (the
slug is cosmetic); so does the document id. `/page/<pageId>` is the scheme
UI_CLONE_PLAN.md fixes. The editor and sign-in paths are this clone's own:
Sigma edits a formula in the formula bar of the page itself, and a
server-rendered form needs a URL to post to. Only `/alkera...` is the UI's.
Every other path, `/v2/...` included, is still the service's `unknown_route`
404, because the service keeps its fallback route last.

HOOKS a browser script (or a test) can rely on, beside html.py's:
    <form id="login">                 fields `email`, `password`; button "Sign in"
    <section data-element="<id>">     one per element on the page, titled <h2>
    <table class="summary" data-element="<id>">   the element's numbers
    <table class="columns" data-element="<id>">   its columns, each linked to
                                      "Edit formula"
    <form id="formula-editor">        <textarea name="formula">, the hidden
                                      `documentVersion`; button "Publish"

SIGN-IN. A cookie session (`sigma_session`, a name of this clone's) that only
the service's UI_PASSWORD opens (`SigmaStore.sign_in`). It is not an API
credential: the token endpoint refuses it, this form refuses the API secret,
and a session cookie is not a bearer token. Any email signs in.

A FORMULA is checked by publishing it. The edited workbook must read back
through the `store.read` a `PUT .../contents` runs (Sigma's formula front-end,
formats/sigma/formula.py). A refusal shows the front-end's message verbatim
(`unsupported (Sigma): CumulativeSum`, `'Orders' has no column ...`) and
publishes nothing. So does a stale page: the form carries the
`documentVersion` it was rendered from, and a workbook published since then is
Sigma's 409, not a silent overwrite. Only that one `formula` changes in the
published representation; the workspace (git) is never written.

NOT IMITATED. Editing anything but an existing column's formula: adding or
renaming columns, layout, element settings, filters, controls. Also not
imitated: drafts and the edit/publish split (a save publishes), editing a data
model (its page is read-only; `PUT /v2/dataModels/{id}/spec` edits it),
folders, search, sharing, version history and tags, downloads (the API's export
is the download), and rendering text, image and container elements (their kind
is named instead).
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ...records import ToolError
from ..context import Ledger, ServiceError
from ..http import RawBody
from . import html as H

#: formats/sigma/_common.ORG_SLUG. Spelled here rather than imported because
#: the routes are declared at import, and the service module must import
#: without `world` (its `_semantic` docstring); tests/test_ui_sigma.py checks
#: the two agree.
ORG = "alkera"
HOME = f"/{ORG}"
LOGIN = f"/{ORG}/login"
COOKIE = "sigma_session"
_CHARTS = {"barChart": H.bar_chart, "lineChart": H.line_chart, "areaChart": H.line_chart}
_STATUS_TITLES = {400: "Bad request", 404: "Not found", 409: "Conflict"}

router = APIRouter()


# ==================================================================== plumbing
class _SignIn(Exception):
    """No session: the row is an error, the response a redirect to sign in."""

    code = "unauthorized"

    def __init__(self, location: str):
        super().__init__("Sign in to continue")
        self.location = location


class _Visit:
    """One request in flight: the store, the row being written, the reader."""

    def __init__(self, request: Request, store, observation, ledger: Ledger, row):
        self.request, self.store, self.observation = request, store, observation
        self.ledger, self.row = ledger, row
        self.result: object = None
        self.email: str | None = None
        self.found: dict = {}            # what a refusal page needs to redraw a form

    def session(self) -> str:
        """The signed-in reader's email, or a redirect to the sign-in page that
        comes back here."""
        found = self.store.ui_session(self.request.cookies.get(COOKIE))
        if found is None:
            url = self.request.url
            target = url.path + (f"?{url.query}" if url.query else "")
            raise _SignIn(f"{LOGIN}?redirect={quote(target, safe='/')}")
        self.email = self.ledger.actor = found["email"]
        return self.email


def _serve(request: Request, tool: str, call, *, refused=None, **args) -> Response:
    """One request, one `sigma.<tool>` row, through the service's `Ledger`
    as its own `_serve` does. `call(visit)` classifies the observation and
    returns the page. A refusal is an HTML page, `refused(visit, exc)` or the
    generic one, written after its row is on file."""
    ledger = Ledger(request, svc.NAME)
    out: dict = {}
    try:
        with ledger.span(tool, **args) as row:
            def run(_operations, observation):
                visit = out["visit"] = _Visit(request, svc.SigmaStore(request), observation,
                                              ledger, row)
                out["response"] = call(visit)
                return visit.result
            row.run(run)
    except _SignIn as exc:
        return RedirectResponse(exc.location, status_code=303)
    except (ServiceError, ToolError) as exc:
        if isinstance(exc, ToolError):
            exc = ServiceError.from_tool_error(exc)
        visit = out.get("visit")
        page = refused(visit, exc) if refused is not None and visit is not None else None
        if page is None:
            page = _html(visit, _STATUS_TITLES.get(exc.status, "Error"),
                         H.error_box(exc.message), status=exc.status)
        return page
    return out["response"]


def _header(email: str | None) -> str:
    who = ""
    if email:
        who = (f'<form method="post" action="/{ORG}/logout"><span>{H.esc(email)}</span> '
               f'<button type="submit">Log out</button></form>')
    return (f'<header><a class="brand" href="{HOME}">Sigma (Alkera)</a>'
            f'<a href="{HOME}">Home</a>{who}</header>')


def _html(visit: _Visit | None, title: str, body: str, crumbs=(), status: int = 200
          ) -> HTMLResponse:
    email = visit.email if visit is not None else None
    return HTMLResponse(H.page(title, body, header=_header(email), crumbs=list(crumbs)),
                        status_code=status)


def _form(raw: bytes) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace"),
                                         keep_blank_values=True).items()}


def _safe_redirect(target: str | None) -> str:
    """Back to a page of this host, never elsewhere."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return HOME


def _name(el: dict) -> str:
    name = el.get("name")
    if isinstance(name, dict):
        name = name.get("text")
    return name if isinstance(name, str) and name else el["id"]


def _path(doc: dict, kind: str, url_id: str | None, doc_id: str) -> str:
    """The page of a document: the path of its `url`, else its id."""
    path = urlsplit(doc.get("url") or "").path
    if path.startswith(f"/{ORG}/{kind}/"):
        return path
    return f"/{ORG}/{kind}/{url_id or doc_id}"


def _workbook_path(doc: dict) -> str:
    return _path(doc, "workbook", doc.get("workbookUrlId"), doc["workbookId"])


def _data_model_path(doc: dict) -> str:
    return _path(doc, "data-model", None, doc["dataModelId"])


def _latest(entries: dict) -> list[tuple[str, dict]]:
    return sorted(((i, e["versions"][-1]) for i, e in entries.items()),
                  key=lambda item: (item[1].get("name", ""), item[0]))


def _find_workbook(store, ref: str) -> tuple[str, dict]:
    tail = ref.rsplit("-", 1)[-1]
    for wid, doc in _latest(store.state["workbooks"]):
        if ref == wid or tail == doc.get("workbookUrlId"):
            return wid, doc
    raise svc.Refusal(404, "not_found", f"Workbook {ref} not found")


def _find_data_model(store, ref: str) -> tuple[str, dict]:
    tail = ref.rsplit("-", 1)[-1]
    for dmid, doc in _latest(store.state["dataModels"]):
        if ref == dmid or tail == (doc.get("url") or "").rsplit("-", 1)[-1]:
            return dmid, doc
    raise svc.Refusal(404, "not_found", f"Data model {ref} not found")


# ===================================================================== sign in
def _login_form(redirect: str, error: str = "") -> str:
    return ("<h1>Sign in to Sigma</h1>" + (H.error_box(error) if error else "") +
            f'<form id="login" method="post" action="{LOGIN}">'
            f'<input type="hidden" name="redirect" value="{H.esc(redirect)}">'
            '<p><label>Email <input name="email" type="email" autocomplete="username">'
            '</label></p><p><label>Password <input name="password" type="password" '
            'autocomplete="current-password"></label></p>'
            '<p><button type="submit">Sign in</button></p></form>')


@router.get(LOGIN)
def login_page(request: Request) -> Response:
    redirect = _safe_redirect(request.query_params.get("redirect"))

    def call(visit):
        svc._read(visit.observation, "sigma:session")
        return _html(visit, "Sign in", _login_form(redirect))
    return _serve(request, "ui_login_page", call)


@router.post(LOGIN)
def login(request: Request, raw: RawBody) -> Response:
    form = _form(raw)
    email = form.get("email", "").strip()
    redirect = _safe_redirect(form.get("redirect"))

    def call(visit):
        visit.row.args["email"] = email                    # never the password
        cookie = visit.store.sign_in(email, form.get("password", ""), visit.observation)
        visit.ledger.actor = email
        visit.result = {"email": email}
        resp = RedirectResponse(redirect, status_code=303)
        resp.set_cookie(COOKIE, cookie, httponly=True, samesite="lax", path="/")
        return resp

    def refused(visit, exc):
        return _html(visit, "Sign in", _login_form(redirect, exc.message), status=exc.status)
    return _serve(request, "ui_login", call, refused=refused)


@router.post(f"/{ORG}/logout")
def logout(request: Request) -> Response:
    def call(visit):
        svc._read(visit.observation, "sigma:session")
        visit.store.sign_out(request.cookies.get(COOKIE))
        resp = RedirectResponse(LOGIN, status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp
    return _serve(request, "ui_logout", call)


# ======================================================================== home
@router.get(HOME)
def home(request: Request) -> Response:
    def call(visit):
        visit.session()
        state = visit.store.state
        workbooks, models = _latest(state["workbooks"]), _latest(state["dataModels"])
        targets = [svc._wb_target(i) for i, _ in workbooks] + \
                  [svc._dm_target(i) for i, _ in models]
        svc._read(visit.observation, *(targets or ["sigma:workbooks"]))
        wb_rows = "".join(
            f'<tr><td><a href="{_workbook_path(d)}">{H.esc(d.get("name", i))}</a></td>'
            f'<td>{H.esc(d.get("path", ""))}</td><td>{H.esc(d.get("documentVersion", ""))}</td>'
            f'<td>{H.esc(d.get("updatedAt", ""))}</td></tr>' for i, d in workbooks)
        dm_rows = "".join(
            f'<tr><td><a href="{_data_model_path(d)}">{H.esc(d.get("name", i))}</a></td>'
            f'<td>{H.esc(d.get("documentVersion", ""))}</td>'
            f'<td>{H.esc(d.get("updatedAt", ""))}</td></tr>' for i, d in models)
        body = ('<h1>Home</h1><h2>Workbooks</h2><table class="documents" data-kind="workbook">'
                '<thead><tr><th>Name</th><th>Folder</th><th>Version</th><th>Last published'
                f'</th></tr></thead><tbody>{wb_rows}</tbody></table><h2>Data models</h2>'
                '<table class="documents" data-kind="data-model"><thead><tr><th>Name</th>'
                '<th>Version</th><th>Last published</th></tr></thead>'
                f'<tbody>{dm_rows}</tbody></table>')
        return _html(visit, "Home", body, [("Home", None)])
    return _serve(request, "ui_home", call)


# =================================================================== workbooks
def _figure(name: str, visual, cells: list[list[str]], result: dict) -> str:
    """KPIs for an ungrouped element, the chart a chart element draws; both
    repeat the table below them and carry nothing it lacks."""
    columns, rows = cells[0], cells[1:]
    k = len(visual.group_by)
    if visual.kind == "card" or k == 0:
        return '<div class="kpis">' + "".join(
            f'<div class="kpi"><div class="l">{H.esc(c)}</div><div class="v">{H.esc(v)}</div>'
            f"</div>" for c, v in zip(columns, rows[0] if rows else [], strict=False)) + "</div>"
    chart = _CHARTS.get(visual.kind)
    if chart is None or not rows:
        return ""
    labels = [" / ".join(r[:k]) for r in rows]
    return "".join(chart(labels, [r[j] for r in result["rows"]], [r[j] for r in rows],
                         title=f"{name}: {columns[j]}")
                   for j in range(k, min(len(columns), k + 2)))


def _columns_table(base: str, el: dict, entries: list[dict]) -> str:
    hidden = {c["id"] for c in el.get("columns", []) if c.get("hidden")}
    note = " <small>(hidden)</small>"
    rows = "".join(
        f'<tr><td>{H.esc(e["label"])}{note if e["columnId"] in hidden else ""}'
        f'</td><td><code>{H.esc(e["formula"])}</code></td><td>{H.esc(e["type"]["type"])}</td>'
        f'<td><a href="{_editor_path(base, el["id"], e["columnId"])}">Edit formula</a></td></tr>'
        for e in entries)
    return (f'<h3>Columns</h3><table class="columns" data-element="{H.esc(el["id"])}">'
            '<thead><tr><th>Column</th><th>Formula</th><th>Type</th><th></th></tr></thead>'
            f"<tbody>{rows}</tbody></table>")


def _element(visit: _Visit, base: str, el: dict, model, index, problem: str | None,
             read: set, shown: dict) -> str:
    """One element as the page shows it: its numbers exactly as its export
    computes them, or the engine's reason it cannot."""
    _s, render, SemanticError, _c, _co, element_visual, _common = svc._semantic()
    elements = svc._elements_module()
    name, kind = _name(el), el.get("kind", "")
    out = [f'<section class="sheet" data-element="{H.esc(el["id"])}" '
           f'data-kind="{H.esc(kind)}"><h2>{H.esc(name)}</h2>']
    if not elements.has_data(el):
        out.append(f'<p class="note">A {H.esc(kind)} element (not shown here).</p></section>')
        return "".join(out)
    if problem is not None:
        out.append(H.error_box(problem))
    else:
        try:
            visual = element_visual(el, model, index)
            result = render.render_visual(model, visual, visit.store.warehouse)
        except SemanticError as exc:
            out.append(H.error_box(str(exc)))
        else:
            text = svc._file(result["columns"], result["rows"], "csv")
            cells = list(csv.reader(io.StringIO(text)))
            read.update(svc._relations(model, visual))
            shown[el["id"]] = hashlib.sha256(text.encode("utf-8")).hexdigest()
            out.append(_figure(name, visual, cells, result))
            out.append(H.table(cells[0], cells[1:], numeric_from=len(visual.group_by),
                               attrs={"class": "summary", "data-element": el["id"]}))
    out.append(_columns_table(base, el, elements.element_columns(el, model, index)))
    out.append("</section>")
    return "".join(out)


def _workbook_page(request: Request, ref: str, page_id: str | None) -> Response:
    def call(visit):
        visit.session()
        wid, doc = _find_workbook(visit.store, ref)
        target = svc._wb_target(wid)
        svc._read(visit.observation, target)
        visit.row.args["workbookId"] = wid
        contents, placed = svc._pages(visit.store, wid)
        pages = contents.get("pages", [])
        page = next((p for p in pages if p["id"] == page_id), None) if page_id else \
            (pages[0] if pages else None)
        if page is None:
            raise svc.Refusal(404, "not_found", f"Page {page_id} not found" if page_id
                              else "Workbook has no pages")
        base = _workbook_path(doc)
        model, index, problem = svc._published_model(visit.store, doc)
        by_id = {el["id"]: el for el in contents.get("elements", [])}
        read: set = set()
        shown: dict = {}
        sections = [_element(visit, base, by_id[eid], model, index, problem, read, shown)
                    if eid in by_id else H.error_box(f"The layout places unknown element {eid}")
                    for eid in placed.get(page["id"], [])]
        visit.observation.read_targets = sorted(read) + [target]
        visit.result = {"workbookId": wid, "pageId": page["id"],
                        "documentVersion": doc.get("documentVersion"), "elements": shown}
        source = ""
        try:
            dm = visit.store.data_model_for(doc)
            source = (f' &middot; Data model <a href="{_data_model_path(dm)}">'
                      f'{H.esc(dm.get("name", ""))}</a>')
        except svc.Refusal:
            pass
        tabs = '<nav class="tabs">' + "".join(
            f'<a href="{base}/page/{quote(p["id"])}"{" class=current" if p is page else ""}>'
            f'{H.esc(p["name"])}{" (hidden)" if p.get("visibility") == "hidden" else ""}</a>'
            for p in pages) + "</nav>"
        meta = (f'<p class="meta">Version {H.esc(doc.get("documentVersion", ""))} &middot; '
                f'Last published {H.esc(doc.get("updatedAt", ""))}{source}</p>')
        return _html(visit, f'{doc.get("name", wid)} - {page["name"]}',
                     f'<h1>{H.esc(doc.get("name", wid))}</h1>{meta}{tabs}' + "".join(sections),
                     [("Home", HOME), (doc.get("name", wid), base), (page["name"], None)])
    return _serve(request, "ui_workbook", call)


@router.get(f"/{ORG}/workbook/{{ref}}")
def workbook(ref: str, request: Request) -> Response:
    return _workbook_page(request, ref, None)


@router.get(f"/{ORG}/workbook/{{ref}}/page/{{page_id}}")
def workbook_page(ref: str, page_id: str, request: Request) -> Response:
    return _workbook_page(request, ref, page_id)


# ============================================================ formula editor
def _editor_path(base: str, element_id: str, column_id: str) -> str:
    return f"{base}/element/{quote(element_id)}/column/{quote(column_id)}"


def _column(visit: _Visit, ref: str, element_id: str, column_id: str):
    """(workbook id, its latest document, the element, the column), resolved
    for a signed-in reader; the row names the workbook from here on."""
    visit.session()
    wid, doc = _find_workbook(visit.store, ref)
    visit.observation.targets = [svc._wb_target(wid)]
    visit.row.args.update(workbookId=wid, elementId=element_id, columnId=column_id)
    el = svc._one_element(svc._semantic()[4](doc).get("elements", []), element_id)
    col = next((c for c in el.get("columns", []) if c.get("id") == column_id), None)
    if col is None:
        raise svc.Refusal(404, "not_found", f"Column {column_id} not found")
    visit.found.update(doc=doc, el=el, col=col)
    return wid, doc, el, col


def _editor(doc: dict, el: dict, col: dict, formula: str, version, *, message: str = "",
            error: str = "") -> tuple[str, str, list]:
    """(title, body, crumbs) of the editor for one column."""
    base = _workbook_path(doc)
    path = _editor_path(base, el["id"], col["id"])
    label = col.get("name") or col.get("formula", col["id"])
    reload = (f' <a href="{path}">Open the latest version</a>' if error.startswith(
        "Workbook has changed") else "")
    body = (f"<h1>{H.esc(label)}</h1>"
            f'<p class="meta">Column of <a href="{base}">{H.esc(_name(el))}</a> in '
            f'{H.esc(doc.get("name", ""))}, version {H.esc(version)}</p>'
            + (f'<div class="ok" role="status">{H.esc(message)}</div>' if message else "")
            + (H.error_box(error) + reload if error else "")
            + f'<form id="formula-editor" method="post" action="{path}">'
            f'<input type="hidden" name="documentVersion" value="{H.esc(version)}">'
            '<p><label for="formula">Formula</label></p>'
            f'<textarea id="formula" name="formula" rows="4">{H.esc(formula)}</textarea>'
            f'<p><button type="submit">Publish</button> <a href="{base}">Cancel</a></p></form>')
    return label, body, [("Home", HOME), (doc.get("name", ""), base), (label, None)]


_EDITOR = f"/{ORG}/workbook/{{ref}}/element/{{element_id}}/column/{{column_id:path}}"


@router.get(_EDITOR)
def formula_edit(ref: str, element_id: str, column_id: str, request: Request) -> Response:
    def call(visit):
        _wid, doc, el, col = _column(visit, ref, element_id, column_id)
        visit.observation.read_targets = list(visit.observation.targets)
        version = doc.get("documentVersion")
        message = f"Published version {version}." if request.query_params.get("saved") else ""
        title, body, crumbs = _editor(doc, el, col, col.get("formula", ""), version,
                                      message=message)
        return _html(visit, title, body, crumbs)
    return _serve(request, "ui_formula_edit", call)


@router.post(_EDITOR)
def formula_save(ref: str, element_id: str, column_id: str, request: Request,
                 raw: RawBody) -> Response:
    form = _form(raw)
    typed = form.get("formula", "").replace("\r\n", "\n")
    stated = form.get("documentVersion", "")

    def call(visit):
        wid, doc, _el, _col = _column(visit, ref, element_id, column_id)
        visit.row.args.update(formula=typed, documentVersion=stated)
        try:
            version = int(stated)
        except ValueError:
            raise svc.Refusal(400, "bad_request", "`documentVersion` is required: open the "
                                                  "editor again") from None
        contents = copy.deepcopy(svc._semantic()[4](doc))
        edited = next(e for e in contents["elements"] if e.get("id") == element_id)
        next(c for c in edited["columns"] if c.get("id") == column_id)["formula"] = typed
        meta = svc._publish_workbook(visit.store, wid,
                                     {"contents": contents, "documentVersion": version},
                                     "contents", visit.observation)
        visit.result = {"workbookId": wid, "elementId": element_id, "columnId": column_id,
                        "formula": typed, "latestVersion": meta.get("latestVersion")}
        return RedirectResponse(_editor_path(_workbook_path(doc), element_id, column_id)
                                + "?saved=1", status_code=303)

    def refused(visit, exc):
        if "col" not in visit.found:
            return None
        found = visit.found
        title, body, crumbs = _editor(found["doc"], found["el"], found["col"], typed, stated,
                                      error=exc.message)
        return _html(visit, title, body, crumbs, status=exc.status)
    return _serve(request, "ui_formula_save", call, refused=refused)


# ================================================================ data models
def _source(source: dict) -> str:
    kind = source.get("kind")
    if kind == "warehouse-table":
        return f'<p>Source: <code>{H.esc(".".join(source.get("path", [])))}</code></p>'
    if kind == "sql":
        return (f'<p>Source: Custom SQL</p><pre><code>{H.esc(source.get("statement", ""))}'
                f"</code></pre>")
    return f"<p>Source: {H.esc(kind)}</p>"


def _model_element(el: dict, names: dict[str, str]) -> str:
    def label(col: dict) -> str:
        formula = col.get("formula", "")
        if col.get("name"):
            return col["name"]
        return formula[1:-1].split("/")[-1] if formula.startswith("[") else formula

    columns = "".join(
        f'<tr><td>{H.esc(label(c))}</td><td><code>{H.esc(c.get("formula", ""))}</code></td>'
        f'<td>{"yes" if c.get("hidden") else ""}</td></tr>' for c in el.get("columns", []))
    out = [f'<section class="sheet" data-element="{H.esc(el["id"])}" '
           f'data-kind="{H.esc(el.get("kind", ""))}"><h2>{H.esc(_name(el))}</h2>',
           _source(el.get("source") or {}),
           '<table class="columns"><thead><tr><th>Column</th><th>Formula</th><th>Hidden</th>'
           f"</tr></thead><tbody>{columns}</tbody></table>"]
    if el.get("metrics"):
        out.append('<h3>Metrics</h3><table class="metrics"><thead><tr><th>Metric</th>'
                   '<th>Formula</th><th>Description</th></tr></thead><tbody>' + "".join(
                       f'<tr><td>{H.esc(m.get("name", ""))}</td><td><code>'
                       f'{H.esc(m.get("formula", ""))}</code></td>'
                       f'<td>{H.esc(m.get("description", ""))}</td></tr>'
                       for m in el["metrics"]) + "</tbody></table>")
    if el.get("relationships"):
        out.append('<h3>Relationships</h3><table class="relationships"><thead><tr>'
                   '<th>Relationship</th><th>To</th></tr></thead><tbody>' + "".join(
                       f'<tr><td>{H.esc(r.get("name", ""))}</td><td>'
                       f'{H.esc(names.get(r.get("targetElementId"), ""))}</td></tr>'
                       for r in el["relationships"]) + "</tbody></table>")
    out.append("</section>")
    return "".join(out)


@router.get(f"/{ORG}/data-model/{{ref}}")
def data_model(ref: str, request: Request) -> Response:
    def call(visit):
        visit.session()
        dmid, doc = _find_data_model(visit.store, ref)
        svc._read(visit.observation, svc._dm_target(dmid))
        visit.row.args["dataModelId"] = dmid
        _s, _r, SemanticError, _c, contents_of, *_rest = svc._semantic()
        from world.semantic.formats.sigma.datamodel import read_data_model

        problem = ""
        try:
            read_data_model(doc, visit.store.catalog())
        except SemanticError as exc:
            problem = H.error_box(str(exc))
        elements = [el for page in doc.get("pages", []) for el in page.get("elements", [])]
        names = {el["id"]: _name(el) for el in elements}
        used = [(i, d) for i, d in _latest(visit.store.state["workbooks"])
                if any((el.get("source") or {}).get("dataModelId") == dmid
                       for el in contents_of(d).get("elements", []))]
        workbooks = "".join(f'<li><a href="{_workbook_path(d)}">{H.esc(d.get("name", i))}'
                            f"</a></li>" for i, d in used)
        visit.result = {"dataModelId": dmid, "documentVersion": doc.get("documentVersion")}
        body = (f'<h1>{H.esc(doc.get("name", dmid))}</h1>'
                f'<p class="meta">Data model &middot; Version '
                f'{H.esc(doc.get("documentVersion", ""))} &middot; Last published '
                f'{H.esc(doc.get("updatedAt", ""))}</p>{problem}'
                + "".join(_model_element(el, names) for el in elements)
                + (f"<h2>Workbooks built on it</h2><ul>{workbooks}</ul>" if used else ""))
        return _html(visit, doc.get("name", dmid), body,
                     [("Home", HOME), (doc.get("name", dmid), None)])
    return _serve(request, "ui_data_model", call)


# ==================================================================== wiring
def mount(app: FastAPI) -> FastAPI:
    """Serve the UI from the vendor's own app. Every UI path is under
    `/alkera`, which no API route uses. services/sigma.py calls this when it
    is imported and then moves its `/{path:path}` fallback behind these routes
    (`_catch_all_last`): included after the fallback, they would never match."""
    app.include_router(router)
    return app


# Last, on purpose. services/sigma.py imports this module at ITS end to mount
# it, so whichever of the two is imported first, the other finds what it needs
# already defined: every use of `svc` above is at request time.
from ..services import sigma as svc  # noqa: E402
