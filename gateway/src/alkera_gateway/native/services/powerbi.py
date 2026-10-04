"""Power BI / Fabric REST - the subset a data engineer's CI/CD and checks use.

Why a fake Power BI (gateway/NATIVE.md, docs/environments/BI_BUILD_PLAN.md):
on a team that ships Power BI the semantic model is a PBIP project in git,
edited as TMDL text, deployed by a pipeline through the Fabric item-definition
API, refreshed through the Power BI REST API, and checked by running DAX
against the published dataset. This module is that service, over the world's
warehouse and the `world.semantic` core, so the numbers it answers are the
numbers the grader recomputes from the files.

ENDPOINTS (one host serves the three Microsoft hosts' paths; none collide):

  login.microsoftonline.com   POST /{tenant}/oauth2/v2.0/token   client_credentials
                              POST /{tenant}/oauth2/token        (v1, resource=)
  api.powerbi.com             GET  /v1.0/myorg/groups[?$filter=name eq '..'&$top=]
                              GET  /v1.0/myorg/groups/{g}/datasets[/{d}]
                              POST /v1.0/myorg/groups/{g}/datasets/{d}/refreshes      202
                              GET  /v1.0/myorg/groups/{g}/datasets/{d}/refreshes[?$top=]
                              GET  /v1.0/myorg/groups/{g}/datasets/{d}/refreshes/{id}
                              POST /v1.0/myorg/groups/{g}/datasets/{d}/executeQueries
                              GET  /v1.0/myorg/groups/{g}/reports
  api.fabric.microsoft.com    GET  /v1/workspaces
                              GET  /v1/workspaces/{w}/semanticModels[/{id}]
                              POST /v1/workspaces/{w}/semanticModels/{id}/getDefinition
                              POST /v1/workspaces/{w}/semanticModels/{id}/updateDefinition
  /health                     like every native service

AUTH. Entra client credentials: any tenant, any client id, the workspace's
obviously-fake secret (`alkera-local-dev`, context.DEV_TOKEN), a scope of
`https://analysis.windows.net/powerbi/api/.default` or
`https://api.fabric.microsoft.com/.default`. The token is a JWT-shaped string
HMAC-signed with that secret (alg HS256 - honest about what it is), stamped on
the world's virtual clock with a 3599 s lifetime; every API call needs it as
`Authorization: Bearer`. Expiry is enforced against the virtual clock, so a
token outlives a request but not an `advance`. Both audiences are accepted by
both APIs (the real Fabric API also takes Power BI-audience tokens).

STATE lives outside the world tree, in `<bi_state_dir>/powerbi/` (bi_state_dir
defaults to `<audit_dir>/bi`, like the Metabase tiles in bi.py):
`state.json` (workspace, published semantic models as definition parts,
reports, refresh history) and `snapshots/<datasetId>.duckdb`.

PUBLISHED STATE starts as the PBIP project in the workspace under
`bi/powerbi/` (`*.pbip`, `*.Report/`, `*.SemanticModel/`) AS FIRST COMMITTED to
the workspace's git repository - the version a deployment pipeline would have
shipped at provisioning - falling back to the working tree when there is no
such commit. Read once, on the first request (the harness's /health probe at
episode start), never again: editing the TMDL locally changes nothing in the
service until the agent deploys it with updateDefinition, exactly as at work.

IMPORT MODE, decided: datasets are import-mode models. A refresh copies every
warehouse relation the published model reads into the dataset's snapshot
(a DuckDB file, the "VertiPaq cache"), and executeQueries evaluates against
that snapshot with world.semantic.render, never against the live warehouse.
So a late partition is invisible until the next refresh - the stale-publish
archetype behaves as it does at work. Provisioning takes the first snapshot
and records it as a `Scheduled` refresh at the virtual time it happened.
Measures are evaluated at query time (as in VertiPaq), so a measure-only
deployment takes effect immediately; a deployment that changes a table's
source or columns leaves that table unprocessed, and queries are refused with
a "needs refresh" error until a refresh reloads it. Every refresh records the
values of every visual of every report bound to the dataset (rendered by the
engine on the new snapshot) - evidence of what was published when, like
BIStore.refresh's events. Graders still recompute from files.

ROWS. Every request writes exactly one 9.2.2 row through native.context's
Ledger, failures and refused credentials included: `powerbi.token`,
`powerbi.groups_list`, `powerbi.datasets_list`, `powerbi.dataset_get`,
`powerbi.refresh`, `powerbi.refreshes_list`, `powerbi.refresh_get`,
`powerbi.execute_queries`, `powerbi.reports_list`, `powerbi.workspaces_list`,
`powerbi.semantic_models_list`, `powerbi.semantic_model_get`,
`powerbi.get_definition`, `powerbi.update_definition`, `powerbi.health`.
There is no MCP twin to borrow a classification from, so PowerBIStore - the
module the handlers call, like bi.BIStore - fills the observation: targets
`powerbi:workspace:<name>`, `powerbi:semantic_model:<name>`,
`powerbi:report:<name>`, `powerbi:token`; `op_class` `write` for refresh and
updateDefinition; a refresh's `read_targets` are the warehouse relations it
copied (`warehouse.fct_orders`), a query's the semantic model it read.

ERRORS in each vendor's own shape: Power BI `{"error": {"code", "message"}}`
(DAX failures as DatasetExecuteQueriesError with a DetailsMessage), Fabric
`{"requestId", "errorCode", "message"}` (an updateDefinition the model reader
refuses is `Workload_FailedToParseFile`, the code Fabric uses), Entra
`{"error", "error_description", "error_codes", ...}` with AADSTS codes.
Anything outside the DAX/TMDL subset is refused with the Unsupported message
(world.semantic rule 1: loud, never silently different).

Sources: Power BI REST API reference (learn.microsoft.com/rest/api/power-bi/:
groups/get-groups, datasets/get-datasets-in-group, get-dataset-in-group,
refresh-dataset-in-group, get-refresh-history-in-group,
get-refresh-execution-details-in-group, execute-queries-in-group,
reports/get-reports-in-group); Fabric REST (learn.microsoft.com/rest/api/
fabric/: core/workspaces/list-workspaces, semanticmodel/items/list-semantic-
models, get-semantic-model, get-semantic-model-definition,
update-semantic-model-definition); Microsoft identity platform client
credentials flow and AADSTS error codes (learn.microsoft.com/entra/
identity-platform/v2-oauth2-client-creds-grant-flow, reference-error-codes);
Fabric error bodies as reported in github.com/microsoft/fabric-cicd issue 534
and microsoft/Analysis-Services issue 378.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import json
import os
import subprocess
import tarfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import duckdb
import sqlglot
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlglot import exp

from ...records import Observation, ToolError, canonical, digest, json_value
from ..context import DEV_TOKEN, Context, Ledger, audit_unrouted, context_of, service_health
from ..http import JSONBody, RawBody

NAME = "powerbi"
HOST = "powerbi.internal"
PORT = 8095

#: Where the world keeps the team's Power BI project, relative to the workspace.
PROJECT_DIR = "bi/powerbi"
#: The one Power BI workspace (group) the world's semantic models are published to.
WORKSPACE_NAME = "Analytics"
#: Who the datasets say configured them: the deployment service principal.
CONFIGURED_BY = "svc-powerbi-deploy@alkera.local"
POWER_BI_AUDIENCE = "https://analysis.windows.net/powerbi/api"
FABRIC_AUDIENCE = "https://api.fabric.microsoft.com"
AUDIENCES = (POWER_BI_AUDIENCE, FABRIC_AUDIENCE)
TOKEN_LIFETIME_S = 3599
REFRESH_HISTORY_DEFAULT = 60

app = FastAPI(title="Power BI REST (alkera native surface)", docs_url=None, redoc_url=None,
              openapi_url=None)
# One row per request, including the ones no handler ran for (context.py).
audit_unrouted(app, NAME)

_STATE_LOCK = threading.RLock()


# --------------------------------------------------------------------------
# Errors, in three vendors' shapes
# --------------------------------------------------------------------------
class ApiError(Exception):
    """A refusal on its way out, already in its vendor's body shape. `code` is
    what the 9.2.2 row records."""

    def __init__(self, status: int, code: str, message: str, body: dict,
                 headers: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.body, self.headers = body, headers or {}


def powerbi_error(status: int, code: str, message: str, headers: dict | None = None
                  ) -> ApiError:
    return ApiError(status, code, message, {"error": {"code": code, "message": message}},
                    headers)


def powerbi_entity_error(status: int, code: str, message: str, headers: dict | None = None
                         ) -> ApiError:
    """The `pbi.error` envelope Power BI uses for entity and authorization
    failures, which carries no message of its own."""
    return ApiError(status, code, message, {"error": {
        "code": code, "pbi.error": {"code": code, "parameters": {}, "details": [],
                                    "exceptionCulprit": 1}}}, headers)


def dax_error(message: str) -> ApiError:
    return ApiError(400, "DatasetExecuteQueriesError", message, {"error": {
        "code": "DatasetExecuteQueriesError",
        "pbi.error": {"code": "DatasetExecuteQueriesError", "parameters": {},
                      "details": [{"code": "DetailsMessage",
                                   "detail": {"type": 1, "value": message}},
                                  {"code": "AnalysisServicesErrorCode",
                                   "detail": {"type": 1, "value": "3238002695"}}],
                      "exceptionCulprit": 1}}})


def fabric_error(status: int, code: str, message: str, headers: dict | None = None
                 ) -> ApiError:
    return ApiError(status, code, message, {"requestId": str(uuid.uuid4()), "errorCode": code,
                                            "message": message}, headers)


def entra_error(status: int, error: str, code: int, description: str) -> ApiError:
    trace = str(uuid.uuid4())
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%SZ")
    text = (f"AADSTS{code}: {description} Trace ID: {trace} Correlation ID: "
            f"{uuid.uuid4()} Timestamp: {stamp}")
    return ApiError(status, error, text, {
        "error": error, "error_description": text, "error_codes": [code], "timestamp": stamp,
        "trace_id": trace, "correlation_id": str(uuid.uuid4()),
        "error_uri": f"https://login.microsoftonline.com/error?code={code}"})


@app.exception_handler(ApiError)
async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(exc.body, status_code=exc.status, headers=exc.headers)


@app.middleware("http")
async def _request_id(request: Request, call_next):
    """Power BI stamps every response with a RequestId header."""
    response = await call_next(request)
    response.headers.setdefault("RequestId", str(uuid.uuid4()))
    return response


# --------------------------------------------------------------------------
# The world.semantic core (the image puts world/ on PYTHONPATH, as for the
# WorldScheduler in scheduling.py)
# --------------------------------------------------------------------------
def semantic():
    try:
        from world.semantic import errors
        from world.semantic.formats.powerbi import daxquery, mquery, pbir, project, tmdl
    except ImportError as exc:                       # pragma: no cover
        raise RuntimeError(
            f"the Power BI service evaluates models with world.semantic, which is not "
            f"importable: {exc}. Put the repository's world/ directory on PYTHONPATH."
        ) from exc
    return errors, daxquery, mquery, pbir, project, tmdl


def engine():
    """world.semantic.render, or None while the engine is not installed."""
    try:
        from world.semantic import render
    except ImportError:
        return None
    return render


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def epoch(moment: str) -> int:
    return int(datetime.fromisoformat(moment.replace("Z", "+00:00")).timestamp())


def mint(tenant: str, client_id: str, audience: str, now: int) -> str:
    header = {"typ": "JWT", "alg": "HS256"}
    claims = {"aud": audience, "iss": f"https://sts.windows.net/{tenant}/", "iat": now,
              "nbf": now, "exp": now + TOKEN_LIFETIME_S, "appid": client_id, "appidacr": "1",
              "idtyp": "app", "oid": str(uuid.uuid5(uuid.NAMESPACE_URL, client_id)),
              "tid": tenant, "ver": "1.0"}
    signing = (_b64url(json.dumps(header, separators=(",", ":")).encode()) + "."
               + _b64url(json.dumps(claims, separators=(",", ":")).encode()))
    signature = hmac.new(DEV_TOKEN.encode(), signing.encode(), hashlib.sha256).digest()
    return f"{signing}.{_b64url(signature)}"


def verify(token: str) -> dict | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    expected = hmac.new(DEV_TOKEN.encode(), f"{parts[0]}.{parts[1]}".encode(),
                        hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(_unb64url(parts[2]), expected):
            return None
        return json.loads(_unb64url(parts[1]))
    except (ValueError, binascii.Error):
        return None


def authenticate(request: Request, family: str) -> dict:
    """The bearer token's claims, or the vendor's 401/403."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    claims = verify(token.strip()) if scheme.lower() == "bearer" else None
    challenge = {"WWW-Authenticate": 'Bearer authorization_uri="https://login.microsoftonline'
                                     '.com/common/oauth2/authorize", error="invalid_token"'}
    if claims is None:
        if family == "fabric":
            code = "TokenNotProvided" if not header else "InvalidToken"
            raise fabric_error(401, code, "Access token is missing or invalid", challenge)
        raise powerbi_entity_error(401, "PowerBINotAuthorizedException",
                                   "the request carried no valid bearer token", challenge)
    if claims.get("aud") not in AUDIENCES:
        if family == "fabric":
            raise fabric_error(401, "InvalidToken", "The token audience is not valid")
        raise powerbi_entity_error(403, "PowerBINotAuthorizedException",
                                   "token audience is not Power BI")
    now = epoch(context_of(request.app).clock())
    if claims.get("exp", 0) <= now:
        if family == "fabric":
            raise fabric_error(401, "TokenExpired", "Access token has expired, resubmit with "
                                                    "a new access token")
        raise powerbi_error(403, "TokenExpired", "Access token has expired, resubmit with a "
                                                 "new access token")
    return claims


# --------------------------------------------------------------------------
# The store: published models, refreshes, snapshots
# --------------------------------------------------------------------------
def guid(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "powerbi.internal/" + "/".join(parts)))


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str) -> bytes:
    return base64.b64decode(text, validate=True)


def git_project(workspace: Path) -> dict[str, bytes] | None:
    """bi/powerbi as of the first commit that added it, or None."""
    git = ["git", "-c", "safe.directory=*", "-C", str(workspace)]
    try:
        commits = subprocess.run(git + ["rev-list", "--reverse", "HEAD", "--", PROJECT_DIR],
                                 capture_output=True, check=True, timeout=30).stdout.split()
        if not commits:
            return None
        archive = subprocess.run(git + ["archive", "--format=tar", commits[0].decode(),
                                        PROJECT_DIR], capture_output=True, check=True,
                                 timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    out = {}
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar.getmembers():
            if member.isfile():
                rel = member.name[len(PROJECT_DIR) + 1:]
                handle = tar.extractfile(member)
                if handle is not None and rel:
                    out[rel] = handle.read()
    return out


def tree_project(workspace: Path) -> dict[str, bytes]:
    root = workspace / PROJECT_DIR
    if not root.is_dir():
        return {}
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def items(files: dict[str, bytes], suffix: str) -> dict[str, dict[str, bytes]]:
    """{"Commerce.SemanticModel": {relative path: bytes}} for each item folder,
    without Desktop's local-only `.pbi/` files."""
    out: dict[str, dict[str, bytes]] = {}
    for path, data in files.items():
        head, _, rest = path.partition("/")
        if head.endswith(suffix) and rest and not rest.startswith(".pbi/"):
            out.setdefault(head, {})[rest] = data
    return out


def display_name(parts: dict[str, bytes], folder: str, suffix: str) -> str:
    try:
        meta = json.loads(parts[".platform"].decode("utf-8-sig"))["metadata"]
        return meta.get("displayName") or folder.removesuffix(suffix)
    except (KeyError, ValueError, UnicodeDecodeError):
        return folder.removesuffix(suffix)


class PowerBIStore:
    """The published side of Power BI for one world. Like bi.BIStore it is the
    module the handlers call, and it fills the 9.2.2 observation."""

    def __init__(self, context: Context):
        self.config = context.config
        self.clock = context.clock
        self.dir = Path(self.config.bi_state_dir) / "powerbi"
        self.path = self.dir / "state.json"

    # ------------------------------------------------------------ state
    def load(self) -> dict:
        with _STATE_LOCK:
            if self.path.exists():
                return json.loads(self.path.read_text(encoding="utf-8"))
            state = self.provision()
            self.save(state)
            return state

    def save(self, state: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_bytes(canonical(state))
        temporary.replace(self.path)

    def provision(self) -> dict:
        workspace = Path(self.config.workspace)
        files = git_project(workspace)
        if files is None:
            files = tree_project(workspace)
        now = self.clock()
        group = {"id": guid("workspace", WORKSPACE_NAME), "name": WORKSPACE_NAME}
        datasets, folder_ids = {}, {}
        for folder, parts in sorted(items(files, ".SemanticModel").items()):
            name = display_name(parts, folder, ".SemanticModel")
            dataset_id = guid("dataset", name)
            folder_ids[folder] = dataset_id
            datasets[dataset_id] = {"id": dataset_id, "name": name, "createdDate": now,
                                    "parts": {p: b64(d) for p, d in sorted(parts.items())},
                                    "refreshes": [], "processed": {}}
        reports = {}
        for folder, parts in sorted(items(files, ".Report").items()):
            name = display_name(parts, folder, ".Report")
            target = None
            try:
                doc = json.loads(parts["definition.pbir"].decode("utf-8-sig"))
                path = doc["datasetReference"]["byPath"]["path"]
                target = folder_ids.get(path.replace("\\", "/").rstrip("/").split("/")[-1])
            except (KeyError, ValueError, TypeError, UnicodeDecodeError):
                target = None
            report_id = guid("report", name)
            reports[report_id] = {"id": report_id, "name": name, "datasetId": target,
                                  "parts": {p: b64(d) for p, d in sorted(parts.items())}}
        state = {"workspace": group, "datasets": datasets, "reports": reports}
        for dataset in datasets.values():
            self.refresh_dataset(state, dataset, "Scheduled", Observation())
        return state

    # --------------------------------------------------------- lookups
    def group(self, state: dict, group_id: str, observation: Observation) -> dict:
        group = state["workspace"]
        if group_id.lower() != group["id"]:
            observation.targets = [f"powerbi:workspace:{group_id}"]
            raise powerbi_entity_error(404, "PowerBIEntityNotFound",
                                       f"workspace {group_id} not found")
        return group

    def dataset(self, state: dict, group_id: str, dataset_id: str,
                observation: Observation, fabric: bool = False) -> dict:
        group = state["workspace"]
        if group_id.lower() != group["id"]:
            observation.targets = [f"powerbi:workspace:{group_id}"]
            if fabric:
                raise fabric_error(404, "WorkspaceNotFound", "The requested workspace was "
                                                             "not found")
            raise powerbi_entity_error(404, "PowerBIEntityNotFound",
                                       f"workspace {group_id} not found")
        dataset = state["datasets"].get(dataset_id.lower())
        if dataset is None:
            observation.targets = [f"powerbi:semantic_model:{dataset_id}"]
            if fabric:
                raise fabric_error(404, "ItemNotFound", "The requested item was not found")
            raise powerbi_error(404, "ItemNotFound", f"Dataset {dataset_id} is not found! "
                                                     f"please verify datasetId is correct and "
                                                     f"user have sufficient permissions.")
        return dataset

    @staticmethod
    def target(dataset: dict) -> str:
        return f"powerbi:semantic_model:{dataset['name']}"

    def model_of(self, dataset: dict):
        _, _, _, _, _, tmdl = semantic()
        files = {path: unb64(data) for path, data in dataset["parts"].items()}
        model, _info = tmdl.read_semantic_model(files, dataset["name"])
        return model

    def reports_of(self, state: dict, dataset: dict, model) -> list:
        _, _, _, pbir, _, tmdl = semantic()
        out = []
        files = {path: unb64(data) for path, data in dataset["parts"].items()}
        _, info = tmdl.read_semantic_model(files, dataset["name"])
        for report in sorted(state["reports"].values(), key=lambda r: r["name"]):
            if report["datasetId"] != dataset["id"]:
                continue
            parts = {path: unb64(data) for path, data in report["parts"].items()}
            out.append((report, lambda parts=parts, name=report["name"]:
                        pbir.read_report(parts, model, info, name)))
        return out

    # ---------------------------------------------------------- refresh
    def snapshot_path(self, dataset: dict) -> Path:
        return self.dir / "snapshots" / f"{dataset['id']}.duckdb"

    @staticmethod
    def signature(table) -> str:
        return digest([table.source, table.source_sql,
                       [[c.name, c.source, c.data_type, repr(c.expr)] for c in table.columns]])

    def relations(self, model) -> list[tuple[str, str]]:
        errors, *_ = semantic()
        found = set()
        for table in model.tables:
            if table.source is not None:
                schema, _, name = table.source.partition(".")
                found.add((schema, name))
                continue
            try:
                tree = sqlglot.parse_one(table.source_sql, read="duckdb")
            except sqlglot.errors.ParseError as exc:
                raise errors.ModelError(f"table {table.name!r}: native query does not "
                                        f"parse: {exc}") from None
            ctes = {cte.alias for cte in tree.find_all(exp.CTE)}
            for ref in tree.find_all(exp.Table):
                if ref.name in ctes and not ref.db:
                    continue
                if not ref.db:
                    raise errors.Unsupported("native query over an unqualified table",
                                             "Power BI", f"{table.name}: {ref.name}")
                found.add((ref.db, ref.name))
        return sorted(found)

    def take_snapshot(self, dataset: dict, relations) -> None:
        if self.config.warehouse_backend != "duckdb":
            errors, *_ = semantic()
            raise errors.Unsupported(f"refresh from a {self.config.warehouse_backend} "
                                     f"warehouse", "Power BI")
        path = self.snapshot_path(dataset)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.stem + ".loading.duckdb")
        if temporary.exists():
            temporary.unlink()
        warehouse = str(self.config.warehouse).replace("'", "''")
        try:
            with duckdb.connect(str(temporary), config={"threads": 1}) as con:
                con.execute(f"ATTACH '{warehouse}' AS wh (READ_ONLY)")
                for schema, name in relations:
                    q_schema = '"' + schema.replace('"', '""') + '"'
                    q_name = '"' + name.replace('"', '""') + '"'
                    con.execute(f"CREATE SCHEMA IF NOT EXISTS {q_schema}")
                    con.execute(f"CREATE TABLE {q_schema}.{q_name} AS "
                                f"SELECT * FROM wh.{q_schema}.{q_name}")
                con.execute("DETACH wh")
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def refresh_dataset(self, state: dict, dataset: dict, refresh_type: str,
                        observation: Observation) -> dict:
        """One synchronous refresh: snapshot, record, render. A failure keeps
        the previous snapshot (Power BI commits a refresh transactionally)."""
        errors, *_ = semantic()
        now = self.clock()
        number = len(dataset["refreshes"]) + 1
        request_id = guid(dataset["id"], "refresh", str(number))
        event: dict[str, Any] = {"id": number, "requestId": request_id,
                                 "refreshType": refresh_type, "startTime": now, "endTime": now,
                                 "status": "Completed",
                                 "definitionHash": digest(dataset["parts"])}
        try:
            model = self.model_of(dataset)
            relations = self.relations(model)
            observation.read_targets = [f"{s}.{n}" for s, n in relations]
            self.take_snapshot(dataset, relations)
            dataset["processed"] = {t.name: self.signature(t) for t in model.tables}
            event["values"] = self.render_reports(state, dataset, model)
        except (errors.SemanticError, duckdb.Error) as exc:
            event["status"] = "Failed"
            event["serviceExceptionJson"] = json.dumps(
                {"errorCode": "ModelRefresh_ShortMessage_ProcessingError",
                 "errorDescription": str(exc)}, separators=(",", ":"))
        dataset["refreshes"].append(event)
        return event

    def render_reports(self, state: dict, dataset: dict, model) -> dict:
        errors, *_ = semantic()
        render = engine()
        values: dict[str, Any] = {}
        for report, read in self.reports_of(state, dataset, model):
            if render is None:
                values[report["name"]] = {"error": "world.semantic.render is not installed"}
                continue
            try:
                values[report["name"]] = json_value(
                    render.render_report(model, read(), self.snapshot_path(dataset)))
            except errors.SemanticError as exc:
                values[report["name"]] = {"error": str(exc)}
        return values

    # ---------------------------------------------------------- queries
    def execute(self, state: dict, dataset: dict, text: str, include_nulls: bool,
                observation: Observation) -> dict:
        errors, daxquery, *_ = semantic()
        observation.targets = observation.read_targets = [self.target(dataset)]
        try:
            model = self.model_of(dataset)
        except errors.SemanticError as exc:
            raise dax_error(f"The semantic model cannot be loaded: {exc}") from None
        try:
            query = daxquery.parse_query(text, model)
        except errors.SemanticError as exc:
            raise dax_error(str(exc)) from None
        processed = dataset.get("processed", {})
        for table in model.tables:
            if processed.get(table.name) != self.signature(table):
                raise dax_error(f"The table '{table.name}' has not been processed since the "
                                f"semantic model was last deployed. Refresh the semantic "
                                f"model and run the query again.")
        render = engine()
        if render is None:
            raise powerbi_error(503, "ServiceUnavailable",
                                "the query engine (world.semantic.render) is not installed")
        snapshot = self.snapshot_path(dataset)
        try:
            rendered = render.render_visual(query.model, query.visual, snapshot)
        except errors.SemanticError as exc:
            raise dax_error(str(exc)) from None
        return daxquery.execute_queries_result(query, rendered, include_nulls=include_nulls)

    # ------------------------------------------------------- definitions
    def update_definition(self, state: dict, dataset: dict, parts: list,
                          update_metadata: bool, observation: Observation) -> None:
        errors, *_, tmdl = semantic()
        observation.op_class = "write"
        observation.targets = [self.target(dataset)]
        files: dict[str, bytes] = {}
        for part in parts:
            if not isinstance(part, dict) or not isinstance(part.get("path"), str):
                raise fabric_error(400, "InvalidDefinition", "Each definition part needs a "
                                                             "path and a payload")
            if part.get("payloadType", "InlineBase64") != "InlineBase64":
                raise fabric_error(400, "InvalidDefinition", f"Unsupported payloadType "
                                                             f"{part.get('payloadType')}")
            try:
                files[part["path"].lstrip("/")] = unb64(part.get("payload", ""))
            except (binascii.Error, ValueError):
                raise fabric_error(400, "CorruptedPayload", "The provided payload is "
                                                            "corrupted.") from None
        try:
            model, _ = tmdl.read_semantic_model(files, dataset["name"])
            render = engine()
            if render is not None and hasattr(render, "validate"):
                render.validate(model, ())
        except errors.SemanticError as exc:
            raise fabric_error(400, "Workload_FailedToParseFile",
                               f"Dataset Workload failed to import the dataset with dataset "
                               f"id {dataset['id']}. {exc}") from None
        if ".platform" not in files and ".platform" in dataset["parts"]:
            files[".platform"] = unb64(dataset["parts"][".platform"])
        if update_metadata and ".platform" in files:
            dataset["name"] = display_name(files, dataset["name"], "")
        dataset["parts"] = {p: b64(d) for p, d in sorted(files.items())}
        observation.targets = [self.target(dataset)]


def store_of(request: Request) -> PowerBIStore:
    return PowerBIStore(context_of(request.app))


def reads_what_it_targets(run, store: PowerBIStore, observation: Observation) -> Any:
    """Run one store call; a READ row then names what it read.

    The handlers below say what a request touched in `targets` and leave
    `read_targets` to the few calls that read something else (a refresh reads
    warehouse relations, a DAX query reads the semantic model). The Ledger
    writes `read_targets` verbatim, exactly as the MCP gateway does, so before
    this every Power BI read - the token, groups, datasets, reports, refresh
    history, getDefinition - wrote `read_targets: []` while Jira, Slack,
    Sigma and Tableau name the object they read (measured 2026-10-04,
    tasks/harness/bi_e2e_check.py). Filled in before the observation is
    sealed, on success and on refusal alike, and only for `read` rows whose
    call left `read_targets` empty."""
    try:
        return run(store, observation)
    finally:
        if observation.op_class == "read" and not observation.read_targets:
            observation.read_targets = list(observation.targets)


def handled(request: Request, tool: str, args: dict, family: str, run) -> Any:
    """One request, one row: authenticate inside the span (so a refused
    credential is a row too), then run the store call that classifies it."""
    ledger = Ledger(request, NAME)
    with ledger.span(tool, **args) as row:
        claims = authenticate(request, family)
        ledger.actor = claims.get("appid", "")
        return row.run(lambda ops, obs: reads_what_it_targets(run, store_of(request), obs))


def base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


# --------------------------------------------------------------------------
# Entra ID
# --------------------------------------------------------------------------
def token_response(request: Request, tenant: str, raw: bytes, version: int) -> dict:
    form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
    ledger = Ledger(request, NAME)
    args = {"tenant": tenant, "grant_type": form.get("grant_type"),
            "client_id": form.get("client_id"),
            "scope": form.get("scope") if version == 2 else form.get("resource")}
    with ledger.span("token", **args) as row:
        ledger.actor = form.get("client_id") or ""

        def issue(store: PowerBIStore, observation: Observation) -> dict:
            observation.targets = ["powerbi:token"]
            if form.get("grant_type") != "client_credentials":
                raise entra_error(400, "unsupported_grant_type", 70003,
                                  "The app requested an unsupported grant type "
                                  f"'{form.get('grant_type')}'.")
            if not form.get("client_id"):
                raise entra_error(400, "invalid_request", 900144,
                                  "The request body must contain the following parameter: "
                                  "'client_id'.")
            if version == 2:
                scope = form.get("scope") or ""
                if not scope:
                    raise entra_error(400, "invalid_request", 90014,
                                      "The required field 'scope' is missing from the "
                                      "credential.")
                if not scope.endswith("/.default"):
                    raise entra_error(400, "invalid_scope", 1002012,
                                      f"The provided value for scope {scope} is not valid. "
                                      f"Client credential flows must have a scope value with "
                                      f"/.default suffixed to the resource identifier "
                                      f"(application ID URI).")
                audience = scope.removesuffix("/.default")
            else:
                audience = (form.get("resource") or "").rstrip("/")
            if audience not in AUDIENCES:
                raise entra_error(400, "invalid_resource", 500011,
                                  f"The resource principal named {audience} was not found in "
                                  f"the tenant named {tenant}.")
            if form.get("client_secret") != DEV_TOKEN:
                raise entra_error(401, "invalid_client", 7000215,
                                  "Invalid client secret provided. Ensure the secret being "
                                  "sent in the request is the client secret value, not the "
                                  "client secret ID, for a secret added to app "
                                  f"'{form.get('client_id')}'.")
            now = epoch(store.clock())
            token = mint(tenant, form["client_id"], audience, now)
            if version == 2:
                return {"token_type": "Bearer", "expires_in": TOKEN_LIFETIME_S,
                        "ext_expires_in": TOKEN_LIFETIME_S, "access_token": token}
            return {"token_type": "Bearer", "expires_in": str(TOKEN_LIFETIME_S),
                    "ext_expires_in": str(TOKEN_LIFETIME_S),
                    "expires_on": str(now + TOKEN_LIFETIME_S), "not_before": str(now),
                    "resource": audience, "access_token": token}

        return row.run(lambda ops, obs: reads_what_it_targets(issue, store_of(request), obs))


@app.post("/{tenant}/oauth2/v2.0/token")
def token_v2(tenant: str, request: Request, raw: RawBody) -> dict:
    return token_response(request, tenant, raw, 2)


@app.post("/{tenant}/oauth2/token")
def token_v1(tenant: str, request: Request, raw: RawBody) -> dict:
    return token_response(request, tenant, raw, 1)


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------
@app.get("/health")
def health(request: Request) -> object:
    """Published semantic models, counted from the service's state."""
    def count(ops, observation: Observation) -> int:
        state = store_of(request).load()
        observation.read_targets = [PowerBIStore.target(d) for d in state["datasets"].values()]
        if not state["datasets"]:
            # Plugged but given no project is a broken world, not an empty
            # workspace: say so the way the other services do (503).
            raise ToolError("no_semantic_models",
                            f"no Power BI project under <workspace>/{PROJECT_DIR}")
        return len(state["datasets"])
    return service_health(request, NAME, count)


# --------------------------------------------------------------------------
# Power BI REST
# --------------------------------------------------------------------------
def group_json(group: dict) -> dict:
    return {"id": group["id"], "isReadOnly": False, "isOnDedicatedCapacity": True,
            "capacityId": guid("capacity", group["name"]).upper(),
            "defaultDatasetStorageFormat": "Small", "type": "Workspace", "name": group["name"]}


def dataset_json(group: dict, dataset: dict) -> dict:
    gid, did = group["id"], dataset["id"]
    return {"id": did, "name": dataset["name"],
            "webUrl": f"https://app.powerbi.com/groups/{gid}/datasets/{did}",
            "addRowsAPIEnabled": False, "configuredBy": CONFIGURED_BY, "isRefreshable": True,
            "isEffectiveIdentityRequired": False, "isEffectiveIdentityRolesRequired": False,
            "isOnPremGatewayRequired": False, "targetStorageMode": "Abf",
            "createdDate": dataset["createdDate"],
            "createReportEmbedURL": f"https://app.powerbi.com/reportEmbed?config=&groupId={gid}",
            "qnaEmbedURL": f"https://app.powerbi.com/qnaEmbed?groupId={gid}&datasetId={did}",
            "upstreamDatasets": [], "users": [],
            "queryScaleOutSettings": {"autoSyncReadOnlyReplicas": True,
                                      "maxReadOnlyReplicas": 0}}


def report_json(group: dict, report: dict) -> dict:
    gid, rid = group["id"], report["id"]
    return {"id": rid, "reportType": "PowerBIReport", "name": report["name"],
            "webUrl": f"https://app.powerbi.com/groups/{gid}/reports/{rid}",
            "embedUrl": f"https://app.powerbi.com/reportEmbed?reportId={rid}&groupId={gid}",
            "isFromPbix": False, "isOwnedByMe": True, "datasetId": report["datasetId"],
            "datasetWorkspaceId": gid, "users": [], "subscriptions": []}


def refresh_json(event: dict) -> dict:
    out = {"id": event["id"], "requestId": event["requestId"],
           "refreshType": event["refreshType"], "startTime": event["startTime"],
           "endTime": event["endTime"], "status": event["status"]}
    attempt = {"attemptId": 1, "startTime": event["startTime"], "endTime": event["endTime"],
               "type": "Data"}
    if "serviceExceptionJson" in event:
        out["serviceExceptionJson"] = event["serviceExceptionJson"]
        attempt["serviceExceptionJson"] = event["serviceExceptionJson"]
        out["refreshAttempts"] = [attempt]
    else:
        out["refreshAttempts"] = [attempt, {"attemptId": 1, "startTime": event["endTime"],
                                            "endTime": event["endTime"], "type": "Query"}]
    return out


def odata(request: Request, fragment: str) -> str:
    return f"{base_url(request)}/v1.0/myorg/$metadata#{fragment}"


def top_skip(request: Request, values: list, default: int | None = None) -> list:
    params = request.query_params
    try:
        skip = int(params.get("$skip", "0"))
        top = int(params["$top"]) if "$top" in params else default
    except ValueError:
        raise powerbi_error(400, "InvalidRequest", "$top and $skip must be integers") from None
    values = values[skip:]
    return values if top is None else values[:max(top, 0)]


@app.get("/v1.0/myorg/groups")
def groups_list(request: Request) -> dict:
    flt = request.query_params.get("$filter")

    def run(store: PowerBIStore, observation: Observation) -> list:
        group = store.load()["workspace"]
        observation.targets = [f"powerbi:workspace:{group['name']}"]
        groups = [group]
        if flt:
            field, op, value = (flt.split(" ", 2) + ["", ""])[:3]
            if field != "name" or op != "eq" or not (value.startswith("'")
                                                     and value.endswith("'")):
                raise powerbi_error(400, "InvalidRequest",
                                    f"unsupported (Power BI): $filter {flt}")
            groups = [g for g in groups if g["name"] == value[1:-1].replace("''", "'")]
        return groups

    groups = handled(request, "groups_list", {"filter": flt}, "powerbi", run)
    values = top_skip(request, [group_json(g) for g in groups])
    return {"@odata.context": odata(request, "groups"), "@odata.count": len(values),
            "value": values}


@app.get("/v1.0/myorg/groups/{group_id}/datasets")
def datasets_list(group_id: str, request: Request) -> dict:
    def run(store: PowerBIStore, observation: Observation):
        state = store.load()
        group = store.group(state, group_id, observation)
        datasets = sorted(state["datasets"].values(), key=lambda d: d["name"])
        observation.targets = [store.target(d) for d in datasets] or [
            f"powerbi:workspace:{group['name']}"]
        return [dataset_json(group, d) for d in datasets]

    values = handled(request, "datasets_list", {"groupId": group_id}, "powerbi", run)
    return {"@odata.context": f"{base_url(request)}/v1.0/myorg/groups/{group_id}/$metadata"
                              f"#datasets", "value": values}


@app.get("/v1.0/myorg/groups/{group_id}/datasets/{dataset_id}")
def dataset_get(group_id: str, dataset_id: str, request: Request) -> dict:
    def run(store: PowerBIStore, observation: Observation):
        state = store.load()
        dataset = store.dataset(state, group_id, dataset_id, observation)
        observation.targets = [store.target(dataset)]
        return dataset_json(state["workspace"], dataset)

    body = handled(request, "dataset_get", {"groupId": group_id, "datasetId": dataset_id},
                  "powerbi", run)
    return {"@odata.context": f"{base_url(request)}/v1.0/myorg/groups/{group_id}/$metadata"
                              f"#datasets/$entity", **body}


ENHANCED_KEYS = ("type", "commitMode", "maxParallelism", "retryCount", "objects",
                 "applyRefreshPolicy", "effectiveDate", "timeout")


@app.post("/v1.0/myorg/groups/{group_id}/datasets/{dataset_id}/refreshes", status_code=202)
def refresh(group_id: str, dataset_id: str, request: Request, body: JSONBody) -> Response:
    enhanced = any(key in body for key in ENHANCED_KEYS)

    def run(store: PowerBIStore, observation: Observation):
        with _STATE_LOCK:
            state = store.load()
            dataset = store.dataset(state, group_id, dataset_id, observation)
            observation.op_class = "write"
            observation.targets = [store.target(dataset)]
            if body.get("objects"):
                raise powerbi_error(400, "InvalidRequest",
                                    "unsupported (Power BI): partial refresh (objects)")
            kind = str(body.get("type", "Full"))
            if kind.lower() not in ("full", "automatic", "dataonly"):
                raise powerbi_error(400, "InvalidRequest",
                                    f"unsupported (Power BI): refresh type {kind}")
            event = store.refresh_dataset(state, dataset,
                                          "ViaEnhancedApi" if enhanced else "ViaApi",
                                          observation)
            store.save(state)
            # The row carries what the refresh published - the rendered visual
            # values - so a number shown before a fix stays in the action log.
            return {key: event[key] for key in ("id", "requestId", "status", "startTime",
                                                "endTime", "values", "serviceExceptionJson")
                    if key in event}

    event = handled(request, "refresh", {"groupId": group_id, "datasetId": dataset_id,
                                        **{k: body[k] for k in body if k != "objects"}},
                   "powerbi", run)
    location = (f"{base_url(request)}/v1.0/myorg/groups/{group_id}/datasets/{dataset_id}"
                f"/refreshes/{event['requestId']}")
    headers = {"Location": location, "x-ms-request-id": event["requestId"],
               "RequestId": event["requestId"]}
    return Response(status_code=202, headers=headers)


@app.get("/v1.0/myorg/groups/{group_id}/datasets/{dataset_id}/refreshes")
def refreshes_list(group_id: str, dataset_id: str, request: Request) -> dict:
    def run(store: PowerBIStore, observation: Observation):
        state = store.load()
        dataset = store.dataset(state, group_id, dataset_id, observation)
        observation.targets = [store.target(dataset)]
        return [refresh_json(e) for e in reversed(dataset["refreshes"])]

    events = handled(request, "refreshes_list", {"groupId": group_id, "datasetId": dataset_id,
                                                "top": request.query_params.get("$top")},
                    "powerbi", run)
    return {"@odata.context": f"{base_url(request)}/v1.0/myorg/groups/{group_id}/$metadata"
                              f"#refreshes",
            "value": top_skip(request, events, REFRESH_HISTORY_DEFAULT)}


@app.get("/v1.0/myorg/groups/{group_id}/datasets/{dataset_id}/refreshes/{refresh_id}")
def refresh_get(group_id: str, dataset_id: str, refresh_id: str, request: Request) -> dict:
    """Get Refresh Execution Details In Group: the status a Location header
    from POST .../refreshes points at."""
    def run(store: PowerBIStore, observation: Observation):
        state = store.load()
        dataset = store.dataset(state, group_id, dataset_id, observation)
        observation.targets = [store.target(dataset)]
        for event in dataset["refreshes"]:
            if event["requestId"] == refresh_id.lower():
                return event
        raise powerbi_error(404, "ItemNotFound", f"Refresh {refresh_id} is not found")

    event = handled(request, "refresh_get", {"groupId": group_id, "datasetId": dataset_id,
                                            "refreshId": refresh_id}, "powerbi", run)
    out = {"startTime": event["startTime"], "endTime": event["endTime"], "type": "Full",
           "commitMode": "Transactional", "status": event["status"],
           "extendedStatus": event["status"], "currentRefreshType": "Full",
           "numberOfAttempts": 1, "objects": []}
    if "serviceExceptionJson" in event:
        out["messages"] = [{"code": "ModelRefresh_ShortMessage_ProcessingError",
                            "message": json.loads(event["serviceExceptionJson"])
                            .get("errorDescription", ""), "type": "Error"}]
    return out


@app.post("/v1.0/myorg/groups/{group_id}/datasets/{dataset_id}/executeQueries")
def execute_queries(group_id: str, dataset_id: str, request: Request, body: JSONBody) -> dict:
    queries = body.get("queries")
    include_nulls = bool((body.get("serializerSettings") or {}).get("includeNulls", False))
    text = queries[0].get("query") if isinstance(queries, list) and queries \
        and isinstance(queries[0], dict) else None

    def run(store: PowerBIStore, observation: Observation):
        state = store.load()
        dataset = store.dataset(state, group_id, dataset_id, observation)
        observation.targets = [store.target(dataset)]
        if not isinstance(queries, list) or not queries or not isinstance(text, str):
            raise powerbi_error(400, "InvalidRequest", "The request must contain one query "
                                                       "in 'queries'.")
        if len(queries) > 1:
            raise dax_error("More than one query in a request is not supported. Send one "
                            "query per API call.")
        return store.execute(state, dataset, text, include_nulls, observation)

    return handled(request, "execute_queries", {"groupId": group_id, "datasetId": dataset_id,
                                               "query": text}, "powerbi", run)


@app.get("/v1.0/myorg/groups/{group_id}/reports")
def reports_list(group_id: str, request: Request) -> dict:
    def run(store: PowerBIStore, observation: Observation):
        state = store.load()
        group = store.group(state, group_id, observation)
        reports = sorted(state["reports"].values(), key=lambda r: r["name"])
        observation.targets = [f"powerbi:report:{r['name']}" for r in reports] or [
            f"powerbi:workspace:{group['name']}"]
        return [report_json(group, r) for r in reports]

    values = handled(request, "reports_list", {"groupId": group_id}, "powerbi", run)
    return {"@odata.context": f"{base_url(request)}/v1.0/myorg/groups/{group_id}/$metadata"
                              f"#reports", "value": values}


# --------------------------------------------------------------------------
# Fabric REST
# --------------------------------------------------------------------------
def semantic_model_item(group: dict, dataset: dict) -> dict:
    return {"id": dataset["id"], "type": "SemanticModel", "displayName": dataset["name"],
            "description": "", "workspaceId": group["id"]}


@app.get("/v1/workspaces")
def workspaces_list(request: Request) -> dict:
    def run(store: PowerBIStore, observation: Observation):
        group = store.load()["workspace"]
        observation.targets = [f"powerbi:workspace:{group['name']}"]
        return [{"id": group["id"], "displayName": group["name"], "description": "",
                 "type": "Workspace", "capacityId": guid("capacity", group["name"])}]

    return {"value": handled(request, "workspaces_list", {}, "fabric", run)}


@app.get("/v1/workspaces/{workspace_id}/semanticModels")
def semantic_models_list(workspace_id: str, request: Request) -> dict:
    def run(store: PowerBIStore, observation: Observation):
        state = store.load()
        group = state["workspace"]
        if workspace_id.lower() != group["id"]:
            observation.targets = [f"powerbi:workspace:{workspace_id}"]
            raise fabric_error(404, "WorkspaceNotFound", "The requested workspace was not "
                                                         "found")
        datasets = sorted(state["datasets"].values(), key=lambda d: d["name"])
        observation.targets = [store.target(d) for d in datasets] or [
            f"powerbi:workspace:{group['name']}"]
        return [semantic_model_item(group, d) for d in datasets]

    return {"value": handled(request, "semantic_models_list", {"workspaceId": workspace_id},
                            "fabric", run)}


@app.get("/v1/workspaces/{workspace_id}/semanticModels/{model_id}")
def semantic_model_get(workspace_id: str, model_id: str, request: Request) -> dict:
    def run(store: PowerBIStore, observation: Observation):
        state = store.load()
        dataset = store.dataset(state, workspace_id, model_id, observation, fabric=True)
        observation.targets = [store.target(dataset)]
        return semantic_model_item(state["workspace"], dataset)

    return handled(request, "semantic_model_get", {"workspaceId": workspace_id,
                                                  "semanticModelId": model_id}, "fabric", run)


def part_order(path: str) -> tuple:
    """definition/*.tmdl first, then definition.pbism, then .platform, as the
    Fabric reference sample lists them."""
    return (0 if path.startswith("definition/") else 1 if path == "definition.pbism" else 2,
            path)


@app.post("/v1/workspaces/{workspace_id}/semanticModels/{model_id}/getDefinition")
def get_definition(workspace_id: str, model_id: str, request: Request) -> dict:
    fmt = request.query_params.get("format", "TMDL")

    def run(store: PowerBIStore, observation: Observation):
        state = store.load()
        dataset = store.dataset(state, workspace_id, model_id, observation, fabric=True)
        observation.targets = [store.target(dataset)]
        if fmt != "TMDL":
            raise fabric_error(400, "OperationNotSupportedForItem",
                               f"unsupported (Power BI): getDefinition format {fmt}")
        return [{"path": path, "payload": dataset["parts"][path],
                 "payloadType": "InlineBase64"}
                for path in sorted(dataset["parts"], key=part_order)]

    parts = handled(request, "get_definition", {"workspaceId": workspace_id,
                                               "semanticModelId": model_id, "format": fmt},
                   "fabric", run)
    return {"definition": {"parts": parts}}


@app.post("/v1/workspaces/{workspace_id}/semanticModels/{model_id}/updateDefinition")
def update_definition(workspace_id: str, model_id: str, request: Request,
                      body: JSONBody) -> Response:
    update_metadata = request.query_params.get("updateMetadata", "false").lower() == "true"
    definition = body.get("definition") if isinstance(body.get("definition"), dict) else {}
    parts = definition.get("parts")

    def run(store: PowerBIStore, observation: Observation):
        with _STATE_LOCK:
            state = store.load()
            dataset = store.dataset(state, workspace_id, model_id, observation, fabric=True)
            observation.op_class = "write"
            observation.targets = [store.target(dataset)]
            if not isinstance(parts, list) or not parts:
                raise fabric_error(400, "InvalidDefinition", "The definition must contain "
                                                             "parts")
            store.update_definition(state, dataset, parts, update_metadata, observation)
            store.save(state)
            return {"semanticModel": dataset["name"], "parts": sorted(dataset["parts"]),
                    "definitionHash": digest(dataset["parts"])}

    handled(request, "update_definition",
           {"workspaceId": workspace_id, "semanticModelId": model_id,
            "updateMetadata": update_metadata,
            "parts": sorted(p.get("path", "") for p in parts if isinstance(p, dict))
            if isinstance(parts, list) else None},
           "fabric", run)
    return Response(status_code=200)


__all__ = ["HOST", "NAME", "PORT", "PowerBIStore", "app", "mint", "verify"]

