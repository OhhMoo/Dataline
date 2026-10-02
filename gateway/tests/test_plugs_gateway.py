"""The gateway serves only the profile's MCP tools (plugs.py).

No profile changes nothing: the served table is `TOOLS` itself. A profile
filters by the tools its unplugged slots own, never by an allowlist, so a tool
no slot owns (the runtime `bash`) survives.
"""

import pytest
from conftest import config
from pydantic import ValidationError
from test_dual_warehouse import dual_config

from alkera_gateway import Gateway
from alkera_gateway.tools import TOOLS
from alkera_gateway.warehouses import build_tools

DA = {"profile": "da/laptop", "plugs": {"notebook": []}}


def test_no_profile_serves_the_tool_table_object_itself(world, tmp_path):
    assert build_tools(config(world, tmp_path / "a")) is TOOLS


def test_profile_drops_tools_of_unplugged_slots(world, tmp_path):
    tools = build_tools(config(world, tmp_path / "a", **DA))
    assert "sql_query" in tools
    assert "sched_trigger" not in tools
    assert "deploy_run" not in tools


def test_runtime_tool_survives_profile(world, tmp_path, monkeypatch):
    monkeypatch.setitem(TOOLS, "bash", TOOLS["fs_list"])
    assert "bash" in build_tools(config(world, tmp_path / "a", **DA))


def test_excluded_tool_is_unknown_to_the_gateway(world, tmp_path):
    gateway = Gateway(config(world, tmp_path / "a", **DA))
    result = gateway.call("sched_list", {})
    assert not result["ok"]
    assert result["error"]["code"] == "unknown_tool"


def test_planned_plug_is_rejected(world, tmp_path):
    with pytest.raises(ValidationError, match="ml_workers"):
        config(world, tmp_path / "a", profile="ds/laptop")


def test_plugs_require_a_profile(world, tmp_path):
    with pytest.raises(ValidationError, match="profile"):
        config(world, tmp_path / "a", plugs={"notebook": []})


def test_warehouse_plug_must_match_backend(world, tmp_path):
    with pytest.raises(ValidationError, match="postgres.*duckdb"):
        config(world, tmp_path / "a", profile="de/postgres")


def test_dual_warehouse_profile_keeps_warehouse_argument(world, tmp_path):
    tools = build_tools(dual_config(world, tmp_path / "a", profile="de/laptop"))
    assert "warehouse" in tools["sql_query"][0].model_fields
