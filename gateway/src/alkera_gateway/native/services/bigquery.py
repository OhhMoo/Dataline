"""BigQuery REST API v2 at bigquery.internal:9050: an audited proxy to goccy's emulator.

The BigQuery plug (`alkera_gateway.backends.BACKENDS["bigquery"]`, hosted) is two
processes. The ENGINE is goccy/bigquery-emulator v0.8.1, compose.yml's `bigquery`
service on backend_net, which the episode cannot reach. The FRONT DOOR is this
module, mounted by native-services on episode_net as `bigquery.internal:9050`
only when a profile plugs it (`de/gcp`). The agent's own clients -
`google.cloud.bigquery.Client(credentials=AnonymousCredentials(),
client_options={"api_endpoint": $BIGQUERY_API_URL})`, curl, and dbt-bigquery
where an image carries it (measured through this proxy; not in the client lock,
gateway/docker/bigquery-clients.in says why) - talk to this module, and this
module talks to `$ALKERA_BIGQUERY_UPSTREAM`
(`http://bigquery:9050` in compose; a supervised child on 127.0.0.1:19050 in
Harbor). The upstream is never hard-coded here.

ONE 9.2.2 ROW PER REQUEST, WRITTEN BEFORE THE RESPONSE. The request is forwarded,
the upstream answers, the row is appended (fsync'd, under the writer lock), and
only then does the client get its bytes - so an answer the agent holds always
has its row on file. Tools:

  bigquery.statement      a query: `POST .../jobs` with `configuration.query`
                          (jobs.insert) or `POST .../queries` (jobs.query).
                          `args.sql` is the text, so `tasks/harness/path.py`
                          reads it as SQL (TOOL_KINDS); op_class and targets are
                          the gateway's own classifier's (`sql.inspect_sql`,
                          dialect bigquery), the Postgres tailer's verb rule
                          behind it - exactly what the Snowflake proxy does.
  bigquery.<op>           every other route BigQuery publishes that the
                          emulator serves (ROUTES): `table_get`, `tabledata_list`,
                          `tabledata_insert_all`, `job_get`, `query_results`, ...
  bigquery.refused        a request for something this environment does not have,
                          answered 501 `notImplemented` with a message naming it
                          (REFUSALS). Not a `statement`: it never ran.
  bigquery.unknown_route  a path BigQuery does not publish: Google's 404 envelope.
  bigquery.health         `GET /health`, under the target `health:bigquery`.

No suffix is an MCP tool name (`blindness.row_tools` suffix-matches them, and a
row ending in `sql_query` would explain any change to the world's DuckDB file);
gateway/tests/test_bigquery_proxy.py holds that.

WHAT THE PROXY CORRECTS, AND WHY. Each is a measured v0.8.1 behaviour a real
client would otherwise act on wrongly; "loud unsupported is fine, silent wrong is
disqualifying" (alkera_gateway/bigquery.py, whose fences carry over):

  * COST IS NOT MODELLED. A dry run (`dryRun`) is refused with COST_REFUSAL - the
    emulator would execute the "dry" query for real - and every cost field
    (COST_FIELDS: totalBytesProcessed, totalBytesBilled, slot ms, cacheHit, ...)
    is removed from every response, so a client reads None rather than the
    emulator's inverted figures.
  * LEGACY SQL IS REFUSED. `useLegacySql: true` is run as GoogleSQL by the
    emulator; a legacy-only query (`[project:dataset.table]`) would then answer
    something else. Absent is taken as false (every client library sends it).
  * QUERY ERRORS ARE NOT RETRIED FOREVER. The emulator answers a bad query with
    reason `jobInternalError`, which google-cloud-bigquery's job retry restarts
    until its 10-minute deadline. A parse/analysis/runtime error becomes
    `invalidQuery` (400; `notFound`, 404, for a missing table), what BigQuery
    says. Any other emulator failure - a 500 `internalError` included, which the
    client also retries - is a feature it lacks: 501 `notImplemented`, its
    message kept and prefixed NOT_AVAILABLE.
  * statementType IS THE STATEMENT'S. The emulator reports every job as
    `SELECT`; it is recomputed from the job's own SQL (CREATE_TABLE_AS_SELECT,
    INSERT, DROP_TABLE, SCRIPT, ...), with `ddlTargetTable` for DDL and the
    created table as a CTAS job's `destinationTable` - which dbt-bigquery reads.
  * ROW COUNTS OF DML ARE NOT INVENTED. The emulator never reports
    numDmlAffectedRows; it stays absent (the client says None) and the row is
    flagged `rows_affected_unreported`.

REFUSED BY NAME (REFUSALS): dry runs, legacy SQL, load and extract jobs (both need
Cloud Storage or an upload; the emulator's upload route crashes), and anything the
emulator itself cannot do (the NOT_AVAILABLE prefix). The Storage Read API (gRPC)
is not proxied: the client library falls back to REST without
google-cloud-bigquery-storage, which the client lock does not install.

WRITES STAY IN THE EMULATOR. Seeding (`native/bigquery_seed.py`) copied the
world's warehouse into it at boot; whatever the agent creates or changes there
never reaches the world's files, and the grader never reads through this plug.

SEEDING HAPPENS AT IMPORT. serve.py imports a module before it binds any socket,
and native-services' healthcheck passes only once every declared port is in
LISTEN (/proc/net/tcp, never dialled). So when `$ALKERA_BIGQUERY_UPSTREAM` is set,
importing this module waits for the emulator, seeds it, prints one
`{"seed": "bigquery", ...}` line, and only then lets serve.py bind 9050: the agent
is never handed an empty warehouse. A seed that fails raises SystemExit - the
process, not just this module, stops - so `compose up --wait` fails loudly
instead of serving the other services beside a dead one. Unset (the tests, every
unplugged stack) nothing is dialled at import.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from ...bigquery import COST_REFUSAL, UNREPORTED
from ..audit import record
from ..context import audit_unrouted, mark_audited

NAME = "bigquery"
HOST = "bigquery.internal"
PORT = 9050
#: Where the emulator answers. compose: `http://bigquery:9050` (backend_net);
#: Harbor: the supervised child on loopback. Read per request, never defaulted.
UPSTREAM_ENV = "ALKERA_BIGQUERY_UPSTREAM"
#: tabledata.insertAll's HTTP request limit in BigQuery is 10 MB.
MAX_BODY_BYTES = 10 * 1024 * 1024
#: One upstream request's budget (a query runs inside jobs.insert / jobs.query).
UPSTREAM_TIMEOUT_S = 600.0

#: Response keys removed wherever they appear: cost is not modelled.
COST_FIELDS = frozenset({
    "totalBytesProcessed", "totalBytesBilled", "totalBytesProcessedAccuracy",
    "estimatedBytesProcessed", "totalSlotMs", "cacheHit", "billingTier",
    "totalPartitionsProcessed",
})
#: The prefix on every message for something the emulator cannot do.
NOT_AVAILABLE = "BigQuery feature not available in this environment"
#: Why each refused request is refused, by the feature it asked for.
REFUSALS = {
    "dry_run": COST_REFUSAL,
    "legacy_sql": (f"{NOT_AVAILABLE}: legacy SQL (useLegacySql=true); "
                   "send GoogleSQL with useLegacySql=false"),
    "load_job": (f"{NOT_AVAILABLE}: load jobs (Cloud Storage and file uploads); "
                 "use INSERT statements or tabledata.insertAll"),
    "extract_job": f"{NOT_AVAILABLE}: extract jobs (they write to Cloud Storage)",
    "alter": (f"{NOT_AVAILABLE}: ALTER statements (the engine acknowledges them and "
              "changes nothing); create the table again instead"),
}

_P = r"/bigquery/v2/projects/(?P<project>[^/]+)"
_D = _P + r"/datasets/(?P<dataset>[^/]+)"
_T = _D + r"/tables/(?P<table>[^/]+)"
_J = _P + r"/jobs/(?P<job>[^/]+)"
#: (method, path, op, op_class): every BigQuery v2 route the emulator serves.
#: A query job (`job_insert` with `configuration.query`) and `queries` are
#: recorded as `statement`, classified by their SQL instead of this column.
ROUTES = (
    ("GET", r"/bigquery/v2/projects", "project_list", "read"),
    ("GET", _P + r"/datasets", "dataset_list", "read"),
    ("POST", _P + r"/datasets", "dataset_insert", "ddl"),
    ("GET", _D, "dataset_get", "read"),
    ("PATCH", _D, "dataset_patch", "ddl"),
    ("PUT", _D, "dataset_update", "ddl"),
    ("DELETE", _D, "dataset_delete", "destructive"),
    ("GET", _D + r"/tables", "table_list", "read"),
    ("POST", _D + r"/tables", "table_insert", "ddl"),
    ("GET", _T, "table_get", "read"),
    ("PATCH", _T, "table_patch", "ddl"),
    ("PUT", _T, "table_update", "ddl"),
    ("DELETE", _T, "table_delete", "destructive"),
    ("GET", _T + r"/data", "tabledata_list", "read"),
    ("POST", _T + r"/insertAll", "tabledata_insert_all", "write"),
    ("POST", _P + r"/jobs", "job_insert", "write"),
    ("GET", _P + r"/jobs", "job_list", "read"),
    ("GET", _J, "job_get", "read"),
    ("POST", _J + r"/cancel", "job_cancel", "write"),
    ("DELETE", _J + r"/delete", "job_delete", "write"),
    ("POST", _P + r"/queries", "statement", "read"),
    ("GET", _P + r"/queries/(?P<job>[^/]+)", "query_results", "read"),
    ("POST", r"/upload/bigquery/v2/projects/(?P<project>[^/]+)/jobs", "job_upload", "write"),
    ("GET", r"/discovery/v1/apis/bigquery/v2/rest", "discovery", "read"),
)
_COMPILED = tuple((method, re.compile(pattern + r"/?"), op, op_class)
                  for method, pattern, op, op_class in ROUTES)
#: Google's error `status` string for each HTTP code this module answers with.
STATUS_NAMES = {400: "INVALID_ARGUMENT", 404: "NOT_FOUND", 413: "INVALID_ARGUMENT",
                501: "UNIMPLEMENTED", 503: "UNAVAILABLE"}
#: The request headers forwarded upstream; everything else (Authorization
#: included - the emulator checks no credential) stays here.
FORWARD_HEADERS = ("accept", "content-type", "content-encoding", "host", "user-agent")
#: The emulator's prefixes for an error in the query itself.
QUERY_ERRORS = ("failed to parse", "failed to analyze", "failed to scan")
#: The flag on a statement whose `CREATE OR REPLACE` target was dropped before
#: the statement ran (`prepare`).
REPLACED = "or_replace_dropped_first"


def route(method: str, path: str) -> tuple[str, str, dict[str, str]] | None:
    """(op, op_class, path parameters) for a published route, else None."""
    for want, pattern, op, op_class in _COMPILED:
        match = pattern.fullmatch(path)
        if match and want == method:
            return op, op_class, match.groupdict()
    return None


def google_error(status: int, reason: str, message: str) -> dict:
    """Google's JSON error envelope, as BigQuery answers with it."""
    return {"error": {"code": status, "message": message, "status": STATUS_NAMES.get(
        status, "UNKNOWN"), "errors": [{"message": message, "domain": "global",
                                        "reason": reason}]}}


def strip_cost(value: Any) -> Any:
    """`value` with every COST_FIELDS key removed, at any depth."""
    if isinstance(value, dict):
        return {k: strip_cost(v) for k, v in value.items() if k not in COST_FIELDS}
    if isinstance(value, list):
        return [strip_cost(v) for v in value]
    return value


def query_error(message: str) -> tuple[int, str, str]:
    """(HTTP status, reason, message) for one emulator `jobInternalError`.

    A missing object is `notFound`, an existing one `duplicate`, an error in
    the query itself `invalidQuery`; anything else is the engine failing at
    something BigQuery does, which is named as not available here. Every one of
    them is a reason the client library does not retry."""
    lowered = message.lower()
    if "not found" in lowered:
        return 404, "notFound", message
    if lowered.startswith("duplicate") or "already exists" in lowered:
        return 400, "duplicate", message
    if any(lowered.startswith(prefix) for prefix in QUERY_ERRORS):
        return 400, "invalidQuery", message
    return 501, "notImplemented", f"{NOT_AVAILABLE}: {message}"


def vendor_error(status: int, body: Any, text: str = "") -> tuple[int, dict]:
    """An upstream error response, with what the client library would retry
    forever turned into what BigQuery says (see the module docstring)."""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        error = {"message": text[:300] or f"HTTP {status}", "errors": [{}]}
        body = {"error": error}
    errors = error.get("errors") or [{}]
    first = errors[0] if isinstance(errors[0], dict) else {}
    message = str(error.get("message") or first.get("message") or "")
    if first.get("reason") == "jobInternalError":
        status, reason, message = query_error(message)
        return status, google_error(status, reason, message)
    if status >= 500:
        return 501, google_error(501, "notImplemented", f"{NOT_AVAILABLE}: {message}")
    error.setdefault("status", STATUS_NAMES.get(status, "UNKNOWN"))
    return status, body


def _table_ref(table, project: str, dataset: str | None) -> dict | None:
    from sqlglot import exp

    if not isinstance(table, exp.Table) or not table.name:
        return None
    ds = table.db or dataset
    if not ds:
        return None
    return {"projectId": table.catalog or project, "datasetId": ds, "tableId": table.name}


def statement_type(sql: str, project: str, dataset: str | None = None
                   ) -> tuple[str | None, dict | None]:
    """BigQuery's statementType for `sql`, and its DDL target table (or None).

    None when the SQL does not parse or is a kind this does not name: the
    emulator's value then stands, which is the one it would have had anyway."""
    import sqlglot
    from sqlglot import exp

    try:
        trees = [t for t in sqlglot.parse(sql, read="bigquery") if t is not None]
    except sqlglot.errors.SqlglotError:
        return None, None
    if len(trees) > 1:
        return "SCRIPT", None
    if not trees:
        return None, None
    tree = trees[0]
    if isinstance(tree, exp.Query):
        return "SELECT", None
    for kind, name in ((exp.Insert, "INSERT"), (exp.Update, "UPDATE"),
                       (exp.Delete, "DELETE"), (exp.Merge, "MERGE"),
                       (exp.TruncateTable, "TRUNCATE_TABLE")):
        if isinstance(tree, kind):
            return name, None
    target = _table_ref(tree.find(exp.Table), project, dataset)
    kind = str(tree.args.get("kind") or "").upper()
    if isinstance(tree, exp.Create):
        if kind == "TABLE":
            return ("CREATE_TABLE_AS_SELECT" if isinstance(tree.expression, exp.Query)
                    else "CREATE_TABLE"), target
        if kind == "VIEW":
            materialized = any(isinstance(p, exp.MaterializedProperty)
                               for p in tree.find_all(exp.MaterializedProperty))
            return ("CREATE_MATERIALIZED_VIEW" if materialized else "CREATE_VIEW"), target
        if kind == "SCHEMA":
            return "CREATE_SCHEMA", None
        if kind in ("FUNCTION", "PROCEDURE"):
            return f"CREATE_{kind}", None
        return None, None
    if isinstance(tree, exp.Drop):
        if kind in ("TABLE", "VIEW"):
            return f"DROP_{kind}", target
        if kind in ("SCHEMA", "FUNCTION", "PROCEDURE"):
            return f"DROP_{kind}", None
        return None, None
    if isinstance(tree, exp.Alter):
        if kind in ("TABLE", "VIEW"):
            return f"ALTER_{kind}", target
        if kind == "SCHEMA":
            return "ALTER_SCHEMA", None
    return None, None


def correct_job(job: Any, project: str) -> Any:
    """One job resource with its statementType (and DDL target) recomputed from
    its own SQL and every `jobInternalError` reason translated (`query_error`)."""
    if not isinstance(job, dict):
        return job
    status = job.get("status") if isinstance(job.get("status"), dict) else {}
    for entry in [status.get("errorResult"), *(status.get("errors") or [])]:
        if isinstance(entry, dict) and entry.get("reason") == "jobInternalError":
            _, entry["reason"], entry["message"] = query_error(str(entry.get("message", "")))
    query = (job.get("configuration") or {}).get("query")
    if not isinstance(query, dict) or not query.get("query"):
        # A job listed without its configuration: the emulator's `SELECT` is
        # not known to be true, so the client is told nothing rather than it.
        stats = (job.get("statistics") or {}).get("query")
        if isinstance(stats, dict):
            stats.pop("statementType", None)
        return job
    default = query.get("defaultDataset") or {}
    kind, target = statement_type(query["query"], default.get("projectId") or project,
                                  default.get("datasetId"))
    if kind is None:
        return job
    stats = job.setdefault("statistics", {}).setdefault("query", {})
    stats["statementType"] = kind
    if target is not None:
        stats["ddlTargetTable"] = target
        if kind == "CREATE_TABLE_AS_SELECT" and not status.get("errorResult"):
            query["destinationTable"] = target
    return job


def correct(op: str, body: Any, project: str) -> Any:
    """A successful upstream body for route `op`, as BigQuery would have
    answered: no cost field, and every job resource in it `correct_job`ed."""
    body = strip_cost(body)
    if op in ("job_insert", "job_get"):
        correct_job(body, project)
    elif op == "job_cancel" and isinstance(body, dict):
        correct_job(body.get("job"), project)
    elif op == "job_list" and isinstance(body, dict):
        for job in body.get("jobs") or []:
            correct_job(job, project)
    return body


def classify(sql: str):
    """op_class / targets for one statement: the gateway's own classifier, the
    Postgres tailer's verb rule behind it (an unparsed statement is still one
    row, flagged, never dropped) - gateway/docker/snowflake_start.observation."""
    from ...records import Observation, ToolError
    from ...sql import inspect_sql
    from ..tailers.postgres import fallback_observation

    found = Observation()
    try:
        inspect_sql(sql, found, read_only=False, dialect="bigquery")
    except ToolError:
        found = fallback_observation(sql, dialect="bigquery")
    return found


def targets_for(op: str, params: dict[str, str]) -> list[str]:
    project = params.get("project")
    if params.get("table"):
        return [f"{project}.{params['dataset']}.{params['table']}"]
    if params.get("dataset"):
        return [f"{project}.{params['dataset']}"]
    if params.get("job"):
        return [f"job:{project}.{params['job']}"]
    if project:
        return [project]
    return []


# ----------------------------------------------------------------- the app
async def _body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise HTTPException(413, "Request too large")
        body.extend(chunk)
    return bytes(body)


Body = Annotated[bytes, Depends(_body)]


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw) if raw else None
    except (ValueError, UnicodeError):
        return None


def _respond(status: int, body: Any, raw: bytes, media: str) -> Response:
    """The JSON body as corrected, or the upstream's bytes when it sent none."""
    if body is None:
        return Response(raw, status_code=status, media_type=media)
    return JSONResponse(body, status_code=status)


class Plan:
    """What one request is, decided before anything is forwarded: its tool, its
    classification, the arguments its row carries, or why it is refused."""

    def __init__(self, op: str, op_class: str, params: dict[str, str], payload: Any):
        self.route = op
        self.tool = f"{NAME}.{op}"
        self.op_class = op_class
        self.targets = targets_for(op, params)
        self.read_targets: list[str] | None = None
        self.flags: list[str] = []
        self.args: dict = {}
        self.refused: str | None = None
        self.trees: list = []
        body = payload if isinstance(payload, dict) else {}
        config = body.get("configuration") if isinstance(body.get("configuration"),
                                                          dict) else {}
        if op == "statement":
            self._statement(body, body)
        elif op == "job_insert" and isinstance(config.get("query"), dict):
            self._statement(config, config["query"])
        elif op == "job_upload" or (op == "job_insert" and "load" in config):
            self.refused = "load_job"
        elif op == "job_insert" and "extract" in config:
            self.refused = "extract_job"
        elif op == "job_insert" and isinstance(config.get("copy"), dict):
            dest = config["copy"].get("destinationTable") or {}
            if dest.get("tableId"):
                self.targets = [f"{dest.get('projectId') or params['project']}."
                                f"{dest.get('datasetId')}.{dest['tableId']}"]
        elif op == "tabledata_insert_all":
            self.args["rows"] = len(body.get("rows") or [])
        if self.refused:
            self.tool = f"{NAME}.refused"
            self.args["feature"] = self.refused

    @property
    def statement(self) -> bool:
        return self.tool == f"{NAME}.statement"

    def _statement(self, holder: dict, query: dict) -> None:
        """A query: jobs.query's body is both `holder` and `query`; jobs.insert
        holds `dryRun` on the configuration and the rest under `query`."""
        self.tool = f"{NAME}.statement"
        self.args["sql"] = sql = str(query.get("query") or "")
        default = query.get("defaultDataset")
        if isinstance(default, dict) and default.get("datasetId"):
            self.args["default_dataset"] = default["datasetId"]
        self.trees = statements(sql)
        if holder.get("dryRun") or query.get("dryRun"):
            self.refused = "dry_run"
        elif query.get("useLegacySql") is True:
            self.refused = "legacy_sql"
        elif any(_is_alter(tree) for tree in self.trees):
            self.refused = "alter"
        else:
            found = classify(sql)
            self.op_class, self.targets = found.op_class, list(found.targets)
            self.read_targets, self.flags = list(found.read_targets), list(found.flags)


def statements(sql: str) -> list:
    """`sql` parsed as GoogleSQL, one tree per statement; [] when it does not
    parse (the engine then says why, in its own words)."""
    import sqlglot

    try:
        return [t for t in sqlglot.parse(sql, read="bigquery") if t is not None]
    except sqlglot.errors.SqlglotError:
        return []


def _is_alter(tree) -> bool:
    from sqlglot import exp

    return isinstance(tree, exp.Alter)


def prepare(http, plan: Plan, project: str) -> tuple[int, str, str] | None:
    """Give a statement's DDL the effect BigQuery gives it, where the engine
    alone would not, before the statement is forwarded. None, or (status,
    reason, message) to answer with instead. Creates no job (REST, not SQL).

      CREATE SCHEMA       the engine runs it and creates no dataset, so the
                          next CREATE TABLE in it fails: the dataset is created
                          here (datasets.insert); an existing one without IF NOT
                          EXISTS is BigQuery's `duplicate`.
      CREATE OR REPLACE   over an existing table or view the engine replaces
                          the data, then fails `duplicate` and leaves the
                          catalogue stale (a replaced view stops answering): the
                          old one is deleted first (tables.delete) and the row
                          is flagged REPLACED. If the new query then fails, the
                          old table is gone - BigQuery would have kept it.
    """
    from sqlglot import exp

    default = plan.args.get("default_dataset")
    for tree in plan.trees:
        if not isinstance(tree, exp.Create):
            continue
        kind = str(tree.args.get("kind") or "").upper()
        table = tree.find(exp.Table)
        if kind == "SCHEMA" and isinstance(table, exp.Table):
            # sqlglot spells a schema name `db` (its project `catalog`), not `this`
            owner, dataset = ((table.db or project, table.name) if table.name
                              else (table.catalog or project, table.db))
            if not dataset:
                continue
            path = f"/bigquery/v2/projects/{owner}/datasets"
            if http.get(f"{path}/{dataset}").status_code == 200:
                if not tree.args.get("exists"):
                    return 400, "duplicate", f"Already Exists: Dataset {owner}:{dataset}"
                continue
            made = http.post(path, json={"datasetReference": {"projectId": owner,
                                                              "datasetId": dataset}})
            if made.status_code >= 300:
                status, body = vendor_error(made.status_code, _json(made.content), made.text)
                return status, body["error"]["errors"][0]["reason"], body["error"]["message"]
        elif kind in ("TABLE", "VIEW") and tree.args.get("replace"):
            ref = _table_ref(table, project, default)
            if ref is None:
                continue
            path = (f"/bigquery/v2/projects/{ref['projectId']}/datasets/{ref['datasetId']}"
                    f"/tables/{ref['tableId']}")
            if http.get(path).status_code == 200:
                http.delete(path)
                plan.flags.append(REPLACED)
    return None


def anonymous_datasets(http, project: str) -> set[str]:
    """The datasets the engine made to hold query results, one per job and named
    after it. BigQuery hides its anonymous datasets from datasets.list unless
    `all=true`; so does this proxy, by the engine's own job list."""
    listed = http.get(f"/bigquery/v2/projects/{project}/jobs")
    if listed.status_code != 200:
        return set()
    return {str((job.get("jobReference") or {}).get("jobId"))
            for job in (_json(listed.content) or {}).get("jobs") or []}


def outcome(plan: Plan, status: int, body: Any) -> tuple[dict | None, dict, int | None]:
    """(error, result, rows_affected) for the row, from what the client gets."""
    if status >= 400:
        detail = body.get("error", {}) if isinstance(body, dict) else {}
        reason = ((detail.get("errors") or [{}])[0] or {}).get("reason")
        return ({"code": str(reason or f"http_{status}"),
                 "message": str(detail.get("message") or f"HTTP {status}")}, {}, None)
    if not isinstance(body, dict):
        return None, {}, None
    job_status = body.get("status") if isinstance(body.get("status"), dict) else {}
    stats = (body.get("statistics") or {}).get("query") or {}
    result = {k: v for k, v in {
        "job_id": (body.get("jobReference") or {}).get("jobId"),
        "statement_type": stats.get("statementType"),
        "state": job_status.get("state"),
        "total_rows": body.get("totalRows"),
        "rows": len(body["rows"]) if isinstance(body.get("rows"), list) else None,
        "insert_errors": len(body.get("insertErrors") or [])
        if plan.route == "tabledata_insert_all" else None,
    }.items() if v is not None}
    failed = job_status.get("errorResult")
    error = ({"code": str(failed.get("reason") or "job_error"),
              "message": str(failed.get("message") or "")}
             if plan.statement and isinstance(failed, dict) else None)
    rows_affected = None
    if plan.route == "tabledata_insert_all":
        rows_affected = plan.args["rows"] - result["insert_errors"]
    elif plan.statement and not error and plan.op_class in ("write", "destructive"):
        dml = body.get("numDmlAffectedRows") or stats.get("numDmlAffectedRows")
        if dml is not None:
            rows_affected = int(dml)
        else:
            plan.flags.append(UNREPORTED)
    return error, result, rows_affected


def build_app(upstream: str | None = None, seed: dict | None = None) -> FastAPI:
    """The proxy, forwarding to `upstream` (else `$ALKERA_BIGQUERY_UPSTREAM`,
    read per request). `seed` is the boot seed's record, for `/health`."""
    import httpx

    app = FastAPI(title="Alkera BigQuery (emulator, audited)", version="0.1.0",
                  docs_url=None, redoc_url=None, openapi_url=None)
    # A request refused before any handler ran (413 from the body limit) still
    # gets its row: `bigquery.request` (native/context.py).
    audit_unrouted(app, NAME)
    app.state.upstream = upstream
    app.state.seed = seed
    clients: dict[str, httpx.Client] = {}

    def client() -> httpx.Client | None:
        base = (app.state.upstream or os.environ.get(UPSTREAM_ENV) or "").rstrip("/")
        if base and base not in clients:
            clients[base] = httpx.Client(base_url=base, timeout=UPSTREAM_TIMEOUT_S)
        return clients.get(base)

    def write(request: Request, tool: str, started: float, *, op_class: str,
              targets: list[str], status: int, error: dict | None, result: dict,
              read_targets: list[str] | None = None, flags: list[str] | None = None,
              rows_affected: int | None = None, args: dict | None = None) -> None:
        """The request's one 9.2.2 row. Called before the response is built."""
        mark_audited(request)
        query = {"query": str(request.url.query)} if request.url.query else {}
        record(tool=tool, args={"method": request.method, "path": request.url.path,
                                **query, **(args or {})},
               op_class=op_class, targets=targets, read_targets=read_targets,
               exit="error" if error else "ok", error=error,
               latency_ms=(time.monotonic() - started) * 1000,
               result={"status": status, **result}, transport="native",
               rows_affected=rows_affected, flags=flags)

    def refuse(request: Request, plan: Plan, started: float, status: int, reason: str,
               code: str, message: str) -> Response:
        """Answer without the upstream's answer, the row first. A request refused
        by name (`plan.refused`) touched nothing: `read`, no targets. Any other
        (the engine unreachable, a prepare step's verdict) keeps the request's
        own classification - it was an attempt at what it names."""
        quiet = plan.refused is not None
        write(request, plan.tool, started, op_class="read" if quiet else plan.op_class,
              targets=[] if quiet else plan.targets,
              read_targets=None if quiet else plan.read_targets,
              status=status, error={"code": code, "message": message}, result={},
              flags=plan.flags, args=plan.args)
        return JSONResponse(google_error(status, reason, message), status_code=status)

    @app.get("/health")
    def health(request: Request) -> Response:
        """The tables the boot seed loaded. Not a BigQuery route; every native
        service answers it (serve.HEALTH_PATH)."""
        started = time.monotonic()
        items = len((app.state.seed or {}).get("tables") or {})
        write(request, f"{NAME}.health", started, op_class="read",
              targets=[f"health:{NAME}"], status=200 if items else 503,
              error=None if items else {"code": "not_seeded",
                                        "message": "no table was seeded"},
              result={"items": items})
        return JSONResponse({"ok": items > 0, "service": NAME, "items": items},
                            status_code=200 if items else 503)

    @app.api_route("/{path:path}",
                   methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    def proxy(path: str, request: Request, raw: Body) -> Response:
        started = time.monotonic()
        found = route(request.method, request.url.path)
        if found is None:
            message = (f"Not found: {request.method} {request.url.path} is not a "
                       f"BigQuery API v2 route this environment serves")
            write(request, f"{NAME}.unknown_route", started, op_class="read", targets=[],
                  status=404, error={"code": "unknown_route", "message": message},
                  result={})
            return JSONResponse(google_error(404, "notFound", message), status_code=404)
        op, op_class, params = found
        plan = Plan(op, op_class, params, _json(raw))
        if plan.refused:
            return refuse(request, plan, started, 501, "notImplemented", "not_available",
                          REFUSALS[plan.refused])
        http = client()
        if http is None:
            return refuse(request, plan, started, 503, "backendUnavailable",
                          "upstream_unconfigured",
                          f"the BigQuery engine is not configured: {UPSTREAM_ENV} is "
                          f"unset in native-services")
        headers = {k: v for k, v in request.headers.items() if k in FORWARD_HEADERS}
        project = params.get("project", "")
        try:
            early = prepare(http, plan, project) if plan.statement else None
            if early is not None:
                status, reason, message = early
                return refuse(request, plan, started, status, reason, reason, message)
            answer = http.request(request.method, request.url.path,
                                  params=list(request.query_params.multi_items()),
                                  content=raw, headers=headers)
            hidden = (anonymous_datasets(http, project) if op == "dataset_list"
                      and request.query_params.get("all") != "true"
                      and answer.status_code == 200 else set())
        except httpx.HTTPError as exc:
            return refuse(request, plan, started, 503, "backendUnavailable",
                          "upstream_unavailable",
                          f"the BigQuery engine behind {HOST} is not answering "
                          f"({type(exc).__name__}: {exc})")
        status, body = answer.status_code, _json(answer.content)
        if hidden and isinstance(body, dict):
            body["datasets"] = [d for d in body.get("datasets") or [] if str(
                (d.get("datasetReference") or {}).get("datasetId")) not in hidden]
        if status >= 400:
            status, body = vendor_error(status, body, answer.text)
        elif body is not None:
            body = correct(op, body, params.get("project", ""))
        error, result, rows_affected = outcome(plan, status, body)
        write(request, plan.tool, started, op_class=plan.op_class, targets=plan.targets,
              read_targets=plan.read_targets, status=status, error=error, result=result,
              flags=plan.flags, rows_affected=rows_affected, args=plan.args)
        media = answer.headers.get("content-type", "application/json").split(";")[0]
        return _respond(status, body, answer.content, media)

    return app


def boot() -> dict | None:
    """Seed the emulator when this process is native-services with the plug on
    (`$ALKERA_BIGQUERY_UPSTREAM` set); None otherwise. See the docstring."""
    upstream = os.environ.get(UPSTREAM_ENV)
    if not upstream:
        return None
    from ...backends import BACKENDS
    from .. import bigquery_seed

    project = BACKENDS[NAME].env["BIGQUERY_PROJECT"]
    world = Path(os.environ.get("ALKERA_WORLD_ROOT") or "/world")
    files = sorted((world / "var" / "warehouse").glob("*.duckdb"))
    started = time.monotonic()
    try:
        if not files:
            raise bigquery_seed.SeedError(f"no warehouse file under {world}/var/warehouse")
        waited = bigquery_seed.wait_for(upstream, project)
        tables = bigquery_seed.seed(upstream, project, files)
    except Exception as exc:  # noqa: BLE001 - every failure stops the process, named
        raise SystemExit(f"bigquery: the emulator at {upstream} could not be seeded from "
                         f"{world}: {type(exc).__name__}: {exc}") from exc
    seeded = {"seed": NAME, "project": project, "upstream": upstream,
              "files": [f.name for f in files], "tables": tables,
              "rows": sum(tables.values()), "wait_s": waited,
              "seconds": round(time.monotonic() - started, 3)}
    print(json.dumps(seeded, sort_keys=True), file=sys.stdout, flush=True)
    return seeded


SEED = boot()
app = build_app(seed=SEED)
