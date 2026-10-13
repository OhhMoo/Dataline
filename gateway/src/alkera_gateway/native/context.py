"""The one context every native service runs in (gateway/NATIVE.md).

WHY THIS FILE EXISTS. Nine fake services answer a vendor-shaped HTTP request by
running `alkera_gateway.operations.Operations` methods against the episode's
world and writing one agent.md 9.2.2 row per action. Before this module the
run-and-record step existed four times: once in `services/_ops.py` for the ops
plane and once more, verbatim, in each of `jira.py`, `slack.py` and
`confluence.py` (SERVICES_A_REQUESTS.md item 1, SERVICES_B_REQUESTS.md item 3).
Four copies of the writer that must not drift is exactly the risk the
single-row-writer rule exists to remove, so there is now one.

HOW EQUIVALENCE IS OBTAINED, NOT ASSERTED. A span hands the handler the same
`records.Observation` object the MCP gateway hands the same `Operations` method,
and reads `op_class`, `targets`, `read_targets`, `flags` and `rows_affected`
back off it afterwards. The native row's classification is therefore *produced*
by the module that produces the MCP row's; no handler names an `op_class` or a
target and no table maps one transport to the other.

Three rules follow, and all three are mechanical rather than editorial:

- **One action, one row.** `Span.run` may be called once. It seals the
  observation the moment the module call returns, so a second classifying call
  raises `SealedObservation` instead of quietly re-pointing the row.
- **Header and resolution reads pay nothing.** `Span.aside` runs them against
  `observation.child()`, a detached observation whose targets never reach the
  row (HUMAN_PLANE.md rule 3).
- **An unclassified success cannot be recorded.** A handler that answers without
  running a module call raises `UnclassifiedObservation` rather than emitting a
  row with `read`/`[]` in it. Adding an endpoint and forgetting to classify it
  now fails loudly at the first request.

A *refusal* is allowed to be unclassified, and classifies exactly the way the
MCP gateway classifies a failure that never reached a module: `op_class: read`,
no targets. An authentication failure is the common case; an `issues_get` for a
missing issue is not, because `Operations.issues_get` names its target before
the workflow layer raises, on both transports.

CONFIGURATION. `ALKERA_GATEWAY_CONFIG` names the gateway config JSON that
`docker/start.py` already writes; unset, or naming a file that is not there,
falls back to `GatewayConfig()` defaults. `serve.py` and the tests may instead
call `set_context(app, context)` per app. `native.audit` reads the virtual clock
from `ALKERA_SCHEDULER` and the rows file from `ALKERA_NATIVE_AUDIT`
(SERVICES_A_REQUESTS.md item 5).
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..config import GatewayConfig
from ..operations import Operations
from ..records import Observation, ToolError, UnclassifiedObservation, json_value
from ..scheduling import episode_scheduler
from .audit import record

CONFIG_ENV = "ALKERA_GATEWAY_CONFIG"
DEFAULT_CONFIG = "/tmp/gateway.json"

# Obviously fake, by repo rule: a credential in a workspace must never be
# mistakable for a real key. Every service in this package accepts this one.
#
# It MUST equal what the workspace hands the agent - world/world/native_workspace
# .FAKE_TOKEN, the compose environment (JIRA_API_TOKEN, CONFLUENCE_API_TOKEN,
# SLACK_BOT_TOKEN) and agent_under_test.NATIVE_FAKE_CREDENTIAL, all
# `alkera-local-dev`. It was `alkera-local-dev-token` from 2026-09-16 until
# 2026-09-26, so Jira, Slack and Confluence refused the workspace's own
# credentials; the first native Opus run (opus-decision-k1-20260926T033648Z)
# could not read a ticket or tell an owner, on every decision arm.
# world/tests/test_native_workspace.py now fails when the two differ.
DEV_TOKEN = "alkera-local-dev"

# ToolError code -> HTTP status. Everything a module can raise is one of these
# situations; anything unlisted is a bug in the service and says 500.
_STATUS = {
    "connector_paused": 409,
    "not_refreshed": 404,
    "no_observability": 503,
    "no_ingest_state": 503,
}


class ServiceError(Exception):
    """A module refusal on its way to a vendor-shaped error body.

    Each service renders this in its own vendor's spelling (Prometheus has
    `errorType`, Airbyte has RFC-7807, GitHub has `message`), so the shape stays
    the vendor's and the cause stays the module's.
    """

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    @classmethod
    def from_tool_error(cls, exc: ToolError) -> ServiceError:
        status = _STATUS.get(exc.code)
        if status is None:
            status = 404 if exc.code.endswith("_not_found") else 400
        return cls(status, exc.code, str(exc))


class Context:
    """The world one service process serves: a config, an `Operations` over it,
    the injected scheduler, and the virtual clock.

    A `Gateway` is deliberately not used even though it builds the same three
    things: its `AuditLog` takes an exclusive lock on the audit directory, and
    in a mixed episode the MCP gateway holds that lock. Everything else it does
    to build `Operations` is done here, by the same calls - the clock mirrors
    `Gateway._virtual_time` and the scheduler comes from
    `scheduling.episode_scheduler`, which is what `cli serve` injects - because
    a service that resolved the world differently from the gateway would be a
    second world.
    """

    def __init__(self, config: GatewayConfig):
        self.config = config
        # The SAME scheduler the MCP gateway gets. `alkera_gateway.cli serve`
        # builds `Gateway(cfg, scheduler=episode_scheduler(cfg))`, and this line
        # is that call: `WorldScheduler` when the config names a world with
        # `var/sched/state.json`, the built-in `FileScheduler` otherwise.
        # Without it the nine services ran on `FileScheduler` while
        # `./ops/sched` ran on `WorldScheduler`, so the two halves of native
        # mode disagreed about which scheduler the world has - and
        # `FileScheduler` reports `not_executed` for every job that is not
        # dbt-shaped, which is the failure `scheduling.py` exists to end.
        self.operations = Operations(config, self.clock,
                                     episode_scheduler(config))

    def clock(self) -> str:
        try:
            state = json.loads(self.config.scheduler.read_text(encoding="utf-8"))
            return state.get("vnow", self.config.virtual_time)
        except (OSError, ValueError):
            return self.config.virtual_time


# (config path, its bytes, the context built from them). Keyed by the bytes and
# not built once, because the human plane resolves its world per request so a
# test or a reload can repoint a running app; keyed at all, because building
# `Operations` stands the whole module stack up and a service answers many
# requests per episode.
_process_context: tuple[str, bytes, Context] | None = None
# Native sync handlers share the same mutable world and must serialize their
# read/modify/write spans, as Gateway.call does. Keep the lock across reloads.
_WORLD_LOCK = RLock()


def process_context() -> Context:
    """The context named by `$ALKERA_GATEWAY_CONFIG`, rebuilt when it changes."""
    global _process_context
    name = os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG
    with _WORLD_LOCK:
        try:
            raw = Path(name).read_bytes()
        except FileNotFoundError:
            if os.environ.get(CONFIG_ENV):
                raise
            raw = None
        if _process_context is None or _process_context[:2] != (name, raw):
            config = GatewayConfig.for_services(raw) if raw is not None else GatewayConfig()
            _process_context = (name, raw, Context(config))
        return _process_context[2]


def set_context(app: FastAPI, context: Context) -> None:
    """Point one service app at a world. `serve.py` and the tests call this;
    without it a service falls back to `process_context()`."""
    app.state.alkera_context = context


def context_of(app: FastAPI) -> Context:
    return getattr(app.state, "alkera_context", None) or process_context()


class Span:
    """One 9.2.2 row in flight, and the observation the module fills in.

    `args` and `result` are mutable because both can be known only once the
    request has been parsed and the module has answered; `op_class`, `targets`,
    `read_targets`, `flags` and `rows_affected` are not settable at all - they
    are read off `observation` when the row is written.
    """

    def __init__(self, tool: str, args: dict, operations: Operations):
        self.tool = tool
        self.args = args
        self.result: object = None
        self.operations = operations
        self.observation = Observation()
        self._ran = False

    def run(self, call: Callable[[Operations, Observation], Any]) -> Any:
        """Run the one module call that defines this action, and seal it.

        Sealed on return, not at the end of the handler, so a header read that
        is handed this observation by mistake raises there and then rather than
        overwriting the row's targets with whatever it touched.
        """
        if self._ran:
            raise RuntimeError(
                f"{self.tool} already ran the operation that defines it; a second "
                f"module call is a second action and needs its own row, or "
                f"aside() if it is a header read that costs none"
            )
        self._ran = True
        try:
            self.result = call(self.operations, self.observation)
        finally:
            self.observation.seal()
        return self.result

    def aside(self, call: Callable[[Operations, Observation], Any]) -> Any:
        """Run a header or resolution read this vendor shape forces, for no row.

        The project key behind a Jira issue, the status vocabulary, the world
        channel behind a `C…` id, the space header: the MCP result carries all
        of them for free, so charging a row for them would make `GET /issue/KEY`
        cost two rows where `issues_get` costs one, and that parity is the whole
        point of the log.
        """
        return call(self.operations, self.observation.child())


class Ledger:
    """Every row one request writes, all attributed to one caller.

    **What costs a row.** A read endpoint opens one span per MCP-equivalent read
    it answers with, named after that read, so a native trajectory costs what
    the same work costs over MCP: a JQL search is one `issues_list`, an
    `issues_search` when the query has a `text ~` clause, and one `issues_get`
    per row it returns. A write endpoint opens exactly one span, the write's:
    the precondition reads it does to turn a vendor payload into the arguments
    `issues_update` takes are part of the write, not separate questions.

    **Failures count.** A span writes its row on the way out of an exception
    too, authentication included, because the world-diff rule can only call a
    change unaudited if every *attempt* left a row.
    """

    def __init__(self, request: Request, service: str):
        self.request = request
        self.service = service
        self.actor = ""

    @contextmanager
    def span(self, tool: str, /, **args):
        with _WORLD_LOCK, self._span(tool, **args) as row:
            yield row

    @contextmanager
    def _span(self, tool: str, /, **args):
        """Open one row. `tool` is the MCP-equivalent operation it pays for and
        `args` the vendor parameters the caller actually sent, so the row says
        what the agent did over HTTP rather than a reconstruction of it. `tool`
        is positional-only because a vendor parameter may legitimately be called
        `tool`."""
        row = Span(
            f"{self.service}.{tool}",
            {"method": self.request.method, "path": self.request.url.path, **args},
            context_of(self.request.app).operations,
        )
        started = time.monotonic()
        try:
            yield row
        except BaseException as exc:
            self._write(row, started, {"code": getattr(exc, "code", type(exc).__name__),
                                       "message": str(exc)})
            raise
        self._write(row, started)

    def _write(self, row: Span, started: float, error: dict | None = None) -> dict:
        mark_audited(self.request)
        observation = row.observation
        if error is None and not observation.classified:
            raise UnclassifiedObservation(
                f"{row.tool} answered without running a module call, so there is no "
                f"op_class or target to record. Every native row's classification is "
                f"read off the observation an Operations method filled in."
            )
        return record(
            tool=row.tool,
            args=row.args,
            op_class=observation.op_class,
            targets=observation.targets,
            read_targets=observation.read_targets,
            exit="error" if error else "ok",
            error=error,
            flags=observation.flags,
            rows_affected=observation.rows_affected,
            latency_ms=(time.monotonic() - started) * 1000,
            # The module result, not the vendor-shaped payload, so a native row
            # and the MCP row for the same action differ only in `tool`, `args`
            # and `transport` - and so a grader reads one shape from both.
            result=json_value(row.result),
            transport="native",
            actor=self.actor,
        )


def served(
    request: Request,
    service: str,
    tool: str,
    run: Callable[[Operations, Observation], Any],
    args: dict | None = None,
) -> Any:
    """Run one module operation, write its 9.2.2 row, return its result.

    The single-call shape the ops plane's endpoints have: one vendor request,
    one module method, one row. `args` is the vendor request as the service saw
    it, so the row records what the agent actually did over HTTP rather than a
    reconstruction of it. A module refusal becomes a `ServiceError` for the
    service's own error renderer, after the row for the failed attempt is on
    file.
    """
    try:
        with Ledger(request, service).span(tool, **(args or {})) as row:
            return row.run(run)
    except ToolError as exc:
        raise ServiceError.from_tool_error(exc) from None


def service_health(request: Request, service: str,
                   count: Callable[[Operations, Observation], int]) -> object:
    """`GET /health` for one service: `{"ok", "service", "items"}`.

    `items` is counted from the world on every request, through the same module
    call the service's handlers use - a constant here would let a service that
    is wired to nothing pass the surface gate, which is the one thing the gate
    exists to catch.

    The probe writes a row, like every other request, but under the target
    `health:<service>` rather than the target the read touched. A harness that
    polls health must not inject `metrics` or `dashboard:x` into the episode's
    target set and break MCP/native parity; what was really read is still
    reported, in `read_targets`.

    A world that has no such surface at all (`no_observability`,
    `no_ingest_state`) answers 503 with `ok: false` and the module's code, not a
    zero that reads as "wired, and empty".
    """
    mark_audited(request)
    operations = context_of(request.app).operations
    observation = Observation()
    started = time.monotonic()
    error = None
    items = 0
    try:
        items = int(count(operations, observation))
    except ToolError as exc:
        error = exc.code
    record(
        tool=f"{service}.health",
        args={"method": request.method, "path": request.url.path},
        op_class="read",
        targets=[f"health:{service}"],
        read_targets=observation.read_targets,
        exit="error" if error else "ok",
        error=error,
        latency_ms=(time.monotonic() - started) * 1000,
        result={"items": items},
        transport="native",
    )
    body = {"ok": error is None, "service": service, "items": items}
    if error:
        return JSONResponse(status_code=503, content={**body, "error": error})
    return body


def note(request: Request, service: str, operation: str, *, op_class: str,
         targets: list[str], args: dict | None = None, result: Any = None,
         error: dict | None = None) -> dict:
    """Record a row for an endpoint that has no module behind it.

    Two endpoints across the nine services are like that: a Metabase session,
    which authenticates against a constant, and Jira's field directory, whose
    vocabulary is a tuple in `jira.py`. Neither touches world state, so no
    module classifies them and `Ledger` would refuse them a row. The row is
    still written, because an unaudited native request is exactly the hole
    native mode exists to close - and both are named in `OPS_PLANE.md` and
    `HUMAN_PLANE.md` as the only hand-written classifications left. `error`
    exists for the same reason a span writes its row on the way out of an
    exception: a refused request is still a request served.
    """
    mark_audited(request)
    return record(
        tool=f"{service}.{operation}",
        args={"method": request.method, "path": request.url.path, **(args or {})},
        op_class=op_class,
        targets=targets,
        exit="error" if error else "ok",
        error=error,
        result=json_value(result),
        transport="native",
    )


#: The request-state flag every row-writing path above sets, so the fallback
#: below can tell "this request already has its row" from "no handler ran".
AUDITED_FLAG = "alkera_audited"


def mark_audited(request: Request) -> None:
    """Record, on the request, that its 9.2.2 row has been (or is being) written."""
    try:
        setattr(request.state, AUDITED_FLAG, True)
    except AttributeError:  # pragma: no cover - a request double with no state
        pass


def was_audited(request: Request) -> bool:
    return bool(getattr(request.state, AUDITED_FLAG, False))


def audit_unrouted(app: FastAPI, service: str) -> FastAPI:
    """Give `app` a row for every request no handler wrote one for.

    Measured 2026-10-04 (tasks/harness/bi_e2e_check.py): a request for a path
    the service does not route - `GET /wiki/rest/api/content/ae/metrics/net-
    revenue`, which is the page id the onboarding page prints, or any `GET
    /rest/api/3/<typo>` - answered FastAPI's default 404 and wrote NOTHING,
    on jira, slack, confluence and powerbi alike; so did a 405, a 422 from
    request validation and the 400/413 `native/http.py` raises while reading a
    body, because all of them are decided before a handler opens its span.
    Every module's docstring promises "one row per request, failures
    included", and the world-diff rule can only call a change unaudited if
    every ATTEMPT left a row (`Ledger`'s docstring) - so a request the service
    refused without running anything still gets one: `<service>.request`,
    `op_class: read`, no targets (nothing was touched), `exit: error` with the
    HTTP status as the code. The response is FastAPI's own default, byte for
    byte, so a client sees exactly what it saw before.

    Rows already written are never doubled: `Ledger`, `service_health` and
    `note` mark the request, and only an unmarked request is recorded here.
    Tableau keeps its own handler (vendor-shaped XML errors, the same rule);
    Sigma routes every unknown path to a handler of its own and gets this for
    the validation and body errors that still bypass it.
    """

    def unrouted_row(request: Request, status: int, message: str) -> None:
        if was_audited(request):
            return
        mark_audited(request)
        record(
            tool=f"{service}.request",
            args={"method": request.method, "path": request.url.path,
                  "query": str(request.url.query or "")},
            op_class="read",
            targets=[],
            exit="error",
            error={"code": f"http_{status}", "message": message},
            result={"status": status},
            transport="native",
        )

    @app.exception_handler(StarletteHTTPException)
    async def _unrouted_http(request: Request, exc: StarletteHTTPException):
        unrouted_row(request, exc.status_code, str(exc.detail))
        return await http_exception_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _unrouted_validation(request: Request, exc: RequestValidationError):
        unrouted_row(request, 422, "request validation failed")
        return await request_validation_exception_handler(request, exc)

    return app
