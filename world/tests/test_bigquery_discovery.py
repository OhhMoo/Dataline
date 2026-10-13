"""The BigQuery plug as a box that plugs it is told about it (native_workspace
.DISCOVERY["bigquery"]): its variables, its `[bigquery]` section and its onboarding
row - which must say how to point `google.cloud.bigquery.Client` at the endpoint -
and the box's dbt profile, whose default target is dbt-bigquery against the same
endpoint (gateway/docker/bigquery-clients.in carries the adapter). A box that does not
plug it names it nowhere (test_discovery_registry.test_unplugged_backend_is_absent_everywhere
covers every DISCOVERY row).

    gateway/.venv/bin/python -m pytest world/tests/test_bigquery_discovery.py -q
"""
import sys
import tomllib
from pathlib import Path

import yaml

WORLD_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORLD_DIR))

from world import native_workspace as nw  # noqa: E402

#: The data engineer's box (alkera_gateway.plugs ROLES["de"]), stack gcp.
DE_SLOTS = ["files", "warehouse", "transform", "orchestrator", "object_store", "ingest",
            "bi", "observability", "release", "human"]
WORLD_PROFILE = ("world_dbt:\n  outputs:\n    dev:\n"
                 "      path: /world/var/warehouse/warehouse.duckdb\n"
                 "      threads: 1\n      type: duckdb\n  target: dev\n")


def _gcp_box():
    return DE_SLOTS, nw.profile_plugs("de/gcp", None)


def test_gcp_stack_plugs_bigquery_and_nothing_else_remote():
    _, plugs = _gcp_box()
    assert plugs["warehouse"] == ["bigquery"]
    env = nw.environment(*_gcp_box())
    assert env["BIGQUERY_API_URL"] == "http://bigquery.internal:9050"
    assert env["BIGQUERY_PROJECT"] == "alkera-local"
    assert not any(k.startswith(("PG", "SNOWFLAKE_")) for k in env)


def test_bigquery_box_has_services_section():
    toml = tomllib.loads(nw.services_toml("/world", *_gcp_box()))
    section = toml["bigquery"]
    assert (section["host"], section["port"]) == ("bigquery.internal", 9050)
    assert section["endpoint"] == "http://bigquery.internal:9050"
    assert (section["project"], section["dataset"]) == ("alkera-local", "warehouse")
    assert "warehouse" not in toml                       # no Postgres on this box


def test_onboarding_says_how_to_point_the_client_at_the_endpoint(tmp_path):
    page = nw.onboarding_markdown(tmp_path, {}, *_gcp_box())
    row = next(line for line in page.splitlines() if "bigquery.internal:9050" in line)
    assert row.startswith("| the warehouse |")
    for needed in ("AnonymousCredentials()", "api_endpoint", "BIGQUERY_API_URL",
                   "BIGQUERY_PROJECT", "REST API v2"):
        assert needed in row, needed
    assert "dbt-bigquery" in row                         # the box profile's target
    assert row.count("|") == 5                           # one table row, no stray pipe
    page.encode("ascii")                                 # what _write accepts


def test_dbt_bigquery_profile_targets_the_proxy():
    """The target a bare `dbt` finds on a de/gcp box. dbt-bigquery's `oauth-secrets`
    method with a static token is the one method that never calls Google: the proxy
    forwards no credential and the emulator checks none (the Python client's
    AnonymousCredentials). The project and the endpoint are read from the box's own
    variables, and that endpoint, rendered on the box, is the audited proxy - never the
    engine and never googleapis.com. It becomes the default target; the world's DuckDB
    `dev` stays. No port: it is in the endpoint URL, and the adapter has no such field."""
    from jinja2 import Template

    sys.path.insert(0, str(WORLD_DIR.parent / "gateway" / "src"))
    from alkera_gateway.backends import BACKENDS
    from alkera_gateway.warehouses import DBT_ADAPTERS, dbt_output

    d = nw.DISCOVERY["bigquery"]
    assert d.dbt_type == "bigquery" and d.dbt_type in DBT_ADAPTERS
    text = nw.box_profiles_yaml(WORLD_PROFILE, ["bigquery"])
    profile = yaml.safe_load(text)["world_dbt"]
    assert profile["target"] == "bigquery"
    assert profile["outputs"]["dev"] == yaml.safe_load(WORLD_PROFILE)["world_dbt"]["outputs"]["dev"]
    target = profile["outputs"]["bigquery"]
    assert target == dbt_output("bigquery", schema="warehouse", env=d.dbt_env, port=9050)
    assert target == {"type": "bigquery", "method": "oauth-secrets", "token": "anonymous",
                      "project": "{{ env_var('BIGQUERY_PROJECT') }}",
                      "api_endpoint": "{{ env_var('BIGQUERY_API_URL') }}",
                      "schema": "warehouse", "threads": 1}
    box = nw.environment(*_gcp_box())
    rendered = {k: Template(v).render(env_var=box.__getitem__) if isinstance(v, str) else v
                for k, v in target.items()}
    proxy = BACKENDS["bigquery"]
    assert rendered["api_endpoint"] == f"{proxy.scheme}://{proxy.host}:{proxy.port}"
    assert rendered["project"] == "alkera-local"
    # the dataset is the one the box's services.toml names
    assert target["schema"] == tomllib.loads(
        nw.services_toml("/world", *_gcp_box()))["bigquery"]["dataset"]


def test_bigquery_is_explicit_only():
    for slots, plugs in ((None, None), (DE_SLOTS, nw.profile_plugs("de/laptop", None)),
                         (DE_SLOTS, nw.profile_plugs("de/snowflake", None))):
        env = nw.environment(slots, plugs)
        assert not any(k.startswith("BIGQUERY_") for k in env)
        assert "bigquery" not in nw.services_toml("/world", slots, plugs)
