"""The scheduler plane: `./ops/sched`'s backend, served like the other eight.

WHY THERE IS A SERVER FOR A CLI. `gateway/NATIVE_SURFACES.md` maps `sched_*`
onto a scheduler, and this world's scheduler is a state file plus an executor -
`var/sched/state.json`, the run logs beside it, `var/obs`, `build/sched`. On the
HOST topology `shims/sched_cli.py` drives all of that in its own process, which
is the right shape for an engineer's laptop and is what
`tasks/harness/test_native_stack.py` proves row for row against
`alkera_gateway.Gateway`.

Under the CONTAINER topology none of those files is in the episode. The episode
container mounts the working tree, the warehouse FILES and the audit directory
and nothing else, because that is what makes a `cp` beside the warehouse land in
the container instead of in the world and the rollout directory have no path at
all (`tasks/harness/NATIVE_CONTAINER.md`, containment P7). So the plane has to
be reached the way the other eight are reached: over HTTP, from the
`native-services` container, which does mount the whole world. `SCHED_URL` in
the episode's environment is what switches the same CLI onto this app; the agent
still finds `./ops/sched` in its own repo and cannot tell which mode it is in.

WHAT IS NOT DUPLICATED. Each handler is a `native/context.served` call around
the SAME function the CLI's local mode runs - `sched_cli.list_jobs`,
`.read_logs`, `.trigger_job` - so the row it writes (`sched.sched_list`,
`sched.sched_logs`, `sched.sched_trigger`) is classified by the `Observation`
the same `Operations` method filled in, exactly as the nine services' rows are
and exactly as the CLI's own are. There is no second implementation of `logs`'s
run-id-or-job rule, and nothing here names an `op_class` or a target.

There is no `advance`, here or in the CLI. `Operations` has no `sched_advance`,
the GRADER moves the world's clock between checkpoints, and a native-only
capability would contaminate the very MCP-vs-native comparison native mode
exists to make.

WHY THIS IS A MODULE AND NOT A FUNCTION IN `serve.py`. `serve.py` carries
`from __future__ import annotations`, so every annotation in it is a string and
FastAPI resolves a handler's `request: Request` against that module's globals.
With the fastapi import kept local - which `serve.py` does on purpose, so the
process still starts on an image without the `native` extra - `Request` is not a
module global, FastAPI reads `request` as a missing QUERY parameter, and every
route answers 422. Measured, on the first container run of this plane: the
surface gate reported `no_health_schema` and the scheduler surface came out
dead. Here there is no `from __future__ import annotations` and the import is at
module level, which is the same shape `native/services/*.py` already have.
"""

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .context import ServiceError, served, service_health
from .shims import sched_cli

#: The name every row this plane writes is prefixed with, and the alias it is
#: served under (`sched.internal`). Not one of the nine vendor-shaped services:
#: the aggregate does not report it, containment P6's registry does not expect
#: it, and `surface_audit` does not sweep it as `~sched` - it is the backend of
#: a CLI the agent's own repository carries.
NAME = "sched"


def build_app() -> FastAPI:
    """The three operations `./ops/sched` runs, plus `/health`."""
    app = FastAPI(title="alkera scheduler plane")

    @app.exception_handler(ServiceError)
    async def refused(_request: Request, exc: ServiceError) -> JSONResponse:
        # The shape `sched_cli.remote` parses back into the same message its
        # local mode would have printed, so a refusal reads identically to the
        # agent whichever mode it is in.
        return JSONResponse(status_code=exc.status,
                            content={"error": exc.code, "message": exc.message})

    @app.get("/health")
    def health(request: Request):
        """`{"ok", "service", "items"}`, `items` = the jobs this world declares.

        Counted through the same `sched_list` the `/jobs` route answers with,
        for the reason `native/context.service_health` states: a constant here
        would let a plane wired to nothing pass the surface gate, and the gate
        exists to catch exactly that. A world whose scheduler declares no job is
        a dead surface and says so.
        """
        return service_health(
            request, NAME,
            lambda ops, obs: len((sched_cli.list_jobs(ops, obs) or {})
                                 .get("jobs") or []))

    @app.get("/jobs")
    def jobs(request: Request):
        """`sched_list`. One row, `sched.sched_list`."""
        return served(request, NAME, "sched_list", sched_cli.list_jobs)

    @app.get("/runs")
    def runs(request: Request, selector: str = ""):
        """`sched_logs`, by run id or by job name.

        `selector` is optional and empty means the whole history, which is what
        `./ops/sched logs` with no argument asks for. A selector that is neither
        a run id nor a job with a run in it is the CLI's own refusal, re-raised
        with its own wording so both modes say the same thing.
        """
        try:
            return served(request, NAME, "sched_logs",
                          lambda ops, obs: sched_cli.read_logs(
                              ops, obs, selector or None),
                          args={"selector": selector})
        except sched_cli.SchedulerError as exc:
            raise ServiceError(404, "run_not_found", str(exc)) from None

    @app.post("/runs")
    async def trigger(request: Request):
        """`sched_trigger`. One row, `sched.sched_trigger`, and a real run.

        The body is `{"job": str, "date": str, "vars": {}}` - the three things
        `./ops/sched trigger` takes. `date` empty means the world's own current
        virtual day, which is what a manual trigger scopes to; a wall-clock date
        would file the run under a day the world has never reached.
        """
        try:
            payload = json.loads(await request.body() or b"{}")
        except ValueError:
            raise ServiceError(400, "invalid_arguments",
                               "the body is not JSON") from None
        job = str(payload.get("job") or "")
        if not job:
            raise ServiceError(400, "invalid_arguments",
                               "trigger takes a job name")
        date = str(payload.get("date") or "")
        variables = dict(payload.get("vars") or {})
        return served(request, NAME, "sched_trigger",
                      lambda ops, obs: sched_cli.trigger_job(ops, obs, job,
                                                             date, variables),
                      args={"job": job, "date": date, "vars": variables})

    return app
