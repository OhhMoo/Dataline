"""Tableau Server-shaped REST service over the world's Tableau content (gateway/NATIVE.md).

A data engineer on a Tableau shop keeps data sources (.tds) and workbooks (.twb)
in git, publishes them with `tabcmd publish` or the Tableau Server Client
(`tableauserverclient`), refreshes extracts with `tabcmd refreshextracts`, and
checks a dashboard's numbers with `tabcmd export --csv` / `views.populate_csv`.
This service is the server end of exactly that, at `tableau.internal:8096`,
spoken in the Tableau REST API's own shapes (help.tableau.com REST API
reference; response shapes from the MIT-licensed test assets of
github.com/tableau/server-client-python, request shapes from its request
factory and from github.com/tableau/tabcmd):

    GET  /api/serverinfo, /api/{v}/serverInfo                 unauthenticated
    POST /api/{v}/auth/signin | signout                       PAT or name/password
    GET  /api/{v}/sites[/{id}]  /sites/{s}/users/{id}  /sites/{s}/projects
    GET  /api/{v}/sites/{s}/workbooks[/{id}[/views|/connections|/content]]
    POST /api/{v}/sites/{s}/workbooks                         publish (multipart/mixed)
    POST /api/{v}/sites/{s}/workbooks/{id}/refresh            -> job
    GET  /api/{v}/sites/{s}/datasources[/{id}[/connections|/content]]
    POST /api/{v}/sites/{s}/datasources                       publish (multipart/mixed)
    POST /api/{v}/sites/{s}/datasources/{id}/refresh          -> job
    GET  /api/{v}/sites/{s}/views[/{id}]  /views/{id}/data    view data as CSV
    GET  /api/{v}/sites/{s}/jobs[/{id}]
    GET  /health

WHAT A VIEW'S DATA IS. `views/{id}/data` reads the PUBLISHED workbook (not the
workspace copy - publishing is the act that changes what the server shows),
lowers it through `world.semantic.formats.tableau`, and evaluates the view with
`world.semantic.render` on the world's warehouse: the same engine the graders
run on the agent's final files, so the server and the grader cannot disagree
about what a definition means - only about WHICH definition (published or
committed) and WHEN (live or extract). A sheet outside the supported subset
answers 400 naming the Tableau feature, never an approximate table.

EXTRACTS WITHOUT HYPER. There is no Hyper API for Linux arm64. An extract is
modelled for what it is to the person reading the numbers: a copy of the data
source's tables taken at refresh time. Publishing or refreshing an
extract-enabled data source (`<extract enabled='true'>`, or a workbook that
embeds one) copies every warehouse relation its model reads into a DuckDB file
under the state directory; its views then render from that copy until the next
refresh, so a refresh run before a late partition landed leaves the dashboard
stale exactly as a real extract would. A live connection renders from the
warehouse on every request, and refreshing one is refused as Tableau refuses it
(403104). Any .hyper inside an uploaded .twbx/.tdsx is ignored: the copy is
taken from the warehouse at publish time.

STATE. Published content, sessions, jobs and extract copies live under
`<bi_state_dir>/tableau/` - outside the world tree, beside the BI plane's other
server-side state (alkera_gateway.bi keeps its cached tiles under bi_state_dir).
The initial published state is the world's own Tableau project,
`<workspace>/bi/tableau/` (every .tds/.tdsx and .twb/.twbx under it), taken the
first time the service is touched - in an episode, the harness's /health probe,
before the agent acts. Ids are uuid5s of names and a counter, timestamps the
virtual clock: the same requests on the same world give the same responses.

AUDIT. Every request writes one 9.2.2 row through `native.audit.record`. There
is no `Operations` method behind Tableau (no MCP tool reaches it), so - like
`metabase.session_create` and `jira.fields` - the classification is written
here: reads are `read` on `tableau:<kind>:<contentUrl>`, publish and refresh are
`write` on the content they replace, and a view's row names the warehouse
relations it read in `read_targets`. Refused requests write their row too.

NOT IMITATED. Permissions, groups, schedules, subscriptions, tags, revisions,
images/PDF/Excel exports, chunked uploads (> 64 MB), sites other than Default,
cross-site switching, and Tableau's number formatting beyond the format string a
measure declares (Tableau's "automatic" format is approximated: thousands
separators on integers, reals as stored).
"""
from __future__ import annotations

import csv
import datetime as dt
import email
import email.policy
import hashlib
import io
import json
import re
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from threading import RLock

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import Response
from filelock import FileLock
from starlette.exceptions import HTTPException as StarletteHTTPException
from world.semantic import render
from world.semantic.errors import ModelError, SemanticError, Unsupported
from world.semantic.expr import Col, FilterValues
from world.semantic.formats.tableau import project as tproject
from world.semantic.formats.tableau import tds as ttds
from world.semantic.formats.tableau import xmlio

from ..audit import record
from ..context import DEV_TOKEN, context_of

NAME = "tableau"
HOST = "tableau.internal"
PORT = 8096

# Tableau Server 2025.1 speaks REST API 3.25 ("API and Tableau Server versions").
API_VERSION = "3.25"
PRODUCT_VERSION = xmlio.PRODUCT_VERSION
BUILD = xmlio.BUILD
NS = "http://tableau.com/api"
SCHEMA = (f"{NS} https://help.tableau.com/samples/en-us/rest_api/"
          f"ts-api_{API_VERSION.replace('.', '_')}.xsd")
#: Above this TSC switches to chunked fileUploads, which is not imitated.
MAX_UPLOAD = 64 * 1024 * 1024
SITE_NAME = "Default"
PROJECT_NAME = "Default"
PROJECT_DESCRIPTION = "The default project that was automatically created by Tableau."
DEFAULT_USER = "alkera"
#: The browser sign-in's password (native/ui/tableau.py): a credential of its
#: own, not the REST token. The REST sign-in refuses it and the browser refuses
#: DEV_TOKEN, so an arm can hand an agent the one without the other. The box is
#: to carry it as TABLEAU_UI_PASSWORD (not wired yet: see UI_CLONE_PLAN.md).
#: Obviously fake, by repo rule.
UI_PASSWORD = "alkera-local-dev-ui"
_UUID_NS = uuid.UUID("6a1c3c2e-0d7e-5d43-9b8e-7461626c6561")
#: The in-process half of the writer lock (the file lock is `Store.lock`).
LOCK = RLock()

app = FastAPI(title="Alkera Tableau Server (fake)", version=PRODUCT_VERSION)


# ================================================================== errors
class TableauError(Exception):
    """A refusal in Tableau's `<tsResponse><error code=..>` shape."""

    def __init__(self, status: int, code: str, summary: str, detail: str):
        super().__init__(detail)
        self.status, self.code, self.summary, self.detail = status, code, summary, detail


def _esc(value) -> str:
    return (str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _xml(tag: str, attrs: dict | None = None, *children: str, text: str | None = None) -> str:
    a = "".join(f' {k}="{_esc(v)}"' for k, v in (attrs or {}).items() if v is not None)
    inner = "".join(children) + (_esc(text) if text is not None else "")
    return f"<{tag}{a}>{inner}</{tag}>" if inner else f"<{tag}{a} />"


def _ts(*children: str, status: int = 200, headers: dict | None = None) -> Response:
    body = ("<?xml version='1.0' encoding='UTF-8'?>"
            f'<tsResponse xmlns="{NS}" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            f'xsi:schemaLocation="{SCHEMA}">' + "".join(children) + "</tsResponse>")
    return Response(body.encode("utf-8"), status_code=status,
                    media_type="application/xml;charset=utf-8", headers=headers)


@app.exception_handler(TableauError)
async def _tableau_error(request: Request, exc: TableauError) -> Response:
    if not getattr(request.state, "audited", False):
        # Refused before a handler opened its row (unknown path, bad API version,
        # oversized body): still one row per request.
        request.state.audited = True
        record(tool=f"{NAME}.request", args={"method": request.method,
                                             "path": request.url.path},
               op_class="read", targets=[f"{NAME}:server"], exit="error",
               error={"code": exc.code, "message": exc.detail}, transport="native")
    return _ts(_xml("error", {"code": exc.code}, _xml("summary", text=exc.summary),
                    _xml("detail", text=exc.detail)), status=exc.status)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
    if exc.status_code == 404:
        path = request.url.path.removeprefix("/api")
        return await _tableau_error(request, TableauError(
            404, "404003", "Resource Not Found", f"Unknown resource '{path}' specified in URI."))
    if exc.status_code == 405:
        return await _tableau_error(request, TableauError(
            405, "405000", "Method Not Allowed",
            f"Request type '{request.method}' is not supported for this resource."))
    return await _tableau_error(request, TableauError(exc.status_code, f"{exc.status_code}000",
                                                      "Bad Request", str(exc.detail)))


@app.exception_handler(RequestValidationError)
async def _bad_request(request: Request, exc: RequestValidationError) -> Response:
    return await _tableau_error(request, TableauError(
        400, "400000", "Bad Request", "The request parameters are invalid."))


def not_found(kind: str, code: str, ident: str) -> TableauError:
    return TableauError(404, code, "Resource Not Found",
                        f"{kind} '{ident}' could not be found.")


# ===================================================================== audit
class Row:
    """One 9.2.2 row for this request, written on the way out - failures too."""

    def __init__(self, request: Request, operation: str, *, op_class: str = "read",
                 targets: list[str] | None = None, args: dict | None = None):
        self.request = request
        self.tool = f"{NAME}.{operation}"
        self.op_class = op_class
        self.targets = list(targets or [])
        self.read_targets: list[str] | None = None
        self.flags: list[str] = []
        self.args = {"method": request.method, "path": request.url.path, **(args or {})}
        self.result: object = None
        self.started = time.monotonic()

    def __enter__(self) -> Row:
        return self

    def __exit__(self, kind, exc, tb) -> None:
        self.request.state.audited = True
        error = None
        if exc is not None:
            error = {"code": getattr(exc, "code", type(exc).__name__),
                     "message": getattr(exc, "detail", str(exc))}
        record(tool=self.tool, args=self.args, op_class=self.op_class,
               targets=self.targets or [f"{NAME}:server"],
               read_targets=self.read_targets if self.read_targets is not None else (
                   (self.targets or [f"{NAME}:server"]) if self.op_class == "read" else []),
               exit="error" if error else "ok", error=error, flags=self.flags,
               latency_ms=(time.monotonic() - self.started) * 1000,
               result=self.result, transport="native")


# ===================================================================== state
def uid(*parts) -> str:
    return str(uuid.uuid5(_UUID_NS, "/".join(map(str, parts))))


SITE_ID = uid("site", SITE_NAME)
PROJECT_ID = uid("project", PROJECT_NAME)


def projects() -> list[dict]:
    """The site's projects: Default alone (projects beyond it are not imitated).
    `GET .../projects` and the browser's /projects page both list these."""
    return [{"id": PROJECT_ID, "name": PROJECT_NAME, "description": PROJECT_DESCRIPTION}]


def project_of(item: dict) -> dict:
    """The project a published workbook or data source is in."""
    return find(projects(), item["project_id"], "Project", "404005")


class Store:
    """The server's published content, under `<bi_state_dir>/tableau/`."""

    def __init__(self, request: Request):
        context = context_of(request.app)
        self.config = context.config
        self.clock = context.clock
        self.root = Path(self.config.bi_state_dir) / "tableau"
        self.manifest_path = self.root / "manifest.json"
        self.base_url = str(request.base_url).rstrip("/")

    # -- persistence
    def load(self) -> dict:
        if not self.manifest_path.exists():
            self._seed()
        return json.loads(self.manifest_path.read_text("utf-8"))

    def save(self, state: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, sort_keys=True, indent=1), "utf-8")
        tmp.replace(self.manifest_path)

    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        return FileLock(str(self.root / "state.lock"))

    def now(self) -> str:
        return self.clock()

    def _seed(self) -> None:
        """Publish the world's own Tableau project: what the server held before
        the episode began."""
        state = {"counter": 0, "sessions": {}, "workbooks": [], "datasources": [],
                 "jobs": [], "extracts": {}}
        self.save(state)
        source = Path(self.config.workspace) / "bi" / "tableau"
        if not source.is_dir():
            return
        files = sorted(p for p in source.rglob("*") if p.is_file()
                       and not tproject._skip(p.relative_to(source)))
        # A file the server would not accept stays unpublished, and says so in
        # the manifest; the rest of the project is still published.
        errors = {}
        for suffixes, publish in ((tproject.DATASOURCE_SUFFIXES, self.publish_datasource),
                                  (tproject.WORKBOOK_SUFFIXES, self.publish_workbook)):
            for p in files:
                if p.suffix.lower() not in suffixes:
                    continue
                extra = {"show_tabs": True} if publish == self.publish_workbook else {}
                try:
                    publish(state, p.stem, p.read_bytes(), p.suffix[1:].lower(),
                            overwrite=True, **extra)
                except TableauError as exc:
                    errors[str(p.relative_to(source))] = exc.detail
        state["seeded_from"] = str(source)
        state["seed_errors"] = errors
        self.save(state)

    def next(self, state: dict) -> int:
        state["counter"] += 1
        return state["counter"]

    # -- content files
    def content_path(self, item: dict) -> Path:
        return self.root / "content" / f"{item['id']}.{item['file_type']}"

    def _store(self, item: dict, data: bytes) -> None:
        path = self.content_path(item)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        item["size"] = max(1, len(data) // 1_048_576)

    # -- publishing
    @staticmethod
    def _unique_url(items: list[dict], name: str, keep: str | None) -> str:
        base = ttds.content_url(name)
        taken = {i["content_url"] for i in items if i["id"] != keep}
        url, n = base, 0
        while url in taken:
            url = f"{base}_{n}"
            n += 1
        return url

    def publish_datasource(self, state: dict, name: str, data: bytes, file_type: str, *,
                           overwrite: bool, description: str | None = None) -> dict:
        try:
            xml = tproject.document_bytes(data, f"{name}.{file_type}")
            root = xmlio.parse(xml, f"{name}.{file_type}")
        except ModelError as exc:
            raise TableauError(400, "400011", "Publishing error", str(exc)) from None
        if root.tag != "datasource":
            raise TableauError(400, "400011", "Publishing error",
                               f"The file is not a Tableau data source (<{root.tag}>).")
        info = _connection_info(root)
        existing = next((d for d in state["datasources"] if d["name"] == name), None)
        if existing is not None and not overwrite:
            raise TableauError(409, "409005", "Conflict",
                               f"A data source named '{name}' already exists in project "
                               f"'{PROJECT_NAME}'; publish with overwrite.")
        now = self.now()
        if existing is None:
            existing = {"id": uid("datasource", name, self.next(state)), "name": name,
                        "content_url": self._unique_url(state["datasources"], name, None),
                        "created_at": now, "project_id": PROJECT_ID}
            state["datasources"].append(existing)
        existing.update({"file_type": file_type, "updated_at": now, "type": info["class"],
                         "server": info["server"], "port": info["port"],
                         "username": info["username"], "has_extract": info["extract"],
                         "caption": root.get("caption") or name})
        if description is not None:
            existing["description"] = description
        self._store(existing, data)
        self._initial_extract(state, ("datasource", existing["id"]), existing["has_extract"])
        return existing

    def _initial_extract(self, state: dict, owner: tuple[str, str], enabled: bool) -> None:
        """Publishing extract-enabled content takes its first copy. A source the
        server cannot read still publishes (Tableau would accept the file); its
        views then answer with the reason instead of numbers."""
        key = f"{owner[0]}:{owner[1]}"
        if not enabled:
            state["extracts"].pop(key, None)
            return
        try:
            self.refresh_extract(state, owner)
        except Exception as exc:                      # recorded, surfaced per view
            state["extracts"][key] = {"error": f"{type(exc).__name__}: {exc}"}

    def publish_workbook(self, state: dict, name: str, data: bytes, file_type: str, *,
                         overwrite: bool, show_tabs: bool,
                         description: str | None = None) -> dict:
        try:
            xml = tproject.document_bytes(data, f"{name}.{file_type}")
            root = xmlio.parse(xml, f"{name}.{file_type}")
        except ModelError as exc:
            raise TableauError(400, "400011", "Publishing error", str(exc)) from None
        if root.tag != "workbook":
            raise TableauError(400, "400011", "Publishing error",
                               f"The file is not a Tableau workbook (<{root.tag}>).")
        sheets = _sheets(root)
        existing = next((w for w in state["workbooks"] if w["name"] == name), None)
        if existing is not None and not overwrite:
            raise TableauError(403, "403007", "Overwrite forbidden",
                               "A workbook with the specified name already exists and the "
                               "overwrite parameter was not set to true.")
        now = self.now()
        if existing is None:
            existing = {"id": uid("workbook", name, self.next(state)), "name": name,
                        "content_url": self._unique_url(state["workbooks"], name, None),
                        "created_at": now, "project_id": PROJECT_ID}
            state["workbooks"].append(existing)
        url = existing["content_url"]
        existing.update({
            "file_type": file_type, "updated_at": now, "show_tabs": show_tabs,
            "views": [{"id": uid("view", existing["id"], sheet), "name": sheet,
                       "content_url": f"{url}/sheets/{re.sub(r'[^A-Za-z0-9_-]', '', sheet)}",
                       "sheet_type": kind} for sheet, kind in sheets],
            "connections": _workbook_connections(root)})
        if description is not None:
            existing["description"] = description
        self._store(existing, data)
        embedded_extract = any(c["extract"] for c in existing["connections"]
                               if c["class"] != "sqlproxy")
        existing["has_extract"] = embedded_extract
        self._initial_extract(state, ("workbook", existing["id"]), embedded_extract)
        return existing

    # -- models
    def datasource_by_url(self, state: dict, key: str) -> dict | None:
        for d in state["datasources"]:
            if key in (d["content_url"], d["name"], d.get("caption")):
                return d
        return None

    def read_datasource(self, item: dict) -> ttds.DataSource:
        data = self.content_path(item).read_bytes()
        return tproject.read_datasource_bytes(data, f"{item['name']}.{item['file_type']}",
                                              strict=False)

    def read_workbook(self, state: dict, item: dict, deferred: dict,
                      data: bytes | None = None):
        """The published workbook, or `data` as it would read if published in
        its place (the browser checks an edit this way before publishing it)."""
        def resolve(name):
            d = self.datasource_by_url(state, name)
            return None if d is None else self.read_datasource(d)

        if data is None:
            data = self.content_path(item).read_bytes()
        return tproject.read_workbook_bytes(data, f"{item['name']}.{item['file_type']}",
                                            strict=False, resolve_published=resolve,
                                            deferred=deferred)

    def field_captions(self, state: dict, item: dict, ds) -> dict[str, str]:
        """Field name -> caption, for the fields of workbook `item`'s data
        source `ds` that have one: the `caption` of their `<column>` in the
        document that defines them - the published data source when the
        workbook connects to one, else the copy the workbook embeds."""
        if ds.published:
            source = self.datasource_by_url(state, ds.published)
            if source is None:
                return {}
            what = f"{source['name']}.{source['file_type']}"
            elem = xmlio.parse(tproject.document_bytes(
                self.content_path(source).read_bytes(), what), what)
        else:
            what = f"{item['name']}.{item['file_type']}"
            root = xmlio.parse(tproject.document_bytes(
                self.content_path(item).read_bytes(), what), what)
            elem = next((d for d in root.findall("datasources/datasource")
                         if d.get("name") == ds.name), None)
            if elem is None:
                return {}
        return {c.get("name")[1:-1].replace("]]", "]"): c.get("caption")
                for c in elem.findall("column") if c.get("caption")
                and c.get("name", "").startswith("[") and "].[" not in c.get("name")}

    def warehouse_for(self, state: dict, owner: tuple[str, str]) -> tuple[Path, str | None]:
        """The DuckDB file a view of `owner`'s data renders from: its extract copy,
        or the live warehouse."""
        extract = state["extracts"].get(f"{owner[0]}:{owner[1]}")
        if extract is not None and "error" in extract:
            raise TableauError(400, "400000", "Bad Request",
                               f"The extract could not be created: {extract['error']}")
        if extract is not None:
            return self.root / extract["file"], extract["refreshed_at"]
        if self.config.warehouse_backend != "duckdb":
            raise TableauError(400, "400000", "Bad Request",
                               f"unsupported: live connection to a {self.config.warehouse_backend}"
                               f" warehouse - this server renders from the DuckDB warehouse only")
        return Path(self.config.warehouse), None

    def refresh_extract(self, state: dict, owner: tuple[str, str]) -> dict:
        """Copy every relation the owner's model reads into its extract file."""
        kind, ident = owner
        items = state["datasources"] if kind == "datasource" else state["workbooks"]
        item = next(i for i in items if i["id"] == ident)
        if kind == "datasource":
            model = self.read_datasource(item).model
        else:
            model = self.read_workbook(state, item, {})[0].model
        relations = sorted(_relations(model))
        rel_file = f"extracts/{kind}-{ident}-{self.next(state)}.duckdb"
        target = self.root / rel_file
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_relations(Path(self.config.warehouse), target, relations)
        old = state["extracts"].get(f"{kind}:{ident}")
        if old is not None and old.get("file") not in (None, rel_file):
            (self.root / old["file"]).unlink(missing_ok=True)
        entry = {"file": rel_file, "refreshed_at": self.now(), "relations": relations}
        state["extracts"][f"{kind}:{ident}"] = entry
        return entry


def _relations(model) -> set[str]:
    """Warehouse relations a model reads: Table.source, and the tables named in
    native SQL."""
    import sqlglot
    from sqlglot import exp

    out = set()
    for t in model.tables:
        if t.source:
            out.add(t.source)
        elif t.source_sql:
            for table in sqlglot.parse_one(t.source_sql, read="duckdb").find_all(exp.Table):
                if table.db:
                    out.add(f"{table.db}.{table.name}")
                else:
                    out.add(table.name)
    return out


def visual_relations(model, visual) -> list[str]:
    """The warehouse relations ONE view's query reads, as `schema.table`.

    `_relations(model)` is every relation the data source declares, which is
    right for an extract (the copy takes them all) and wrong for a view: before
    this a `views/{id}/data` row for the KPIs card named all five marts in
    `read_targets` while the query the engine ran reads `warehouse.fct_orders`
    alone (measured 2026-10-04, tasks/harness/bi_e2e_check.py; Sigma's export
    row already names only the element's relations). So the view is compiled
    the way `world.semantic.render` compiles it, and the tables named in that
    SQL and in its pre-flight checks are the answer - including a table a join
    passes through, which a walk over the visual's expressions would miss.
    Called after the render succeeded, so the visual is known to compile."""
    import sqlglot
    from sqlglot import exp
    from world.semantic import compile as semantic_compile

    if visual.sql is not None:
        statements = [visual.sql]
    else:
        graph = semantic_compile.check_model(model)
        semantic_compile.check_visual(graph, visual)
        compiled = semantic_compile.compile_visual(graph, visual)
        statements = [compiled.sql, *(check.sql for check in compiled.checks)]
    out: set[str] = set()
    for sql in statements:
        tree = sqlglot.parse_one(sql, read="duckdb")
        ctes = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
        for table in tree.find_all(exp.Table):
            if not table.name or (not table.db and table.name in ctes):
                continue
            out.add(f"{table.db}.{table.name}" if table.db else table.name)
    return sorted(out)


def _copy_relations(warehouse: Path, target: Path, relations: list[str]) -> None:
    import duckdb

    if target.exists():
        target.unlink()
    with duckdb.connect(str(target), config={"threads": 1}) as con:
        con.execute("ATTACH '" + str(warehouse).replace("'", "''") + "' AS src (READ_ONLY)")
        for rel in relations:
            parts = rel.split(".")
            schema, name = (parts[-2], parts[-1]) if len(parts) > 1 else ("main", parts[0])
            q = '"' + schema.replace('"', '""') + '"'
            n = '"' + name.replace('"', '""') + '"'
            con.execute(f"CREATE SCHEMA IF NOT EXISTS {q}")
            con.execute(f"CREATE TABLE {q}.{n} AS SELECT * FROM src.{q}.{n}")
        con.execute("DETACH src")


def _connection_info(root: ET.Element) -> dict:
    conn = root.find("connection")
    info = {"class": "", "server": "", "port": "", "username": "",
            "extract": False, "published": None}
    if conn is None:
        return info
    extract = root.find("extract")
    info["extract"] = extract is not None and extract.get("enabled", "true") == "true"
    if conn.get("class") == "sqlproxy":
        loc = root.find("repository-location")
        info.update({"class": "sqlproxy", "server": conn.get("server", ""),
                     "port": conn.get("port", ""), "username": conn.get("username", ""),
                     "published": (loc.get("id") if loc is not None and loc.get("id")
                                   else conn.get("dbname"))})
        return info
    inner = conn.find("named-connections/named-connection/connection")
    src = inner if inner is not None else conn
    info.update({"class": src.get("class", ""), "server": src.get("server", ""),
                 "port": src.get("port", ""), "username": src.get("username", "")})
    return info


def _workbook_connections(root: ET.Element) -> list[dict]:
    out = []
    for d in root.findall("datasources/datasource"):
        if d.get("name") == "Parameters":
            continue
        info = _connection_info(d)
        info["name"] = d.get("caption") or d.get("name")
        info["ds_name"] = d.get("name")
        out.append(info)
    return out


def _sheets(root: ET.Element) -> list[tuple[str, str]]:
    """The workbook's views in tab order: (name, 'worksheet'|'dashboard'|'story').
    Sheets whose window is hidden are not published as views."""
    kinds = {}
    for ws in root.findall("worksheets/worksheet"):
        kinds[ws.get("name")] = "worksheet"
    for d in root.findall("dashboards/dashboard"):
        kinds[d.get("name")] = "story" if d.get("type") == "storyboard" else "dashboard"
    hidden = {w.get("name") for w in root.findall("windows/window") if w.get("hidden") == "true"}
    ordered = [w.get("name") for w in root.findall("windows/window") if w.get("name") in kinds]
    ordered += [n for n in kinds if n not in ordered]
    return [(n, kinds[n]) for n in ordered if n not in hidden]


# =================================================================== helpers
def _version(version: str) -> None:
    if not re.fullmatch(r"\d+\.\d+", version):
        raise StarletteHTTPException(404)


def _auth(request: Request, state: dict, site: str | None = None) -> dict:
    token = request.headers.get("x-tableau-auth")
    if not token:
        raise TableauError(401, "401000", "Unauthorized Access",
                           "No authentication credentials were provided.")
    session = state["sessions"].get(token)
    if session is None:
        raise TableauError(401, "401002", "Unauthorized Access",
                           "Invalid authentication credentials were provided.")
    if site is not None and site != session["site_id"]:
        raise TableauError(403, "403069", "Forbidden",
                           "The site ID in the URI doesn't match the site ID in the "
                           "authentication token.")
    return session


def _parse_request(raw: bytes) -> ET.Element:
    if b"<!doctype" in raw[:4096].lower() or b"<!entity" in raw.lower():
        raise TableauError(400, "400000", "Bad Request", "Malformed tsRequest.")
    try:
        root = ET.fromstring(raw) if raw.strip() else ET.Element("tsRequest")
    except ET.ParseError:
        raise TableauError(400, "400000", "Bad Request",
                           "The request body is not well-formed XML.") from None
    for elem in root.iter():                    # drop a namespace if a client sends one
        if isinstance(elem.tag, str) and elem.tag.startswith("{"):
            elem.tag = elem.tag.split("}", 1)[1]
    return root


async def _body(request: Request, limit: int) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise TableauError(413, "413000", "Payload Too Large",
                               "Files over 64 MB are published in chunks (fileUploads), which "
                               "this server does not accept.")
    return bytes(body)


def _multipart(content_type: str, body: bytes) -> dict[str, tuple[str | None, bytes]]:
    """multipart/mixed (TSC, tabcmd) or multipart/form-data -> {name: (filename, data)}."""
    if not content_type.lower().startswith("multipart/"):
        raise TableauError(400, "400000", "Bad Request",
                           "Publishing requires a multipart/mixed request body.")
    msg = email.message_from_bytes(b"Content-Type: " + content_type.encode("latin-1")
                                   + b"\r\n\r\n" + body, policy=email.policy.HTTP)
    parts = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if name:
            parts[name] = (part.get_filename(), part.get_payload(decode=True) or b"")
    return parts


def _paginate(request: Request, items: list[dict]) -> tuple[list[dict], str]:
    try:
        size = int(request.query_params.get("pageSize", 100))
        number = int(request.query_params.get("pageNumber", 1))
    except ValueError:
        raise TableauError(400, "400006", "Bad Request",
                           "Invalid page number or page size.") from None
    if size < 1 or number < 1 or size > 1000:
        raise TableauError(400, "400006", "Bad Request", "Invalid page number or page size.")
    page = items[(number - 1) * size: number * size]
    return page, _xml("pagination", {"pageNumber": number, "pageSize": size,
                                     "totalAvailable": len(items)})


_CASELESS = {"name", "projectName", "ownerName", "caption"}


def _filters(request: Request, fields: dict) -> list:
    """`filter=name:eq:Commerce,projectName:eq:Default` -> predicates over items.
    Names compare case-insensitively (tabcmd looks the project up as "Default")."""
    raw = request.query_params.get("filter")
    out = []
    if not raw:
        return out
    for clause in re.split(r",(?![^\[]*\])", raw):
        try:
            field, op, value = clause.split(":", 2)
        except ValueError:
            raise TableauError(400, "400065", "Bad Request",
                               f"Invalid filter expression '{clause}'.") from None
        if field not in fields:
            raise TableauError(400, "400065", "Bad Request",
                               f"Filter field '{field}' is not supported for this resource.")
        getter = fields[field]
        fold = (lambda s: s.lower()) if field in _CASELESS else (lambda s: s)
        if op == "eq":
            out.append(lambda item, g=getter, v=value, f=fold: f(str(g(item))) == f(v))
        elif op == "in":
            values = {fold(v) for v in value.strip("[]").split(",")}
            out.append(lambda item, g=getter, vs=values, f=fold: f(str(g(item))) in vs)
        elif op == "has":
            out.append(lambda item, g=getter, v=value, f=fold: f(v) in f(str(g(item))))
        else:
            raise TableauError(400, "400065", "Bad Request",
                               f"Filter operator '{op}' is not supported for '{field}'.")
    return out


def _select(request: Request, items: list[dict], fields: dict) -> list[dict]:
    preds = _filters(request, fields)
    out = [i for i in items if all(p(i) for p in preds)]
    sort = request.query_params.get("sort")
    if sort:
        for clause in reversed(sort.split(",")):
            field, _, direction = clause.partition(":")
            if field in fields:
                out.sort(key=lambda i, g=fields[field]: str(g(i)), reverse=direction == "desc")
    return out


def _owner_xml() -> str:
    return _xml("owner", {"id": uid("user", DEFAULT_USER), "name": DEFAULT_USER})


def _project_xml() -> str:
    return _xml("project", {"id": PROJECT_ID, "name": PROJECT_NAME})


def _web(store: Store, kind: str, item: dict) -> str:
    return f"{store.base_url}/#/{kind}/{item['id']}"


def _workbook_xml(store: Store, wb: dict, *, views: bool = False) -> str:
    children = [_project_xml(), _owner_xml(), _xml("tags")]
    if views:
        children.append(_xml("views", None, *(_view_xml(v, wb, short=True)
                                              for v in wb["views"])))
    first = wb["views"][0]["id"] if wb["views"] else None
    return _xml("workbook", {
        "id": wb["id"], "name": wb["name"], "description": wb.get("description", ""),
        "contentUrl": wb["content_url"], "webpageUrl": _web(store, "workbooks", wb),
        "showTabs": str(wb.get("show_tabs", True)).lower(), "size": wb.get("size", 1),
        "createdAt": wb["created_at"], "updatedAt": wb["updated_at"],
        "encryptExtracts": "false", "defaultViewId": first,
        "hasExtracts": str(wb.get("has_extract", False)).lower()}, *children)


def _view_xml(view: dict, wb: dict, *, short: bool = False) -> str:
    attrs = {"id": view["id"], "name": view["name"], "contentUrl": view["content_url"]}
    if short:
        return _xml("view", attrs)
    attrs.update({"createdAt": wb["created_at"], "updatedAt": wb["updated_at"],
                  "viewUrlName": view["content_url"].rsplit("/", 1)[-1],
                  "sheetType": view["sheet_type"]})
    return _xml("view", attrs, _xml("workbook", {"id": wb["id"]}), _owner_xml(),
                _xml("project", {"id": PROJECT_ID}), _xml("tags"))


def _datasource_xml(store: Store, ds: dict) -> str:
    return _xml("datasource", {
        "id": ds["id"], "name": ds["name"], "description": ds.get("description", ""),
        "contentUrl": ds["content_url"], "type": ds["type"],
        "createdAt": ds["created_at"], "updatedAt": ds["updated_at"],
        "encryptExtracts": "false", "hasExtracts": str(ds["has_extract"]).lower(),
        "isCertified": "false", "useRemoteQueryAgent": "false",
        "webpageUrl": _web(store, "datasources", ds), "size": ds.get("size", 1)},
        _project_xml(), _owner_xml(), _xml("tags"))


def _job_xml(job: dict, *, full: bool) -> str:
    target = _xml(job["target_kind"], {"id": job["target_id"], "name": job["target_name"]})
    inner = [target] + ([_xml("notes", text=job["notes"])] if full and job.get("notes") else [])
    attrs = {"id": job["id"], "mode": "Asynchronous", "type": job["type"],
             "createdAt": job["created_at"]}
    if full:
        attrs.update({"progress": "100", "startedAt": job["started_at"],
                      "updatedAt": job["completed_at"], "completedAt": job["completed_at"],
                      "finishCode": str(job["finish_code"])})
    return _xml("job", attrs, _xml("extractRefreshJob", None, *inner))


def find(items: list[dict], ident: str, kind: str, code: str) -> dict:
    for item in items:
        if item["id"] == ident:
            return item
    raise not_found(kind, code, ident)


# ===================================================================== routes
@app.get("/health")
def health(request: Request) -> object:
    """Published workbooks + data sources, counted from the server's state."""
    with Row(request, "health", targets=[f"health:{NAME}"]) as row, LOCK:
        store = Store(request)
        with store.lock():
            state = store.load()
        items = len(state["workbooks"]) + len(state["datasources"])
        row.read_targets = [f"{NAME}:workbook:{w['content_url']}" for w in state["workbooks"]] \
            + [f"{NAME}:datasource:{d['content_url']}" for d in state["datasources"]]
        row.result = {"items": items}
        return {"ok": True, "service": NAME, "items": items}


def _serverinfo(request: Request) -> Response:
    with Row(request, "server_info", targets=[f"{NAME}:server"]) as row:
        row.result = {"restApiVersion": API_VERSION}
        return _ts(_xml("serverInfo", None,
                        _xml("productVersion", {"build": BUILD}, text=PRODUCT_VERSION),
                        _xml("restApiVersion", text=API_VERSION)))


@app.get("/api/serverinfo")
@app.get("/api/serverInfo")
def serverinfo_unversioned(request: Request) -> Response:
    return _serverinfo(request)


@app.get("/api/{version}/serverinfo")
@app.get("/api/{version}/serverInfo")
def serverinfo(version: str, request: Request) -> Response:
    _version(version)
    return _serverinfo(request)


@app.post("/api/{version}/auth/signin")
async def signin(version: str, request: Request) -> Response:
    """Personal access token or name/password; the secret is the workspace's
    obviously-fake credential. Anything else is Tableau's 401001."""
    _version(version)
    raw = await _body(request, 1_048_576)
    with Row(request, "auth_signin", targets=[f"{NAME}:session"]) as row:
        creds = _parse_request(raw).find("credentials")
        if creds is None:
            raise TableauError(400, "400000", "Bad Request", "Missing <credentials>.")
        site = creds.find("site")
        content_url = site.get("contentUrl", "") if site is not None else ""
        pat_name = creds.get("personalAccessTokenName")
        user = pat_name and DEFAULT_USER or creds.get("name") or DEFAULT_USER
        row.args.update({"user": creds.get("name") or pat_name, "site": content_url,
                         "method_kind": "pat" if pat_name else "password"})
        secret = creds.get("personalAccessTokenSecret") if pat_name else creds.get("password")
        if secret != DEV_TOKEN or content_url not in ("", SITE_NAME.lower()):
            raise TableauError(401, "401001", "Signin Error",
                               "Error signing in to Tableau Server")
        with LOCK:
            store = Store(request)
            with store.lock():
                state = store.load()
                n = store.next(state)
                token = hashlib.sha256(f"session/{n}/{user}".encode()).hexdigest()[:32]
                user_id = uid("user", user)
                state["sessions"][token] = {"user": user, "user_id": user_id,
                                            "site_id": SITE_ID}
                store.save(state)
        row.result = {"user": user, "site": SITE_ID}
        return _ts(_xml("credentials", {"token": token, "estimatedTimeToExpiration": "365:23:59"},
                        _xml("site", {"id": SITE_ID, "contentUrl": ""}),
                        _xml("user", {"id": user_id})))


@app.post("/api/{version}/auth/signout")
def signout(version: str, request: Request) -> Response:
    _version(version)
    with Row(request, "auth_signout", targets=[f"{NAME}:session"]), LOCK:
        store = Store(request)
        with store.lock():
            state = store.load()
            _auth(request, state)
            state["sessions"].pop(request.headers.get("x-tableau-auth"), None)
            store.save(state)
        return Response(status_code=204)


def _site_xml() -> str:
    return _xml("site", {"id": SITE_ID, "name": SITE_NAME, "contentUrl": "",
                         "adminMode": "ContentAndUsers", "state": "Active",
                         "revisionHistoryEnabled": "true", "revisionLimit": "25",
                         "subscribeOthersEnabled": "true", "catalogingEnabled": "true",
                         "runNowEnabled": "true", "useDefaultTimeZone": "true"})


def _read_state(request: Request, row: Row, site: str | None = None) -> tuple[Store, dict]:
    with LOCK:
        store = Store(request)
        with store.lock():
            state = store.load()
        session = _auth(request, state, site)
        row.args["user"] = session["user"]
        return store, state


@app.get("/api/{version}/sites")
def sites(version: str, request: Request) -> Response:
    _version(version)
    with Row(request, "sites_list", targets=[f"{NAME}:site:{SITE_NAME}"]) as row:
        _read_state(request, row)
        page, pagination = _paginate(request, [{}])
        return _ts(pagination, _xml("sites", None, *(_site_xml() for _ in page)))


@app.get("/api/{version}/sites/{site}")
def site_get(version: str, site: str, request: Request) -> Response:
    _version(version)
    with Row(request, "sites_get", targets=[f"{NAME}:site:{SITE_NAME}"]) as row:
        _read_state(request, row)
        key = request.query_params.get("key", "id")
        match = {"id": SITE_ID, "name": SITE_NAME, "contentUrl": ""}.get(key)
        if site != match and not (key == "name" and site.lower() == SITE_NAME.lower()):
            raise not_found("Site", "404000", site)
        return _ts(_site_xml())


@app.get("/api/{version}/sites/{site}/users/{user_id}")
def user_get(version: str, site: str, user_id: str, request: Request) -> Response:
    _version(version)
    with Row(request, "users_get", targets=[f"{NAME}:user:{user_id}"]) as row:
        _, state = _read_state(request, row, site)
        users = {s["user_id"]: s["user"] for s in state["sessions"].values()}
        users.setdefault(uid("user", DEFAULT_USER), DEFAULT_USER)
        if user_id not in users:
            raise not_found("User", "404002", user_id)
        name = users[user_id]
        return _ts(_xml("user", {"id": user_id, "name": name, "fullName": name,
                                 "siteRole": "SiteAdministratorCreator",
                                 "authSetting": "ServerDefault", "lastLogin": None},
                        _xml("domain", {"name": "local"})))


@app.get("/api/{version}/sites/{site}/projects")
def projects_list(version: str, site: str, request: Request) -> Response:
    _version(version)
    with Row(request, "projects_list", targets=[f"{NAME}:project:{PROJECT_NAME}"]) as row:
        _read_state(request, row, site)
        items = _select(request, projects(),
                        {"name": lambda p: p["name"], "id": lambda p: p["id"],
                         "parentProjectId": lambda p: "", "topLevelProject": lambda p: "true"})
        page, pagination = _paginate(request, items)
        return _ts(pagination, _xml("projects", None, *(
            _xml("project", {"id": p["id"], "name": p["name"],
                             "description": p["description"],
                             "contentPermissions": "ManagedByOwner",
                             "topLevelProject": "true", "writeable": "true"},
                 _owner_xml()) for p in page)))


# ----------------------------------------------------------------- workbooks
_WB_FIELDS = {"name": lambda w: w["name"], "contentUrl": lambda w: w["content_url"],
              "projectName": lambda w: PROJECT_NAME, "id": lambda w: w["id"],
              "ownerName": lambda w: DEFAULT_USER, "createdAt": lambda w: w["created_at"],
              "updatedAt": lambda w: w["updated_at"], "projectId": lambda w: w["project_id"]}


@app.get("/api/{version}/sites/{site}/workbooks")
def workbooks_list(version: str, site: str, request: Request) -> Response:
    _version(version)
    with Row(request, "workbooks_list") as row:
        store, state = _read_state(request, row, site)
        items = _select(request, state["workbooks"], _WB_FIELDS)
        page, pagination = _paginate(request, items)
        row.targets = [f"{NAME}:workbook:{w['content_url']}" for w in page] or \
            [f"{NAME}:workbooks"]
        row.result = {"workbooks": [w["name"] for w in page]}
        return _ts(pagination, _xml("workbooks", None,
                                    *(_workbook_xml(store, w) for w in page)))


@app.get("/api/{version}/sites/{site}/workbooks/{wb_id}")
def workbooks_get(version: str, site: str, wb_id: str, request: Request) -> Response:
    _version(version)
    with Row(request, "workbooks_get") as row:
        store, state = _read_state(request, row, site)
        wb = find(state["workbooks"], wb_id, "Workbook", "404006")
        row.targets = [f"{NAME}:workbook:{wb['content_url']}"]
        return _ts(_workbook_xml(store, wb, views=True))


@app.get("/api/{version}/sites/{site}/workbooks/{wb_id}/views")
def workbooks_views(version: str, site: str, wb_id: str, request: Request) -> Response:
    _version(version)
    with Row(request, "workbooks_views") as row:
        _, state = _read_state(request, row, site)
        wb = find(state["workbooks"], wb_id, "Workbook", "404006")
        row.targets = [f"{NAME}:workbook:{wb['content_url']}"]
        return _ts(_xml("views", None, *(_view_xml(v, wb, short=True) for v in wb["views"])))


@app.get("/api/{version}/sites/{site}/workbooks/{wb_id}/connections")
def workbooks_connections(version: str, site: str, wb_id: str, request: Request) -> Response:
    _version(version)
    with Row(request, "workbooks_connections") as row:
        _, state = _read_state(request, row, site)
        wb = find(state["workbooks"], wb_id, "Workbook", "404006")
        row.targets = [f"{NAME}:workbook:{wb['content_url']}"]
        out = []
        for i, c in enumerate(wb.get("connections", [])):
            published = c["published"] and next(
                (d for d in state["datasources"] if c["published"] in
                 (d["content_url"], d["name"])), None)
            ds_id = published["id"] if published else uid("embedded", wb["id"], i)
            out.append(_xml("connection", {
                "id": uid("connection", wb["id"], i), "type": c["class"],
                "embedPassword": "false", "serverAddress": c["server"],
                "serverPort": c["port"], "userName": c["username"],
                "queryTaggingEnabled": "false"},
                _xml("datasource", {"id": ds_id, "name": c["name"]})))
        return _ts(_xml("connections", None, *out))


@app.get("/api/{version}/sites/{site}/workbooks/{wb_id}/content")
def workbooks_download(version: str, site: str, wb_id: str, request: Request) -> Response:
    _version(version)
    with Row(request, "workbooks_download") as row:
        store, state = _read_state(request, row, site)
        wb = find(state["workbooks"], wb_id, "Workbook", "404006")
        row.targets = [f"{NAME}:workbook:{wb['content_url']}"]
        data = store.content_path(wb).read_bytes()
        row.result = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        media = "application/xml" if wb["file_type"] == "twb" else "application/octet-stream"
        return Response(data, media_type=media, headers={
            "Content-Disposition":
                f'name="tableau_workbook"; filename="{wb["name"]}.{wb["file_type"]}"'})


async def _publish(request: Request, version: str, site: str, kind: str) -> Response:
    _version(version)
    body = await _body(request, MAX_UPLOAD)
    with Row(request, f"{kind}s_publish", op_class="write") as row:
        store, state = _read_state(request, row, site)
        if request.query_params.get("uploadSessionId"):
            raise TableauError(400, "400000", "Bad Request",
                               "Chunked uploads (fileUploads) are not accepted by this server.")
        if request.query_params.get("append", "false").lower() == "true":
            raise TableauError(400, "400011", "Publishing error",
                               "Append applies to extract (.hyper) files only.")
        parts = _multipart(request.headers.get("content-type", ""), body)
        payload = parts.get("request_payload")
        file_part = parts.get(f"tableau_{kind}")
        if payload is None or file_part is None:
            raise TableauError(400, "400011", "Publishing error",
                               f"The request needs a request_payload part and a "
                               f"tableau_{kind} part.")
        item_xml = _parse_request(payload[1]).find(kind)
        if item_xml is None or not item_xml.get("name"):
            raise TableauError(400, "400011", "Publishing error",
                               f"The request payload has no <{kind} name=...>.")
        project = item_xml.find("project")
        if project is not None and project.get("id") not in (None, "", PROJECT_ID):
            raise not_found("Project", "404005", project.get("id"))
        filename = file_part[0] or ""
        allowed = ("twb", "twbx") if kind == "workbook" else ("tds", "tdsx")
        file_type = (request.query_params.get(f"{kind}Type")
                     or filename.rsplit(".", 1)[-1]).lower()
        if file_type not in allowed:
            raise TableauError(400, "400035", "Bad Request",
                               f"Missing or invalid file type: {file_type!r}; expected "
                               f"{' or '.join(allowed)}.")
        overwrite = request.query_params.get("overwrite", "false").lower() == "true"
        name = item_xml.get("name")
        row.args.update({"name": name, "file": filename, "overwrite": overwrite})
        with LOCK, store.lock():
            state = store.load()
            if kind == "workbook":
                item = store.publish_workbook(
                    state, name, file_part[1], file_type, overwrite=overwrite,
                    show_tabs=item_xml.get("showTabs", "false") == "true",
                    description=item_xml.get("description"))
            else:
                item = store.publish_datasource(state, name, file_part[1], file_type,
                                                overwrite=overwrite,
                                                description=item_xml.get("description"))
            store.save(state)
        row.targets = [f"{NAME}:{kind}:{item['content_url']}"]
        if overwrite:
            row.flags.append("overwrite")
        row.result = {kind: item["name"], "id": item["id"], "sha256": hashlib.sha256(
            file_part[1]).hexdigest(), "extract": item.get("has_extract", False)}
        if request.query_params.get("asJob", "false").lower() == "true":
            job = _job(store, state, "PublishWorkbook" if kind == "workbook"
                       else "PublishDatasource", kind, item, 0, None)
            return _ts(_job_xml(job, full=False), status=202)
        if kind == "workbook":
            return _ts(_workbook_xml(store, item, views=True), status=201)
        return _ts(_datasource_xml(store, item), status=201)


@app.post("/api/{version}/sites/{site}/workbooks")
async def workbooks_publish(version: str, site: str, request: Request) -> Response:
    return await _publish(request, version, site, "workbook")


@app.post("/api/{version}/sites/{site}/datasources")
async def datasources_publish(version: str, site: str, request: Request) -> Response:
    return await _publish(request, version, site, "datasource")


def _job(store: Store, state: dict, jtype: str, kind: str, item: dict, code: int,
         notes: str | None) -> dict:
    with store.lock():
        fresh = store.load()
        n = store.next(fresh)
        now = store.now()
        job = {"id": uid("job", n), "type": jtype, "created_at": now, "started_at": now,
               "completed_at": now, "finish_code": code, "target_kind": kind,
               "target_id": item["id"], "target_name": item["name"], "notes": notes}
        fresh["jobs"].append(job)
        store.save(fresh)
    return job


def _refresh(request: Request, version: str, site: str, kind: str, ident: str) -> Response:
    _version(version)
    with Row(request, f"{kind}s_refresh", op_class="write") as row:
        store, state = _read_state(request, row, site)
        items = state["workbooks" if kind == "workbook" else "datasources"]
        item = find(items, ident, "Workbook" if kind == "workbook" else "Data source",
                     "404006" if kind == "workbook" else "404004")
        row.targets = [f"{NAME}:{kind}:{item['content_url']}"]
        if not item.get("has_extract"):
            raise TableauError(403, "403104", "Forbidden",
                               f"The {kind} '{item['name']}' uses a live connection; there is "
                               f"no extract to refresh.")
        notes, code = None, 0
        with LOCK, store.lock():
            state = store.load()
            try:
                entry = store.refresh_extract(state, (kind, item["id"]))
                row.read_targets = entry["relations"]
                store.save(state)
            except Exception as exc:              # a failed refresh is a failed job
                notes, code = f"{type(exc).__name__}: {exc}", 1
                row.read_targets = []
        job = _job(store, state, "RefreshExtract", kind, item, code, notes)
        row.result = {"job": job["id"], "finishCode": code}
        return _ts(_job_xml(job, full=False), status=202)


@app.post("/api/{version}/sites/{site}/workbooks/{wb_id}/refresh")
def workbooks_refresh(version: str, site: str, wb_id: str, request: Request) -> Response:
    return _refresh(request, version, site, "workbook", wb_id)


@app.post("/api/{version}/sites/{site}/datasources/{ds_id}/refresh")
def datasources_refresh(version: str, site: str, ds_id: str, request: Request) -> Response:
    return _refresh(request, version, site, "datasource", ds_id)


# --------------------------------------------------------------- datasources
_DS_FIELDS = {"name": lambda d: d["name"], "contentUrl": lambda d: d["content_url"],
              "projectName": lambda d: PROJECT_NAME, "id": lambda d: d["id"],
              "type": lambda d: d["type"], "hasExtracts": lambda d: str(d["has_extract"]).lower(),
              "ownerName": lambda d: DEFAULT_USER, "createdAt": lambda d: d["created_at"],
              "updatedAt": lambda d: d["updated_at"], "projectId": lambda d: d["project_id"]}


@app.get("/api/{version}/sites/{site}/datasources")
def datasources_list(version: str, site: str, request: Request) -> Response:
    _version(version)
    with Row(request, "datasources_list") as row:
        store, state = _read_state(request, row, site)
        items = _select(request, state["datasources"], _DS_FIELDS)
        page, pagination = _paginate(request, items)
        row.targets = [f"{NAME}:datasource:{d['content_url']}" for d in page] or \
            [f"{NAME}:datasources"]
        return _ts(pagination, _xml("datasources", None,
                                    *(_datasource_xml(store, d) for d in page)))


@app.get("/api/{version}/sites/{site}/datasources/{ds_id}")
def datasources_get(version: str, site: str, ds_id: str, request: Request) -> Response:
    _version(version)
    with Row(request, "datasources_get") as row:
        store, state = _read_state(request, row, site)
        ds = find(state["datasources"], ds_id, "Data source", "404004")
        row.targets = [f"{NAME}:datasource:{ds['content_url']}"]
        return _ts(_datasource_xml(store, ds))


@app.get("/api/{version}/sites/{site}/datasources/{ds_id}/connections")
def datasources_connections(version: str, site: str, ds_id: str, request: Request) -> Response:
    _version(version)
    with Row(request, "datasources_connections") as row:
        _, state = _read_state(request, row, site)
        ds = find(state["datasources"], ds_id, "Data source", "404004")
        row.targets = [f"{NAME}:datasource:{ds['content_url']}"]
        return _ts(_xml("connections", None, _xml("connection", {
            "id": uid("connection", ds["id"]), "type": ds["type"], "embedPassword": "false",
            "serverAddress": ds["server"], "serverPort": ds["port"],
            "userName": ds["username"], "queryTaggingEnabled": "false"})))


@app.get("/api/{version}/sites/{site}/datasources/{ds_id}/content")
def datasources_download(version: str, site: str, ds_id: str, request: Request) -> Response:
    _version(version)
    with Row(request, "datasources_download") as row:
        store, state = _read_state(request, row, site)
        ds = find(state["datasources"], ds_id, "Data source", "404004")
        row.targets = [f"{NAME}:datasource:{ds['content_url']}"]
        data = store.content_path(ds).read_bytes()
        row.result = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        media = "application/xml" if ds["file_type"] == "tds" else "application/octet-stream"
        return Response(data, media_type=media, headers={
            "Content-Disposition":
                f'name="tableau_datasource"; filename="{ds["name"]}.{ds["file_type"]}"'})


# --------------------------------------------------------------------- views
def all_views(state: dict) -> list[tuple[dict, dict]]:
    return [(v, wb) for wb in state["workbooks"] for v in wb["views"]]


_VIEW_FIELDS = {"name": lambda vw: vw[0]["name"], "contentUrl": lambda vw: vw[0]["content_url"],
                "id": lambda vw: vw[0]["id"], "workbookName": lambda vw: vw[1]["name"],
                "projectName": lambda vw: PROJECT_NAME, "ownerName": lambda vw: DEFAULT_USER,
                "viewUrlName": lambda vw: vw[0]["content_url"].rsplit("/", 1)[-1],
                "sheetType": lambda vw: vw[0]["sheet_type"]}


@app.get("/api/{version}/sites/{site}/views")
def views_list(version: str, site: str, request: Request) -> Response:
    _version(version)
    with Row(request, "views_list") as row:
        _, state = _read_state(request, row, site)
        items = _select(request, all_views(state), _VIEW_FIELDS)
        page, pagination = _paginate(request, items)
        row.targets = [f"{NAME}:view:{v['content_url']}" for v, _ in page] or [f"{NAME}:views"]
        return _ts(pagination, _xml("views", None, *(_view_xml(v, wb) for v, wb in page)))


def _view(state: dict, view_id: str, code: str = "404011") -> tuple[dict, dict]:
    for v, wb in all_views(state):
        if v["id"] == view_id:
            return v, wb
    if code == "400080":
        raise TableauError(400, "400080", "Bad Request",
                           f"The view ID '{view_id}' doesn't correspond to a view available "
                           f"on the specified site.")
    raise not_found("View", code, view_id)


@app.get("/api/{version}/sites/{site}/views/{view_id}")
def views_get(version: str, site: str, view_id: str, request: Request) -> Response:
    _version(version)
    with Row(request, "views_get") as row:
        _, state = _read_state(request, row, site)
        v, wb = _view(state, view_id)
        row.targets = [f"{NAME}:view:{v['content_url']}"]
        return _ts(_view_xml(v, wb))


@app.get("/api/{version}/sites/{site}/views/{view_id}/data")
def views_data(version: str, site: str, view_id: str, request: Request) -> Response:
    """The view's summary data as CSV, rendered from the PUBLISHED workbook."""
    _version(version)
    with Row(request, "views_data") as row:
        store, state = _read_state(request, row, site)
        v, wb = _view(state, view_id, code="400080")
        row.targets = [f"{NAME}:view:{v['content_url']}"]
        vf = {k[3:]: val for k, val in request.query_params.items() if k.startswith("vf_")}
        row.args.update({"view": v["content_url"], "filters": vf})
        try:
            deferred: dict = {}
            ds, report = store.read_workbook(state, wb, deferred)
            sheet = v["name"]
            if sheet in deferred:
                raise deferred[sheet]
            visual = visual_for(report, sheet, v["sheet_type"])
            if vf:
                visual = with_url_filters(ds, visual, vf, store.field_captions(state, wb, ds))
            owner = extract_owner(state, wb, ds)
            warehouse, refreshed = store.warehouse_for(state, owner)
            row.read_targets = sorted(_relations(ds.model))
            if refreshed is not None:
                row.flags.append(f"extract:{refreshed}")
            result = render.render_visual(ds.model, visual, warehouse)
            # What the view's query read, not every relation its data source
            # declares: the KPIs card reads warehouse.fct_orders and nothing else.
            row.read_targets = visual_relations(ds.model, visual)
        except Unsupported as exc:
            raise TableauError(400, "400000", "Bad Request", str(exc)) from None
        except (ModelError, SemanticError) as exc:
            raise TableauError(400, "400000", "Bad Request", f"{type(exc).__name__}: {exc}") \
                from None
        body = view_csv(ds.model, visual, result)
        row.result = {"columns": result["columns"], "rows": len(result["rows"]),
                      "sha256": hashlib.sha256(body).hexdigest()}
        return Response(body, media_type="text/csv; charset=utf-8")


def visual_for(report, sheet: str, sheet_type: str):
    for page in report.pages:
        if sheet_type == "worksheet":
            for visual in page.visuals:
                if visual.name == sheet:
                    return visual
        elif page.name == sheet:
            # "If the request is for a dashboard, only data for the dashboard's
            # first view is returned." (REST API reference, Query View Data)
            if not page.visuals:
                raise TableauError(400, "400000", "Bad Request",
                                   f"Dashboard '{sheet}' has no worksheet.")
            return page.visuals[0]
    raise TableauError(400, "400000", "Bad Request", f"View '{sheet}' has no data to export.")


def extract_owner(state: dict, wb: dict, ds) -> tuple[str, str]:
    """Whose extract a view reads: the published data source it connects to, or
    the workbook itself when it embeds its source."""
    if ds.published:
        published = next((d for d in state["datasources"]
                          if ds.published in (d["content_url"], d["name"])), None)
        if published is not None:
            return "datasource", published["id"]
    return "workbook", wb["id"]


def url_filter_fields(ds, captions: dict[str, str]) -> dict[str, tuple[Col, str]]:
    """The name a URL filter gives each dimension field of `ds` -> (its column,
    its type). The name is the one Tableau shows: the field's caption
    (`captions`, from `Store.field_captions`), else its own name - so of the
    two `order_id` fields of Orders and Order Lines, `order_id` is Orders' and
    the other is `order_id (fct_order_lines)`. A name two fields share names
    neither."""
    types = {(t.name, c.name): c.data_type for t in ds.model.tables for c in t.columns}
    out: dict[str, tuple[Col, str] | None] = {}
    for name, ref in ds.names.refs.items():
        if not isinstance(ref, Col) or (ref.table, ref.column) not in types:
            continue
        shown = captions.get(name, name)
        out[shown] = None if shown in out else (ref, types[(ref.table, ref.column)])
    return {k: v for k, v in out.items() if v is not None}


def with_url_filters(ds, visual, vf: dict, captions: dict[str, str]):
    """`vf_<field>=a,b` (REST) or `?<field>=a,b` (a view's URL) -> a filter on
    the field captioned <field> (`url_filter_fields`), replacing any filter the
    view has on it. Unknown field names are ignored, as Tableau Server ignores
    URL filters on fields the view's data source lacks."""
    if not vf:
        return visual
    fields = url_filter_fields(ds, captions)
    filters = list(visual.filters)
    for field, raw in vf.items():
        if field not in fields:
            continue
        col, dtype = fields[field]
        values = tuple(_typed(v, dtype) for v in raw.split(","))
        filters = [f for f in filters if f.column != col] + [FilterValues(col, values)]
    return replace(visual, filters=tuple(filters))


def _typed(text: str, dtype: str):
    try:
        if dtype == "int":
            return int(text)
        if dtype == "decimal":
            return Decimal(text)
        if dtype == "float":
            return float(text)
        if dtype == "bool":
            return text.lower() == "true"
        if dtype == "date":
            return dt.date.fromisoformat(text[:10])
        if dtype == "timestamp":
            return dt.datetime.fromisoformat(text)
    except (ValueError, InvalidOperation):
        raise TableauError(400, "400000", "Bad Request",
                           f"Filter value '{text}' is not a valid {dtype}.") from None
    return text


def view_csv(model, visual, result: dict) -> bytes:
    """CSV the way Query View Data writes it: field captions as headers, measures
    in their declared number format (or Tableau's automatic one)."""
    formats = {}
    measures = {m.name: m for m in model.measures}
    for label, expr in visual.values:
        name = getattr(expr, "name", None)
        formats[label] = measures[name].format_string if name in measures else None
    n_groups = len(visual.group_by)
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(result["columns"])
    for row in result["rows"]:
        cells = []
        for i, (column, value) in enumerate(zip(result["columns"], row, strict=True)):
            cells.append(_cell(value, formats.get(column) if i >= n_groups else None,
                               measure=i >= n_groups))
        writer.writerow(cells)
    return out.getvalue().encode("utf-8")


def _cell(value, fmt: str | None, *, measure: bool) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if not measure:
        return str(value)
    if fmt:
        formatted = format_number(value, fmt)
        if formatted is not None:
            return formatted
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


_FMT = re.compile(r"^(?P<prefix>[^#0,.%]*)(?P<int>[#0,]+)(?:\.(?P<dec>0+))?(?P<pct>%?)$")


def format_number(value, fmt: str) -> str | None:
    """An Excel-style number format (`$#,0.00`, `#,##0`, `0.0%`) applied exactly
    (half away from zero, as Tableau rounds for display); None for a pattern
    outside that grammar, which is then shown unformatted."""
    m = _FMT.match(fmt.split(";")[0])
    if m is None:
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    if m.group("pct"):
        number *= 100
    places = len(m.group("dec") or "")
    quantum = Decimal(1).scaleb(-places)
    number = number.quantize(quantum, rounding=ROUND_HALF_UP)
    sign = "-" if number < 0 else ""
    text = f"{abs(number):,.{places}f}" if "," in m.group("int") else f"{abs(number):.{places}f}"
    return f"{sign}{m.group('prefix').replace(chr(34), '')}{text}{m.group('pct')}"


# ---------------------------------------------------------------------- jobs
@app.get("/api/{version}/sites/{site}/jobs/{job_id}")
def jobs_get(version: str, site: str, job_id: str, request: Request) -> Response:
    _version(version)
    with Row(request, "jobs_get", targets=[f"{NAME}:job:{job_id}"]) as row:
        _, state = _read_state(request, row, site)
        job = next((j for j in state["jobs"] if j["id"] == job_id), None)
        if job is None:
            raise not_found("Job", "404018", job_id)
        row.result = {"finishCode": job["finish_code"]}
        return _ts(_job_xml(job, full=True))


@app.get("/api/{version}/sites/{site}/jobs")
def jobs_list(version: str, site: str, request: Request) -> Response:
    _version(version)
    with Row(request, "jobs_list", targets=[f"{NAME}:jobs"]) as row:
        _, state = _read_state(request, row, site)
        page, pagination = _paginate(request, list(reversed(state["jobs"])))
        status = {0: "Success", 1: "Failed", 2: "Cancelled"}
        return _ts(pagination, _xml("backgroundJobs", None, *(
            _xml("backgroundJob", {"id": j["id"], "status": status.get(j["finish_code"],
                                                                       "Success"),
                                   "createdAt": j["created_at"], "startedAt": j["started_at"],
                                   "endedAt": j["completed_at"], "priority": "50",
                                   "jobType": "refresh_extracts", "title": j["target_name"]})
            for j in page)))


# What the browser UI (native/ui/tableau.py) and other modules may use: the
# same store, lock, audit row and render path as the REST routes above.
__all__ = [
    "NAME", "HOST", "PORT", "app", "format_number",
    "DEFAULT_USER", "SITE_ID", "PROJECT_ID", "PROJECT_NAME", "UI_PASSWORD",
    "LOCK", "Row", "Store", "TableauError",
    "all_views", "extract_owner", "find", "not_found", "project_of", "projects",
    "uid", "url_filter_fields", "view_csv", "visual_for", "visual_relations",
    "with_url_filters",
]


# The browser UI (native/ui/tableau.py): same app, same Store, same audit rows.
from ..ui import tableau as _ui  # noqa: E402  (imports this module; must come last)

_ui.mount(app)
