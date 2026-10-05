"""Sigma Computing REST API v2 - the subset a data engineer drives BI with.

WHY A FAKE SIGMA (gateway/NATIVE.md, docs/environments/BI_BUILD_PLAN.md phase 3).
At a Sigma shop the engineer does not click: workbooks and data models are
managed as code through the REST API, a CI job pulls their representations into
git, and a broken number is checked by exporting the element and comparing it
with the warehouse. This service is that API over the world: its numbers come
from `world.semantic.render` on the world's warehouse, never from a fixture.

SHAPES (all from Sigma's public OpenAPI document "Sigma Computing Public REST
API" 2.0.0+20260930.a3bda220, https://assets.sigmacomputing.com/openapi/
public-rest-api/sigma-computing-public-rest-api.json, and the pages under
https://help.sigmacomputing.com/reference; research notes in
world/world/semantic/formats/sigma/__init__.py):

  POST /v2/auth/token                    form-encoded grant_type=client_credentials,
                                         client id/secret as HTTP Basic (or form
                                         fields); refresh_token grant; bearer after
  GET  /v2/whoami                        {userId, organizationId}
  GET  /v2/workbooks                     {entries, nextPage, total, hasMore}
  POST /v2/workbooks                     create from `contents`; `dryRun` validates
  GET  /v2/workbooks/{id}                ?includeContents=true -> contents (+YAML)
  PUT  /v2/workbooks/{id}/contents       replace; a GET response is accepted as is
  GET|PUT /v2/workbooks/{id}/spec        the deprecated spelling (`document`)
  GET  /v2/workbooks/{id}/pages, /pages/{pageId}/elements, /elements
  GET  /v2/workbooks/{id}/elements/{elementId}/columns   {entries: [{columnId,
                                         label, formula, type}], nextPage, total}
  GET  /v2/workbooks/{id}/elements/{elementId}/query     {elementId, name, sql |
                                         error}; `/queries` lists one per element
  GET  /v2/workbooks/{id}/columns        every element's columns (+ elementId)
  GET  /v2/workbooks/{id}/sources        [{type: data-model, dataModelId,
                                         elementIds}]
  GET  /v2/workbooks/{id}/version-history
  POST /v2/workbooks/{id}/export         {elementId, format: {type}} -> {queryId}
  GET  /v2/query/{queryId}/download      the file
  GET  /v2/dataModels, /{id}, /{id}/elements
  GET|PUT /v2/dataModels/{id}/spec       the data model code representation
  GET  /v2/connections, /{id};  POST /v2/connections/{id}/sync {path}
  errors                                 {message, code, requestId}

STATE. Sigma's published documents are not the agent's repo: what is in git
and what Sigma serves differ until someone deploys. So the published state
lives outside the world tree, beside the BI tile cache
(`<bi_state_dir>/sigma/state.json`, like `alkera_gateway.bi`), seeded on first
use from the repo's spec files (`<workspace>/bi/sigma/{data-models,workbooks,
connections}/*.json`, formats/sigma/project.py). A `PUT` publishes a new
version only if the representation parses: anything the semantic core refuses
(Unsupported) or cannot resolve (ModelError) is a 400 naming it, and nothing
is published. Sigma's connection catalog - which warehouse columns it knows -
is a snapshot of the warehouse taken at seed time and refreshed only by
`POST /v2/connections/{id}/sync`, as in Sigma: a column dbt added after the
last sync is unknown to a data model until the connection is synced.

EXPORTS. An export renders the element from the PUBLISHED workbook and data
model with `world.semantic.render.render_visual` on the world's warehouse at
the moment of the request, and stores the file under the state directory;
`/v2/query/{queryId}/download` serves it. So an export disagrees with the
warehouse exactly when the published model computes something else.

AUDIT. One 9.2.2 row per request, through `native.context.Ledger`, named
`sigma.<operation>`; there is no MCP equivalent to name it after. Refusals,
authentication included, write their row too. Targets: `sigma:workbook:<id>`,
`sigma:data-model:<id>`, `sigma:connection:<id>`, `sigma:query:<id>`,
`sigma:session`; an export's `read_targets` are the warehouse relations it read.

ELEMENT SQL AND COLUMNS (formats/sigma/elements.py). An element's `query` is
the engine's query for it - the one an export runs - over the PUBLISHED
workbook and data model, in the connection's table names
(`ANALYTICS.WAREHOUSE.FCT_ORDERS`), with the element's sort and Top N in it, so
the statement returns the rows the export does; an element the engine cannot
compute answers `error` (the OpenAPI's field for it), not a guess. Column types
come from the semantic model. Measured 2026-10-04 (BI001 K=8 Opus run): four
rollouts asked `/elements/{id}/query`, and `/columns`, `/queries` and
`/sources` once each, to see what Sigma computes - all four real, all four
404'd here. What still 404s, because Sigma has no such route: `GET
/v2/workbooks/{id}/contents` (contents is PUT-only), `/elements/{id}/data`,
`/elements/{id}/export`, `/elements/{id}`, `/versions`.

NOT IMITATED. Members, teams, grants, folders, tags, schedules, embeds,
materialization, datasets (retired by Sigma on 2026-09-15), reports, lineage
(`/lineage`, `/lineage/elements/{id}`: their node and source ids are not
defined anywhere a fake could follow), `/schema` (deprecated since 2023-12-15
and "no longer compatible with workbook layouts", which every workbook here
has), `tagName`/`bookmarkId` on an element query (400, as on export), PDF/PNG/
XLSX exports, page and whole-workbook exports, export `parameters`, token
expiry (tokens never expire: the episode clock is virtual), `POST
/v2/dataModels/spec`. The default page size (50) is not documented by Sigma.
"""
from __future__ import annotations

import base64
import binascii
import csv
import io
import json
from pathlib import Path
from urllib.parse import parse_qs

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from ...records import Observation, ToolError
from ...sql import inspect_sql
from ..context import DEV_TOKEN, Ledger, ServiceError, audit_unrouted, context_of, service_health
from ..http import RawBody

NAME = "sigma"
HOST = "sigma.internal"
PORT = 8097

DEFAULT_LIMIT = 50
MAX_LIMIT = 1000
TOKEN_SECONDS = 3599
EXPORT_TYPES = {"csv": "text/csv", "json": "application/json", "jsonl": "application/jsonl"}

app = FastAPI(title="Sigma Computing REST API (alkera native surface)", docs_url=None,
              redoc_url=None, openapi_url=None)
# One row per request, including the ones no handler ran for (context.py).
audit_unrouted(app, NAME)


def _semantic():
    """The Sigma front-end and the engine. Imported late: `world` is the
    lowest layer and is on the path in the episode image, but a gateway that
    never serves Sigma must not need it to import this module."""
    try:
        from world.semantic import render
        from world.semantic.errors import SemanticError
        from world.semantic.formats import sigma
        from world.semantic.formats.sigma import _common as common
        from world.semantic.formats.sigma.catalog import Catalog
        from world.semantic.formats.sigma.workbook import contents_of, element_visual
    except ImportError as exc:  # pragma: no cover - a broken image
        raise ServiceError(503, "unavailable", f"Sigma's model engine is not installed: "
                                               f"{exc}") from None
    return sigma, render, SemanticError, Catalog, contents_of, element_visual, common


class Refusal(ServiceError):
    """A request Sigma would refuse; `ServiceError` with Sigma's codes."""


def _error_body(request: Request, exc: ServiceError) -> dict:
    from hashlib import sha256

    digest = sha256(f"{request.method} {request.url.path}".encode()).hexdigest()
    request_id = f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"
    return {"message": exc.message, "code": exc.code, "requestId": request_id}


@app.exception_handler(ServiceError)
async def _service_error(request: Request, exc: ServiceError) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else {}
    return JSONResponse(status_code=exc.status, content=_error_body(request, exc),
                        headers=headers)


def _stamp(vts: str) -> str:
    """The world clock (`2026-03-05T09:00:00Z`) in Sigma's spelling."""
    if vts.endswith("Z") and "." not in vts:
        return vts[:-1] + ".000Z"
    return vts


# ------------------------------------------------------------------- the store
class SigmaStore:
    """Sigma's published state for one world (see the module docstring)."""

    def __init__(self, request: Request):
        context = context_of(request.app)
        self.config = context.config
        self.clock = context.clock
        self.root = Path(self.config.bi_state_dir) / "sigma"
        self.path = self.root / "state.json"
        self.repo = Path(self.config.workspace) / "bi" / "sigma"
        self.warehouse = Path(self.config.warehouse)
        self.state = self._load()

    # ------------------------------------------------------------ persistence
    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8"))
        return self._seed()

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, sort_keys=True, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def _seed(self) -> dict:
        def docs(kind: str) -> list[dict]:
            folder = self.repo / kind
            return [json.loads(p.read_text(encoding="utf-8"))
                    for p in sorted(folder.glob("*.json"))] if folder.is_dir() else []

        state = {"dataModels": {}, "workbooks": {}, "connections": {}, "catalog": {},
                 "tokens": {}, "refreshTokens": {}, "queries": {}, "counter": 0}
        for doc in docs("data-models"):
            state["dataModels"][doc["dataModelId"]] = {"versions": [doc]}
        for doc in docs("workbooks"):
            state["workbooks"][doc["workbookId"]] = {"versions": [doc]}
        snapshots = docs("connections")
        for doc in snapshots:
            state["connections"][doc["connectionId"]] = {
                k: v for k, v in doc.items() if k not in ("tables", "statements")}
        if not state["connections"] and (state["dataModels"] or state["workbooks"]):
            sigma, *_rest, common = _semantic()
            state["connections"][common.CONNECTION_ID] = _default_connection(common)
        state["catalog"] = self._warehouse_catalog(snapshots)
        self.state = state
        self.save()
        return state

    def _warehouse_catalog(self, snapshots: list[dict]) -> dict:
        _s, _r, _e, Catalog, *_rest = _semantic()
        if self.warehouse.exists():
            return Catalog.from_warehouse(self.warehouse).tables
        return Catalog.from_snapshots(snapshots).tables

    # ------------------------------------------------------------------- auth
    def authenticate(self, request: Request) -> str:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or token not in self.state["tokens"]:
            raise Refusal(401, "unauthorized", "Unauthorized")
        return self.state["tokens"][token]["clientId"]

    def issue(self, client_id: str, observation: Observation) -> dict:
        self.state["counter"] += 1
        n = str(self.state["counter"])
        _s, _r, _e, _c, _co, _ev, common = _semantic()
        access = f"{DEV_TOKEN}.{common.base62('access', client_id, n, length=40)}"
        refresh = f"{DEV_TOKEN}.{common.base62('refresh', client_id, n, length=40)}"
        self.state["tokens"][access] = {"clientId": client_id, "issuedAt": self.clock()}
        self.state["refreshTokens"][refresh] = {"clientId": client_id}
        self.save()
        observation.targets = ["sigma:session"]
        observation.read_targets = ["sigma:session"]
        return {"access_token": access, "refresh_token": refresh, "token_type": "bearer",
                "expires_in": TOKEN_SECONDS}

    # -------------------------------------------------------------- documents
    def workbook(self, workbook_id: str) -> dict:
        entry = self.state["workbooks"].get(workbook_id)
        if entry is None:
            raise Refusal(404, "not_found", f"Workbook {workbook_id} not found")
        return entry

    def data_model(self, data_model_id: str) -> dict:
        entry = self.state["dataModels"].get(data_model_id)
        if entry is None:
            raise Refusal(404, "not_found", f"Data model {data_model_id} not found")
        return entry

    def connection(self, connection_id: str) -> dict:
        found = self.state["connections"].get(connection_id)
        if found is None:
            raise Refusal(404, "not_found", f"Connection {connection_id} not found")
        return found

    def catalog(self):
        _s, _r, _e, Catalog, *_rest = _semantic()
        return Catalog(tables={k: dict(v) for k, v in self.state["catalog"].items()},
                       warehouse=self.warehouse if self.warehouse.exists() else None)

    def data_model_for(self, workbook_doc: dict) -> dict:
        _s, _r, _e, _c, contents_of, _ev, _co = _semantic()
        ids = {(el.get("source") or {}).get("dataModelId")
               for el in contents_of(workbook_doc).get("elements", [])} - {None}
        if len(ids) > 1:
            raise Refusal(400, "bad_request", "Workbook reads several data models")
        if not ids:
            if len(self.state["dataModels"]) == 1:
                return next(iter(self.state["dataModels"].values()))["versions"][-1]
            raise Refusal(400, "bad_request", "Workbook reads no data model")
        return self.data_model(ids.pop())["versions"][-1]

    def read(self, data_model: dict, workbook: dict | None):
        """Model and reports, or a 400 naming what does not parse."""
        sigma, _r, SemanticError, *_rest = _semantic()
        try:
            return sigma.read_documents(data_model, [workbook] if workbook else [],
                                        self.catalog())
        except SemanticError as exc:
            raise Refusal(400, "bad_request", str(exc)) from None
        except (KeyError, TypeError, ValueError) as exc:
            raise Refusal(400, "bad_request", f"Malformed representation: {exc}") from None


def _default_connection(common) -> dict:
    return {"organizationId": common.ORGANIZATION_ID, "connectionId": common.CONNECTION_ID,
            "isSample": False, "isAuditLog": False, "lastActiveAt": common.EPOCH,
            "name": common.CONNECTION_NAME, "type": common.CONNECTION_TYPE, "useOauth": False,
            "createdBy": common.OWNER_ID, "updatedBy": common.OWNER_ID,
            "createdAt": common.EPOCH, "updatedAt": common.EPOCH, "isArchived": False}


# ------------------------------------------------------------------- plumbing
def _serve(request: Request, tool: str, call, *, auth: bool = True, **args):
    """One request, one row: authenticate, run, classify - all inside the span."""
    ledger = Ledger(request, NAME)
    try:
        with ledger.span(tool, **args) as row:
            def run(_operations, observation):
                store = SigmaStore(request)
                if auth:
                    ledger.actor = store.authenticate(request)
                return call(store, observation)
            return row.run(run)
    except ToolError as exc:
        raise ServiceError.from_tool_error(exc) from None


def _json(raw: bytes, request: Request | None = None) -> dict:
    """A JSON body - or YAML, which the code-representation endpoints also
    accept (`Content-Type: application/yaml`, docs/manage-workbooks-as-code)."""
    yaml_body = request is not None and "yaml" in request.headers.get("content-type", "")
    try:
        if yaml_body:
            body = yaml.safe_load(raw.decode("utf-8")) if raw else {}
        else:
            body = json.loads(raw) if raw else {}
    except (ValueError, UnicodeError, yaml.YAMLError):
        raise Refusal(400, "bad_request",
                      f"Invalid {'YAML' if yaml_body else 'JSON'} body") from None
    if not isinstance(body, dict):
        raise Refusal(400, "bad_request", "Expected a JSON object")
    return body


def _page(request: Request, entries: list, *, has_more: bool = True) -> dict:
    """`{entries, nextPage, total}`, plus the deprecated `hasMore` that the
    older list endpoints still document (and the newer ones do not)."""
    params = request.query_params
    try:
        limit = int(float(params.get("limit", DEFAULT_LIMIT)))
        offset = int(params.get("page") or 0)
    except ValueError:
        raise Refusal(400, "bad_request", "Invalid page or limit") from None
    if not 1 <= limit <= MAX_LIMIT or offset < 0:
        raise Refusal(400, "bad_request", f"limit must be between 1 and {MAX_LIMIT}")
    window = entries[offset:offset + limit]
    more = offset + limit < len(entries)
    page = {"entries": window, "nextPage": str(offset + limit) if more else None,
            "total": len(entries)}
    if has_more:
        page["hasMore"] = more
    return page


def _flag(request: Request, name: str) -> bool:
    return request.query_params.get(name, "").lower() == "true"


def _wants_yaml(request: Request) -> bool:
    fmt = request.query_params.get("format", "").lower()
    return fmt in ("yaml", "yml") or "application/yaml" in request.headers.get("accept", "")


def _respond(request: Request, body) -> Response:
    if _wants_yaml(request):
        return Response(yaml.safe_dump(body, sort_keys=False, allow_unicode=True),
                        media_type="application/yaml")
    return JSONResponse(body)


_META = ("workbookId", "workbookUrlId", "name", "url", "path", "latestVersion", "ownerId",
         "createdBy", "updatedBy", "createdAt", "updatedAt", "isArchived", "description")


def _workbook_meta(doc: dict) -> dict:
    return {k: doc[k] for k in _META if k in doc}


def _data_model_meta(doc: dict) -> dict:
    url_id = doc.get("url", "").rsplit("-", 1)[-1]
    return {"dataModelId": doc["dataModelId"], "dataModelUrlId": url_id, "name": doc["name"],
            "url": doc.get("url", ""), "path": doc.get("path", "Analytics"),
            "latestVersion": doc.get("documentVersion", 1), "ownerId": doc.get("ownerId"),
            "createdBy": doc.get("createdBy"), "updatedBy": doc.get("updatedBy"),
            "createdAt": doc.get("createdAt"), "updatedAt": doc.get("updatedAt"),
            "isArchived": False}


def _wb_target(workbook_id: str) -> str:
    return f"sigma:workbook:{workbook_id}"


def _dm_target(data_model_id: str) -> str:
    return f"sigma:data-model:{data_model_id}"


def _read(observation: Observation, *targets: str) -> None:
    observation.targets = list(targets)
    observation.read_targets = list(targets)


# ------------------------------------------------------------------ endpoints
@app.get("/health")
def health(request: Request) -> object:
    """Published documents, counted from the state on every probe."""
    def count(_operations, observation):
        store = SigmaStore(request)
        targets = [_wb_target(i) for i in store.state["workbooks"]] + \
                  [_dm_target(i) for i in store.state["dataModels"]]
        observation.read_targets = sorted(targets) or ["sigma:state"]
        return len(targets)

    return service_health(request, NAME, count)


@app.post("/v2/auth/token")
def auth_token(request: Request, raw: RawBody) -> dict:
    form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
    grant = form.get("grant_type")

    def call(store: SigmaStore, observation):
        content_type = request.headers.get("content-type", "")
        if "application/x-www-form-urlencoded" not in content_type:
            raise Refusal(400, "bad_request",
                          "Content-Type must be application/x-www-form-urlencoded")
        if grant == "refresh_token":
            known = store.state["refreshTokens"].get(form.get("refresh_token", ""))
            if known is None:
                raise Refusal(401, "unauthorized", "Invalid refresh token")
            return store.issue(known["clientId"], observation)
        if grant != "client_credentials":
            raise Refusal(400, "bad_request", f"Unsupported grant_type: {grant}")
        client_id, secret = form.get("client_id"), form.get("client_secret")
        scheme, _, encoded = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "basic":
            try:
                client_id, _, secret = base64.b64decode(encoded, validate=True) \
                    .decode("utf-8").partition(":")
            except (binascii.Error, UnicodeDecodeError):
                raise Refusal(401, "unauthorized", "Malformed Basic credentials") from None
        # Any client id; the secret is the workspace's obviously-fake token.
        if not client_id or secret != DEV_TOKEN:
            raise Refusal(401, "unauthorized", "Invalid client credentials")
        return store.issue(client_id, observation)

    return _serve(request, "auth_token", call, auth=False, grant_type=grant)


@app.get("/v2/whoami")
def whoami(request: Request) -> dict:
    def call(store, observation):
        _read(observation, "sigma:session")
        _s, _r, _e, _c, _co, _ev, common = _semantic()
        return {"userId": common.OWNER_ID, "organizationId": common.ORGANIZATION_ID}

    return _serve(request, "whoami", call)


# ---------------------------------------------------------------- workbooks
@app.get("/v2/workbooks")
def list_workbooks(request: Request) -> dict:
    def call(store, observation):
        docs = [e["versions"][-1] for e in store.state["workbooks"].values()]
        docs.sort(key=lambda d: (d["name"], d["workbookId"]))
        page = _page(request, [_workbook_meta(d) for d in docs])
        _read(observation, *(_wb_target(d["workbookId"]) for d in page["entries"]))
        if not page["entries"]:
            _read(observation, "sigma:workbooks")
        return page

    return _serve(request, "workbooks_list", call, **dict(request.query_params))


def _version(entry: dict, request: Request) -> dict:
    wanted = request.query_params.get("documentVersion")
    if wanted is None:
        return entry["versions"][-1]
    try:
        index = int(wanted) - 1
        if index < 0:
            raise IndexError
        return entry["versions"][index]
    except (ValueError, IndexError):
        raise Refusal(404, "not_found", f"Document version {wanted} not found") from None


@app.get("/v2/workbooks/{workbook_id}")
def get_workbook(workbook_id: str, request: Request) -> Response:
    def call(store, observation):
        doc = _version(store.workbook(workbook_id), request)
        _read(observation, _wb_target(workbook_id))
        if _flag(request, "includeContents"):
            return doc
        return _workbook_meta(doc)

    return _respond(request, _serve(request, "workbook_get", call,
                                    **dict(request.query_params)))


@app.get("/v2/workbooks/{workbook_id}/spec")
def get_workbook_spec(workbook_id: str, request: Request) -> Response:
    """Deprecated in the OpenAPI document; the same representation as
    `document` rather than `contents`."""
    def call(store, observation):
        doc = store.workbook(workbook_id)["versions"][-1]
        _read(observation, _wb_target(workbook_id))
        meta = _workbook_meta(doc)
        return {"workbookId": meta["workbookId"], "name": meta["name"], "url": meta["url"],
                "documentVersion": doc.get("documentVersion", meta["latestVersion"]),
                "latestDocumentVersion": meta["latestVersion"], "ownerId": meta["ownerId"],
                "folderId": doc.get("folderId", ""), "createdBy": meta["createdBy"],
                "updatedBy": meta["updatedBy"], "createdAt": meta["createdAt"],
                "updatedAt": meta["updatedAt"], "document": doc["contents"]}

    return _respond(request, _serve(request, "workbook_spec", call))


def _publish_workbook(store: SigmaStore, workbook_id: str, body: dict, key: str,
                      observation: Observation) -> dict:
    entry = store.workbook(workbook_id)
    current = entry["versions"][-1]
    contents = body.get(key)
    if not isinstance(contents, dict):
        raise Refusal(400, "bad_request", f"`{key}` is required: partial updates are not "
                                          f"supported")
    expected = body.get("documentVersion")
    if expected is not None and expected != current.get("documentVersion"):
        raise Refusal(409, "conflict", f"Workbook has changed since version {expected}")
    candidate = {**current, "contents": contents}
    store.read(store.data_model_for(candidate), candidate)
    version = current.get("latestVersion", 1) + 1
    published = {**candidate, "latestVersion": version, "documentVersion": version,
                 "updatedAt": _stamp(store.clock()), "updatedBy": current.get("ownerId")}
    entry["versions"].append(published)
    store.save()
    observation.op_class = "write"
    observation.targets = [_wb_target(workbook_id)]
    return _workbook_meta(published)


@app.put("/v2/workbooks/{workbook_id}/contents")
def put_workbook_contents(workbook_id: str, request: Request, raw: RawBody) -> dict:
    def call(store, observation):
        body = _json(raw, request)
        return _publish_workbook(store, workbook_id, body, "contents", observation)

    return _serve(request, "workbook_update", call)


@app.put("/v2/workbooks/{workbook_id}/spec")
def put_workbook_spec(workbook_id: str, request: Request, raw: RawBody) -> dict:
    def call(store, observation):
        return _publish_workbook(store, workbook_id, _json(raw, request), "document",
                                 observation)

    return _serve(request, "workbook_update", call)


@app.post("/v2/workbooks")
def create_workbook(request: Request, raw: RawBody) -> Response:
    """Create a workbook from `contents`, or with `dryRun` only validate it."""
    def call(store, observation):
        body = _json(raw, request)
        name, folder = body.get("name"), body.get("folderId")
        contents = body.get("contents")
        if not name or not folder:
            raise Refusal(400, "bad_request", "`name` and `folderId` are required")
        sigma, _r, _e, _c, _co, _ev, common = _semantic()
        workbook_id = common.stable_uuid("workbook", name)
        url_id = common.base62("url", "workbook", name, length=22)
        stamp = _stamp(store.clock())
        doc = {"workbookId": workbook_id, "workbookUrlId": url_id, "name": name,
               "url": common.document_url("workbook", name, url_id), "path": common.FOLDER_PATH,
               "latestVersion": 1, "ownerId": common.OWNER_ID, "createdBy": common.OWNER_ID,
               "updatedBy": common.OWNER_ID, "createdAt": stamp, "updatedAt": stamp,
               "isArchived": False, "documentVersion": 1, "folderId": folder,
               "contents": contents or {"schemaVersion": 1, "kind": "workbook",
                                        "pages": [], "elements": [], "layout": ""}}
        if body.get("dryRun"):
            _read(observation, "sigma:workbooks")
            if contents is None:
                raise Refusal(400, "bad_request", "A dry run requires `contents`")
            try:
                store.read(store.data_model_for(doc), doc)
            except Refusal as exc:
                return 400, {"dryRun": True, "valid": False,
                             "errors": [{"summary": exc.message}], "warnings": []}
            nulls = dict.fromkeys(("workbookId", "workbookUrlId", "url", "createdAt",
                                   "updatedAt"))
            return 200, {**_workbook_meta(doc), **nulls, "dryRun": True, "valid": True,
                         "warnings": []}
        if workbook_id in store.state["workbooks"]:
            raise Refusal(409, "conflict", f"A workbook named {name!r} exists")
        if contents is not None:
            store.read(store.data_model_for(doc), doc)
        store.state["workbooks"][workbook_id] = {"versions": [doc]}
        store.save()
        observation.op_class = "write"
        observation.targets = [_wb_target(workbook_id)]
        return 200, _workbook_meta(doc)

    status, body = _serve(request, "workbook_create", call)
    return JSONResponse(body, status_code=status)


@app.get("/v2/workbooks/{workbook_id}/version-history")
def version_history(workbook_id: str, request: Request) -> dict:
    def call(store, observation):
        entry = store.workbook(workbook_id)
        _read(observation, _wb_target(workbook_id))
        entries = [{"version": v.get("latestVersion", i + 1), "publishedAt": v.get("updatedAt"),
                    "publishedBy": v.get("updatedBy")}
                   for i, v in enumerate(entry["versions"])][::-1]
        return _page(request, entries)

    return _serve(request, "workbook_versions", call)


_VIZ = {"bar-chart": "bar", "line-chart": "line", "area-chart": "area", "kpi-chart": "kpi"}


def _element_entry(el: dict) -> dict:
    kind = el.get("kind")
    entry = {"elementId": el["id"],
             "name": el["name"] if isinstance(el.get("name"), str) else
             (el.get("name") or {}).get("text", ""),
             "type": "visualization" if kind in _VIZ else kind,
             "columns": [c.get("name") or c.get("formula", "") for c in el.get("columns", [])]}
    if kind in _VIZ:
        entry["vizualizationType"] = _VIZ[kind]
    return entry


def _pages(store: SigmaStore, workbook_id: str) -> tuple[dict, dict[str, list[str]]]:
    _s, _r, _e, _c, contents_of, _ev, _co = _semantic()
    from world.semantic.formats.sigma.workbook import parse_layout

    doc = store.workbook(workbook_id)["versions"][-1]
    contents = contents_of(doc)
    try:
        placed = parse_layout(contents.get("layout", ""))
    except Exception as exc:  # noqa: BLE001 - a stored layout that does not parse
        raise Refusal(400, "bad_request", str(exc)) from None
    return contents, placed


@app.get("/v2/workbooks/{workbook_id}/pages")
def list_pages(workbook_id: str, request: Request) -> dict:
    def call(store, observation):
        contents, _placed = _pages(store, workbook_id)
        _read(observation, _wb_target(workbook_id))
        entries = []
        for p in contents.get("pages", []):
            entry = {"pageId": p["id"], "name": p["name"]}
            if p.get("visibility") == "hidden":
                entry["hidden"] = True
            entries.append(entry)
        return _page(request, entries)

    return _serve(request, "workbook_pages", call)


@app.get("/v2/workbooks/{workbook_id}/pages/{page_id}/elements")
def list_page_elements(workbook_id: str, page_id: str, request: Request) -> dict:
    def call(store, observation):
        contents, placed = _pages(store, workbook_id)
        _read(observation, _wb_target(workbook_id))
        if page_id not in placed:
            raise Refusal(404, "not_found", f"Page {page_id} not found")
        by_id = {el["id"]: el for el in contents.get("elements", [])}
        return _page(request, [_element_entry(by_id[e]) for e in placed[page_id]
                               if e in by_id])

    return _serve(request, "workbook_page_elements", call)


@app.get("/v2/workbooks/{workbook_id}/elements")
def list_elements(workbook_id: str, request: Request) -> dict:
    def call(store, observation):
        contents, _placed = _pages(store, workbook_id)
        _read(observation, _wb_target(workbook_id))
        return _page(request, [_element_entry(el) for el in contents.get("elements", [])])

    return _serve(request, "workbook_elements", call)


def _relations(model, visual) -> list[str]:
    """The warehouse relations an element's numbers are read from: every table
    its values, groupings and filters touch, through measure references and
    calculated columns."""
    measures = {m.name: m.expr for m in model.measures}
    calculated = {(t.name, c.name): c.expr for t in model.tables for c in t.columns if c.expr}
    tables: set[str] = set()
    seen: set = set()

    def walk(node):
        if not hasattr(node, "__dataclass_fields__"):
            return
        if type(node).__name__ == "MeasureRef":
            if node.name not in seen and node.name in measures:
                seen.add(node.name)
                walk(measures[node.name])
            return
        table = getattr(node, "table", None)
        if isinstance(table, str):
            tables.add(table)
            key = (table, getattr(node, "column", None))
            if key in calculated and key not in seen:
                seen.add(key)
                walk(calculated[key])
        for value in vars(node).values():
            for item in (value if isinstance(value, tuple) else (value,)):
                walk(item)

    for item in [e for _label, e in visual.values] + list(visual.group_by) + \
            list(visual.filters):
        walk(item)
    out: set[str] = set()
    for table in model.tables:
        if table.name not in tables:
            continue
        if table.source:
            out.add(table.source)
            continue
        found = Observation()
        try:
            inspect_sql(table.source_sql, found, True, "duckdb")
            out.update(found.read_targets)
        except ToolError:
            out.add(f"sql:{table.name}")
    return sorted(out)


def _file(columns: list[str], rows: list[list], kind: str) -> str:
    def plain(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        return "" if v is None else v

    if kind == "csv":
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(columns)
        for row in rows:
            writer.writerow([plain(v) for v in row])
        return out.getvalue()
    records = [dict(zip(columns, row, strict=True)) for row in rows]
    if kind == "json":
        return json.dumps(records, ensure_ascii=False)
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)


@app.post("/v2/workbooks/{workbook_id}/export")
def export_workbook(workbook_id: str, request: Request, raw: RawBody) -> dict:
    def call(store, observation):
        body = _json(raw)
        fmt = (body.get("format") or {}).get("type")
        element_id = body.get("elementId")
        if not element_id:
            raise Refusal(400, "bad_request", "Only element exports are supported here: "
                                              "`elementId` is required")
        if fmt not in EXPORT_TYPES:
            raise Refusal(400, "bad_request", f"Unsupported export format: {fmt}")
        if body.get("parameters"):
            raise Refusal(400, "bad_request", "Control parameters are not supported")
        if body.get("tag"):
            raise Refusal(400, "bad_request", "Version tags are not supported")
        try:
            offset = int(body.get("offset") or 0)
            limit = None if body.get("rowLimit") is None else int(body["rowLimit"])
        except (TypeError, ValueError):
            raise Refusal(400, "bad_request", "`rowLimit` and `offset` are numbers") from None
        doc = store.workbook(workbook_id)["versions"][-1]
        _s, render, SemanticError, _c, contents_of, element_visual, common = _semantic()
        from world.semantic.formats.sigma.datamodel import read_data_model

        element = next((el for el in contents_of(doc).get("elements", [])
                        if el.get("id") == element_id), None)
        if element is None:
            raise Refusal(404, "not_found", f"Element {element_id} not found")
        # One element is computed, from the PUBLISHED data model: another
        # broken element of the same workbook does not stop this export.
        try:
            model, index = read_data_model(store.data_model_for(doc), store.catalog())
            visual = element_visual(element, model, index)
            result = render.render_visual(model, visual, store.warehouse)
        except SemanticError as exc:
            raise Refusal(400, "bad_request", str(exc)) from None
        rows = result["rows"][offset:]
        if limit is not None:
            rows = rows[:limit]
        store.state["counter"] += 1
        query_id = common.stable_uuid("query", workbook_id, str(store.state["counter"]))
        folder = store.root / "queries"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{query_id}.{fmt}").write_text(_file(result["columns"], rows, fmt),
                                                  encoding="utf-8")
        store.state["queries"][query_id] = {"workbookId": workbook_id, "elementId": element_id,
                                            "format": fmt, "createdAt": store.clock(),
                                            "rows": len(rows)}
        store.save()
        observation.targets = [_wb_target(workbook_id)]
        observation.read_targets = _relations(model, visual) + [_wb_target(workbook_id)]
        return {"jobComplete": True, "queryId": query_id}

    return _serve(request, "workbook_export", call)


@app.get("/v2/query/{query_id}/download")
def download_query(query_id: str, request: Request) -> Response:
    def call(store, observation):
        query = store.state["queries"].get(query_id)
        if query is None:
            raise Refusal(404, "not_found", f"Query {query_id} not found or expired")
        _read(observation, f"sigma:query:{query_id}")
        path = store.root / "queries" / f"{query_id}.{query['format']}"
        return {"format": query["format"], "body": path.read_text(encoding="utf-8")}

    result = _serve(request, "query_download", call)
    return Response(result["body"], media_type=EXPORT_TYPES[result["format"]])


# ------------------------------------------------- element SQL, columns, sources
def _elements_module():
    """formats/sigma/elements.py, imported late for the reason `_semantic` is."""
    try:
        from world.semantic.formats.sigma import elements
    except ImportError as exc:  # pragma: no cover - a broken image
        raise ServiceError(503, "unavailable", f"Sigma's model engine is not installed: "
                                               f"{exc}") from None
    return elements


def _published_elements(store: SigmaStore, workbook_id: str) -> tuple[dict, list[dict]]:
    _s, _r, _e, _c, contents_of, _ev, _co = _semantic()
    doc = store.workbook(workbook_id)["versions"][-1]
    return doc, contents_of(doc).get("elements", [])


def _one_element(elements: list[dict], element_id: str) -> dict:
    found = next((el for el in elements if el.get("id") == element_id), None)
    if found is None:
        raise Refusal(404, "not_found", f"Element {element_id} not found")
    return found


def _published_model(store: SigmaStore, doc: dict):
    """(model, index) of the PUBLISHED data model the workbook reads, or the
    reason it does not parse - which every element query then reports."""
    _s, _r, SemanticError, *_rest = _semantic()
    from world.semantic.formats.sigma.datamodel import read_data_model

    try:
        model, index = read_data_model(store.data_model_for(doc), store.catalog())
    except SemanticError as exc:
        return None, None, str(exc)
    except Refusal as exc:
        return None, None, exc.message
    return model, index, None


def _query_entry(el: dict, model, index, problem: str | None) -> dict:
    elements = _elements_module()
    _s, _r, SemanticError, *_rest = _semantic()
    entry = {"elementId": el["id"], "name": elements.element_name(el)}
    if problem is not None:
        entry["error"] = problem
        return entry
    try:
        entry["sql"] = elements.element_sql(el, model, index)
    except SemanticError as exc:
        entry["error"] = str(exc)
    return entry


def _no_versions(request: Request) -> None:
    if request.query_params.get("tagName"):
        raise Refusal(400, "bad_request", "Version tags are not supported")
    if request.query_params.get("bookmarkId"):
        raise Refusal(400, "bad_request", "Saved views (bookmarks) are not supported")


@app.get("/v2/workbooks/{workbook_id}/elements/{element_id}/query")
def element_query(workbook_id: str, element_id: str, request: Request) -> dict:
    """`getElementQuery`: `{elementId, name, sql}`, or `error` instead of `sql`."""
    def call(store, observation):
        _no_versions(request)
        doc, elements = _published_elements(store, workbook_id)
        el = _one_element(elements, element_id)
        _read(observation, _wb_target(workbook_id))
        model, index, problem = _published_model(store, doc)
        return _query_entry(el, model, index, problem)

    return _serve(request, "workbook_element_query", call, **dict(request.query_params))


@app.get("/v2/workbooks/{workbook_id}/queries")
def workbook_queries(workbook_id: str, request: Request) -> dict:
    """`listWorkbookQueries`: one `{elementId, name, sql | error}` per element
    that carries data (text, image and container elements run no query)."""
    def call(store, observation):
        doc, elements = _published_elements(store, workbook_id)
        _read(observation, _wb_target(workbook_id))
        model, index, problem = _published_model(store, doc)
        carrying = [el for el in elements if _elements_module().has_data(el)]
        page = _page(request, carrying, has_more=False)
        page["entries"] = [_query_entry(el, model, index, problem) for el in page["entries"]]
        return page

    return _serve(request, "workbook_queries", call, **dict(request.query_params))


@app.get("/v2/workbooks/{workbook_id}/elements/{element_id}/columns")
def element_columns(workbook_id: str, element_id: str, request: Request) -> dict:
    """`getElementColumns`: `{columnId, label, formula, type}` per column."""
    def call(store, observation):
        doc, elements = _published_elements(store, workbook_id)
        el = _one_element(elements, element_id)
        _read(observation, _wb_target(workbook_id))
        model, index, _problem = _published_model(store, doc)
        return _page(request, _elements_module().element_columns(el, model, index),
                     has_more=False)

    return _serve(request, "workbook_element_columns", call, **dict(request.query_params))


@app.get("/v2/workbooks/{workbook_id}/columns")
def workbook_columns(workbook_id: str, request: Request) -> dict:
    """`listWorkbookColumns`: every element's columns, `{columnId, name,
    formula, type, elementId}` (this endpoint says `name` where the element's
    says `label`)."""
    def call(store, observation):
        doc, elements = _published_elements(store, workbook_id)
        _read(observation, _wb_target(workbook_id))
        model, index, _problem = _published_model(store, doc)
        entries = []
        for el in elements:
            for column in _elements_module().element_columns(el, model, index):
                entries.append({"columnId": column["columnId"], "name": column["label"],
                                "formula": column["formula"], "type": column["type"],
                                "elementId": el["id"]})
        return _page(request, entries, has_more=False)

    return _serve(request, "workbook_columns", call, **dict(request.query_params))


@app.get("/v2/workbooks/{workbook_id}/sources")
def workbook_sources(workbook_id: str, request: Request) -> Response:
    """`getWorkbookSources`: a bare array. Every element here is built on a
    data model (workbook.py refuses the other source kinds at publish time), so
    each entry is `{type: "data-model", dataModelId, elementIds}` - the data
    model elements the workbook's elements read, in first-use order."""
    def call(store, observation):
        _doc, elements = _published_elements(store, workbook_id)
        _read(observation, _wb_target(workbook_id))
        used: dict[str, list[str]] = {}
        for el in elements:
            source = el.get("source") or {}
            if source.get("kind") != "data-model" or not source.get("dataModelId"):
                continue
            ids = used.setdefault(source["dataModelId"], [])
            if source.get("elementId") and source["elementId"] not in ids:
                ids.append(source["elementId"])
        return [{"type": "data-model", "dataModelId": model_id, "elementIds": ids}
                for model_id, ids in used.items()]

    return JSONResponse(_serve(request, "workbook_sources", call))


# -------------------------------------------------------------- data models
@app.get("/v2/dataModels")
def list_data_models(request: Request) -> dict:
    def call(store, observation):
        docs = [e["versions"][-1] for e in store.state["dataModels"].values()]
        docs.sort(key=lambda d: (d["name"], d["dataModelId"]))
        page = _page(request, [_data_model_meta(d) for d in docs])
        _read(observation, *(_dm_target(d["dataModelId"]) for d in page["entries"]))
        if not page["entries"]:
            _read(observation, "sigma:data-models")
        return page

    return _serve(request, "data_models_list", call, **dict(request.query_params))


@app.get("/v2/dataModels/{data_model_id}")
def get_data_model(data_model_id: str, request: Request) -> dict:
    def call(store, observation):
        doc = store.data_model(data_model_id)["versions"][-1]
        _read(observation, _dm_target(data_model_id))
        return _data_model_meta(doc)

    return _serve(request, "data_model_get", call)


@app.get("/v2/dataModels/{data_model_id}/spec")
def get_data_model_spec(data_model_id: str, request: Request) -> Response:
    def call(store, observation):
        entry = store.data_model(data_model_id)
        _read(observation, _dm_target(data_model_id))
        return entry["versions"][-1]

    return _respond(request, _serve(request, "data_model_spec", call))


@app.put("/v2/dataModels/{data_model_id}/spec")
def put_data_model_spec(data_model_id: str, request: Request, raw: RawBody) -> dict:
    """"The endpoint only supports updates to the `pages` array and its
    contents. Additional fields are not required and are ignored." """
    def call(store, observation):
        body = _json(raw, request)
        entry = store.data_model(data_model_id)
        current = entry["versions"][-1]
        if not isinstance(body.get("pages"), list):
            raise Refusal(400, "bad_request", "`pages` is required: partial updates are not "
                                              "supported")
        if body.get("schemaVersion") != 1:
            raise Refusal(400, "bad_request", "`schemaVersion` must be 1")
        expected = body.get("documentVersion")
        if expected is not None and expected != current.get("documentVersion"):
            raise Refusal(409, "conflict", f"Data model has changed since version {expected}")
        candidate = {**current, "pages": body["pages"]}
        store.read(candidate, None)
        version = current.get("documentVersion", 1) + 1
        entry["versions"].append({**candidate, "documentVersion": version,
                                  "latestDocumentVersion": version,
                                  "updatedAt": _stamp(store.clock()),
                                  "updatedBy": current.get("ownerId")})
        store.save()
        observation.op_class = "write"
        observation.targets = [_dm_target(data_model_id)]
        return {"success": True, "dataModelId": data_model_id}

    return _serve(request, "data_model_update", call)


@app.get("/v2/dataModels/{data_model_id}/elements")
def list_data_model_elements(data_model_id: str, request: Request) -> dict:
    def call(store, observation):
        doc = store.data_model(data_model_id)["versions"][-1]
        _read(observation, _dm_target(data_model_id))
        entries = [_element_entry(el) for page in doc.get("pages", [])
                   for el in page.get("elements", [])]
        return _page(request, entries)

    return _serve(request, "data_model_elements", call)


# -------------------------------------------------------------- connections
@app.get("/v2/connections")
def list_connections(request: Request) -> dict:
    def call(store, observation):
        search = request.query_params.get("search", "").lower()
        conns = sorted(store.state["connections"].values(), key=lambda c: c["name"])
        conns = [c for c in conns if search in c["name"].lower()]
        page = _page(request, conns)
        _read(observation, *(f"sigma:connection:{c['connectionId']}" for c in page["entries"]))
        if not page["entries"]:
            _read(observation, "sigma:connections")
        return page

    return _serve(request, "connections_list", call, **dict(request.query_params))


@app.get("/v2/connections/{connection_id}")
def get_connection(connection_id: str, request: Request) -> dict:
    def call(store, observation):
        found = store.connection(connection_id)
        _read(observation, f"sigma:connection:{connection_id}")
        return found

    return _serve(request, "connection_get", call)


@app.post("/v2/connections/{connection_id}/sync")
def sync_connection(connection_id: str, request: Request, raw: RawBody) -> dict:
    """Refresh what Sigma knows of the warehouse under `path`: [] is the whole
    connection, [DATABASE], [DATABASE, SCHEMA], [DATABASE, SCHEMA, TABLE]."""
    def call(store, observation):
        store.connection(connection_id)
        body = _json(raw)
        path = body.get("path")
        if not isinstance(path, list) or len(path) > 3 \
                or not all(isinstance(p, str) for p in path):
            raise Refusal(400, "bad_request", "`path` must be a list of up to three names")
        _s, _r, _e, Catalog, _co, _ev, common = _semantic()
        if not store.warehouse.exists():
            raise Refusal(400, "bad_request", "The connection's warehouse is unreachable")
        if path and path[0].upper() != common.DATABASE:
            raise Refusal(404, "not_found", f"Path {'.'.join(path)} not found")
        live = Catalog.from_warehouse(store.warehouse).tables
        prefix = ".".join(p.lower() for p in path[1:])
        selected = {k: v for k, v in live.items()
                    if not prefix or k == prefix or k.startswith(prefix + ".")}
        if path[1:] and not selected:
            raise Refusal(404, "not_found", f"Path {'.'.join(path)} not found")
        for key in [k for k in store.state["catalog"]
                    if not prefix or k == prefix or k.startswith(prefix + ".")]:
            del store.state["catalog"][key]
        store.state["catalog"].update(selected)
        store.state["connections"][connection_id]["lastActiveAt"] = _stamp(store.clock())
        store.save()
        # A write: the sync replaces what Sigma's catalog says about these
        # tables (measured 2026-10-04, bi_e2e_check.py, it was filed as a read).
        observation.op_class = "write"
        observation.targets = [f"sigma:connection:{connection_id}"]
        observation.read_targets = sorted(selected)
        return {}

    return _serve(request, "connection_sync", call)


# ----------------------------------------------------------------- fallback
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def not_found(path: str, request: Request) -> dict:
    """Every request writes a row, including one for a route Sigma has and this
    surface does not (members, teams, grants, ...)."""
    def call(store, observation):
        raise Refusal(404, "not_found", f"No route {request.method} /{path}")

    return _serve(request, "unknown_route", call, auth=False)


__all__ = ["HOST", "NAME", "PORT", "SigmaStore", "app"]
