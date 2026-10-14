"""The Spark Connect discovery row (native_workspace.DISCOVERY["spark_connect"]).

A `ds/databricks` box - compute is Databricks tier 2, Spark Connect - names the
cluster in the discovery files beside tier 1's workspace: SPARK_REMOTE (and the
SPARK_HOME the thin client needs under `python -c`) in .envrc, an onboarding
row, a `[spark_connect]` services.toml section; no dbt target (the box's dbt
target stays tier 1's). A box without the compute slot (de/databricks) or one
that unplugs it names it nowhere, and every other box keeps its bytes.
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

WORLD_DIR = Path(__file__).resolve().parents[1]
REPO = WORLD_DIR.parent
sys.path.insert(0, str(WORLD_DIR))

from world import native_workspace as nw  # noqa: E402

DS_SLOTS = ["files", "notebook", "compute", "warehouse", "transform", "orchestrator",
            "object_store", "human"]
DE_SLOTS = ["files", "warehouse", "transform", "orchestrator", "object_store", "ingest",
            "bi", "observability", "release", "human"]


def _box():
    return DS_SLOTS, nw.profile_plugs("ds/databricks", None)


def test_the_stack_supplies_the_compute_plug():
    assert nw.profile_plugs("ds/databricks", None) == {
        "warehouse": ["databricks"], "compute": ["spark_connect"]}
    # a record that names its compute keeps it, an empty list included
    assert nw.profile_plugs("ds/databricks", {"compute": []})["compute"] == []
    # no other stack has a registry backend in its compute slot
    assert nw.profile_plugs("ds/laptop", None) == {"warehouse": ["duckdb"]}


def test_stack_compute_is_the_gateways():
    sys.path.insert(0, str(REPO / "gateway" / "src"))
    from alkera_gateway import backends, plugs

    theirs = {stack: tuple(p for p in slots.get("compute", ()) if p in backends.BACKENDS)
              for stack, slots in plugs.STACKS.items()}
    assert nw.STACK_COMPUTE == {k: v for k, v in theirs.items() if v}


def test_ds_databricks_box_exports_spark_remote_beside_tier_1(tmp_path):
    slots, plugs = _box()
    env = nw.environment(slots, plugs)
    assert env["SPARK_REMOTE"] == "sc://spark_connect.internal:15002"
    assert env["SPARK_HOME"] == "/usr/local/lib/python3.11/site-packages/pyspark"
    assert env["DATABRICKS_HOST"] == "https://databricks.internal"
    block = nw.envrc_block(slots, plugs)
    assert 'export SPARK_REMOTE="sc://spark_connect.internal:15002"' in block
    assert "# The cluster." in block
    toml = tomllib.loads(nw.services_toml("/world", slots, plugs))
    assert toml["spark_connect"]["host"] == "spark_connect.internal"
    assert toml["spark_connect"]["port"] == 15002
    assert toml["spark_connect"]["remote"] == "sc://spark_connect.internal:15002"
    assert (toml["spark_connect"]["catalog"], toml["spark_connect"]["schema"]) == \
        ("main", "default")
    page = nw.onboarding_markdown(tmp_path, {}, slots, plugs)
    d = nw.DISCOVERY["spark_connect"]
    assert f"| the cluster | `spark_connect.internal:15002` | {d.clients} |" in page
    assert "SparkSession.builder.getOrCreate()" in d.clients and "no dbutils" in d.clients
    assert d.dbt_type is None


def test_a_box_without_tier_2_names_it_nowhere(tmp_path):
    for slots, plugs in ((None, None),
                         (DE_SLOTS, nw.profile_plugs("de/databricks", None)),
                         (DS_SLOTS, nw.profile_plugs("ds/databricks", {"compute": []})),
                         (DS_SLOTS, nw.profile_plugs("ds/laptop", {"compute": []}))):
        files = [nw.envrc_block(slots, plugs), nw.services_toml("/world", slots, plugs),
                 nw.onboarding_markdown(tmp_path, {}, slots, plugs)]
        assert not any("spark" in text.lower() for text in files), (slots, plugs)
        assert "SPARK_REMOTE" not in nw.environment(slots, plugs)
