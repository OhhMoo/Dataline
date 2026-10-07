"""Databricks workspace REST, tier 1: SQL, Unity Catalog, Jobs - over the world.

WHY A FAKE DATABRICKS (docs/environments/TOOL_INVENTORY.md section 3.2, tier 1). At a
Databricks shop the engineer reaches the lakehouse through a SQL warehouse - the Python
SDK, the SQL connector, dbt-databricks, or plain REST - browses Unity Catalog's
three-level namespace, and runs jobs. This service is that workspace, at
`https://databricks.internal` (port 443: docs/environments/DATABRICKS_SPIKE.md says why
not 8098), hosted in native-services when a profile plugs `warehouse: databricks`
(`alkera_gateway.backends.BACKENDS["databricks"]`).

ROUTES (shapes from Databricks' REST API reference; research in DATABRICKS_SPIKE.md):

  SQL Statement Execution 2.0
    POST   /api/2.0/sql/statements[/]                      run one statement
    GET    /api/2.0/sql/statements/{id}                    its status and result
    GET    /api/2.0/sql/statements/{id}/result/chunks/{n}  a result chunk (one: n = 0)
    POST   /api/2.0/sql/statements/{id}/cancel
    DELETE /api/2.0/sql/statements/{id}                    close (the connector does)
    POST   /api/2.0/sql/sessions, DELETE .../sessions/{id} the connector's SEA sessions
  SQL Warehouses 2.0  GET /api/2.0/sql/warehouses[/{id}]   the one warehouse, `alkera`
  Unity Catalog 2.1   GET /api/2.1/unity-catalog/catalogs[/{name}]
                      GET /api/2.1/unity-catalog/schemas?catalog_name=, /schemas/{full}
                      GET /api/2.1/unity-catalog/tables?catalog_name=&schema_name=,
                          /tables/{full_name}
  SCIM 2.0            GET /api/2.0/preview/scim/v2/Me
  Jobs 2.1 and 2.2    GET /api/2.{1,2}/jobs/list, POST .../jobs/run-now,
                      GET .../jobs/runs/get?run_id=, GET .../jobs/runs/list (the locked
                      SDK, 0.47.0, calls 2.2; the shapes this subset uses are equal)
  discovery           GET /.well-known/databricks-config   (unauthenticated; the SDK
                      >= 0.5x reads it before its first call)
  connector           GET /api/2.0/connector-service/feature-flags/{driver}/{version}
                      (the SQL connector's, at connect: none set)
  telemetry           POST /telemetry-ext, /telemetry-unauth (the connector's own
                      usage reports: accepted, recorded, kept nowhere else)
  /health             like every native service

  The SQL connector reaches feature flags and telemetry on port 443 whatever `_port`
  says - one more reason this workspace is on 443 (DATABRICKS_SPIKE.md).

  Anything else: 404 `{"error_code": "ENDPOINT_NOT_FOUND", "message": "No API found
  for '<METHOD> <path>'"}`, Databricks' own envelope.

AUTH. A personal access token, `Authorization: Bearer <token>`, equal to the workspace's
obviously fake credential (`context.DEV_TOKEN`); anything else is 401
`UNAUTHENTICATED`. Every statement runs as that token's user (`databricks_sql.USER`).

SQL (alkera_gateway.databricks_sql). A seeded DuckDB COPY of the world's warehouse in
this service's own state, `<service-state>/databricks/lakehouse.duckdb` (the parent of
`bi_state_dir`; native-services only, never mounted in the episode), seeded at startup
or on the first request and kept for the rollout. Catalog `main` (alias
`hive_metastore`) -> the world's schemas. Writes land in the copy and never in the
world; the grader never reads through this service. Delta-only SQL is refused by name.
Results are JSON_ARRAY, inline, one chunk - whatever format or disposition was asked
for, and the manifest says so (the connector builds its reader from the manifest, and
there is no cloud storage to presign EXTERNAL_LINKS against). A statement always
completes inside its request: there is no PENDING state to poll.

TRANSPORT. The Statement Execution API only. databricks-sql-connector speaks it with
`use_sea=True`; its default Thrift transport is refused with HTTP 400 and the
`x-thriftserver-error-message` header the connector puts in its exception.

JOBS are the world's scheduler, nothing else. `jobs/list` is `sched_list` (job ids 1..n
in job-name order), `run-now` is `sched_trigger` - the same `Operations` call
`./ops/sched trigger` makes, so the run is WorldScheduler.trigger's: one manual run,
executed for real at trigger time and recorded in `var/sched/state.json`, the clock
moved by that run's own duration and nothing else. There is no `advance` here: only
the grader moves the world's time. `runs/get` and `runs/list` read the ledger
(`sched_state`); a run's id is its 1-based position there.

ROWS. Every request writes exactly one 9.2.2 row before it answers, refusals and
unknown routes included: `databricks.statement` per statement (the registry row's
`statement_tool`, args `sql`, op_class and targets from the gateway's own SQL
classifier, dialect databricks), `databricks.sched_list` / `sched_trigger` /
`sched_state` for Jobs (named after the MCP operation they are, like `sched.*`, so the
blindness detector attributes a triggered run's world changes), and
`databricks.<op>` for the rest: `statement_get`, `statement_chunk`,
`statement_cancel`, `statement_close`, `session_create`, `session_delete`,
`warehouses_list`, `warehouse_get`, `catalogs_list`, `catalog_get`, `schemas_list`,
`schema_get`, `tables_list`, `table_get`, `me`, `host_metadata`, `feature_flags`,
`telemetry`, `thrift`, `health`, `unknown_route`. Only a statement can be a write: the
service's own bookkeeping (sessions, closes, telemetry) changes no data and is a read.

NOT IMITATED. Clusters, notebooks, DBFS and Volumes, Repos, secrets, MLflow, Delta
Sharing, permissions and grants, UC writes over REST (create catalog/schema/table: use
SQL), jobs create/update/delete, run cancel and repair, external links, Arrow results,
async statements, the Databricks CLI (its licence: DATABRICKS_SPIKE.md).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from ... import databricks_sql as dbx
from ...records import Observation, ToolError
from ..context import DEV_TOKEN, Ledger, ServiceError, audit_unrouted, context_of, service_health
from ..http import RawBody
from ..shims import sched_cli

log = logging.getLogger(__name__)

NAME = "databricks"
HOST = "databricks.internal"
PORT = 443
STATEMENT_TOOL = f"{NAME}.statement"
WAREHOUSE_ID = "alkera"
HTTP_PATH = f"/sql/1.0/warehouses/{WAREHOUSE_ID}"
WORKSPACE_ID = "1234567890123456"
USER = dbx.USER
#: Where a TLS-serving process mints into, when not the registry's trust directory
#: (`backends.trust_dir`): a host-topology stack cannot write /etc/alkera.
TRUST_ENV = "ALKERA_DATABRICKS_TRUST"
#: The SQL warehouse states a statement can be in.
SUCCEEDED, FAILED = "SUCCEEDED", "FAILED"
THRIFT_REFUSAL = ("Thrift HS2 transport is not available in this environment: connect "
                  "with use_sea=True (SQL Statement Execution API)")


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Seed the lakehouse before the first request, when served by serve.py."""
    try:
        lakehouse_for(context_of(app).config)
    except Exception as exc:  # noqa: BLE001 - every request retries and says why
        log.warning("databricks: the lakehouse is not seeded yet: %s", exc)
    yield


app = FastAPI(title="Databricks workspace (alkera native surface)", docs_url=None,
              redoc_url=None, openapi_url=None, lifespan=_lifespan)
# A row for the validation errors that bypass the handlers (context.audit_unrouted).
audit_unrouted(app, NAME)


class DatabricksError(ServiceError):
    """A request Databricks refuses over HTTP: `{"error_code", "message"}`."""


@app.exception_handler(ServiceError)
async def _service_error(_request: Request, exc: ServiceError) -> JSONResponse:
    headers = {"x-thriftserver-error-message": THRIFT_REFUSAL} if exc.code == "THRIFT" else {}
    code = "BAD_REQUEST" if exc.code == "THRIFT" else exc.code
    return JSONResponse(status_code=exc.status, headers=headers,
                        content={"error_code": code, "message": exc.message})


# ---------------------------------------------------------------- certificate
def certificate() -> tuple[Path, Path]:
    """Mint this boot's certificate (`native.tls.mint`) and publish its bundle.

    serve.py calls it for a hosted TLS row before it binds. The public bundle goes
    to the registry's trust directory (`/etc/alkera/databricks-tls`, the volume the
    episode mounts read-only; `ALKERA_DATABRICKS_TRUST` overrides it); the key to a
    directory of this process's own, never under it and never kept."""
    from ...backends import BACKENDS, trust_dir
    from ..tls import mint

    trust = Path(os.environ.get(TRUST_ENV) or trust_dir(BACKENDS[NAME]))
    private = Path(tempfile.mkdtemp(prefix="alkera-databricks-tls-"))
    return mint(HOST, trust, private_dir=private)


# ------------------------------------------------------------------ the state
_LOCK = threading.RLock()
_LAKEHOUSES: dict[Path, dbx.Lakehouse] = {}
_STATEMENTS: dict[str, dict] = {}
_SESSIONS: dict[str, dbx.Context] = {}
_COUNTER = {"statement": 0, "session": 0}


def reset() -> None:
    """Close every lakehouse and forget every statement and session (tests)."""
    with _LOCK:
        for lakehouse in _LAKEHOUSES.values():
            lakehouse.close()
        _LAKEHOUSES.clear()
        _STATEMENTS.clear()
        _SESSIONS.clear()
        _COUNTER.update(statement=0, session=0)


def state_dir(config) -> Path:
    """This service's own state: `<service-state>/databricks`, beside the BI vendors'
    `<service-state>/bi` (gateway/docker/native_services.py)."""
    return Path(config.bi_state_dir).parent / NAME


def lakehouse_for(config) -> dbx.Lakehouse:
    """The world's lakehouse copy, seeded on first use, one connection per file."""
    path = state_dir(config) / "lakehouse.duckdb"
    with _LOCK:
        found = _LAKEHOUSES.get(path)
        if found is None:
            seeded = dbx.Lakehouse.seed(Path(config.warehouse), path)
            if seeded is not None:
                (path.parent / "seed.json").write_text(
                    json.dumps({"seed": seeded}, indent=1, sort_keys=True) + "\n",
                    encoding="utf-8")
            found = _LAKEHOUSES[path] = dbx.Lakehouse(path)
        return found


def _new_id(kind: str) -> str:
    with _LOCK:
        _COUNTER[kind] += 1
        n = _COUNTER[kind]
    prefix = "01f0" if kind == "statement" else "02f0"
    raw = f"{prefix}{n:028x}"
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


# ------------------------------------------------------------------- plumbing
def _authenticate(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or token.strip() != DEV_TOKEN:
        raise DatabricksError(401, "UNAUTHENTICATED",
                              "Invalid access token." if token else
                              "Credential was not sent or was of an unsupported type for "
                              "this API.")
    return USER


def _serve(request: Request, tool: str, call, *, auth: bool = True, **args):
    """One request, one row: authenticate, run, classify - all inside the span."""
    ledger = Ledger(request, NAME)
    try:
        with ledger.span(tool, **args) as row:
            def run(_operations, observation):
                if auth:
                    ledger.actor = _authenticate(request)
                return call(observation)
            return row.run(run)
    except ToolError as exc:
        raise ServiceError.from_tool_error(exc) from None


def _read(observation: Observation, *targets: str) -> None:
    """A read of `targets`. Also the class of the service's own bookkeeping - a session
    opened or closed, a statement closed or cancelled, a telemetry report - which
    changes no data anywhere: a write-class row with an HTTP `path` is read by the
    process signals (tasks/harness/path.py) as a write to that path."""
    observation.targets = list(targets)
    observation.read_targets = list(targets)


def _json(raw: bytes) -> dict:
    try:
        body = json.loads(raw) if raw else {}
    except (ValueError, UnicodeError):
        raise DatabricksError(400, "MALFORMED_REQUEST", "Malformed JSON in the request "
                                                        "body.") from None
    if not isinstance(body, dict):
        raise DatabricksError(400, "MALFORMED_REQUEST", "The request body must be a JSON "
                                                        "object.")
    return body


def _lakehouse(request: Request) -> dbx.Lakehouse:
    return lakehouse_for(context_of(request.app).config)


def _warehouse(warehouse_id) -> None:
    if not warehouse_id:
        raise DatabricksError(400, "INVALID_PARAMETER_VALUE", "warehouse_id is required.")
    if str(warehouse_id) != WAREHOUSE_ID:
        raise DatabricksError(404, "RESOURCE_DOES_NOT_EXIST",
                              f"SQL warehouse {warehouse_id} does not exist.")


# ------------------------------------------------------------------ statements
def classify(sql: str) -> Observation:
    """op_class and targets for one statement: the gateway's own SQL classifier
    (dialect databricks), the Postgres tailer's verb fallback behind it, and the
    session commands (USE, SET) as reads of the session. A statement that names no
    table targets the warehouse it ran on."""
    from ...sql import inspect_sql
    from ..tailers.postgres import fallback_observation

    found = Observation()
    try:
        inspect_sql(sql, found, read_only=False, dialect="databricks")
    except ToolError:
        found = fallback_observation(sql, dialect="databricks")
    verb = re.match(r"\s*([A-Za-z]*)", sql).group(1).upper()
    if verb in ("USE", "SET", "RESET"):
        found = Observation(op_class="read", targets=["databricks:session"],
                            read_targets=["databricks:session"])
    if not found.targets:
        found.targets = [f"databricks:warehouse:{WAREHOUSE_ID}"]
        if found.op_class == "read":
            found.read_targets = list(found.targets)
    return found


class StatementFailed(Exception):
    """A statement the warehouse ran and failed: HTTP 200, `status.state: FAILED`."""

    def __init__(self, body: dict, code: str, message: str):
        super().__init__(message)
        self.body = body
        self.code = code


def _manifest(result: dbx.Result, rows: list, truncated: bool) -> dict:
    columns = []
    for position, col in enumerate(result.columns):
        entry = {"name": col.name, "type_text": col.type_text, "type_name": col.type_name,
                 "position": position}
        if col.type_precision is not None:
            entry["type_precision"] = col.type_precision
            entry["type_scale"] = col.type_scale
        columns.append(entry)
    return {"format": "JSON_ARRAY",
            "schema": {"column_count": len(columns), "columns": columns},
            "total_chunk_count": 1,
            "chunks": [{"chunk_index": 0, "row_offset": 0, "row_count": len(rows)}],
            "total_row_count": len(rows), "truncated": truncated}


def _chunk(rows: list) -> dict:
    chunk = {"chunk_index": 0, "row_offset": 0, "row_count": len(rows)}
    if rows:
        chunk["data_array"] = [[dbx.json_value(v) for v in row] for row in rows]
    return chunk


def _failure(statement_id: str, exc: Exception) -> tuple[dict, str, str]:
    if isinstance(exc, dbx.Unsupported):
        error_class, sqlstate = "UNSUPPORTED_FEATURE", "0A000"
        message = f"[{error_class}] {exc} SQLSTATE: {sqlstate}"
    else:
        error_class, sqlstate, message = exc.error_class, exc.sqlstate, str(exc)
    body = {"statement_id": statement_id,
            "status": {"state": FAILED, "sql_state": sqlstate,
                       "error": {"error_code": "BAD_REQUEST", "message": message}}}
    return body, error_class, message


@app.post("/api/2.0/sql/statements")
@app.post("/api/2.0/sql/statements/")
def execute_statement(request: Request, raw: RawBody):
    """One statement on the lakehouse; one `databricks.statement` row, written before
    the answer. A failed statement is HTTP 200 with `status.state: FAILED` (the API's
    contract) and a row with `exit: error`."""
    body = _json(raw) if raw else {}
    sql = body.get("statement")
    args = {"sql": sql if isinstance(sql, str) else None,
            "warehouse_id": body.get("warehouse_id")}
    for key in ("session_id", "catalog", "schema", "parameters", "row_limit"):
        if body.get(key) is not None:
            args[key] = body[key]
    statement_id = _new_id("statement")
    ledger = Ledger(request, NAME)
    try:
        with ledger.span("statement", **args) as row:
            def run(_operations, observation):
                ledger.actor = _authenticate(request)
                _warehouse(body.get("warehouse_id"))
                if not isinstance(sql, str) or not sql.strip():
                    raise DatabricksError(400, "INVALID_PARAMETER_VALUE",
                                          "statement is required.")
                seen = classify(sql)
                observation.op_class = seen.op_class
                observation.targets = seen.targets
                observation.read_targets = seen.read_targets
                observation.flags = list(seen.flags)
                session = body.get("session_id")
                with _LOCK:
                    if session and session not in _SESSIONS:
                        raise DatabricksError(404, "NOT_FOUND",
                                              f"Session {session} does not exist.")
                    context = _SESSIONS.get(session) or dbx.Context(
                        catalog=str(body.get("catalog") or dbx.CATALOG).lower(),
                        schema=str(body.get("schema") or dbx.DEFAULT_SCHEMA).lower())
                try:
                    params = dbx.parameters(body.get("parameters"))
                    result, after = _lakehouse(request).execute(sql, context, params)
                except (dbx.SqlError, dbx.Unsupported) as exc:
                    failed, code, message = _failure(statement_id, exc)
                    with _LOCK:
                        _STATEMENTS[statement_id] = failed
                    raise StatementFailed(failed, code, message) from None
                if session:
                    with _LOCK:
                        _SESSIONS[session] = after
                rows, truncated = result.rows, False
                limit = body.get("row_limit")
                if isinstance(limit, int) and 0 <= limit < len(rows):
                    rows, truncated = rows[:limit], True
                if result.rows_affected is not None:
                    observation.rows_affected = result.rows_affected
                answer = {"statement_id": statement_id, "status": {"state": SUCCEEDED},
                          "manifest": _manifest(result, rows, truncated),
                          "result": _chunk(rows)}
                with _LOCK:
                    _STATEMENTS[statement_id] = answer
                return answer
            answer = row.run(run)
            # The row carries what happened, not the data: a SELECT * is not copied
            # into the action log.
            row.result = {"statement_id": statement_id, "state": SUCCEEDED,
                          "columns": [c["name"] for c in
                                      answer["manifest"]["schema"]["columns"]],
                          "rows": answer["manifest"]["total_row_count"]}
            return answer
    except StatementFailed as failed:
        return failed.body
    except ToolError as exc:
        raise ServiceError.from_tool_error(exc) from None


def _stored(statement_id: str) -> dict:
    with _LOCK:
        found = _STATEMENTS.get(statement_id)
    if found is None:
        raise DatabricksError(404, "RESOURCE_DOES_NOT_EXIST",
                              f"Statement {statement_id} does not exist.")
    return found


@app.get("/api/2.0/sql/statements/{statement_id}")
def get_statement(statement_id: str, request: Request):
    def call(observation):
        _read(observation, f"databricks:statement:{statement_id}")
        return _stored(statement_id)
    return _serve(request, "statement_get", call, statement_id=statement_id)


@app.get("/api/2.0/sql/statements/{statement_id}/result/chunks/{chunk_index}")
def get_chunk(statement_id: str, chunk_index: int, request: Request):
    def call(observation):
        _read(observation, f"databricks:statement:{statement_id}")
        stored = _stored(statement_id)
        if stored["status"]["state"] != SUCCEEDED:
            raise DatabricksError(400, "BAD_REQUEST",
                                  f"Statement {statement_id} has no result: it "
                                  f"{stored['status']['state'].lower()}.")
        if chunk_index != 0:
            raise DatabricksError(400, "INVALID_PARAMETER_VALUE",
                                  f"Chunk index {chunk_index} is out of range: the result "
                                  f"has 1 chunk.")
        return stored["result"]
    return _serve(request, "statement_chunk", call, statement_id=statement_id,
                  chunk_index=chunk_index)


@app.post("/api/2.0/sql/statements/{statement_id}/cancel")
def cancel_statement(statement_id: str, request: Request):
    """Every statement has completed by the time its request returns, so a cancel
    finds nothing running: 200, as the API answers for a finished statement."""
    def call(observation):
        _read(observation, f"databricks:statement:{statement_id}")
        _stored(statement_id)
        return {}
    return _serve(request, "statement_cancel", call, statement_id=statement_id)


@app.delete("/api/2.0/sql/statements/{statement_id}")
def close_statement(statement_id: str, request: Request):
    """Release a statement's result (the connector closes every cursor's statement
    this way); its id answers 404 afterwards."""
    def call(observation):
        _read(observation, f"databricks:statement:{statement_id}")
        _stored(statement_id)
        with _LOCK:
            _STATEMENTS.pop(statement_id, None)
        return {}
    return _serve(request, "statement_close", call, statement_id=statement_id)


@app.get("/api/2.0/connector-service/feature-flags/{driver}/{version}")
def feature_flags(driver: str, version: str, request: Request):
    """The SQL connector's feature-flag lookup at connect: none are set here."""
    def call(observation):
        _read(observation, "databricks:workspace")
        return {"flags": [], "ttl_seconds": 900}
    return _serve(request, "feature_flags", call, auth=False, driver=driver,
                  version=version)


@app.post("/api/2.0/sql/sessions")
@app.post("/api/2.0/sql/sessions/")
def create_session(request: Request, raw: RawBody):
    body = _json(raw)

    def call(observation):
        _warehouse(body.get("warehouse_id"))
        catalog = str(body.get("catalog") or dbx.CATALOG).lower()
        schema = str(body.get("schema") or dbx.DEFAULT_SCHEMA).lower()
        if catalog not in dbx.CATALOG_ALIASES:
            raise DatabricksError(404, "NOT_FOUND", f"Catalog '{catalog}' was not found.")
        # A schema that does not exist yet is a session's to create (dbt opens its
        # session in its target schema, then runs CREATE SCHEMA IF NOT EXISTS); an
        # unqualified name in it fails until then (Lakehouse.execute).
        session = _new_id("session")
        with _LOCK:
            _SESSIONS[session] = dbx.Context(catalog=catalog, schema=schema)
        _read(observation, f"databricks:session:{session}")
        return {"session_id": session}
    return _serve(request, "session_create", call,
                  **{k: body.get(k) for k in ("warehouse_id", "catalog", "schema")
                     if body.get(k) is not None})


@app.delete("/api/2.0/sql/sessions/{session_id}")
def delete_session(session_id: str, request: Request):
    def call(observation):
        with _LOCK:
            if _SESSIONS.pop(session_id, None) is None:
                raise DatabricksError(404, "NOT_FOUND", f"Session {session_id} does not "
                                                        f"exist.")
        _read(observation, f"databricks:session:{session_id}")
        return {}
    return _serve(request, "session_delete", call, session_id=session_id)


# ------------------------------------------------------------------ warehouses
def _warehouse_info() -> dict:
    with _LOCK:
        sessions = len(_SESSIONS)
    return {
        "id": WAREHOUSE_ID, "name": WAREHOUSE_ID, "cluster_size": "2X-Small",
        "min_num_clusters": 1, "max_num_clusters": 1, "num_clusters": 1,
        "num_active_sessions": sessions, "auto_stop_mins": 10, "creator_name": USER,
        "state": "RUNNING", "warehouse_type": "PRO", "enable_serverless_compute": True,
        "enable_photon": True, "spot_instance_policy": "COST_OPTIMIZED",
        "channel": {"name": "CHANNEL_NAME_CURRENT"}, "health": {"status": "HEALTHY"},
        "odbc_params": {"hostname": HOST, "path": HTTP_PATH, "protocol": "https",
                        "port": PORT},
        "jdbc_url": f"jdbc:databricks://{HOST}:{PORT}/default;transportMode=http;ssl=1;"
                    f"AuthMech=3;httpPath={HTTP_PATH};",
    }


@app.get("/api/2.0/sql/warehouses")
def list_warehouses(request: Request):
    def call(observation):
        _read(observation, f"databricks:warehouse:{WAREHOUSE_ID}")
        return {"warehouses": [_warehouse_info()]}
    return _serve(request, "warehouses_list", call)


@app.get("/api/2.0/sql/warehouses/{warehouse_id}")
def get_warehouse(warehouse_id: str, request: Request):
    def call(observation):
        _read(observation, f"databricks:warehouse:{warehouse_id}")
        _warehouse(warehouse_id)
        return _warehouse_info()
    return _serve(request, "warehouse_get", call, warehouse_id=warehouse_id)


# --------------------------------------------------------------- unity catalog
def _catalog(name: str | None, *, required: bool = True) -> str:
    if not name:
        if required:
            raise DatabricksError(400, "INVALID_PARAMETER_VALUE", "catalog_name is required.")
        return dbx.CATALOG
    if name.lower() not in dbx.CATALOG_ALIASES:
        raise DatabricksError(404, "CATALOG_DOES_NOT_EXIST", f"Catalog '{name}' does not "
                                                             f"exist.")
    return name.lower()


def _catalog_info(name: str) -> dict:
    return {"name": name, "full_name": name, "owner": USER, "catalog_type":
            "MANAGED_CATALOG", "isolation_mode": "OPEN", "securable_type": "CATALOG",
            "metastore_id": WORKSPACE_ID, "comment": "The workspace's lakehouse: the "
            "world's warehouse, one schema per warehouse schema."}


def _schema_info(catalog: str, schema: str) -> dict:
    return {"name": schema, "catalog_name": catalog, "full_name": f"{catalog}.{schema}",
            "owner": USER, "catalog_type": "MANAGED_CATALOG", "securable_type": "SCHEMA",
            "metastore_id": WORKSPACE_ID}


#: The Spark JSON type name of a column, for `type_json`.
_SPARK_JSON = {"int": "integer", "bigint": "long", "smallint": "short",
               "tinyint": "byte"}


def _table_info(lake: dbx.Lakehouse, catalog: str, table: dict) -> dict:
    schema, name = table["table_schema"], table["table_name"]
    columns = []
    for col in lake.columns(schema, name):
        type_text = col["full_data_type"]
        entry = {"name": col["column_name"], "type_text": type_text,
                 "type_name": col["data_type"], "position": col["ordinal_position"] - 1,
                 "nullable": col["is_nullable"] == "YES",
                 "type_json": json.dumps({"name": col["column_name"],
                                          "type": _SPARK_JSON.get(type_text, type_text),
                                          "nullable": col["is_nullable"] == "YES",
                                          "metadata": {}}, separators=(",", ":"))}
        found = re.fullmatch(r"decimal\((\d+),(\d+)\)", type_text)
        if found:
            entry["type_precision"], entry["type_scale"] = map(int, found.groups())
        columns.append(entry)
    full = f"{catalog}.{schema}.{name}"
    info = {"name": name, "catalog_name": catalog, "schema_name": schema,
            "full_name": full, "table_type": table["table_type"], "owner": USER,
            "columns": columns, "securable_type": "TABLE", "metastore_id": WORKSPACE_ID,
            "table_id": _uuid(full), "properties": {}}
    if table["table_type"] == "VIEW":
        definition = next((v for v in _views(lake, schema) if v[0] == name), None)
        if definition:
            info["view_definition"] = definition[1]
    else:
        info["data_source_format"] = "DELTA"
    return info


def _views(lake: dbx.Lakehouse, schema: str) -> list[tuple[str, str]]:
    with lake.lock:
        return lake.con.execute(
            f"SELECT table_name, view_definition FROM {dbx.INFO_CATALOG}.{dbx.INFO_SCHEMA}"
            f".views WHERE table_schema = ?", [schema]).fetchall()


def _uuid(text: str) -> str:
    raw = hashlib.sha256(text.encode()).hexdigest()
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def _page(request: Request, items: list, key: str) -> dict:
    """`max_results` / `page_token` paging; `next_page_token` while more remain."""
    try:
        size = int(request.query_params.get("max_results") or 0)
        start = int(request.query_params.get("page_token") or 0)
    except ValueError:
        raise DatabricksError(400, "INVALID_PARAMETER_VALUE",
                              "max_results and page_token must be integers.") from None
    if size < 0 or start < 0:
        raise DatabricksError(400, "INVALID_PARAMETER_VALUE",
                              "max_results and page_token must not be negative.")
    end = len(items) if size == 0 else start + size
    out = {key: items[start:end]}
    if end < len(items):
        out["next_page_token"] = str(end)
    return out


@app.get("/api/2.1/unity-catalog/catalogs")
def list_catalogs(request: Request):
    def call(observation):
        _read(observation, f"databricks:catalog:{dbx.CATALOG}")
        return {"catalogs": [_catalog_info(dbx.CATALOG)]}
    return _serve(request, "catalogs_list", call)


@app.get("/api/2.1/unity-catalog/catalogs/{name}")
def get_catalog(name: str, request: Request):
    def call(observation):
        _read(observation, f"databricks:catalog:{name.lower()}")
        return _catalog_info(_catalog(name))
    return _serve(request, "catalog_get", call, name=name)


@app.get("/api/2.1/unity-catalog/schemas")
def list_schemas(request: Request):
    params = dict(request.query_params)

    def call(observation):
        catalog = _catalog(params.get("catalog_name"))
        schemas = _lakehouse(request).schemas()
        _read(observation, *(f"{catalog}.{s}" for s in schemas))
        return _page(request, [_schema_info(catalog, s) for s in schemas], "schemas")
    return _serve(request, "schemas_list", call, **params)


@app.get("/api/2.1/unity-catalog/schemas/{full_name}")
def get_schema(full_name: str, request: Request):
    def call(observation):
        _read(observation, full_name.lower())
        catalog, _, schema = full_name.lower().partition(".")
        catalog = _catalog(catalog)
        if not schema or schema not in _lakehouse(request).schemas():
            raise DatabricksError(404, "SCHEMA_DOES_NOT_EXIST",
                                  f"Schema '{full_name}' does not exist.")
        return _schema_info(catalog, schema)
    return _serve(request, "schema_get", call, full_name=full_name)


@app.get("/api/2.1/unity-catalog/tables")
def list_tables(request: Request):
    params = dict(request.query_params)

    def call(observation):
        catalog = _catalog(params.get("catalog_name"))
        schema = (params.get("schema_name") or "").lower()
        if not schema:
            raise DatabricksError(400, "INVALID_PARAMETER_VALUE", "schema_name is required.")
        lake = _lakehouse(request)
        if schema not in lake.schemas():
            raise DatabricksError(404, "SCHEMA_DOES_NOT_EXIST",
                                  f"Schema '{catalog}.{schema}' does not exist.")
        tables = lake.tables(schema)
        _read(observation, *(f"{catalog}.{schema}.{t['table_name']}" for t in tables))
        if not tables:
            _read(observation, f"{catalog}.{schema}")
        return _page(request, [_table_info(lake, catalog, t) for t in tables], "tables")
    return _serve(request, "tables_list", call, **params)


@app.get("/api/2.1/unity-catalog/tables/{full_name}")
def get_table(full_name: str, request: Request):
    def call(observation):
        _read(observation, full_name.lower())
        parts = full_name.lower().split(".")
        missing = DatabricksError(404, "TABLE_DOES_NOT_EXIST",
                                  f"Table '{full_name}' does not exist.")
        if len(parts) != 3:
            raise missing
        catalog = _catalog(parts[0])
        lake = _lakehouse(request)
        found = [t for t in lake.tables(parts[1]) if t["table_name"] == parts[2]] \
            if parts[1] in lake.schemas() else []
        if not found:
            raise missing
        return _table_info(lake, catalog, found[0])
    return _serve(request, "table_get", call, full_name=full_name)


# --------------------------------------------------------------- identity, misc
@app.get("/api/2.0/preview/scim/v2/Me")
def me(request: Request):
    def call(observation):
        _read(observation, f"databricks:user:{USER}")
        return {"schemas": ["urn:ietf:params:scim:schemas:core:2.0:User",
                            "urn:ietf:params:scim:schemas:extension:workspace:2.0:User"],
                "id": "7000000000000001", "userName": USER, "displayName": USER,
                "active": True, "emails": [{"type": "work", "value": USER, "primary": True}],
                "groups": [{"display": "users", "type": "direct",
                            "value": "7000000000000002"}],
                "entitlements": [{"value": "databricks-sql-access"},
                                 {"value": "workspace-access"}]}
    return _serve(request, "me", call)


@app.get("/.well-known/databricks-config")
def host_metadata(request: Request):
    """The SDK's unauthenticated discovery document (SDK >= 0.5x reads it first)."""
    def call(observation):
        _read(observation, "databricks:workspace")
        return {"oidc_endpoint": f"https://{HOST}/oidc", "workspace_id": WORKSPACE_ID,
                "cloud": "aws"}
    return _serve(request, "host_metadata", call, auth=False)


@app.post("/telemetry-ext")
@app.post("/telemetry-unauth")
def telemetry(request: Request, raw: RawBody):
    """The connector's usage reports. Accepted (so the client neither retries nor
    warns), counted in the row, kept nowhere else."""
    body = _json(raw) if raw else {}
    items = body.get("protoLogs") or body.get("items") or []

    def call(observation):
        _read(observation, "databricks:telemetry")
        return {"errors": [], "numSuccess": len(items), "numProtoSuccess": len(items),
                "numRealtimeSuccess": 0}
    return _serve(request, "telemetry", call, auth=request.url.path == "/telemetry-ext",
                  events=len(items))


@app.post("/sql/1.0/warehouses/{warehouse_id}")
@app.post("/sql/1.0/endpoints/{warehouse_id}")
@app.post("/sql/protocolv1/{rest:path}")
def thrift(request: Request, raw: RawBody):
    """The connector's default transport, Thrift HS2 over HTTP: refused with the header
    the connector reads into its exception (DATABRICKS_SPIKE.md)."""
    def call(observation):
        raise DatabricksError(400, "THRIFT", THRIFT_REFUSAL)
    return _serve(request, "thrift", call, auth=False, content_type=request.headers.get(
        "content-type"), bytes=len(raw))


@app.get("/health")
def health(request: Request) -> object:
    """Tables and views in the lakehouse, counted (and seeded) on every probe."""
    def count(_operations, observation):
        lake = _lakehouse(request)
        observation.read_targets = [f"databricks:catalog:{dbx.CATALOG}"]
        return lake.count()
    return service_health(request, NAME, count)


# ------------------------------------------------------------------------ jobs
def _jobs(ops, observation) -> dict[str, int]:
    """{job name: job_id}, ids 1..n in name order (sched_list is sorted by name)."""
    listed = ops.sched_list(observation.child()) or {}
    return {job["id"]: n for n, job in enumerate(listed.get("jobs") or [], 1)}


def _ms(vts: str | None) -> int | None:
    if not vts:
        return None
    return int(datetime.fromisoformat(str(vts).replace("Z", "+00:00")).timestamp() * 1000)


def _job(job: dict, job_id: int) -> dict:
    settings = {"name": job["id"], "max_concurrent_runs": 1, "format": "MULTI_TASK",
                "tasks": [{"task_key": job["id"],
                           **({"description": job["command"]} if job.get("command") else {})}],
                "timeout_seconds": 0}
    found = re.fullmatch(r"(\d{1,2}):(\d{2})", str(job.get("time") or ""))
    if found:
        settings["schedule"] = {"quartz_cron_expression":
                                f"0 {int(found.group(2))} {int(found.group(1))} * * ?",
                                "timezone_id": "UTC", "pause_status": "UNPAUSED"}
    return {"job_id": job_id, "creator_user_name": USER, "settings": settings}


def _run(run: dict, run_id: int, job_ids: dict[str, int]) -> dict:
    status = str(run.get("status") or "")
    result = {"success": "SUCCESS", "failed": "FAILED"}.get(status)
    state = {"life_cycle_state": "TERMINATED" if result else "INTERNAL_ERROR",
             "result_state": result or "FAILED",
             "state_message": str(run.get("error") or run.get("detail") or
                                  ("" if result else f"ledger status {status!r}")),
             "user_cancelled_or_timedout": False}
    start, end = _ms(run.get("vts_start")), _ms(run.get("vts_end"))
    duration = (end - start) if start is not None and end is not None else None
    job = run.get("job")
    return {"job_id": job_ids.get(job), "run_id": run_id, "number_in_job": run_id,
            "run_name": job, "creator_user_name": USER, "state": state,
            "start_time": start, "end_time": end, "setup_duration": 0,
            "execution_duration": duration, "cleanup_duration": 0, "run_duration": duration,
            "trigger": "ONE_TIME" if run.get("trigger") == "manual" else "PERIODIC",
            "run_type": "JOB_RUN", "format": "MULTI_TASK",
            "run_page_url": f"https://{HOST}/#job/{job_ids.get(job)}/run/{run_id}",
            "tasks": [{"task_key": job, "run_id": run_id, "state": state,
                       "start_time": start, "end_time": end}]}


def _jobs_served(request: Request, tool: str, call, **args):
    """A Jobs request as the MCP operation it is (`sched_*`), one row, Databricks' errors."""
    try:
        with Ledger(request, NAME).span(tool, **args) as row:
            def run(operations, observation):
                _authenticate(request)
                return call(operations, observation)
            return row.run(run)
    except ToolError as exc:
        raise DatabricksError(400, "INVALID_PARAMETER_VALUE", str(exc)) from None
    except (ValueError, sched_cli.SchedulerError) as exc:
        raise DatabricksError(400, "INVALID_PARAMETER_VALUE", str(exc)) from None


def _int(value, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise DatabricksError(400, "INVALID_PARAMETER_VALUE",
                              f"{name} must be an integer, got {value!r}.") from None


@app.get("/api/2.1/jobs/list")
@app.get("/api/2.2/jobs/list")
def list_jobs(request: Request):
    def call(ops, observation):
        listed = ops.sched_list(observation) or {}
        jobs = [_job(job, n) for n, job in enumerate(listed.get("jobs") or [], 1)]
        return {"jobs": jobs, "has_more": False}
    return _jobs_served(request, "sched_list", call, **dict(request.query_params))


@app.post("/api/2.1/jobs/run-now")
@app.post("/api/2.2/jobs/run-now")
def run_now(request: Request, raw: RawBody):
    body = _json(raw)
    variables = {str(k): str(v) for k, v in
                 (body.get("job_parameters") or body.get("notebook_params") or {}).items()}

    def call(ops, observation):
        job_id = _int(body.get("job_id"), "job_id")
        names = {n: name for name, n in _jobs(ops, observation).items()}
        if job_id not in names:
            raise DatabricksError(400, "INVALID_PARAMETER_VALUE",
                                  f"Job {job_id} does not exist.")
        run = sched_cli.trigger_job(ops, observation, names[job_id], "", variables)
        run_id = int(str(run.get("id", "run-0")).rsplit("-", 1)[-1])
        return {"run_id": run_id, "number_in_job": run_id}
    return _jobs_served(request, "sched_trigger", call, job_id=body.get("job_id"),
                        job_parameters=variables)


@app.get("/api/2.1/jobs/runs/get")
@app.get("/api/2.2/jobs/runs/get")
def get_run(request: Request):
    params = dict(request.query_params)

    def call(ops, observation):
        run_id = _int(params.get("run_id"), "run_id")
        job_ids = _jobs(ops, observation)
        try:
            run = ops.sched_state(f"run-{run_id:06d}", observation)
        except ValueError:
            raise DatabricksError(400, "INVALID_PARAMETER_VALUE",
                                  f"Run {run_id} does not exist.") from None
        return _run(run, run_id, job_ids)
    return _jobs_served(request, "sched_state", call, **params)


@app.get("/api/2.1/jobs/runs/list")
@app.get("/api/2.2/jobs/runs/list")
def list_runs(request: Request):
    params = dict(request.query_params)

    def call(ops, observation):
        job_ids = _jobs(ops, observation)
        state = ops.sched_state(None, observation)
        runs = [_run(r, n, job_ids) for n, r in enumerate(state.get("runs") or [], 1)]
        if params.get("job_id"):
            wanted = _int(params["job_id"], "job_id")
            runs = [r for r in runs if r["job_id"] == wanted]
        if str(params.get("active_only", "")).lower() == "true":
            runs = []                    # every ledger run has finished
        runs.sort(key=lambda r: (r["start_time"] or 0, r["run_id"]), reverse=True)
        offset = _int(params.get("offset") or 0, "offset")
        limit = _int(params.get("limit") or 25, "limit")
        if not 1 <= limit <= 25 or offset < 0:
            raise DatabricksError(400, "INVALID_PARAMETER_VALUE",
                                  "limit must be between 1 and 25, offset not negative.")
        page = runs[offset:offset + limit]
        out = {"has_more": offset + limit < len(runs)}
        if page:
            out["runs"] = page
        return out
    return _jobs_served(request, "sched_state", call, **params)


# --------------------------------------------------------------------- fallback
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def unknown_route(path: str, request: Request):
    """Every request writes a row, including one for a route Databricks has and this
    workspace does not (clusters, DBFS, notebooks, ...)."""
    def call(observation):
        raise DatabricksError(404, "ENDPOINT_NOT_FOUND",
                              f"No API found for '{request.method} /{path}'")
    return _serve(request, "unknown_route", call, auth=False)


__all__ = ["HOST", "NAME", "PORT", "app", "certificate", "lakehouse_for", "reset"]
