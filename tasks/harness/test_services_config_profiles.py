"""The services' gateway config, as each topology writes it, for every warehouse profile.

    cd gateway && uv run pytest ../tasks/harness/test_services_config_profiles.py -q

Who writes the config the native services resolve their world through:

  host topology   native_stack.NativeStack._config_path - episode.write_gateway_config
                  with the profile and its plugs (`<rollout>/native_gateway.json`)
  compose         gateway/docker/native_services.py - serve.py's derived config, no
                  profile; the selection is ALKERA_NATIVE_PROFILE_ARGS on serve's argv
  Harbor          worldbuild.stage_built_world - write_gateway_config, the profile
                  dropped where the gateway refuses it (harbor/tests cover it)

A profile whose warehouse plug is postgres, snowflake or bigquery names the
world's DuckDB file as its `warehouse_backend` in every one of them, which the MCP
gateway refuses (its sql_* would misdescribe the box) and the services must not:
until `GatewayConfig.for_services` they did, at the first request, and every
service answered 500 (gateway/tests/test_services_config_profiles.py drives jira
through it). No docker, no model.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import episode  # noqa: E402
import native_stack  # noqa: E402
from alkera_gateway.config import GatewayConfig  # noqa: E402

PROFILES = [
    ("da/laptop", {}),
    ("da/snowflake", {}),
    ("de/postgres", {}),
    ("de/aws", {}),
    ("de/databricks", {}),
    ("de/gcp", {}),
    ("de/laptop", {"orchestrator": ["airflow"]}),
]
PROFILE_IDS = [p if not plugs else f"{p}+{','.join(plugs)}" for p, plugs in PROFILES]
#: The profiles the MCP gateway refuses over the world's DuckDB file.
MCP_REFUSES = {"da/snowflake", "de/postgres", "de/gcp"}


@pytest.fixture
def world(tmp_path: Path) -> dict:
    root = tmp_path / "world"
    (root / "var" / "warehouse").mkdir(parents=True)
    (root / "var" / "warehouse" / "warehouse.duckdb").write_bytes(b"x")
    (root / "workspace").mkdir()
    return {"world_root": str(root), "workspace": str(root / "workspace"),
            "warehouse": str(root / "var" / "warehouse" / "warehouse.duckdb"),
            "scheduler": str(root / "var" / "sched" / "state.json"),
            "dashboards": str(root / "var" / "bi"),
            "profiles_dir": str(root / "build"),
            "virtual_time": "2026-03-08T09:00:00Z"}


@pytest.mark.parametrize(("profile", "plugs"), PROFILES, ids=PROFILE_IDS)
def test_host_topology_config_loads_for_the_services(world, tmp_path, profile, plugs):
    spec = SimpleNamespace(profile=profile, plugs=plugs)
    rollout = tmp_path / "rollout"
    rollout.mkdir()
    if profile == "de/databricks":
        # refused before any config is written (native_stack.HOST_TOPOLOGY_REFUSED)
        with pytest.raises(episode.HarnessError, match="--topology container"):
            native_stack.NativeStack(world=world, audit_dir=rollout / "audit",
                                     rollout_dir=rollout, spec=spec)
        return
    stack = native_stack.NativeStack(world=world, audit_dir=rollout / "audit",
                                     rollout_dir=rollout, spec=spec)
    path = stack._config_path()
    written = json.loads(path.read_text(encoding="utf-8"))
    # the bytes it always wrote: the profile, its plugs, no warehouse_backend
    assert (written["profile"], written["plugs"]) == (profile, plugs)
    assert "warehouse_backend" not in written
    loaded = GatewayConfig.for_services(path.read_bytes())
    assert (loaded.profile, loaded.plugs) == (profile, plugs)
    if profile in MCP_REFUSES:
        with pytest.raises(ValidationError, match="disagrees with warehouse_backend"):
            GatewayConfig.load(path)
    else:
        assert GatewayConfig.load(path).profile == profile


@pytest.mark.parametrize(("profile", "plugs"), PROFILES, ids=PROFILE_IDS)
def test_compose_services_config_names_no_profile(tmp_path, profile, plugs):
    """native_services.py's config is serve.py's derivation for every profile:
    it never names the profile, so the services' reading of it was never the
    one that refused - the profile reaches serve.py as argv."""
    sys.path.insert(0, str(REPO / "gateway" / "docker"))
    import native_services

    world, audit, state = tmp_path / "world", tmp_path / "audit", tmp_path / "state"
    (world / "workspace").mkdir(parents=True)
    audit.mkdir()
    config = native_services.services_config(world, audit, state)
    assert config.get("profile") is None and not config.get("plugs")
    GatewayConfig.for_services(json.dumps(config))
    argv = episode.profile_argv(profile, plugs)
    assert argv[:2] == ["--profile", profile]
