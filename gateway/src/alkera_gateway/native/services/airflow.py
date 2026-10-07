"""Airflow 3 REST API v2 over the world ledger - the Airflow plug (orchestrator slot).

WHY A FAKE AIRFLOW AND NOT AIRFLOW. The world's scheduler is a ledger
(`var/sched/state.json`, the single time authority) plus an executor, driven by
the grader's virtual clock. Real Airflow would be a second scheduler with its own
clock and its own metadata database - 2.5 GB of image, a wall clock in its
timetable (`_archive/world/world/airflow_bridge.py`), and a record of what ran
that could disagree with the ledger. So this is Airflow's REST surface over the
ledger instead (`world.airflow_view`): DAGs read from the workspace's own
`dags/*.py`, DAG runs and task instances derived from ledger runs, logs from the
engine output the executor keeps. Real headless Airflow can still replace it
later under the same plug name.

NO CLOCK. `POST .../dagRuns` and `POST .../clearTaskInstances` QUEUE runs through
`WorldScheduler.enqueue`: `vnow` does not move and nothing executes until the
grader's `advance`, which drains the queue first. Backfills are refused by name
(the clock is the world's), and so is pausing (not modelled: the world's
schedule runs regardless). There is no `airflow` CLI on the box - it needs a
metadata database this box does not have; `airflowctl` (REST-only) is, in images
built with the `airflow` client set.

SHAPES. Airflow 3.3.2's OpenAPI (apache-airflow-core 3.3.2,
`v2-rest-api-generated.yaml` and the simple auth manager's), a superset of 3.0's
- REST v2 is additive - and what airflowctl 0.1.5's generated pydantic models
validate. Errors are FastAPI's `{"detail": ...}` (`HTTPExceptionResponse`;
422 is `HTTPValidationError`). gateway/tests/testdata/airflow_v2_schemas.json is
the subset of that document every response here is checked against.

  POST /auth/token, /auth/token/cli   {username, password} (JSON, or form for
                                      /auth/token) -> 201 {access_token}: a JWT
                                      whose `sub` is the row's `actor`. The one
                                      user is the box's AIRFLOW_USER, with its
                                      AIRFLOW_PASSWORD (the backend row's env), as
                                      a simple-auth-manager deployment has its
                                      configured users and no others.
  GET  /api/v2/version, /monitor/health   no token needed, as in Airflow
  GET  /api/v2/dags, /dags/{id}, /dags/{id}/details, /dags/{id}/tasks[/{task}]
  GET  /api/v2/importErrors[/{id}]    a DAG file that cannot be read (syntax, or
                                      too deep or too big for `ast`) is that
                                      file's import error, never a 500
  GET  /api/v2/dagSources/{id}        JSON, or text/plain with that Accept
  GET  /api/v2/dags/{id}/dagRuns      `~` for every DAG; state, run_type, the
                                      date filters, order_by, limit/offset
  POST /api/v2/dags/{id}/dagRuns      queue every job-task (TriggerDAGRunPostBody)
  GET  /api/v2/dags/{id}/dagRuns/{run}
  GET  .../dagRuns/{run}/taskInstances[/{task}]   `~` for dag and run
  GET  .../taskInstances/{task}/logs/{try}        StructuredLogMessage content,
                                                  or NDJSON with that Accept
  POST /api/v2/dags/{id}/clearTaskInstances       one DAG run (dag_run_id);
                                                  dry_run defaults to true

  refused, 400, by name    /api/v2/backfills* ("the clock is the world's"),
                           PATCH /dags/{id} {is_paused: true} ("pause is not
                           modelled"; {is_paused: false} is the state it is in
                           and answers 200), a clear across runs, a trigger
                           whose logical_date is after the world's clock
                           (airflowctl sends the wall clock when it has none),
                           a non-empty `conf` or a data interval (the world's
                           jobs take neither, so they would be recorded and
                           never reach the run).
  refused, 409, by name    a trigger or clear that would queue a DAG's 17th run
                           (max_active_runs_per_dag, 16) or overflow the
                           world's queue (world.scheduler.MAX_QUEUED, 256): every
                           queued run is work the grader's next advance does.
  every other route        404 {"detail": ...}: a route real Airflow serves
                           (ROUTES) is named as one this environment does not
                           model; anything else is "Not Found".

AUDIT. One 9.2.2 row per request, written before the response, through
`native.context.Ledger`, refusals and unknown routes included. Tool names are
`native/tailers/airflow.py`'s vocabulary - the rows the access-log tailer would
write for the same request against real Airflow: `airflow.dag_list`,
`dag_get`, `dag_source`, `dagrun_list`, `dagrun_get`, `dag_trigger`,
`task_list`, `task_get`, `task_logs`, `task_clear`, `dag_update`; plus
`auth_token`, `version`, `monitor_health`, `dag_tasks`, `import_errors`,
`backfill` and `unknown_route`. Targets are the MCP scheduler's strings (`scheduler:<dag_id>`,
`scheduler:<dag_run_id>`), the parity the tailer keeps. A trigger and an
applied clear are what blindness.SCHEDULER_TOOLS attributes a ledger change to.

MCP PARITY. The MCP tools `sched_*` stay on a box that plugs Airflow and read
the same ledger: a run queued here is in `sched_state`'s `queue`, and once the
grader's advance has run it, in `sched_logs` like any run (`trigger: manual`,
with this API's `origin`). `sched_trigger` still executes immediately - the
frozen MCP contract - and shows up here as a manual DAG run of its one task.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from ...backends import BACKENDS
from ...records import Observation, ToolError
from ..context import DEV_TOKEN, Ledger, ServiceError, audit_unrouted, context_of, service_health
from ..http import RawBody

NAME = "airflow"
HOST = "airflow.internal"
PORT = 8100
API = "/api/v2"
#: The Airflow release whose REST v2 shapes this serves (module docstring).
VERSION = "3.3.2"
#: Airflow's `[api] maximum_page_limit` default: a larger `limit` is clamped to it.
MAX_PAGE_LIMIT = 100
DEFAULT_LIMIT = 50
#: The JWT audience Airflow's auth manager issues tokens for.
AUDIENCE = "apache-airflow"
#: The token's signing key: derived from the workspace's obviously-fake
#: credential, so nothing in it is a secret and two processes agree on it.
JWT_KEY = hashlib.sha256(f"alkera-airflow-jwt:{DEV_TOKEN}".encode()).hexdigest()
TOKEN_SECONDS = 86400
#: The one user (username, password): what the box exports as AIRFLOW_USER and
#: AIRFLOW_PASSWORD (backends.BACKENDS["airflow"].env), both the workspace's
#: obviously-fake credential.
USER = (BACKENDS[NAME].env["AIRFLOW_USER"], BACKENDS[NAME].env["AIRFLOW_PASSWORD"])
assert USER[1] == DEV_TOKEN, "the box's Airflow password is the shared fake credential"

#: Every route Airflow 3.3.2's REST API and simple auth manager serve, from the
#: OpenAPI documents (module docstring): a request for one this module does not
#: model is refused by name, not answered with a bare "Not Found".
#: `@TI` abbreviates the task-instance prefix, which is too long to repeat.
_TI = "/api/v2/dags/{dag_id}/dagRuns/{dag_run_id}/taskInstances/{task_id}"
ROUTES = tuple(tuple(line.replace("@TI", _TI).split(" ", 1)) for line in """\
GET /api/v2/assets
GET /api/v2/assets/aliases
GET /api/v2/assets/aliases/{asset_alias_id}
GET,POST /api/v2/assets/events
GET /api/v2/assets/{asset_id}
POST /api/v2/assets/{asset_id}/materialize
DELETE,GET /api/v2/assets/{asset_id}/queuedEvents
DELETE,GET /api/v2/assets/{asset_id}/state-store
DELETE,GET,PUT /api/v2/assets/{asset_id}/state-store/{key}
GET /api/v2/auth/login
GET /api/v2/auth/logout
GET,POST /api/v2/backfills
POST /api/v2/backfills/dry_run
GET /api/v2/backfills/{backfill_id}
PUT /api/v2/backfills/{backfill_id}/cancel
PUT /api/v2/backfills/{backfill_id}/pause
PUT /api/v2/backfills/{backfill_id}/unpause
GET /api/v2/config
GET /api/v2/config/section/{section}/option/{option}
GET,PATCH,POST /api/v2/connections
POST /api/v2/connections/defaults
GET,POST /api/v2/connections/enqueue-test
POST /api/v2/connections/test
DELETE,GET,PATCH /api/v2/connections/{connection_id}
GET /api/v2/dagSources/{dag_id}
GET /api/v2/dagStats
GET /api/v2/dagTags
GET /api/v2/dagWarnings
GET,PATCH /api/v2/dags
DELETE,GET,PATCH /api/v2/dags/{dag_id}
DELETE,GET /api/v2/dags/{dag_id}/assets/queuedEvents
DELETE,GET /api/v2/dags/{dag_id}/assets/{asset_id}/queuedEvents
POST /api/v2/dags/{dag_id}/clearDagRuns
POST /api/v2/dags/{dag_id}/clearPartitions
POST /api/v2/dags/{dag_id}/clearTaskInstances
GET,PATCH,POST /api/v2/dags/{dag_id}/dagRuns
POST /api/v2/dags/{dag_id}/dagRuns/list
DELETE,GET,PATCH /api/v2/dags/{dag_id}/dagRuns/{dag_run_id}
POST /api/v2/dags/{dag_id}/dagRuns/{dag_run_id}/clear
GET /api/v2/dags/{dag_id}/dagRuns/{dag_run_id}/hitlDetails
PATCH /api/v2/dags/{dag_id}/dagRuns/{dag_run_id}/taskGroupInstances/{group_id}
PATCH /api/v2/dags/{dag_id}/dagRuns/{dag_run_id}/taskGroupInstances/{group_id}/dry_run
GET,PATCH /api/v2/dags/{dag_id}/dagRuns/{dag_run_id}/taskInstances
POST /api/v2/dags/{dag_id}/dagRuns/{dag_run_id}/taskInstances/list
DELETE,GET,PATCH @TI
GET @TI/dependencies
PATCH @TI/dry_run
GET @TI/externalLogUrl/{try_number}
GET @TI/links
GET @TI/listMapped
GET @TI/logs/{try_number}
DELETE,GET @TI/state-store
DELETE,GET,PATCH,PUT @TI/state-store/{key}
GET @TI/tries
GET @TI/tries/{task_try_number}
GET,POST @TI/xcomEntries
DELETE,GET,PATCH @TI/xcomEntries/{xcom_key}
GET,PATCH @TI/{map_index}
GET @TI/{map_index}/dependencies
PATCH @TI/{map_index}/dry_run
GET,PATCH @TI/{map_index}/hitlDetails
GET @TI/{map_index}/hitlDetails/tries/{try_number}
GET @TI/{map_index}/tries
GET @TI/{map_index}/tries/{task_try_number}
GET /api/v2/dags/{dag_id}/dagRuns/{dag_run_id}/upstreamAssetEvents
GET /api/v2/dags/{dag_id}/dagRuns/{dag_run_id}/wait
GET /api/v2/dags/{dag_id}/dagVersions
GET /api/v2/dags/{dag_id}/dagVersions/{version_number}
GET /api/v2/dags/{dag_id}/details
POST /api/v2/dags/{dag_id}/favorite
GET /api/v2/dags/{dag_id}/tasks
GET /api/v2/dags/{dag_id}/tasks/{task_id}
POST /api/v2/dags/{dag_id}/unfavorite
GET /api/v2/eventLogs
GET /api/v2/eventLogs/{event_log_id}
GET /api/v2/importErrors
GET /api/v2/importErrors/{import_error_id}
GET /api/v2/jobs
GET /api/v2/monitor/health
PUT /api/v2/parseDagFile/{file_token}
GET /api/v2/plugins
GET /api/v2/plugins/importErrors
GET,PATCH,POST /api/v2/pools
DELETE,GET,PATCH /api/v2/pools/{pool_name}
GET /api/v2/providers
GET,PATCH,POST /api/v2/variables
DELETE,GET,PATCH /api/v2/variables/{variable_key}
GET /api/v2/version
GET,POST /auth/token
POST /auth/token/cli
GET /auth/token/login""".splitlines())

app = FastAPI(title="Airflow REST API v2 (alkera native surface)", docs_url=None,
              redoc_url=None, openapi_url=None)
# One row per request, including the ones no handler ran for (context.py).
audit_unrouted(app, NAME)


def _view():
    """`world.airflow_view`. Imported late, like sigma's engine: a gateway that
    never serves Airflow must not need `world` to import this module."""
    try:
        from world import airflow_view
    except ImportError as exc:  # pragma: no cover - a broken image
        raise Refusal(503, "unavailable", f"the world package is not importable: "
                                          f"{exc}") from None
    return airflow_view


class Refusal(ServiceError):
    """A request Airflow would refuse; `detail` is the body's `detail` (a string,
    or FastAPI's validation list for a 422)."""

    def __init__(self, status: int, code: str, detail: Any):
        super().__init__(status, code, detail if isinstance(detail, str)
                         else json.dumps(detail, sort_keys=True))
        self.detail = detail


@app.exception_handler(ServiceError)
async def _service_error(_request: Request, exc: ServiceError) -> JSONResponse:
    headers = {"WWW-Authenticate": "Bearer"} if exc.status == 401 else {}
    return JSONResponse(status_code=exc.status,
                        content={"detail": getattr(exc, "detail", exc.message)},
                        headers=headers)


# ------------------------------------------------------------------- bodies
class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginBody(_Body):
    username: str
    password: str


class TriggerBody(_Body):
    """TriggerDAGRunPostBody (3.3.2): `logical_date` is required and may be null."""
    dag_run_id: str | None = None
    data_interval_start: datetime | None = None
    data_interval_end: datetime | None = None
    logical_date: datetime | None
    run_after: datetime | None = None
    conf: dict | None = None
    note: str | None = None
    partition_key: str | None = None


class ClearBody(_Body):
    """ClearTaskInstancesBody (3.3.2), Airflow's defaults included."""
    dry_run: bool = True
    start_date: datetime | None = None
    end_date: datetime | None = None
    only_failed: bool = True
    only_running: bool = False
    reset_dag_runs: bool = True
    task_ids: list[str | list[str | int]] | None = None
    dag_run_id: str | None = None
    include_upstream: bool = False
    include_downstream: bool = False
    include_future: bool = False
    include_past: bool = False
    run_on_latest_version: bool | None = None
    prevent_running_task: bool = False
    note: str | None = None


class PatchBody(_Body):
    is_paused: bool


def _validated(model, payload: Any):
    """`model` from a parsed JSON body, or the 422 FastAPI would have answered."""
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        detail = [{**{k: v for k, v in e.items() if k in ("type", "msg", "input", "ctx")},
                   "loc": ["body", *e["loc"]]} for e in exc.errors(include_url=False)]
        for entry in detail:
            if "ctx" in entry:
                entry["ctx"] = {k: str(v) for k, v in entry["ctx"].items()}
        raise Refusal(422, "validation_error", detail) from None


def _peek(raw: bytes) -> dict | None:
    """The body as the row's `args` record it: a JSON object, or None."""
    try:
        body = json.loads(raw) if raw else None
    except (ValueError, UnicodeError):
        return None
    return body if isinstance(body, dict) else None


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw) if raw else {}
    except (ValueError, UnicodeError):
        raise Refusal(422, "validation_error", [{
            "type": "json_invalid", "loc": ["body", 0], "msg": "JSON decode error",
            "input": {}, "ctx": {"error": "Expecting value"}}]) from None


# ------------------------------------------------------------------- auth
def _clock(request: Request) -> str:
    return context_of(request.app).clock()


def _epoch(vts: str) -> int:
    return int(datetime.fromisoformat(vts.replace("Z", "+00:00")).timestamp())


def _issue(request: Request, username: str) -> str:
    import jwt

    now = _epoch(_clock(request))
    claims = {"sub": username, "role": "ADMIN", "aud": AUDIENCE, "iat": now, "nbf": now,
              "exp": now + TOKEN_SECONDS,
              "jti": hashlib.sha256(f"{username}|{now}".encode()).hexdigest()[:32]}
    return jwt.encode(claims, JWT_KEY, algorithm="HS256")


def _authenticate(request: Request) -> str:
    """The token's `sub`, or a 401 in Airflow's words. Expiry is not checked:
    the episode's clock is virtual and does not move while the agent works."""
    import jwt

    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Refusal(401, "unauthorized", "Not authenticated")
    try:
        claims = jwt.decode(token.strip(), JWT_KEY, algorithms=["HS256"], audience=AUDIENCE,
                            options={"verify_exp": False, "verify_nbf": False,
                                     "verify_iat": False})
    except jwt.PyJWTError:
        raise Refusal(401, "unauthorized", "Invalid JWT token") from None
    return str(claims.get("sub") or "")


# ------------------------------------------------------------------- plumbing
def _world(request: Request) -> Path:
    config = context_of(request.app).config
    if config.world_root is not None:
        return Path(config.world_root)
    return Path(config.scheduler).parents[2]


def _serve(request: Request, tool: str, call, *, auth: bool = True, **args):
    """One request, one row: authenticate, run, classify - inside the span, so a
    refusal (authentication included) is a row too."""
    ledger = Ledger(request, NAME)
    query = str(request.url.query or "")
    if query:
        args = {"query": query, **args}
    try:
        with ledger.span(tool, **args) as row:
            def run(_operations, observation):
                if auth:
                    ledger.actor = _authenticate(request)
                view = _view()
                try:
                    return call(view, _world(request), observation, ledger.actor)
                except view.AirflowError as exc:
                    raise Refusal(exc.status, f"http_{exc.status}", exc.detail) from None
            return row.run(run)
    except ToolError as exc:
        raise ServiceError.from_tool_error(exc) from None


def _read(observation: Observation, *targets: str) -> None:
    observation.targets = list(targets)
    observation.read_targets = list(targets)


def _fileloc(world: Path, found) -> str:
    return str(world / "workspace" / "dags" / found.file)


def _ints(request: Request) -> tuple[int, int]:
    """(limit, offset): Airflow's defaults, a limit clamped to the page maximum."""
    out = []
    for name, default in (("limit", DEFAULT_LIMIT), ("offset", 0)):
        raw = request.query_params.get(name)
        try:
            value = int(raw) if raw is not None else default
        except ValueError:
            raise Refusal(422, "validation_error", [{
                "type": "int_parsing", "loc": ["query", name],
                "msg": "Input should be a valid integer, unable to parse string as an "
                       "integer", "input": raw}]) from None
        if value < 0:
            raise Refusal(422, "validation_error", [{
                "type": "greater_than_equal", "loc": ["query", name],
                "msg": "Input should be greater than or equal to 0", "input": raw,
                "ctx": {"ge": "0"}}])
        out.append(value)
    return min(out[0], MAX_PAGE_LIMIT), out[1]


def _many(request: Request, name: str) -> list[str]:
    values = []
    for value in request.query_params.getlist(name):
        values += [v for v in value.split(",") if v] if "," in value else [value]
    return values


def _flag(request: Request, name: str) -> bool | None:
    raw = request.query_params.get(name)
    if raw is None:
        return None
    if raw.lower() in ("true", "1", "yes"):
        return True
    if raw.lower() in ("false", "0", "no"):
        return False
    raise Refusal(422, "validation_error", [{
        "type": "bool_parsing", "loc": ["query", name],
        "msg": "Input should be a valid boolean, unable to interpret input", "input": raw}])


def _when(request: Request, name: str):
    raw = request.query_params.get(name)
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "+"))
    except ValueError:
        raise Refusal(422, "validation_error", [{
            "type": "datetime_from_date_parsing", "loc": ["query", name],
            "msg": "Input should be a valid datetime", "input": raw}]) from None


def _like(pattern: str, text: str) -> bool:
    """SQL `ILIKE '%pattern%'`, as Airflow's `*_pattern` filters apply it."""
    import re

    body = "".join(".*" if c == "%" else "." if c == "_" else re.escape(c) for c in pattern)
    return re.search(body, text, re.IGNORECASE) is not None


def _date_filters(request: Request, items: list[dict], fields: tuple[str, ...]) -> list[dict]:
    view = _view()
    for field in fields:
        for suffix, keep in (("_gte", lambda a, b: a >= b), ("_gt", lambda a, b: a > b),
                             ("_lte", lambda a, b: a <= b), ("_lt", lambda a, b: a < b)):
            bound = _when(request, field + suffix)
            if bound is None:
                continue
            bound = view._dt(bound)
            items = [i for i in items if i.get(field) and keep(view._dt(i[field]), bound)]
    return items


def _ordered(items: list[dict], order: list[str], allowed: dict[str, str]) -> list[dict]:
    """Stable sort by `order_by` (`-` for descending); an unknown field is a 400,
    in Airflow's words."""
    for spec in reversed(order or []):
        field, reverse = spec.lstrip("-"), spec.startswith("-")
        if field not in allowed:
            raise Refusal(400, "bad_request",
                          f"Ordering with '{field}' is disallowed or the attribute does not "
                          f"exist on the model")
        key = allowed[field]
        items = sorted(items, key=lambda i, key=key: (i.get(key) is None, i.get(key) or ""),
                       reverse=reverse)
    return items


# ------------------------------------------------------------------ endpoints
@app.get("/health")
def health(request: Request) -> object:
    """The surface gate's probe: `items` is the DAGs this world's workspace defines."""
    def count(_operations, observation):
        observation.read_targets = ["scheduler"]
        return len(_view().dags(_world(request)))

    return service_health(request, NAME, count)


def _login(request: Request, raw: bytes, form_ok: bool) -> JSONResponse:
    form = form_ok and "application/x-www-form-urlencoded" in request.headers.get(
        "content-type", "")

    def parsed():
        if form:
            return {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
        return _json(raw)

    def call(view, world, observation, _actor):
        _read(observation, "airflow:session")
        body = _validated(LoginBody, parsed())
        if (body.username, body.password) != USER:
            raise Refusal(401, "unauthorized", "Invalid credentials")
        return {"access_token": _issue(request, body.username)}

    tool = "auth_token"
    peek = parsed() if form else _peek(raw)
    result = _serve(request, tool, call, auth=False,
                    username=(peek or {}).get("username") if isinstance(peek, dict) else None)
    return JSONResponse(status_code=201, content=result)


@app.post("/auth/token")
def auth_token(request: Request, raw: RawBody) -> JSONResponse:
    return _login(request, raw, form_ok=True)


@app.post("/auth/token/cli")
def auth_token_cli(request: Request, raw: RawBody) -> JSONResponse:
    return _login(request, raw, form_ok=False)


@app.get("/auth/token")
def auth_token_all_admins(request: Request) -> dict:
    def call(view, world, observation, _actor):
        _read(observation, "airflow:session")
        raise Refusal(403, "forbidden", "Create token with no credentials is only allowed "
                                        "if ``simple_auth_manager_all_admins`` is True")

    return _serve(request, "auth_token", call, auth=False)


@app.get(f"{API}/version")
def version(request: Request) -> dict:
    def call(view, world, observation, _actor):
        _read(observation, "scheduler")
        return {"version": VERSION, "git_version": None}

    return _serve(request, "version", call, auth=False)


@app.get(f"{API}/monitor/health")
def monitor_health(request: Request) -> dict:
    def call(view, world, observation, _actor):
        _read(observation, "scheduler")
        beat = view.stamp(_clock(request))
        return {"metadatabase": {"status": "healthy"},
                "scheduler": {"status": "healthy", "latest_scheduler_heartbeat": beat},
                "triggerer": {"status": None, "latest_triggerer_heartbeat": None},
                "dag_processor": {"status": "healthy",
                                  "latest_dag_processor_heartbeat": beat}}

    return _serve(request, "monitor_health", call, auth=False)


@app.get(f"{API}/dags")
def list_dags(request: Request) -> dict:
    def call(view, world, observation, _actor):
        _read(observation, "scheduler")
        limit, offset = _ints(request)
        found = view.dags(world)
        pattern = request.query_params.get("dag_id_pattern")
        if pattern:
            found = [d for d in found if _like(pattern, d.dag_id)]
        tags = set(_many(request, "tags"))
        if tags:
            match_all = request.query_params.get("tags_match_mode") == "all"
            found = [d for d in found if (tags <= set(d.tags) if match_all
                                          else tags & set(d.tags))]
        owners = set(_many(request, "owners"))
        if owners:
            found = [d for d in found if owners & set(d.owners)]
        if _flag(request, "paused") is True:
            found = []                   # nothing is paused here
        shapes = [view.dag_shape(world, d, _fileloc(world, d)) for d in found]
        shapes = _ordered(shapes, _many(request, "order_by") or ["dag_id"],
                          {"dag_id": "dag_id", "dag_display_name": "dag_display_name",
                           "next_dagrun": "next_dagrun_run_after"})
        return {"dags": shapes[offset:offset + limit], "total_entries": len(shapes)}

    return _serve(request, "dag_list", call)


@app.get(f"{API}/dags/{{dag_id}}")
def get_dag(dag_id: str, request: Request) -> dict:
    def call(view, world, observation, _actor):
        _read(observation, f"scheduler:{dag_id}")
        found = view.dag(world, dag_id)
        return view.dag_shape(world, found, _fileloc(world, found))

    return _serve(request, "dag_get", call, dag_id=dag_id)


@app.get(f"{API}/dags/{{dag_id}}/details")
def get_dag_details(dag_id: str, request: Request) -> dict:
    def call(view, world, observation, _actor):
        _read(observation, f"scheduler:{dag_id}")
        found = view.dag(world, dag_id)
        return view.dag_details_shape(world, found, _fileloc(world, found))

    return _serve(request, "dag_get", call, dag_id=dag_id)


@app.patch(f"{API}/dags/{{dag_id}}")
def patch_dag(dag_id: str, request: Request, raw: RawBody) -> dict:

    def call(view, world, observation, _actor):
        observation.op_class = "write"
        observation.targets = [f"scheduler:{dag_id}"]
        found = view.dag(world, dag_id)
        body = _validated(PatchBody, _json(raw))
        if body.is_paused:
            raise Refusal(400, "bad_request",
                          "Pausing a DAG is not modelled in this environment: the world's "
                          "scheduler runs every job on its schedule, so a paused flag would "
                          "be a claim nothing honours. The DAG stays unpaused.")
        observation.rows_affected = 0     # it is unpaused already: nothing changed
        return view.dag_shape(world, found, _fileloc(world, found))

    return _serve(request, "dag_update", call, dag_id=dag_id,
                  body=_peek(raw))


@app.get(f"{API}/dagSources/{{dag_id}}")
def get_dag_source(dag_id: str, request: Request) -> Response:
    def call(view, world, observation, _actor):
        _read(observation, f"scheduler:{dag_id}")
        return {"content": view.dag_source(world, dag_id), "dag_id": dag_id,
                "version_number": None, "dag_display_name": dag_id}

    result = _serve(request, "dag_source", call, dag_id=dag_id)
    if "text/plain" in request.headers.get("accept", ""):
        return PlainTextResponse(result["content"])
    return JSONResponse(result)


@app.get(f"{API}/dags/{{dag_id}}/tasks")
def get_tasks(dag_id: str, request: Request) -> dict:
    def call(view, world, observation, _actor):
        _read(observation, f"scheduler:{dag_id}")
        found = view.dag(world, dag_id)
        tasks = [view.task_shape(found, t) for t in found.tasks]
        tasks = _ordered(tasks, _many(request, "order_by") or ["task_id"],
                         {"task_id": "task_id"})
        return {"tasks": tasks, "total_entries": len(tasks)}

    return _serve(request, "dag_tasks", call, dag_id=dag_id)


@app.get(f"{API}/dags/{{dag_id}}/tasks/{{task_id}}")
def get_task(dag_id: str, task_id: str, request: Request) -> dict:
    def call(view, world, observation, _actor):
        _read(observation, f"scheduler:{dag_id}")
        found = view.dag(world, dag_id)
        task = found.task(task_id)
        if task is None:
            raise Refusal(404, "not_found", f"Task with id {task_id} not found")
        return view.task_shape(found, task)

    return _serve(request, "dag_tasks", call, dag_id=dag_id, task_id=task_id)


@app.get(f"{API}/importErrors")
def list_import_errors(request: Request) -> dict:
    def call(view, world, observation, _actor):
        _read(observation, "scheduler")
        limit, offset = _ints(request)
        errors = view.import_error_shapes(world)
        filename = request.query_params.get("filename")
        if filename:
            errors = [e for e in errors if e["filename"] == filename]
        errors = _ordered(errors, _many(request, "order_by") or ["id"],
                          {"id": "import_error_id", "import_error_id": "import_error_id",
                           "filename": "filename", "timestamp": "timestamp"})
        return {"import_errors": errors[offset:offset + limit], "total_entries": len(errors)}

    return _serve(request, "import_errors", call)


@app.get(f"{API}/importErrors/{{import_error_id}}")
def get_import_error(import_error_id: str, request: Request) -> dict:
    def call(view, world, observation, _actor):
        _read(observation, "scheduler")
        for error in view.import_error_shapes(world):
            if str(error["import_error_id"]) == import_error_id:
                return error
        raise Refusal(404, "not_found", f"The ImportError with import_error_id: "
                                        f"`{import_error_id}` was not found")

    return _serve(request, "import_errors", call, import_error_id=import_error_id)


#: dagRuns `order_by` fields -> the response key they sort on.
RUN_ORDER = {"id": "run_after", "state": "state", "dag_id": "dag_id",
             "run_id": "dag_run_id", "dag_run_id": "dag_run_id",
             "logical_date": "logical_date", "run_after": "run_after",
             "start_date": "start_date", "end_date": "end_date",
             "updated_at": "end_date", "duration": "duration",
             "data_interval_start": "data_interval_start",
             "data_interval_end": "data_interval_end", "last_scheduling_decision":
             "last_scheduling_decision", "run_type": "run_type"}


def _runs_of(view, world, dag_id: str) -> list[dict]:
    if dag_id == "~":
        return [r for d in view.dags(world) for r in view.dag_runs(world, d.dag_id)]
    return view.dag_runs(world, dag_id)


@app.get(f"{API}/dags/{{dag_id}}/dagRuns")
def list_dag_runs(dag_id: str, request: Request) -> dict:
    def call(view, world, observation, _actor):
        _read(observation, f"scheduler:{dag_id}" if dag_id != "~" else "scheduler")
        limit, offset = _ints(request)
        runs = _runs_of(view, world, dag_id)
        for name, key in (("state", "state"), ("run_type", "run_type")):
            wanted = set(_many(request, name))
            if wanted:
                runs = [r for r in runs if r[key] in wanted]
        runs = _date_filters(request, runs, ("run_after", "logical_date", "start_date",
                                             "end_date"))
        pattern = request.query_params.get("run_id_pattern")
        if pattern:
            runs = [r for r in runs if _like(pattern, r["dag_run_id"])]
        runs = _ordered(runs, _many(request, "order_by") or ["id"], RUN_ORDER)
        return {"dag_runs": runs[offset:offset + limit], "total_entries": len(runs)}

    return _serve(request, "dagrun_list", call, dag_id=dag_id)


@app.post(f"{API}/dags/{{dag_id}}/dagRuns")
def trigger_dag_run(dag_id: str, request: Request, raw: RawBody) -> dict:

    def call(view, world, observation, actor):
        observation.op_class = "exec"
        observation.targets = [f"scheduler:{dag_id}", "dbt_project"]
        view.dag(world, dag_id)                       # 404 before the body is judged
        body = _validated(TriggerBody, _json(raw))
        vnow = _clock(request)
        if body.partition_key is not None:
            raise Refusal(400, "bad_request", "Partitioned DAG runs (partition_key) are not "
                                              "modelled in this environment")
        if body.run_after is not None and view._dt(body.run_after) != view._dt(vnow):
            raise Refusal(400, "bad_request",
                          f"run_after is not modelled in this environment: a queued run "
                          f"starts when the world's clock next moves, and the clock is the "
                          f"world's (it is {view.stamp(vnow)}); leave run_after null")
        if (body.data_interval_start is None) != (body.data_interval_end is None):
            raise Refusal(400, "bad_request", "Either both data_interval_start and "
                                              "data_interval_end must be provided or both "
                                              "must be None")
        result = view.trigger(world, dag_id, dag_run_id=body.dag_run_id,
                              logical_date=body.logical_date, conf=body.conf,
                              note=body.note, user=actor or None,
                              data_interval_start=body.data_interval_start,
                              data_interval_end=body.data_interval_end)
        observation.rows_affected = len(view.dag(world, dag_id).jobs)
        return result

    return _serve(request, "dag_trigger", call, dag_id=dag_id,
                  body=_peek(raw))


@app.get(f"{API}/dags/{{dag_id}}/dagRuns/{{dag_run_id}}")
def get_dag_run(dag_id: str, dag_run_id: str, request: Request) -> dict:
    def call(view, world, observation, _actor):
        _read(observation, f"scheduler:{dag_run_id}")
        found = view.dag(world, dag_id)
        run, newest = view.find_run(world, found, dag_run_id)
        return view.dag_run_shape(found, run, newest)

    return _serve(request, "dagrun_get", call, dag_id=dag_id, dag_run_id=dag_run_id)


TI_ORDER = {"map_index": "task_id", "task_id": "task_id", "state": "state",
            "start_date": "start_date", "end_date": "end_date", "duration": "duration",
            "try_number": "try_number", "id": "task_id", "run_after": "run_after",
            "logical_date": "logical_date", "dag_run_id": "dag_run_id",
            "operator": "operator"}


@app.get(f"{API}/dags/{{dag_id}}/dagRuns/{{dag_run_id}}/taskInstances")
def list_task_instances(dag_id: str, dag_run_id: str, request: Request) -> dict:
    def call(view, world, observation, _actor):
        _read(observation, f"scheduler:{dag_run_id}" if dag_run_id != "~"
              else (f"scheduler:{dag_id}" if dag_id != "~" else "scheduler"))
        limit, offset = _ints(request)
        found_dags = view.dags(world) if dag_id == "~" else [view.dag(world, dag_id)]
        tis = []
        for found in found_dags:
            runs = view.dag_run_objects(world, found)
            if dag_run_id != "~":
                runs = [view.find_run(world, found, dag_run_id)[0]]
            for run in runs:
                tis += [view.task_instance_shape(found, run, t) for t in found.tasks]
        task_ids = set(_many(request, "task_id"))
        if task_ids:
            tis = [t for t in tis if t["task_id"] in task_ids]
        states = set(_many(request, "state"))
        if states:
            tis = [t for t in tis if (t["state"] or "no_status") in states
                   or (t["state"] is None and "none" in states)]
        tries = {int(x) for x in _many(request, "try_number") if x.isdigit()}
        if tries:
            tis = [t for t in tis if t["try_number"] in tries]
        tis = _date_filters(request, tis, ("run_after", "logical_date", "start_date",
                                           "end_date"))
        tis = _ordered(tis, _many(request, "order_by") or ["map_index"], TI_ORDER)
        return {"task_instances": tis[offset:offset + limit], "total_entries": len(tis)}

    return _serve(request, "task_list", call, dag_id=dag_id, dag_run_id=dag_run_id)


@app.get(f"{API}/dags/{{dag_id}}/dagRuns/{{dag_run_id}}/taskInstances/{{task_id}}")
def get_task_instance(dag_id: str, dag_run_id: str, task_id: str, request: Request) -> dict:
    def call(view, world, observation, _actor):
        _read(observation, f"scheduler:{dag_run_id}")
        found = view.dag(world, dag_id)
        run, _ = view.find_run(world, found, dag_run_id)
        task = found.task(task_id)
        if task is None:
            raise Refusal(404, "not_found",
                          f"The Task Instance with dag_id: `{dag_id}`, run_id: "
                          f"`{dag_run_id}`, task_id: `{task_id}` and map_index: `-1` was "
                          f"not found")
        return view.task_instance_shape(found, run, task)

    return _serve(request, "task_get", call, dag_id=dag_id, dag_run_id=dag_run_id,
                  task_id=task_id)


@app.get(f"{API}/dags/{{dag_id}}/dagRuns/{{dag_run_id}}/taskInstances/{{task_id}}"
         f"/logs/{{try_number}}")
def get_log(dag_id: str, dag_run_id: str, task_id: str, try_number: str,
            request: Request) -> Response:
    def call(view, world, observation, _actor):
        _read(observation, f"scheduler:{dag_run_id}")
        if not try_number.isdigit():
            raise Refusal(422, "validation_error", [{
                "type": "int_parsing", "loc": ["path", "try_number"],
                "msg": "Input should be a valid integer, unable to parse string as an "
                       "integer", "input": try_number}])
        return view.task_log_events(world, dag_id, dag_run_id, task_id, int(try_number))

    events = _serve(request, "task_logs", call, dag_id=dag_id, dag_run_id=dag_run_id,
                    task_id=task_id, try_number=try_number)
    if "application/x-ndjson" in request.headers.get("accept", ""):
        return Response("".join(json.dumps(e) + "\n" for e in events),
                        media_type="application/x-ndjson")
    return JSONResponse({"content": events, "continuation_token": None})


@app.post(f"{API}/dags/{{dag_id}}/clearTaskInstances")
def clear_task_instances(dag_id: str, request: Request, raw: RawBody) -> dict:

    def call(view, world, observation, actor):
        observation.targets = [f"scheduler:{dag_id}"]
        view.dag(world, dag_id)
        body = _validated(ClearBody, _json(raw))
        if body.dry_run:
            observation.read_targets = [f"scheduler:{dag_id}"]
        else:
            observation.op_class = "destructive"
        if body.dag_run_id is None or body.include_past or body.include_future \
                or body.start_date is not None or body.end_date is not None:
            raise Refusal(400, "bad_request",
                          "Clearing across DAG runs (no dag_run_id, start_date/end_date, "
                          "include_past or include_future) is not available in this "
                          "environment: re-running history is a backfill, and the clock is "
                          "the world's. Clear one DAG run by its dag_run_id.")
        task_ids = []
        for entry in body.task_ids or []:
            if isinstance(entry, list):
                if len(entry) != 2 or str(entry[1]) != "-1":
                    raise Refusal(400, "bad_request", "Mapped task instances (map_index "
                                                      "other than -1) are not modelled")
                entry = entry[0]
            task_ids.append(str(entry))
        cleared = view.clear(world, dag_id, dag_run_id=body.dag_run_id,
                             task_ids=task_ids or None, only_failed=body.only_failed,
                             only_running=body.only_running,
                             include_upstream=body.include_upstream,
                             include_downstream=body.include_downstream,
                             dry_run=body.dry_run, user=actor or None, note=body.note)
        observation.rows_affected = 0 if body.dry_run else len(cleared)
        if not body.dry_run:
            observation.targets = [f"scheduler:{dag_id}", f"scheduler:{body.dag_run_id}"]
        return {"task_instances": cleared, "total_entries": len(cleared)}

    return _serve(request, "task_clear", call, dag_id=dag_id,
                  body=_peek(raw))


@app.api_route(f"{API}/backfills", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@app.api_route(f"{API}/backfills/{{rest:path}}", methods=["GET", "POST", "PUT", "PATCH",
                                                         "DELETE"])
def backfills(request: Request, rest: str = "") -> dict:
    def call(view, world, observation, _actor):
        observation.op_class = "read" if request.method == "GET" else "exec"
        observation.targets = ["scheduler"]
        raise Refusal(400, "bad_request",
                      "Backfills are not available in this environment: the clock is the "
                      "world's. Runs happen when the world's scheduler advances it; to "
                      "re-run one DAG run, clear it (POST /api/v2/dags/{dag_id}/"
                      "clearTaskInstances with its dag_run_id).")

    return _serve(request, "backfill", call)


def _route_of(method: str, path: str) -> str | None:
    """The real Airflow route template `path` matches, for any method, or None."""
    parts = path.rstrip("/").split("/")
    for methods, template in ROUTES:
        want = template.split("/")
        if len(want) == len(parts) and all(w.startswith("{") or w == p
                                           for w, p in zip(want, parts, strict=True)):
            return f"{methods.replace(',', '/')} {template}"
    return None


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def not_found(path: str, request: Request) -> dict:
    """Every other request writes a row: Airflow's 404, naming a real Airflow
    route this environment does not model."""
    def call(view, world, observation, _actor):
        route = _route_of(request.method, "/" + path)
        if route is not None:
            observation.flags = ["unmodelled_route"]
            raise Refusal(404, "not_found",
                          f"{request.method} /{path} ({route}) is an Airflow REST API route "
                          f"this environment does not model")
        raise Refusal(404, "not_found", "Not Found")

    return _serve(request, "unknown_route", call, auth=False)


__all__ = ["HOST", "NAME", "PORT", "app"]
