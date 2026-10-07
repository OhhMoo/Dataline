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
                          A query that is REFUSED (REFUSALS) is still this row,
                          classified by its SQL so the path rules read the
                          attempt, with `exit: error` and the flag
                          `refused:<feature>`; it never reached the engine.
  bigquery.<op>           every other route BigQuery publishes that the
                          emulator serves (ROUTES): `table_get`, `tabledata_list`,
                          `tabledata_insert_all`, `job_get`, `query_results`, ...
                          refused the same way (`refused:load_job`, ...).
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
    until its 10-minute deadline. A missing table or dataset becomes `notFound`
    (404), an existing one `duplicate` (409; 400 on jobs.insert, where the client
    reads 409 as "re-fetch this job"), any other error in the query `invalidQuery`
    (400) - what BigQuery says. Any other emulator failure - a 500
    `internalError` included, which the client also retries - is a feature it
    lacks: 501 `notImplemented`, its message kept and prefixed NOT_AVAILABLE.
  * statementType IS THE STATEMENT'S. The emulator reports every job as
    `SELECT`; it is recomputed from the job's own SQL (CREATE_TABLE_AS_SELECT,
    INSERT, DROP_TABLE, SCRIPT, ...), with `ddlTargetTable` for DDL and the
    created table as a CTAS job's `destinationTable` - which dbt-bigquery reads.
  * ROW COUNTS OF DML ARE NOT INVENTED. The emulator never reports
    numDmlAffectedRows; it stays absent (the client says None) and the row is
    flagged `rows_affected_unreported`.
  * DDL DOES WHAT IT SAYS (`preflight`). The emulator runs `CREATE SCHEMA` and
    creates no dataset, and `CREATE OR REPLACE` over an existing table or view
    replaces the data, fails `duplicate` and leaves its catalogue stale: the
    dataset is created, or the old table dropped, through REST before the
    statement is forwarded - and a REPLACE that then fails says the old table
    is gone (`replaced_note`), in its own answer and every later read of its
    job.
  * A QUERY JOB'S DESTINATION IS WRITTEN AS ASKED (`preflight`). The emulator
    appends whatever the writeDisposition; WRITE_EMPTY (the default) answers
    `duplicate` on a table with rows, CREATE_NEVER `notFound` on a missing one,
    and the row is a write naming the destination.
  * PAGES ARE PAGES (`page`). The emulator's tabledata.list answers every row
    whatever maxResults / startIndex / pageToken say, and the client keeps every
    row of a page - `list_rows(max_results=5)` returned the whole table. The
    proxy cuts the page and issues the token.
  * QUERY RESULTS ARE NOT DATASETS (`anonymous_datasets`). The emulator files
    each job's result as a dataset named after the job; BigQuery hides its
    anonymous datasets from datasets.list unless `all=true`, and so does this.

REFUSED BY NAME (REFUSALS, 501 `notImplemented`), each a measured case where the
engine answers success and does something else: dry runs (executed for real),
legacy SQL (run as GoogleSQL), transactions and BEGIN...END blocks (an ERROR()
keeps the INSERT before it; no ROLLBACK), ALTER (acknowledged, nothing changes),
EXECUTE IMMEDIATE (acknowledged, nothing runs), ASSERT (always passes), a query
job's WRITE_TRUNCATE (appends), a schema change through tables.patch / update
(recorded, the data keeps the old one), CREATE OR REPLACE of a table its own
query reads (the source would be dropped first), CREATE / DROP SCHEMA or CREATE OR
REPLACE inside a multi-statement script (`preflight` acts statement by
statement), load and extract jobs (both need Cloud Storage or an upload; the
emulator's upload route crashes) - and anything the emulator itself fails at (the
NOT_AVAILABLE prefix). Fenced by each statement's LEADING keyword from sqlglot's
tokenizer (FENCED_VERBS), so a keyword in a string never counts. NOT fenced, and
written down: INT64 arithmetic wraps on overflow where BigQuery raises. The
Storage Read API (gRPC) is not proxied: the client library falls back to REST
without google-cloud-bigquery-storage, which the client lock does not install.

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
    "transaction": (f"{NOT_AVAILABLE}: transactions and BEGIN...END blocks (BEGIN "
                    "[TRANSACTION], COMMIT, ROLLBACK): the engine keeps what ran before "
                    "an error and cannot roll back; send the statements without them"),
    "execute_immediate": (f"{NOT_AVAILABLE}: EXECUTE IMMEDIATE (the engine acknowledges "
                          "it and runs nothing); send the statement itself"),
    "assert": (f"{NOT_AVAILABLE}: ASSERT (the engine passes every assertion); check the "
               "condition with a SELECT"),
    "ddl_script": (f"{NOT_AVAILABLE}: CREATE SCHEMA, DROP SCHEMA or CREATE OR REPLACE "
                   "inside a multi-statement script; send each of those as a statement "
                   "of its own"),
    "replace_reads_itself": (f"{NOT_AVAILABLE}: CREATE OR REPLACE of a table its own "
                             "query reads (the engine cannot replace a table in place, so "
                             "the source would be gone before it is read); build into a "
                             "new table first, then replace from that"),
    "write_truncate": (f"{NOT_AVAILABLE}: writeDisposition WRITE_TRUNCATE on a query job "
                       "(the engine appends instead); use CREATE OR REPLACE TABLE ... AS "
                       "SELECT"),
    "table_schema_change": (f"{NOT_AVAILABLE}: changing a table's schema through "
                            "tables.patch / tables.update (the engine records the new "
                            "schema and its data keeps the old one); create the table "
                            "again"),
}
#: Statements refused by their LEADING keyword, read from sqlglot's tokenizer
#: so a keyword inside a string or comment never counts. By keyword rather
#: than parse tree because sqlglot parses `ASSERT ...` as an alias and falls
#: back to an opaque Command for `ALTER TABLE ... ALTER COLUMN` and `EXECUTE`.
FENCED_VERBS = {"ALTER": "alter", "EXECUTE": "execute_immediate", "ASSERT": "assert",
                "BEGIN": "transaction", "COMMIT": "transaction",
                "ROLLBACK": "transaction"}
#: The flag a refused request's row carries, with the REFUSALS key after it.
REFUSED_FLAG = "refused:"

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
#: the statement ran (`preflight`).
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


#: A missing TABLE or DATASET, in the emulator's words ("Table not found: x",
#: "dataset s is not found") - the one kind of query error BigQuery answers 404.
MISSING = re.compile(r"\b(table|dataset)\b[^;]*?\bnot found\b", re.IGNORECASE)
#: An object that exists already, in the emulator's words ("duplicate: table t:
#: table is already created", "dataset d is already created") - `duplicate`.
DUPLICATE = re.compile(r"^duplicate\b|\balready (created|exists)\b", re.IGNORECASE)


def duplicate_status(op: str) -> int:
    """409, BigQuery's status for `duplicate` - except on jobs.insert. There the
    client library reads a 409 as "this job id exists already", fetches the
    job instead and retries that fetch's 404 for 200 s (google.cloud.bigquery
    _job_helpers.query_jobs_insert); no such job exists here, so it is 400."""
    return 400 if op in ("job_insert", "job_upload") else 409


def query_error(message: str, op: str = "statement") -> tuple[int, str, str]:
    """(HTTP status, reason, message) for one emulator `jobInternalError`.

    A missing table or dataset is `notFound` (404); an existing object is
    `duplicate` (`duplicate_status`); any other error in the query itself - a
    syntax error, an unknown column or function, a runtime error - is
    `invalidQuery` (400); anything else is the engine failing at something
    BigQuery does, named as not available here (501). None of them is a reason
    the client library retries."""
    lowered = message.lower()
    if MISSING.search(lowered):
        return 404, "notFound", message
    if DUPLICATE.search(lowered):
        return duplicate_status(op), "duplicate", message
    if any(lowered.startswith(prefix) for prefix in QUERY_ERRORS):
        return 400, "invalidQuery", message
    return 501, "notImplemented", f"{NOT_AVAILABLE}: {message}"


def vendor_error(status: int, body: Any, text: str = "", op: str = "statement"
                 ) -> tuple[int, dict]:
    """An upstream error response, with what the client library would retry
    forever turned into what BigQuery says (see the module docstring)."""
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        error = {"message": text[:300] or f"HTTP {status}", "errors": [{}]}
        body = {"error": error}
    errors = error.get("errors") or [{}]
    first = errors[0] if isinstance(errors[0], dict) else {}
    message = str(error.get("message") or first.get("message") or "")
    # The engine answers an insert of an existing dataset or table with a 500
    # `internalError`; BigQuery says `duplicate`, which client code relies on
    # (`create_dataset(exists_ok=True)` catches the Conflict).
    if first.get("reason") == "jobInternalError" or (status >= 500
                                                     and DUPLICATE.search(message)):
        status, reason, message = query_error(message, op)
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
    classification, the arguments its row carries, or why it is refused.

    A refused request is still recorded as what it is - a query as
    `bigquery.statement`, classified by its SQL, so the path rules read the
    attempt; anything else as `bigquery.<op>` - with `exit: error` and the flag
    `refused:<feature>` (REFUSALS)."""

    def __init__(self, op: str, op_class: str, params: dict[str, str], payload: Any):
        self.route = op
        self.params = params
        self.project = params.get("project", "")
        self.tool = f"{NAME}.{op}"
        self.op_class = op_class
        self.targets = targets_for(op, params)
        self.read_targets: list[str] | None = None
        self.flags: list[str] = []
        self.args: dict = {}
        self.refused: str | None = None
        self.trees: list = []
        self.sql = ""
        self.default_dataset: str | None = None
        #: a query job's destination table (configuration.query.destinationTable)
        self.destination: dict | None = None
        self.write_disposition = ""
        self.create_disposition = ""
        #: tables a CREATE OR REPLACE dropped before the statement ran (`preflight`)
        self.replaced: list[str] = []
        self.body = payload if isinstance(payload, dict) else {}
        config = self.body.get("configuration")
        config = config if isinstance(config, dict) else {}
        if op == "statement":
            self._statement(self.body, self.body)
        elif op == "job_insert" and isinstance(config.get("query"), dict):
            self._statement(config, config["query"])
        elif op == "job_upload" or (op == "job_insert" and "load" in config):
            self.refuse("load_job")
            dest = (config.get("load") or {}).get("destinationTable") or {}
            self.targets = [_ref_name(dest, self.project)] if dest.get("tableId") \
                else self.targets
        elif op == "job_insert" and "extract" in config:
            self.refuse("extract_job")
        elif op == "job_insert" and isinstance(config.get("copy"), dict):
            dest = config["copy"].get("destinationTable") or {}
            if dest.get("tableId"):
                self.targets = [_ref_name(dest, self.project)]
        elif op == "tabledata_insert_all":
            self.args["rows"] = len(self.body.get("rows") or [])

    def refuse(self, feature: str) -> None:
        self.refused = feature
        self.flags.append(f"{REFUSED_FLAG}{feature}")
        self.args["feature"] = feature

    @property
    def statement(self) -> bool:
        return self.tool == f"{NAME}.statement"

    def _statement(self, holder: dict, query: dict) -> None:
        """A query: jobs.query's body is both `holder` and `query`; jobs.insert
        holds `dryRun` on the configuration and the rest under `query`."""
        self.tool = f"{NAME}.statement"
        self.sql = self.args["sql"] = str(query.get("query") or "")
        default = query.get("defaultDataset")
        if isinstance(default, dict) and default.get("datasetId"):
            self.default_dataset = self.args["default_dataset"] = default["datasetId"]
        found = classify(self.sql)
        self.op_class, self.targets = found.op_class, list(found.targets)
        self.read_targets, self.flags = list(found.read_targets), list(found.flags)
        dest = query.get("destinationTable")
        if isinstance(dest, dict) and dest.get("tableId"):
            # The query's result lands in a table: a write, whatever it reads.
            self.destination = {"projectId": dest.get("projectId") or self.project,
                                "datasetId": dest.get("datasetId"),
                                "tableId": dest["tableId"]}
            name = _ref_name(self.destination, self.project)
            self.args["destination"] = name
            self.write_disposition = str(query.get("writeDisposition") or "WRITE_EMPTY")
            self.create_disposition = str(query.get("createDisposition")
                                          or "CREATE_IF_NEEDED")
            self.args["write_disposition"] = self.write_disposition
            if self.op_class == "read":
                self.op_class = "write"
            self.targets = [name, *(t for t in self.targets if t != name)]
        self.trees = statements(self.sql)
        if holder.get("dryRun") or query.get("dryRun"):
            self.refuse("dry_run")
        elif query.get("useLegacySql") is True:
            self.refuse("legacy_sql")
        elif (feature := fence(self.sql, self.trees, self.project,
                               self.default_dataset)) is not None:
            self.refuse(feature)
        elif self.destination is not None and self.write_disposition == "WRITE_TRUNCATE":
            self.refuse("write_truncate")


def _ref_name(ref: dict, project: str) -> str:
    return f"{ref.get('projectId') or project}.{ref.get('datasetId')}.{ref.get('tableId')}"


def statements(sql: str) -> list:
    """`sql` parsed as GoogleSQL, one tree per statement; [] when it does not
    parse (the engine then says why, in its own words)."""
    import sqlglot

    try:
        return [t for t in sqlglot.parse(sql, read="bigquery") if t is not None]
    except sqlglot.errors.SqlglotError:
        return []


def leading_verbs(sql: str) -> list[str]:
    """The first keyword of every statement in `sql`, upper-cased, from sqlglot's
    BigQuery tokenizer (strings and comments are tokens of their own, never a
    keyword). If the text does not tokenize, a split on `;` stands in."""
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.errors import SqlglotError
    from sqlglot.tokens import TokenType

    try:
        tokens = Dialect.get_or_raise("bigquery").tokenize(sql)
    except SqlglotError:
        return [part.split(None, 1)[0].upper() for part in sql.split(";") if part.split()]
    verbs, start = [], True
    for token in tokens:
        if token.token_type == TokenType.SEMICOLON:
            start = True
        elif start:
            verbs.append(token.text.upper())
            start = False
    return verbs


def fence(sql: str, trees: list, project: str, dataset: str | None) -> str | None:
    """The REFUSALS key a query is refused under, or None to forward it."""
    from sqlglot import exp

    verbs = leading_verbs(sql)
    for verb in verbs:
        if verb in FENCED_VERBS:
            return FENCED_VERBS[verb]
    if any(isinstance(t, (exp.Alter, exp.Transaction, exp.Commit, exp.Rollback))
           for t in trees):
        return "transaction" if not any(isinstance(t, exp.Alter) for t in trees) \
            else "alter"
    for tree in trees:
        if isinstance(tree, exp.Command) and str(tree.this).upper() in FENCED_VERBS:
            return FENCED_VERBS[str(tree.this).upper()]
    prepared = [t for t in trees if _prepared(t)]
    if prepared and len(verbs) > 1:
        return "ddl_script"
    for tree in prepared:
        if not (isinstance(tree, exp.Create) and tree.args.get("replace")
                and isinstance(tree.expression, exp.Query)):
            continue
        target = _table_ref(tree.this.find(exp.Table) if tree.this else None,
                            project, dataset)
        sources = {_ref_name(r, project) for r in (
            _table_ref(t, project, dataset) for t in tree.expression.find_all(exp.Table))
            if r is not None}
        if target is not None and _ref_name(target, project) in sources:
            return "replace_reads_itself"
    return None


def _prepared(tree) -> bool:
    """A statement that cannot run inside a script: CREATE SCHEMA and CREATE OR
    REPLACE TABLE / VIEW, which `preflight` acts on statement by statement
    before forwarding (in a script that would run ahead of the statements
    before them), and DROP SCHEMA, which the engine cannot run at all."""
    from sqlglot import exp

    kind = str(tree.args.get("kind") or "").upper()
    if isinstance(tree, (exp.Create, exp.Drop)) and kind == "SCHEMA":
        return True
    return isinstance(tree, exp.Create) and kind in ("TABLE", "VIEW") \
        and bool(tree.args.get("replace"))


def _table_path(ref: dict) -> str:
    return (f"/bigquery/v2/projects/{ref['projectId']}/datasets/{ref['datasetId']}"
            f"/tables/{ref['tableId']}")


def preflight(http, plan: Plan, project: str) -> tuple[int, str, str] | None:
    """What a request needs from the engine before it is forwarded: None to
    forward it, or (status, reason, message) to answer with instead. REST
    only, so it creates no job. `plan.refused` is set when the answer is a
    refusal by name.

      CREATE SCHEMA       the engine runs it and creates no dataset, so the
                          next CREATE TABLE in it fails: the dataset is created
                          here (datasets.insert); an existing one without IF NOT
                          EXISTS is BigQuery's `duplicate`.
      CREATE OR REPLACE   over an existing table or view the engine replaces
                          the data, then fails `duplicate` and leaves the
                          catalogue stale (a replaced view stops answering): the
                          old one is deleted first (tables.delete), the row is
                          flagged REPLACED, and if the statement then fails its
                          error says the old table is gone (`replaced_note`) -
                          BigQuery would have kept it. One whose query reads
                          its own target is refused before this (`fence`).
      destinationTable    the engine appends whatever the writeDisposition:
                          WRITE_TRUNCATE is refused (`Plan`); WRITE_EMPTY, the
                          default, is BigQuery's `duplicate` when the table
                          holds rows; CREATE_NEVER is `notFound` when it does
                          not exist.
      tables.patch/update a new `schema` is recorded and the data keeps the old
                          one: refused when the fields differ from the table's.
    """
    from sqlglot import exp

    if plan.route in ("table_patch", "table_update") and "schema" in plan.body:
        current = http.get(_table_path({"projectId": project,
                                         "datasetId": plan.params["dataset"],
                                         "tableId": plan.params["table"]}))
        if current.status_code == 200 and _fields(_json(current.content).get("schema")) \
                != _fields(plan.body.get("schema")):
            plan.refuse("table_schema_change")
            return 501, "notImplemented", REFUSALS["table_schema_change"]
        return None
    if not plan.statement:
        return None
    if plan.destination is not None:
        table = http.get(_table_path(plan.destination))
        name = _ref_name(plan.destination, project)
        if table.status_code != 200 and plan.create_disposition == "CREATE_NEVER":
            return 404, "notFound", f"Not found: Table {name.replace('.', ':', 1)}"
        if table.status_code == 200 and plan.write_disposition == "WRITE_EMPTY" \
                and int((_json(table.content) or {}).get("numRows") or 0) > 0:
            return (duplicate_status(plan.route), "duplicate",
                    f"Already Exists: Table {name.replace('.', ':', 1)} (writeDisposition "
                    "WRITE_EMPTY needs an empty table)")
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
                    return (duplicate_status(plan.route), "duplicate",
                            f"Already Exists: Dataset {owner}:{dataset}")
                continue
            made = http.post(path, json={"datasetReference": {"projectId": owner,
                                                              "datasetId": dataset}})
            if made.status_code >= 300:
                status, body = vendor_error(made.status_code, _json(made.content),
                                            made.text, plan.route)
                return status, body["error"]["errors"][0]["reason"], body["error"]["message"]
        elif kind in ("TABLE", "VIEW") and tree.args.get("replace"):
            ref = _table_ref(table, project, plan.default_dataset)
            if ref is None:
                continue
            if http.get(_table_path(ref)).status_code == 200:
                http.delete(_table_path(ref))
                plan.flags.append(REPLACED)
                plan.replaced.append(_ref_name(ref, project))
    return None


def _fields(schema: Any) -> list:
    """A schema's fields as (name, type, mode, sub-fields), for comparison."""
    fields = (schema or {}).get("fields") if isinstance(schema, dict) else None
    return [(str(f.get("name", "")).lower(), str(f.get("type", "")).upper(),
             str(f.get("mode") or "NULLABLE").upper(), _fields(f))
            for f in fields or [] if isinstance(f, dict)]


def replaced_note(tables: list[str]) -> str:
    """Appended to the error of a CREATE OR REPLACE that failed after `preflight`
    dropped its target: the agent must not believe the old table survived."""
    return (f" [{', '.join(tables)} was dropped before this statement ran - the engine "
            "cannot replace a table in place - and is gone]")


def mark_replaced(body: Any, note: str) -> Any:
    """`body` with `note` appended to every error message in it: a top-level
    error envelope, or a job's errorResult and errors."""
    if not isinstance(body, dict):
        return body
    error = body.get("error")
    if isinstance(error, dict):
        error["message"] = str(error.get("message", "")) + note
        for entry in error.get("errors") or []:
            if isinstance(entry, dict):
                entry["message"] = str(entry.get("message", "")) + note
    status = body.get("status") if isinstance(body.get("status"), dict) else {}
    for entry in [status.get("errorResult"), *(status.get("errors") or [])]:
        if isinstance(entry, dict):
            entry["message"] = str(entry.get("message", "")) + note
    return body


def job_id_of(plan: Plan, params: dict[str, str], body: Any) -> str | None:
    """The job a request is about: in its path, in the answer, or - for a
    jobs.insert the engine refused outright - in the request's jobReference."""
    for holder in (body, plan.body):
        ref = holder.get("jobReference") if isinstance(holder, dict) else None
        if params.get("job") or (isinstance(ref, dict) and ref.get("jobId")):
            return params.get("job") or ref["jobId"]
    return params.get("job")


def failed(status: int, body: Any) -> bool:
    return status >= 400 or (isinstance(body, dict) and isinstance(body.get("status"), dict)
                             and bool(body["status"].get("errorResult")))


#: tabledata.list paging (`page`): the engine ignores maxResults, startIndex
#: and pageToken there and answers every row, so the proxy pages.
PAGE_PARAMS = ("maxResults", "startIndex", "pageToken")
TOKEN_PREFIX = "alkera-row-"


def page(body: Any, params) -> tuple[int, Any] | None:
    """A full tabledata.list answer cut to the requested page, BigQuery's way:
    `maxResults` rows from `startIndex` (or from where `pageToken` points),
    `pageToken` while rows remain, `totalRows` always the whole table's; None
    when the request asked for nothing to cut. (400, error) for a page token
    this proxy did not issue."""
    if not isinstance(body, dict):
        return None
    rows = body.get("rows") or []
    token = params.get("pageToken")
    try:
        if token:
            if not token.startswith(TOKEN_PREFIX):
                raise ValueError(token)
            start = int(token[len(TOKEN_PREFIX):])
        else:
            start = int(params.get("startIndex") or 0)
        size = params.get("maxResults")
        end = len(rows) if size in (None, "") else start + int(size)
    except ValueError:
        return 400, google_error(400, "invalid", f"Invalid page token or index: {token!r}")
    body["totalRows"] = str(body.get("totalRows") or len(rows))
    body["rows"] = rows[start:end]
    if end < len(rows):
        body["pageToken"] = f"{TOKEN_PREFIX}{end}"
    else:
        body.pop("pageToken", None)
    return 200, body


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
    #: job id -> replaced_note, for a failed CREATE OR REPLACE that dropped its
    #: target (`preflight`): later reads of the job carry the note too.
    dropped: dict[str, str] = {}

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
        """Answer without the upstream's answer, the row first. The row keeps the
        request's own classification - it was an attempt at what it names, and
        a refused query is a `bigquery.statement` the path rules read - with
        `exit: error`, and `refused:<feature>` among its flags when it was
        refused by name."""
        write(request, plan.tool, started, op_class=plan.op_class, targets=plan.targets,
              read_targets=plan.read_targets, status=status,
              error={"code": code, "message": message}, result={}, flags=plan.flags,
              args=plan.args)
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
        query = [(k, v) for k, v in request.query_params.multi_items()
                 if op != "tabledata_list" or k not in PAGE_PARAMS]
        try:
            early = preflight(http, plan, project)
            if early is not None:
                status, reason, message = early
                code = "not_available" if plan.refused else reason
                return refuse(request, plan, started, status, reason, code, message)
            answer = http.request(request.method, request.url.path, params=query,
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
            status, body = vendor_error(status, body, answer.text, op)
        elif body is not None:
            body = correct(op, body, project)
            if op == "tabledata_list":
                status, body = page(body, request.query_params) or (status, body)
        # A CREATE OR REPLACE that failed after its target was dropped says so,
        # here and in every later read of the same job.
        job_id = job_id_of(plan, params, body)
        if plan.replaced and failed(status, body):
            note = replaced_note(plan.replaced)
            if job_id:
                dropped[job_id] = note
            body = mark_replaced(body, note)
        elif job_id in dropped and failed(status, body):
            body = mark_replaced(body, dropped[job_id])
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
