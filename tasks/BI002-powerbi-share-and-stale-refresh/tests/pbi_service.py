"""The fake Power BI service, in process, for tests/run_baselines.py.

Not part of the measurement. A baseline is a scripted agent, and a BI002
baseline that deploys or refreshes has to do it through the SAME code path the
agent's curl reaches: the real service module
(gateway/src/alkera_gateway/native/services/powerbi.py) driven with
Starlette's TestClient over a GatewayConfig for this world - the way
tasks/BI001-.../tests/bi_services.py drives all three vendors. Its published
state lands in `<audit>/bi/powerbi/` (GatewayConfig.bi_state_dir's default),
which the suite then stages into the grading zone with the harness's own
tasks/harness/bistate.py, exactly as a rollout does after teardown.

`seed()` asks the service for /health first, which is what the harness's
native surface gate does before the agent is handed the terminal: it
provisions the published state - the first commit's project and the world's
import record. A baseline that edited the repo before the first request would
otherwise "deploy" by being read at provisioning.
"""
from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for p in (REPO / "world", REPO / "gateway" / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from fastapi.testclient import TestClient                      # noqa: E402

from alkera_gateway import GatewayConfig                        # noqa: E402
from alkera_gateway.native import context as native_context     # noqa: E402
from alkera_gateway.native.services import powerbi              # noqa: E402

TENANT = "7f3c2a10-5b8e-4d6f-9a1c-2e4b6d8f0a12"
CLIENT = "3b9d6e2f-0c4a-4f1e-8b7d-5a6c9e1f2d34"
SECRET = native_context.DEV_TOKEN
POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"


class ServiceError(RuntimeError):
    pass


def _ok(response, what: str):
    if response.status_code >= 400:
        raise ServiceError(f"{what}: HTTP {response.status_code} {response.text[:600]}")
    return response


class PowerBI:
    """One world's Power BI service, state under `<audit>/bi`."""

    def __init__(self, world: dict, audit: Path):
        self.world = world
        self.audit = Path(audit)
        self.audit.mkdir(parents=True, exist_ok=True)
        root = Path(world["world_root"])
        self.config = GatewayConfig(
            workspace=Path(world["workspace"]), warehouse=Path(world["warehouse"]),
            scheduler=Path(world["scheduler"]), dashboards=Path(world["dashboards"]),
            workflow_dir=Path(world["workflow_dir"]),
            observability=root / "var" / "obs" / "definitions.yaml",
            audit_dir=self.audit, profiles_dir=Path(world["profiles_dir"]),
            episode_id=self.audit.parent.name, world_root=root,
            virtual_time=world["virtual_time"])
        os.environ["ALKERA_NATIVE_AUDIT"] = str(self.audit / "actions.jsonl")
        os.environ["ALKERA_SCHEDULER"] = str(world["scheduler"])
        native_context.set_context(powerbi.app, native_context.Context(self.config))
        self.client = TestClient(powerbi.app, base_url="http://powerbi.internal:8095")
        self._tokens: dict[str, str] = {}

    @property
    def bi_state(self) -> Path:
        return Path(self.config.bi_state_dir)

    def seed(self) -> None:
        _ok(self.client.get("/health"), "powerbi /health")

    # ------------------------------------------------------------- auth
    def headers(self, scope: str = POWERBI_SCOPE) -> dict:
        if scope not in self._tokens:
            r = _ok(self.client.post(f"/{TENANT}/oauth2/v2.0/token", data={
                "grant_type": "client_credentials", "client_id": CLIENT,
                "client_secret": SECRET, "scope": scope}), "token")
            self._tokens[scope] = r.json()["access_token"]
        return {"Authorization": f"Bearer {self._tokens[scope]}"}

    def ids(self) -> tuple[str, str]:
        h = self.headers()
        group = _ok(self.client.get("/v1.0/myorg/groups", headers=h),
                    "groups").json()["value"][0]["id"]
        datasets = _ok(self.client.get(f"/v1.0/myorg/groups/{group}/datasets", headers=h),
                       "datasets").json()["value"]
        return group, next(d["id"] for d in datasets if d["name"] == "Commerce")

    # ----------------------------------------------------------- writes
    def deploy(self, model_folder: Path, *, scope: str = FABRIC_SCOPE,
               update_metadata: bool = False) -> None:
        """Fabric updateDefinition with every file of a .SemanticModel folder
        (Desktop's local .pbi/ left out) - what the deployment pipeline sends."""
        h = self.headers(scope)
        ws = _ok(self.client.get("/v1/workspaces", headers=h), "workspaces").json()["value"][0]
        models = _ok(self.client.get(f"/v1/workspaces/{ws['id']}/semanticModels", headers=h),
                     "semanticModels").json()["value"]
        model = next(m for m in models if m["displayName"] == "Commerce")
        parts = [{"path": p.relative_to(model_folder).as_posix(),
                  "payload": base64.b64encode(p.read_bytes()).decode(),
                  "payloadType": "InlineBase64"}
                 for p in sorted(Path(model_folder).rglob("*"))
                 if p.is_file() and ".pbi" not in p.relative_to(model_folder).parts]
        suffix = "?updateMetadata=true" if update_metadata else ""
        _ok(self.client.post(f"/v1/workspaces/{ws['id']}/semanticModels/{model['id']}"
                             f"/updateDefinition{suffix}",
                             json={"definition": {"parts": parts}}, headers=h),
            "updateDefinition")

    def refresh(self, *, enhanced: bool = False) -> dict:
        group, dataset = self.ids()
        body = ({"type": "Full", "commitMode": "transactional"} if enhanced
                else {"notifyOption": "NoNotification"})
        r = _ok(self.client.post(f"/v1.0/myorg/groups/{group}/datasets/{dataset}/refreshes",
                                 json=body, headers=self.headers()), "refresh")
        request_id = r.headers["x-ms-request-id"]
        return _ok(self.client.get(f"/v1.0/myorg/groups/{group}/datasets/{dataset}"
                                   f"/refreshes/{request_id}", headers=self.headers()),
                   "refresh status").json()

    # ------------------------------------------------------------ reads
    def query(self, dax: str) -> list[dict]:
        group, dataset = self.ids()
        r = _ok(self.client.post(f"/v1.0/myorg/groups/{group}/datasets/{dataset}"
                                 f"/executeQueries", json={"queries": [{"query": dax}]},
                                 headers=self.headers()), "executeQueries")
        return r.json()["results"][0]["tables"][0]["rows"]


def comment(world: dict, issue: str, body: str, author: str = "agent") -> None:
    """A tracker comment in the shape the Jira service writes one (`<issue>#<n>`,
    author, at = the world clock) - the agent's route, or a forger's."""
    path = Path(world["workflow_dir"]) / "issues.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    vnow = json.loads(Path(world["scheduler"]).read_text(encoding="utf-8"))["vnow"]
    for record in doc["issues"]:
        if record["id"] == issue:
            comments = record.setdefault("comments", [])
            comments.append({"id": f"{issue}#{len(comments) + 1}", "author": author,
                             "at": vnow, "body": body})
            record["updated_at"] = max(record.get("updated_at", ""), vnow)
            break
    else:
        raise ServiceError(f"no issue {issue}")
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def post(world: dict, channel: str, text: str, author: str = "agent") -> None:
    """A chat message in the shape the Slack service writes one."""
    path = Path(world["workflow_dir"]) / "chat.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    vnow = json.loads(Path(world["scheduler"]).read_text(encoding="utf-8"))["vnow"]
    for ch in doc["channels"]:
        if ch["id"] == channel:
            messages = ch.setdefault("messages", [])
            messages.append({"id": f"{channel}/{len(messages) + 1:04d}", "author": author,
                             "at": vnow, "thread_id": None, "text": text, "reactions": {}})
            break
    else:
        raise ServiceError(f"no channel {channel}")
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
