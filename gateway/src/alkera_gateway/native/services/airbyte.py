"""Airbyte-shaped ingestion service over `alkera_gateway.ingest` (`ingest_*`).

TWO PREFIXES, BOTH REAL. Airbyte Cloud serves this API at `https://api.airbyte.com/v1`
and a self-hosted deployment serves the same operations at `/api/public/v1`.
Every route below is registered under both, so a client configured either way
works and the audit row is the same row.

IDS. Airbyte keys connections and sources by UUID and jobs by integer. This
world keys connectors by name (`acme_orders`) and runs by `sync-000042`, and
those names are what the MCP targets (`ingest:acme_orders`) and the grader join
on - so connection ids are the connector names, and a job id is the integer in
the run id (`sync-000042` <-> 42). Minting UUIDs would produce identifiers that
join to nothing and hide the ones that do.

NOT IMITATED. No workspaces, no destinations beyond the schema a connector
writes to, no `POST /connections` (this world's connectors are seeded, and there
is no MCP tool that creates one), no streams/catalog discovery beyond the tables
a connector declares, no `DELETE`. Sync alerts have no Airbyte endpoint at all -
a real Airbyte reports failures through jobs and email - so `ingest_alerts` is
served at `/alerts`, which is this service's own route and is marked as such.
"""
from __future__ import annotations

import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..context import ServiceError, audit_unrouted, served, service_health
from ..http import JSONBody

NAME = "airbyte"
HOST = "airbyte.internal"
PORT = 8001

WORKSPACE_ID = "alkera-local-dev-workspace"
JOB_STATUS = {"success": "succeeded", "failed": "failed", "running": "running"}
CONNECTION_STATUS = {"active": "active", "paused": "inactive"}
_RUN_ID = re.compile(r"^sync-(?P<number>\d+)$")
# `tools.IngestRuns` caps `limit` at 500; the native surface stays inside the
# bound the MCP schema serves.
MAX_JOBS = 500

app = FastAPI(title="Alkera Airbyte (fake)", version="0.1.0")
# One row per request, including the ones no handler ran for (context.py).
audit_unrouted(app, NAME)


@app.exception_handler(ServiceError)
async def _service_error(request: Request, exc: ServiceError) -> JSONResponse:
    """Airbyte answers with RFC 7807 problem details."""
    return JSONResponse(
        status_code=exc.status,
        content={
            "type": f"https://{HOST}/problems/{exc.code}",
            "title": exc.code,
            "status": exc.status,
            "detail": exc.message,
        },
    )


def job_id(run_id: str) -> int:
    matched = _RUN_ID.match(run_id)
    if not matched:
        raise ServiceError(500, "unmappable_run", f"Run id is not sync-NNNNNN: {run_id}")
    return int(matched.group("number"))


def run_id(job: int) -> str:
    return f"sync-{job:06d}"


def connection_of(connector: dict) -> dict:
    destination = connector.get("destination") or {}
    schedule = connector.get("schedule")
    return {
        "connectionId": connector["id"],
        "name": connector.get("name") or connector["id"],
        "workspaceId": WORKSPACE_ID,
        "sourceId": connector["id"],
        "destinationId": destination.get("schema"),
        "status": CONNECTION_STATUS.get(connector.get("status"), "deprecated"),
        "schedule": {"scheduleType": "cron", "cronExpression": schedule} if schedule
        else {"scheduleType": "manual"},
        "configurations": {
            "streams": [
                {"name": table, "syncMode": "incremental_append"}
                for table in destination.get("tables") or []
            ]
        },
        "lastJob": connector.get("last_run"),
    }


def source_of(connector: dict) -> dict:
    source = connector.get("source") or {}
    return {
        "sourceId": connector["id"],
        "name": connector.get("name") or connector["id"],
        "sourceType": source.get("type"),
        "workspaceId": WORKSPACE_ID,
        "configuration": {key: value for key, value in source.items() if key != "type"},
    }


def job_of(run: dict) -> dict:
    """One sync run as an Airbyte job.

    `rowsSynced` and `streams` appear only when the run carries its per-table
    counts - a run summary from `ingest_runs` does not, and reporting 0 rows for
    "this listing does not say" is the silent-wrong answer this repo refuses.
    Airbyte's `bytesSynced` is never served: the world counts rows, not bytes.
    """
    job = {
        "jobId": job_id(run["id"]),
        "status": JOB_STATUS.get(run["status"], run["status"]),
        "jobType": "sync",
        "connectionId": run["connector_id"],
        "startTime": run["started_at"],
        "lastUpdatedAt": run.get("finished_at"),
    }
    tables = run.get("tables")
    if tables is not None:
        job["rowsSynced"] = sum(
            int(counts.get("rows_inserted", 0)) + int(counts.get("rows_updated", 0))
            for counts in tables.values()
        )
        job["streams"] = sorted(tables)
    return job


@app.get("/health")
def health(request: Request) -> object:
    """The connections this service can serve, counted through `ingest_list`."""
    return service_health(
        request, NAME, lambda ops, obs: len(ops.ingest_list(obs)["connectors"])
    )


@app.get("/api/public/v1/connections")
@app.get("/v1/connections")
def list_connections(request: Request) -> dict:
    listing = served(request, NAME, "ingest_list", lambda ops, obs: ops.ingest_list(obs))
    return {"data": [connection_of(connector) for connector in listing["connectors"]]}


@app.get("/api/public/v1/connections/{connection_id}")
@app.get("/v1/connections/{connection_id}")
def get_connection(connection_id: str, request: Request) -> dict:
    connector = served(
        request, NAME, "ingest_get",
        lambda ops, obs: ops.ingest_get(connection_id, obs), {"connector_id": connection_id},
    )
    return connection_of(connector)


@app.get("/api/public/v1/sources")
@app.get("/v1/sources")
def list_sources(request: Request) -> dict:
    """One source per connector: this world has no source that is not attached
    to exactly one connector, so inventing a separate source registry would
    invent entities the world does not have."""
    listing = served(request, NAME, "ingest_list", lambda ops, obs: ops.ingest_list(obs))
    return {"data": [source_of(connector) for connector in listing["connectors"]]}


@app.get("/api/public/v1/sources/{source_id}")
@app.get("/v1/sources/{source_id}")
def get_source(source_id: str, request: Request) -> dict:
    connector = served(
        request, NAME, "ingest_get",
        lambda ops, obs: ops.ingest_get(source_id, obs), {"connector_id": source_id},
    )
    return source_of(connector)


@app.get("/api/public/v1/jobs")
@app.get("/v1/jobs")
def list_jobs(request: Request, connectionId: str | None = None, limit: int = 20,
              offset: str | None = None, jobType: str = "sync") -> dict:
    """`offset` is Airbyte's paging cursor; `ingest_runs` pages by continuation
    token and the token is an offset, so the two are the same thing."""
    if jobType != "sync":
        raise ServiceError(400, "unsupported_job_type", f"only sync jobs exist: {jobType}")
    page = max(1, min(limit, MAX_JOBS))
    listing = served(
        request, NAME, "ingest_runs",
        lambda ops, obs: ops.ingest_runs(connectionId, page, offset, obs),
        {"connector_id": connectionId, "limit": page, "continuation_token": offset},
    )
    return {
        "data": [job_of(run) for run in listing["runs"]],
        "next": listing["next_token"],
    }


@app.get("/api/public/v1/jobs/{job}")
@app.get("/v1/jobs/{job}")
def get_job(job: int, request: Request) -> dict:
    identifier = run_id(job)
    run = served(
        request, NAME, "ingest_run",
        lambda ops, obs: ops.ingest_run(identifier, obs), {"run_id": identifier},
    )
    return {**job_of(run), "timeline": run["timeline"]}


@app.post("/api/public/v1/jobs")
@app.post("/v1/jobs")
def create_job(request: Request, body: JSONBody) -> dict:
    """Trigger a sync. `ingest_trigger` is an `exec`, and so is this."""
    connection_id = body.get("connectionId")
    if not connection_id:
        raise ServiceError(400, "missing_connection", "connectionId is required")
    if body.get("jobType", "sync") != "sync":
        raise ServiceError(400, "unsupported_job_type", f"only sync jobs exist: {body['jobType']}")
    run = served(
        request, NAME, "ingest_trigger",
        lambda ops, obs: ops.ingest_trigger(connection_id, obs),
        {"connector_id": connection_id},
    )
    return {**job_of(run), "timeline": run["timeline"]}


@app.get("/api/public/v1/connections/{connection_id}/schema")
@app.get("/v1/connections/{connection_id}/schema")
def get_schema(connection_id: str, request: Request) -> dict:
    """Airbyte's catalog lives inside the connection object; this world keeps a
    per-table schema-change policy that the connection shape has nowhere to put,
    so it is served here."""
    return served(
        request, NAME, "ingest_schema",
        lambda ops, obs: ops.ingest_schema(connection_id, obs),
        {"connector_id": connection_id},
    )


@app.get("/api/public/v1/alerts")
@app.get("/v1/alerts")
def list_alerts(request: Request, connectionId: str | None = None,
                acknowledged: bool | None = None) -> dict:
    """This service's own route, not Airbyte's - see the module docstring."""
    listing = served(
        request, NAME, "ingest_alerts",
        lambda ops, obs: ops.ingest_alerts(connectionId, acknowledged, obs),
        {"connector_id": connectionId, "acknowledged": acknowledged},
    )
    return {"data": listing["alerts"]}
