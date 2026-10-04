"""The three fake BI vendor services, in process, for tests/run_baselines.py.

Not part of the measurement. A baseline is a scripted agent, and a BI001
baseline that publishes has to publish through the SAME code path the agent's
tabcmd / curl reach: the real service modules
(gateway/src/alkera_gateway/native/services/{powerbi,tableau,sigma}.py) driven
with Starlette's TestClient over a GatewayConfig for this world - the way
gateway/tests/test_bi_world_smoke.py drives them. Their published state lands
in `<audit>/bi/<vendor>/` (GatewayConfig.bi_state_dir's default), which the
suite then stages into the grading zone with the harness's own
tasks/harness/bistate.py, exactly as a rollout does after teardown.

`seed()` asks each service for /health first, which is what the harness's
native surface gate does before the agent is handed the terminal: it takes
each service's published state from the repository as provisioned. A baseline
that edited the repo before the first request would otherwise "publish" by
being read at seed time.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
for p in (REPO / "world", REPO / "gateway" / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from fastapi.testclient import TestClient                      # noqa: E402

from alkera_gateway import GatewayConfig                        # noqa: E402
from alkera_gateway.native import context as native_context     # noqa: E402
from alkera_gateway.native.services import powerbi, sigma, tableau  # noqa: E402

TENANT = "7f3c2a10-5b8e-4d6f-9a1c-2e4b6d8f0a12"
CLIENT = "3b9d6e2f-0c4a-4f1e-8b7d-5a6c9e1f2d34"
TABLEAU_V = "3.25"
NS = {"t": "http://tableau.com/api"}
SECRET = native_context.DEV_TOKEN


class ServiceError(RuntimeError):
    pass


def _ok(response, what: str):
    if response.status_code >= 400:
        raise ServiceError(f"{what}: HTTP {response.status_code} {response.text[:600]}")
    return response


class Services:
    """One world's three BI services, state under `<audit>/bi`."""

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
        self.clients = {}
        for name, module, base in (("powerbi", powerbi, "http://powerbi.internal:8095"),
                                   ("tableau", tableau, "http://tableau.internal:8096"),
                                   ("sigma", sigma, "http://sigma.internal:8097")):
            native_context.set_context(module.app, native_context.Context(self.config))
            self.clients[name] = TestClient(module.app, base_url=base)
        self._tableau_token = self._tableau_site = None
        self._sigma_token = None
        self._pbi_token = None

    @property
    def bi_state(self) -> Path:
        return Path(self.config.bi_state_dir)

    def seed(self) -> None:
        for name, client in self.clients.items():
            _ok(client.get("/health"), f"{name} /health")

    # --------------------------------------------------------------- Tableau
    def _tableau_signin(self) -> None:
        if self._tableau_token:
            return
        body = (f'<tsRequest><credentials personalAccessTokenName="{SECRET}" '
                f'personalAccessTokenSecret="{SECRET}"><site contentUrl="" />'
                f'</credentials></tsRequest>')
        r = _ok(self.clients["tableau"].post(f"/api/{TABLEAU_V}/auth/signin", content=body),
                "tableau signin")
        x = ET.fromstring(r.content)
        self._tableau_token = x.find("t:credentials", NS).get("token")
        self._tableau_site = x.find("t:credentials/t:site", NS).get("id")

    def tableau_publish(self, kind: str, path: Path, name: str) -> None:
        """What `tabcmd publish <file> --name <name> --overwrite` sends."""
        self._tableau_signin()
        data = Path(path).read_bytes()
        filename = Path(path).name
        payload = (f'<tsRequest><{kind} name="{name}"><project id="" /></{kind}>'
                   f'</tsRequest>').encode()
        boundary = "bi001Boundary"
        body = b"".join([
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: name="request_payload"\r\nContent-Type: text/xml\r\n\r\n',
            payload, b"\r\n", f"--{boundary}\r\n".encode(),
            f'Content-Disposition: name="tableau_{kind}"; filename="{filename}"\r\n'.encode(),
            b"Content-Type: application/octet-stream\r\n\r\n", data, b"\r\n",
            f"--{boundary}--\r\n".encode()])
        ext = filename.rsplit(".", 1)[-1]
        _ok(self.clients["tableau"].post(
            f"/api/{TABLEAU_V}/sites/{self._tableau_site}/{kind}s?{kind}Type={ext}"
            f"&overwrite=true", content=body,
            headers={"X-Tableau-Auth": self._tableau_token,
                     "Content-Type": f"multipart/mixed; boundary={boundary}"}),
            f"tableau publish {kind} {name}")

    # ----------------------------------------------------------------- Sigma
    def _sigma(self) -> dict:
        if not self._sigma_token:
            r = _ok(self.clients["sigma"].post(
                "/v2/auth/token", data={"grant_type": "client_credentials"},
                auth=(SECRET, SECRET)), "sigma token")
            self._sigma_token = r.json()["access_token"]
        return {"Authorization": f"Bearer {self._sigma_token}"}

    def sigma_put_workbook(self, doc: dict, *, spec: bool = False) -> None:
        """PUT /v2/workbooks/{id}/contents with the committed file's contents
        (or the deprecated /spec with `document`)."""
        wid = doc["workbookId"]
        if spec:
            _ok(self.clients["sigma"].put(f"/v2/workbooks/{wid}/spec",
                                          json={"document": doc["contents"]},
                                          headers=self._sigma()), "sigma PUT spec")
        else:
            _ok(self.clients["sigma"].put(f"/v2/workbooks/{wid}/contents",
                                          json={"contents": doc["contents"]},
                                          headers=self._sigma()), "sigma PUT contents")

    # -------------------------------------------------------------- Power BI
    def _pbi(self) -> dict:
        if not self._pbi_token:
            r = _ok(self.clients["powerbi"].post(f"/{TENANT}/oauth2/v2.0/token", data={
                "grant_type": "client_credentials", "client_id": CLIENT,
                "client_secret": SECRET,
                "scope": "https://api.fabric.microsoft.com/.default"}), "powerbi token")
            self._pbi_token = r.json()["access_token"]
        return {"Authorization": f"Bearer {self._pbi_token}"}

    def powerbi_deploy(self, model_folder: Path, *, refresh: bool = True) -> None:
        """updateDefinition with every file of a .SemanticModel folder, then a
        refresh - what the deployment pipeline does."""
        client, headers = self.clients["powerbi"], self._pbi()
        ws = _ok(client.get("/v1/workspaces", headers=headers), "workspaces").json()["value"][0]
        models = _ok(client.get(f"/v1/workspaces/{ws['id']}/semanticModels",
                                headers=headers), "semanticModels").json()["value"]
        model = next(m for m in models if m["displayName"] == "Commerce")
        parts = [{"path": p.relative_to(model_folder).as_posix(),
                  "payload": base64.b64encode(p.read_bytes()).decode(),
                  "payloadType": "InlineBase64"}
                 for p in sorted(Path(model_folder).rglob("*"))
                 if p.is_file() and ".pbi" not in p.relative_to(model_folder).parts]
        _ok(client.post(f"/v1/workspaces/{ws['id']}/semanticModels/{model['id']}"
                        f"/updateDefinition", json={"definition": {"parts": parts}},
                        headers=headers), "updateDefinition")
        if refresh:
            _ok(client.post(f"/v1.0/myorg/groups/{ws['id']}/datasets/{model['id']}/refreshes",
                            json={"notifyOption": "NoNotification"}, headers=headers),
                "refresh")


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
