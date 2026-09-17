"""Release service shaped like the GitHub Deployments API, over `deploy_*`.

WHY THIS SHAPE. `deploy_list` / `deploy_run` / `deploy_history` are a release
surface, and the release surface a data engineer already knows how to drive is
`POST /repos/{owner}/{repo}/deployments` plus its statuses. The mapping:

    GET  /repos/{o}/{r}/environments             -> deploy_list
    POST /repos/{o}/{r}/deployments              -> deploy_run
    GET  /repos/{o}/{r}/deployments              -> deploy_history
    GET  /repos/{o}/{r}/deployments/{id}/statuses-> deploy_statuses
    POST /repos/{o}/{r}/deployments/{id}/statuses-> deploy_status

WHERE IT DIVERGES, ON PURPOSE. GitHub's create-deployment only *records an
intent* and waits for an external runner to do the work and post statuses. There
is no runner here and no webhook - the world's deploy is executed by the module
- so creating a deployment runs it, and the automatic status that follows
carries the canary verdict. A deployment that only recorded intent would let an
agent "deploy" without touching the world, which is the exact failure the canary
exists to catch.

IDS AND SHAS. A deployment id is derived from the record entry it names
(`deploy`, `started_at`, `finished_at`), so the id the POST returns is the id a
later GET returns, in this process or another one, without storing a counter.
`sha` is that same digest: the world's deploys are not keyed to commits, and
putting the workspace's HEAD there would claim a link that does not exist.

STATUSES ARE RECORDED, NOT REMEMBERED. A status posted here used to live in a
dict in this process and die with it. `deploy_status` / `deploy_statuses` now
append to and read the same observation record `deploy_run` writes its history
into, so an operator's verdict survives a restart and is read back by whichever
transport asks (SERVICES_B_REQUESTS.md item 2). It goes there rather than into
the world tree - which is where an alarm silence goes - because it changes
nothing any other reader of the world sees: it is a note about what was
observed, which is what that record holds.

OWNER AND REPO ARE NOT CHECKED. This world has one warehouse and no repository
registry, so any `{owner}/{repo}` is served; the workspace's `.envrc` names one
for the CLI's convenience.
"""
from __future__ import annotations

import hashlib

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..context import ServiceError, served, service_health
from ..http import JSONBody

NAME = "deploy"
HOST = "deploy.internal"
PORT = 8085

# The canary reports; it never blocks (observability.py). A breach is therefore
# a deployment status of "error", not a refusal.
CANARY_STATE = {"clean": "success", "breached": "error", "unavailable": "error"}
# `tools.DeployHistory` caps `limit` at 200; the native surface stays inside the
# bound the MCP schema serves, and the status log follows it.
MAX_HISTORY = 200
# GitHub's deployment-status vocabulary. The module stores whatever string it is
# given - a verdict is the operator's word - so the vendor's list is checked
# here, where the vendor shape is.
STATES = ("success", "failure", "error", "inactive", "pending", "queued", "in_progress")

app = FastAPI(title="Alkera release API (fake)", version="0.1.0")


@app.exception_handler(ServiceError)
async def _service_error(request: Request, exc: ServiceError) -> JSONResponse:
    """GitHub answers with `message` and a docs link."""
    return JSONResponse(
        status_code=exc.status,
        content={
            "message": exc.message,
            "documentation_url": f"http://{HOST}:{PORT}/docs#{exc.code}",
        },
    )


def digest(entry: dict) -> str:
    key = f"{entry['deploy']}|{entry['started_at']}|{entry['finished_at']}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def deployment_id(entry: dict) -> int:
    return int(digest(entry)[:8], 16)


def deployment_of(entry: dict, owner: str, repo: str) -> dict:
    identifier = deployment_id(entry)
    base = f"http://{HOST}:{PORT}/repos/{owner}/{repo}/deployments/{identifier}"
    return {
        "id": identifier,
        "sha": digest(entry)[:40],
        "ref": entry["deploy"],
        "task": "deploy",
        "environment": entry["deploy"],
        "description": f"{entry['kind']} deploy, canary {entry['canary_status']}",
        "creator": {"login": "agent"},
        "created_at": entry["started_at"],
        "updated_at": entry["finished_at"],
        "url": base,
        "statuses_url": f"{base}/statuses",
        "payload": {"deploy": entry["deploy"], "full_refresh": entry["full_refresh"]},
        "transient_environment": False,
        "production_environment": True,
        # Not a GitHub field. The canary verdict is the whole point of a deploy
        # here and burying it in a status description would make it unreadable.
        "alkera_canary": {
            "status": entry["canary_status"],
            "newly_firing": entry["newly_firing"],
            "cleared": entry["cleared"],
            "returncode": entry["returncode"],
        },
    }


def recorded_status(entry: dict, deployment: dict) -> dict:
    """One recorded verdict -> a GitHub deployment status."""
    return {
        "id": deployment["id"],
        "state": entry["state"],
        "description": entry["description"],
        "environment": entry["deploy"],
        "creator": {"login": entry["actor"]},
        "created_at": entry["at"],
        "updated_at": entry["at"],
        "target_url": None,
    }


def automatic_status(entry: dict, owner: str, repo: str) -> dict:
    """The status the run itself produced: the canary verdict, plus a failed
    command overriding it - a deploy whose command exited non-zero is a failure
    however clean the canary looked."""
    state = CANARY_STATE.get(entry["canary_status"], "error")
    if entry.get("returncode"):
        state = "failure"
    return {
        "id": deployment_id(entry),
        "state": state,
        "description": f"canary {entry['canary_status']}",
        "environment": entry["deploy"],
        "creator": {"login": "alkera-release"},
        "created_at": entry["finished_at"],
        "updated_at": entry["finished_at"],
        "target_url": None,
    }


def history(request: Request, deploy: str | None, limit: int = 50) -> list[dict]:
    page = max(1, min(limit, MAX_HISTORY))
    return served(
        request, NAME, "deploy_history",
        lambda ops, obs: ops.deploy_history(deploy, page, obs),
        {"deploy": deploy, "limit": page},
    )["deploys"]


def find(request: Request, identifier: int) -> dict:
    for entry in history(request, None):
        if deployment_id(entry) == identifier:
            return entry
    raise ServiceError(404, "not_found", f"No deployment with id {identifier}")


@app.get("/health")
def health(request: Request) -> object:
    """The deployable environments this service can serve - NOT the deployments
    recorded so far, which is 0 in every fresh episode and would report a
    working release surface as a dead one."""
    return service_health(
        request, NAME, lambda ops, obs: len(ops.deploy_list(obs)["deploys"])
    )


@app.get("/repos/{owner}/{repo}/environments")
def list_environments(owner: str, repo: str, request: Request) -> dict:
    """One environment per declared deploy, carrying the canary it checks."""
    listing = served(request, NAME, "deploy_list", lambda ops, obs: ops.deploy_list(obs))
    return {
        "total_count": len(listing["deploys"]),
        "environments": [
            {
                "id": index,
                "name": deploy["id"],
                "description": deploy["title"],
                "protection_rules": [],
                "alkera_deploy": {
                    "kind": deploy["kind"],
                    "selector": deploy["selector"],
                    "statements": deploy["statements"],
                    "canary": deploy["canary"],
                },
            }
            for index, deploy in enumerate(listing["deploys"], start=1)
        ],
    }


@app.get("/repos/{owner}/{repo}/deployments")
def list_deployments(owner: str, repo: str, request: Request,
                     environment: str | None = None, per_page: int = 50) -> list[dict]:
    """`environment` is the deploy name. `ref`, `sha` and `task` are not served:
    this world's deploys are not keyed to commits."""
    return [deployment_of(entry, owner, repo)
            for entry in history(request, environment, per_page)]


@app.get("/repos/{owner}/{repo}/deployments/{identifier}")
def get_deployment(owner: str, repo: str, identifier: int, request: Request) -> dict:
    return deployment_of(find(request, identifier), owner, repo)


@app.post("/repos/{owner}/{repo}/deployments", status_code=201)
def create_deployment(owner: str, repo: str, request: Request, body: JSONBody) -> JSONResponse:
    """Run a deploy. `payload.deploy` names it; `ref` and `environment` are
    accepted as the same name so the three spellings a client might use agree.

    A canary breach is reported, not refused (ground rule 5: destructive
    operations are flagged, never blocked), so a breached deploy is still 201
    with an `error` status attached.
    """
    payload = body.get("payload") or {}
    name = payload.get("deploy") or body.get("environment") or body.get("ref")
    if not name:
        raise ServiceError(422, "invalid_deployment",
                           "name the deploy in payload.deploy, environment or ref")
    full_refresh = bool(payload.get("full_refresh"))
    variables = payload.get("variables") or {}
    if not isinstance(variables, dict):
        raise ServiceError(422, "invalid_deployment", "payload.variables must be an object")
    result = served(
        request, NAME, "deploy_run",
        lambda ops, obs: ops.deploy_run(name, full_refresh, variables, obs),
        {"deploy": name, "full_refresh": full_refresh, "variables": variables},
    )
    deployment = deployment_of(result, owner, repo)
    return JSONResponse(
        status_code=201,
        content={**deployment, "alkera_status": automatic_status(result, owner, repo)},
    )


@app.get("/repos/{owner}/{repo}/deployments/{identifier}/statuses")
def list_statuses(owner: str, repo: str, identifier: int, request: Request) -> list[dict]:
    """The run's own status first, then every verdict recorded against the deploy.

    Two rows, because two questions are asked of the world: `deploy_history` to
    find the deployment behind the id, and `deploy_statuses` to read the
    verdicts. That is what an MCP agent would have paid for the same answer.
    """
    entry = find(request, identifier)
    deployment = deployment_of(entry, owner, repo)
    recorded = served(
        request, NAME, "deploy_statuses",
        lambda ops, obs: ops.deploy_statuses(entry["deploy"], MAX_HISTORY, obs),
        {"deploy": entry["deploy"], "limit": MAX_HISTORY},
    )["statuses"]
    return [automatic_status(entry, owner, repo),
            *(recorded_status(item, deployment) for item in recorded)]


@app.post("/repos/{owner}/{repo}/deployments/{identifier}/statuses", status_code=201)
def create_status(owner: str, repo: str, identifier: int,
                        request: Request, body: JSONBody) -> JSONResponse:
    """Record an operator's verdict on a deployment.

    Audited as a write against `deploy:<name>` - the same target `deploy_run`
    names, so the row joins to the deploy it judges - and persisted by
    `deploy_status`, so a later read finds it whether or not this process is
    still running.
    """
    state = body.get("state")
    if state not in STATES:
        raise ServiceError(422, "invalid_status", f"not a deployment state: {state!r}")
    entry = find(request, identifier)
    deployment = deployment_of(entry, owner, repo)
    recorded = served(
        request, NAME, "deploy_status",
        lambda ops, obs: ops.deploy_status(
            entry["deploy"], state, body.get("description") or "",
            body.get("creator") or "agent", obs),
        {"deploy": entry["deploy"], "state": state},
    )
    return JSONResponse(status_code=201, content=recorded_status(recorded, deployment))
