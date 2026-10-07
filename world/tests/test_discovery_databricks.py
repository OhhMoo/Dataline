"""The Databricks discovery row (native_workspace.DISCOVERY["databricks"]).

A `de/databricks` box names the workspace in all four discovery files - its variables
(including the connector's trust, SSL_CERT_FILE), its onboarding row (use_sea=True, no
CLI), a `[databricks]` services.toml section, and a dbt target that is the box
profile's default - and a box that does not plug it names it nowhere.
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import yaml

WORLD_DIR = Path(__file__).resolve().parents[1]
REPO = WORLD_DIR.parent
sys.path.insert(0, str(WORLD_DIR))

from world import native_workspace as nw  # noqa: E402

DE_SLOTS = ["files", "warehouse", "transform", "orchestrator", "object_store", "ingest",
            "bi", "observability", "release", "human"]
WORLD_PROFILE = ("world_dbt:\n  outputs:\n    dev:\n"
                 "      path: /world/var/warehouse/warehouse.duckdb\n"
                 "      threads: 1\n      type: duckdb\n  target: dev\n")


def _box():
    return DE_SLOTS, nw.profile_plugs("de/databricks", {"bi": ["powerbi"]})


def _gateway():
    sys.path.insert(0, str(REPO / "gateway" / "src"))
    from alkera_gateway import backends, warehouses

    return backends, warehouses


def test_databricks_box_exports_the_workspace_and_its_trust():
    slots, plugs = _box()
    env = nw.environment(slots, plugs)
    assert env["DATABRICKS_HOST"] == "https://databricks.internal"
    assert env["DATABRICKS_SERVER_HOSTNAME"] == "databricks.internal"
    assert env["DATABRICKS_HTTP_PATH"] == "/sql/1.0/warehouses/alkera"
    assert env["DATABRICKS_TOKEN"] == nw.FAKE_TOKEN
    assert env["SSL_CERT_FILE"] == "/etc/alkera/databricks-tls/bundle.pem"
    assert "PGHOST" not in env                    # the warehouse slot is Databricks only
    block = nw.envrc_block(slots, plugs)
    assert 'export DATABRICKS_HOST="https://databricks.internal"' in block


def test_databricks_services_section_and_onboarding_row(tmp_path):
    slots, plugs = _box()
    toml = tomllib.loads(nw.services_toml("/world", slots, plugs))
    section = toml["databricks"]
    assert (section["host"], section["port"]) == ("databricks.internal", 443)
    assert section["http_path"] == "/sql/1.0/warehouses/alkera"
    assert (section["catalog"], section["schema"], section["use_sea"]) == \
        ("main", "default", "true")
    page = nw.onboarding_markdown(tmp_path, {}, slots, plugs)
    d = nw.DISCOVERY["databricks"]
    assert f"`databricks.internal:443` | {d.clients} |" in page
    assert "use_sea=True" in page and "no databricks CLI" in page


def test_databricks_box_profile_defaults_to_the_workspace():
    _, warehouses = _gateway()
    text = nw.box_profiles_yaml(WORLD_PROFILE, ["databricks"])
    profile = yaml.safe_load(text)["world_dbt"]
    assert profile["target"] == "databricks"
    assert profile["outputs"]["dev"]["type"] == "duckdb"
    target = profile["outputs"]["databricks"]
    assert target == warehouses.dbt_output("databricks", schema="default",
                                           env=nw.DISCOVERY["databricks"].dbt_env, port=443)
    assert target["type"] == "databricks" and target["catalog"] == "main"
    assert target["connection_parameters"] == {"use_sea": True}
    for field, var in nw.DISCOVERY["databricks"].dbt_env.items():
        assert target[field] == f"{{{{ env_var('{var}') }}}}"
    assert nw.FAKE_TOKEN not in text


def test_unplugged_databricks_is_named_nowhere(tmp_path):
    for slots, plugs in ((None, None), (DE_SLOTS, nw.profile_plugs("de/laptop", None)),
                         (DE_SLOTS, nw.profile_plugs("de/snowflake", None))):
        files = [nw.envrc_block(slots, plugs), nw.services_toml("/world", slots, plugs),
                 nw.onboarding_markdown(tmp_path, {}, slots, plugs)]
        assert not any("databricks" in text.lower() for text in files)
        assert "SSL_CERT_FILE" not in nw.environment(slots, plugs)
