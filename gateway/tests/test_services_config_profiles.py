"""The native services serve every warehouse profile from a profiled config.

A profile's warehouse plug (`postgres`, `snowflake`, `bigquery`, ...) names the
warehouse the BOX offers the agent - a backend the episode runs beside the
services. `GatewayConfig.warehouse_backend` names how the MCP GATEWAY reaches a
warehouse for `sql_*`, and GatewayConfig refuses a profile whose plug disagrees
with it, because a gateway that served Snowflake's tool table over the world's
DuckDB file would be lying about the box.

The services never reach the warehouse through `warehouse_backend`: they serve
the world's files, select what they serve from `serve.py --profile/--plug`, and
the backend is its own process. So a services config that names the profile
(the host topology's `native_gateway.json`, Harbor's exported `gateway.json`)
with the world's DuckDB `warehouse_backend` is a correct config for them - and
until `GatewayConfig.for_services` every service validated it as the gateway
does, at its first request, and answered 500 on da/snowflake, de/postgres and
de/gcp (Harbor's world-up /health caught it on da/snowflake).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from conftest import config
from pydantic import ValidationError

from alkera_gateway.config import GatewayConfig
from alkera_gateway.native import context as native_context
from alkera_gateway.native.services import jira as jira_service
from alkera_gateway.native.shims import sched_cli

#: Every stack's warehouse, on the role that runs it, and de/laptop with Airflow.
PROFILES = [
    ("da/laptop", {}),
    ("da/snowflake", {}),
    ("de/postgres", {}),
    ("de/aws", {}),
    ("de/databricks", {}),
    ("de/gcp", {}),
    ("de/laptop", {"orchestrator": ["airflow"]}),
]
#: The profiles whose warehouse plug GatewayConfig checks against
#: `warehouse_backend` - and which the gateway, unlike the services, refuses.
MCP_REFUSES = {"da/snowflake": "snowflake", "de/postgres": "postgres",
               "de/gcp": "bigquery"}
PROFILE_IDS = [p if not plugs else f"{p}+{','.join(plugs)}" for p, plugs in PROFILES]


def services_config(world: Path, tmp_path: Path, profile: str, plugs: dict) -> Path:
    """A profiled services config, shaped as `tasks/harness/episode
    .write_gateway_config(..., profile=, plugs=)` writes it for the host
    topology and Harbor: the world's paths, the profile and its plugs, and no
    `warehouse_backend` (the world's DuckDB file)."""
    audit = tmp_path / "audit"
    audit.mkdir(exist_ok=True)
    data = json.loads(config(world, audit).model_dump_json())
    data.pop("warehouse_backend")
    data.update(profile=profile, plugs=plugs)
    path = tmp_path / f"{profile.replace('/', '-')}-{len(plugs)}.json"
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


@pytest.fixture
def served(world, tmp_path, monkeypatch):
    """Point the services at `world` through a profiled config, as serve.py does."""
    audit = tmp_path / "native-audit"
    audit.mkdir()
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(audit / "actions.jsonl"))
    monkeypatch.setenv("ALKERA_SCHEDULER", str(world / "var" / "sched" / "state.json"))

    def point(profile: str, plugs: dict) -> Path:
        path = services_config(world, tmp_path, profile, plugs)
        monkeypatch.setenv(native_context.CONFIG_ENV, str(path))
        return path

    return point


async def _issue(key: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=jira_service.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport,
                                 base_url=f"http://{jira_service.HOST}",
                                 auth=("agent@alkera.local", jira_service.TOKEN)) as http:
        return await http.get(f"/rest/api/3/issue/{key}")


@pytest.mark.parametrize(("profile", "plugs"), PROFILES, ids=PROFILE_IDS)
def test_jira_answers_on_every_warehouse_profile(served, profile, plugs):
    served(profile, plugs)
    response = asyncio.run(_issue("DATA-141"))
    assert response.status_code == 200, response.text[:400]
    assert response.json()["key"] == "DATA-141"


@pytest.mark.parametrize(("profile", "plugs"), PROFILES, ids=PROFILE_IDS)
def test_services_load_the_profiled_config(world, tmp_path, profile, plugs):
    path = services_config(world, tmp_path, profile, plugs)
    loaded = GatewayConfig.for_services(path.read_bytes())
    assert (loaded.profile, loaded.plugs) == (profile, plugs)
    assert loaded.warehouse_backend == "duckdb"


@pytest.mark.parametrize(("profile", "plugs"), PROFILES, ids=PROFILE_IDS)
def test_the_mcp_gateway_still_ties_the_plug_to_its_backend(world, tmp_path,
                                                            profile, plugs):
    """Unchanged: the gateway refuses to serve a remote plug's tool table over
    the DuckDB file, so the same file it loads for the services it refuses."""
    path = services_config(world, tmp_path, profile, plugs)
    plug = MCP_REFUSES.get(profile)
    if plug is None:
        assert GatewayConfig.load(path).profile == profile
        return
    with pytest.raises(ValidationError,
                       match=f"profile warehouse plug '{plug}' disagrees with "
                             "warehouse_backend 'duckdb'"):
        GatewayConfig.load(path)


def test_the_services_loader_refuses_everything_else(world, tmp_path):
    """Only the plug-versus-backend tie is the gateway's own; a profile that
    cannot be served, plugs without a profile and a bad layout still refuse."""
    good = json.loads(services_config(world, tmp_path, "da/snowflake", {}).read_text())
    with pytest.raises(ValidationError, match="planned, not built"):
        GatewayConfig.for_services(json.dumps({**good, "profile": "ds/laptop"}))
    with pytest.raises(ValidationError, match="unknown stack"):
        GatewayConfig.for_services(json.dumps({**good, "profile": "da/nope"}))
    without = {k: v for k, v in good.items() if k != "profile"}
    with pytest.raises(ValidationError, match="plugs require a profile"):
        GatewayConfig.for_services(json.dumps({**without, "plugs": {"bi": ["sigma"]}}))
    with pytest.raises(ValidationError, match="audit_dir must be outside workspace"):
        GatewayConfig.for_services(json.dumps(
            {**good, "audit_dir": str(Path(good["workspace"]) / "audit")}))
    with pytest.raises(ValidationError, match="Remote warehouses require"):
        GatewayConfig.for_services(json.dumps(
            {**good, "warehouse_backend": "snowflake", "initial_world_fp": None}))


@pytest.mark.parametrize("profile", ["de/postgres", "de/gcp"])
def test_ops_sched_loads_the_services_config(served, profile):
    """`./ops/sched` resolves its world through the services' config
    (`$ALKERA_GATEWAY_CONFIG`, the host topology's native_gateway.json)."""
    path = served(profile, {})
    cfg = sched_cli.config()
    assert cfg.profile == profile
    assert cfg.scheduler == Path(json.loads(path.read_text())["scheduler"]).resolve()
