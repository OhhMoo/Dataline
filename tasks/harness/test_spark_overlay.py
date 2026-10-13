"""The `ds/databricks` box's overlay: the Spark engine built and started on
backend_net only, seeded from the world's warehouse files; native-services
reaching it; and the proxy's alias, variables and endpoint on the episode.

    gateway/.venv/bin/python -m pytest tasks/harness/test_spark_overlay.py -q

No docker: the overlay is read as YAML (`docker compose config` over the same
text is part of tasks/harness/spark_e2e_check.py's run). The unprofiled,
da/snowflake and da/laptop overlays stay byte-identical (test_backends.py's
goldens), and de/databricks - no compute slot - gets none of tier 2.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agent_under_test as aut  # noqa: E402
import container_episode as ce  # noqa: E402
import test_backends as tb  # noqa: E402

IMAGE = "alkera-episode:t14-ds-databricks-spark"


def _box(tmp_path, monkeypatch, profile="ds/databricks", plugs=None):
    monkeypatch.setattr(ce, "image_path", lambda image: tb.IMAGE_PATH)
    stack = tb._stack(tmp_path, profile.replace("/", "_"), profile=profile, image=IMAGE,
                      **({"plugs": plugs} if plugs is not None else {}))
    return stack, tb._overlay_yaml(stack.write_overlay().read_text(encoding="utf-8"))


def test_engine_is_built_and_started_on_backend_net_only(tmp_path, monkeypatch):
    stack, overlay = _box(tmp_path, monkeypatch)
    assert "spark" in stack.compose_env()["COMPOSE_PROFILES"].split(",")
    engine = overlay["services"]["spark"]
    assert engine["profiles"] == ["spark"]
    assert engine["build"] == {"context": "../..",
                               "dockerfile": "gateway/docker/spark/Dockerfile.spark"}
    assert engine["image"] == "alkera-spark:native-t14-ds-databricks-spark"
    assert engine["pull_policy"] == "build"
    # backend_net only, and no alias there: native-services dials `spark`
    assert engine["networks"] == {"backend_net": {"aliases": []}}
    # the world's warehouse FILES, read-only, at /seed - and nothing else of the world
    warehouse = (stack.world_root / "var/warehouse/warehouse.duckdb").as_posix()
    assert engine["volumes"] == [f"{warehouse}:/seed/warehouse.duckdb:ro"]


def test_native_services_reaches_the_engine_and_waits_for_it(tmp_path, monkeypatch):
    _, overlay = _box(tmp_path, monkeypatch)
    services = overlay["services"]["native-services"]
    assert "backend_net" in services["networks"]
    assert "spark_connect.internal" in services["networks"]["episode_net"]["aliases"]
    assert "databricks.internal" in services["networks"]["episode_net"]["aliases"]
    assert services["environment"] == {"ALKERA_SPARK_CONNECT_UPSTREAM": "spark:15002"}
    assert services["depends_on"] == {"spark": {"condition": "service_started"}}
    # the JVM's cold start fits the healthcheck's start period, not its 60 s of retries
    assert services["healthcheck"] == {"start_period": "600s"}


def test_episode_gets_the_proxy_not_the_engine(tmp_path, monkeypatch):
    stack, overlay = _box(tmp_path, monkeypatch)
    env = overlay["services"]["episode"]["environment"]
    assert env["SPARK_REMOTE"] == "sc://spark_connect.internal:15002"
    assert env["SPARK_HOME"] == "/usr/local/lib/python3.11/site-packages/pyspark"
    assert env["DATABRICKS_HOST"] == "https://databricks.internal"     # tier 1 beside it
    assert "ALKERA_SPARK_CONNECT_UPSTREAM" not in env
    endpoints = stack.episode_endpoints()
    assert endpoints["spark_connect"] == "http://spark_connect.internal:15002"
    allowed = aut.allowed_destinations(endpoints)
    assert ("spark_connect.internal", 15002) in allowed
    assert not any(host == "spark" for host, _ in allowed)
    # the notebook kernel is the same box
    assert overlay["services"]["jupyter"]["environment"]["SPARK_REMOTE"] == \
        env["SPARK_REMOTE"]


def test_a_box_without_tier_2_gets_none_of_it(tmp_path, monkeypatch):
    for profile, plugs in (("de/databricks", None), ("ds/databricks", {"compute": []})):
        stack, overlay = _box(tmp_path / profile.replace("/", "_"), monkeypatch,
                              profile, plugs)
        assert "spark" not in overlay["services"]
        assert "spark" not in stack.compose_env().get("COMPOSE_PROFILES", "").split(",")
        services = overlay["services"]["native-services"]
        assert "backend_net" not in services["networks"] and "environment" not in services
        assert "SPARK_REMOTE" not in overlay["services"]["episode"]["environment"]


def test_ds_databricks_runs_on_the_spark_client_image():
    import episode

    profile = episode.environment_profile("ds/databricks", {})
    assert ce.profile_image(ce.DEFAULT_IMAGE, profile) == \
        "alkera-episode:head-ds-databricks-spark"


def test_spark_rows_are_sql_kind():
    import path as harness_path

    assert harness_path.TOOL_KINDS["databricks.spark"] == "sql"
