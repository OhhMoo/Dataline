"""The `de/gcp` box's overlay: BigQuery's engine started, reachable from
native-services only, and the proxy's alias, variables and endpoint on the episode.

    gateway/.venv/bin/python -m pytest tasks/harness/test_bigquery_overlay.py -q

No docker: the overlay is read as YAML (`docker compose config` over the same text
is in tasks/harness/evidence/bigquery_e2e.json's run). The unprofiled, da/snowflake
and da/laptop overlays stay byte-identical (test_backends.py's goldens).
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agent_under_test as aut  # noqa: E402
import container_episode as ce  # noqa: E402
import test_backends as tb  # noqa: E402


def _gcp(tmp_path, monkeypatch, profile="de/gcp"):
    monkeypatch.setattr(ce, "image_path", lambda image: tb.IMAGE_PATH)
    stack = tb._stack(tmp_path, profile.replace("/", "_"), profile=profile,
                      image="alkera-episode:w1-bigquery")
    return stack, tb._overlay_yaml(stack.write_overlay().read_text(encoding="utf-8"))


def test_engine_started_under_its_profile_on_backend_net_only(tmp_path, monkeypatch):
    stack, overlay = _gcp(tmp_path, monkeypatch)
    assert "bigquery" in stack.compose_env()["COMPOSE_PROFILES"].split(",")
    engine = overlay["services"]["bigquery"]
    assert engine["command"] == ["--project=alkera-local", "--port=9050",
                                 "--grpc-port=9060", "--database=/bigquery-data/emulator.db"]
    # no `bigquery.internal` on backend_net: there it would name the proxy too
    assert engine["networks"] == {"backend_net": {"aliases": []}}


def test_native_services_reaches_the_engine_and_waits_for_it(tmp_path, monkeypatch):
    _, overlay = _gcp(tmp_path, monkeypatch)
    services = overlay["services"]["native-services"]
    assert "backend_net" in services["networks"]
    assert "bigquery.internal" in services["networks"]["episode_net"]["aliases"]
    assert services["environment"] == {"ALKERA_BIGQUERY_UPSTREAM": "http://bigquery:9050"}
    assert services["depends_on"] == {"bigquery": {"condition": "service_started"}}


def test_episode_gets_the_proxy_not_the_engine(tmp_path, monkeypatch):
    stack, overlay = _gcp(tmp_path, monkeypatch)
    episode = overlay["services"]["episode"]
    assert "networks" not in episode                 # compose.native.yml: episode_net only
    env = episode["environment"]
    assert env["BIGQUERY_API_URL"] == "http://bigquery.internal:9050"
    assert env["BIGQUERY_PROJECT"] == "alkera-local"
    assert "ALKERA_BIGQUERY_UPSTREAM" not in env
    assert "depends_on" not in episode               # a hosted backend has no container
    endpoints = stack.episode_endpoints()
    assert endpoints["bigquery"] == "http://bigquery.internal:9050"
    allowed = aut.allowed_destinations(endpoints)
    assert ("bigquery.internal", 9050) in allowed
    assert not any(host == "bigquery" for host, _ in allowed)


def test_a_box_without_bigquery_gets_none_of_it(tmp_path, monkeypatch):
    stack, overlay = _gcp(tmp_path, monkeypatch, profile="de/laptop")
    assert "bigquery" not in overlay["services"]
    assert "bigquery" not in stack.compose_env().get("COMPOSE_PROFILES", "")
    services = overlay["services"]["native-services"]
    assert "backend_net" not in services["networks"] and "environment" not in services
    assert ("bigquery.internal", 9050) not in aut.allowed_destinations(
        stack.episode_endpoints())


def test_de_gcp_runs_on_the_bigquery_client_image():
    import episode

    profile = episode.environment_profile("de/gcp", {})
    assert ce.profile_image(ce.DEFAULT_IMAGE, profile) == "alkera-episode:head-bigquery"
