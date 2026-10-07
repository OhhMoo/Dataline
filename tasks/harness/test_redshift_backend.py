"""The Redshift plug's registry rows, as the container topology and the box read them.

    gateway/.venv/bin/python -m pytest tasks/harness/test_redshift_backend.py -q

The `de/aws` profile plugs `warehouse: redshift` (the Postgres persona,
gateway/docker/redshift_start.py). Its one `backends.BACKENDS` row and its
`native_workspace.DISCOVERY` row are all the harness and the world need: the
overlay declares the whole service, the episode gets REDSHIFT_* and may dial
redshift.internal:5439, and the box's discovery files and dbt profile name it.
No docker; tasks/harness/redshift_e2e_check.py is the live half.
"""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "world"))

import agent_under_test as aut  # noqa: E402
import container_episode as ce  # noqa: E402
import test_backends as tb  # noqa: E402
from world import native_workspace as nw  # noqa: E402

DE_SLOTS = ["files", "warehouse", "transform", "orchestrator", "object_store", "ingest",
            "bi", "observability", "release", "human"]


def _aws(tmp_path, name="r"):
    world = tb._world(tmp_path / name, "2026-03-02T00:00:00Z", "")
    return ce.NativeContainerEpisode(project="t", world=world, audit_dir=tmp_path / name / "audit",
                                     rollout_dir=tmp_path / name / "rollout",
                                     image="alkera-episode:w1-redshift", profile="de/aws")


def test_redshift_row_is_an_own_container_postgres_backend():
    from alkera_gateway import backends

    b = backends.BACKENDS["redshift"]
    assert (b.host, b.port, b.scheme, b.hosted, b.tls) == \
        ("redshift.internal", 5439, "postgres", False, False)
    assert (b.dockerfile, b.clients, b.seeds_world, b.statement_tool) == \
        ("Dockerfile.redshift", "redshift", True, "redshift.statement")
    assert dict(b.env) == {"REDSHIFT_HOST": "redshift.internal", "REDSHIFT_PORT": "5439",
                           "REDSHIFT_DATABASE": "dev", "REDSHIFT_USER": "alkera",
                           "REDSHIFT_PASSWORD": backends.FAKE_CREDENTIAL}
    assert (HERE.parent.parent / "gateway" / "docker" / b.dockerfile).is_file()
    assert backends.plugged({"warehouse": ("redshift",), "bi": ("tableau",)}) == (b,)


def test_de_aws_overlay_declares_the_whole_redshift_service(tmp_path, monkeypatch):
    monkeypatch.setattr(ce, "image_path", lambda image: tb.IMAGE_PATH)
    stack = _aws(tmp_path)
    overlay = tb._overlay_yaml(stack.write_overlay().read_text(encoding="utf-8"))
    service = overlay["services"]["redshift"]
    assert service["profiles"] == ["redshift"]
    assert service["image"] == "alkera-redshift:native-w1-redshift"
    assert service["build"] == {"context": "../..",
                                "dockerfile": "gateway/docker/Dockerfile.redshift",
                                "args": {"BASE": "alkera-episode:w1-redshift"}}
    assert service["networks"] == {"episode_net": {"aliases": ["redshift.internal"]}}
    env = service["environment"]
    assert env["ALKERA_REDSHIFT_SEED"] == "/seed"
    assert env["ALKERA_REDSHIFT_DESTINATIONS"] == "/audit/destinations.redshift.jsonl"
    assert env["ALKERA_REDSHIFT_STATEMENT_LOG"] == "/redshift-data/statements.jsonl"
    assert env["ALKERA_NATIVE_AUDIT"] == "/audit/actions.jsonl"
    assert any(v.endswith(":/seed/warehouse.duckdb:ro") for v in service["volumes"])
    assert ":153F'" in service["healthcheck"]["test"][-1]          # 5439, LISTEN, no dial
    assert overlay["volumes"] == {"redshiftdata": None}
    episode = overlay["services"]["episode"]
    assert episode["depends_on"] == {"redshift": {"condition": "service_healthy"}}
    for key, value in {"REDSHIFT_HOST": "redshift.internal", "REDSHIFT_PORT": "5439",
                       "REDSHIFT_DATABASE": "dev", "REDSHIFT_USER": "alkera"}.items():
        assert episode["environment"][key] == value
    # a Redshift shop's box has no Postgres warehouse and no Snowflake
    assert not any(k.startswith(("PG", "SNOWFLAKE_")) for k in episode["environment"])
    assert "REQUESTS_CA_BUNDLE" not in episode["environment"]
    assert "redshift" in stack.compose_env()["COMPOSE_PROFILES"].split(",")


def test_de_aws_may_dial_redshift_and_only_when_plugged(tmp_path):
    endpoints = _aws(tmp_path).episode_endpoints()
    assert endpoints["redshift"] == "postgres://redshift.internal:5439"
    assert ("redshift.internal", 5439) in aut.allowed_destinations(endpoints)
    laptop = tb._stack(tmp_path, "l", profile="de/laptop").episode_endpoints()
    assert ("redshift.internal", 5439) not in aut.allowed_destinations(laptop)


def test_de_aws_image_carries_the_redshift_clients():
    import episode

    profile = episode.environment_profile("de/aws")
    assert ce.profile_image(ce.DEFAULT_IMAGE, profile) == "alkera-episode:head-redshift"


def test_box_discovery_names_redshift_in_all_four_files(tmp_path):
    plugs = nw.profile_plugs("de/aws", None)
    assert plugs["warehouse"] == ["redshift"]
    env = nw.environment(DE_SLOTS, plugs)
    assert env["REDSHIFT_HOST"] == "redshift.internal" and env["REDSHIFT_DATABASE"] == "dev"
    assert "PGHOST" not in env
    assert 'export REDSHIFT_PORT="5439"' in nw.envrc_block(DE_SLOTS, plugs)
    toml = tomllib.loads(nw.services_toml("/world", DE_SLOTS, plugs))
    assert toml["redshift"] == {"host": "redshift.internal", "port": 5439, "database": "dev",
                                "user": "alkera", "password": nw.FAKE_TOKEN,
                                "schema": "public", "sslmode": "disable"}
    assert "warehouse" not in toml
    page = nw.onboarding_markdown(tmp_path, {}, DE_SLOTS, plugs)
    assert "| the warehouse | `redshift.internal:5439` | psql, redshift_connector, " \
           "dbt-redshift | see `.envrc` |" in page


def test_box_dbt_profile_defaults_to_redshift_without_ssl(tmp_path, monkeypatch):
    rerooted, staged = tb._staged(tmp_path, monkeypatch, "d", profile="de/aws")
    profile = yaml.safe_load(staged)["world_dbt"]
    assert profile["target"] == "redshift"
    assert profile["outputs"]["dev"] == yaml.safe_load(rerooted)["world_dbt"]["outputs"]["dev"]
    target = profile["outputs"]["redshift"]
    assert target == {"type": "redshift", "host": "{{ env_var('REDSHIFT_HOST') }}",
                      "user": "{{ env_var('REDSHIFT_USER') }}",
                      "password": "{{ env_var('REDSHIFT_PASSWORD') }}",
                      "dbname": "{{ env_var('REDSHIFT_DATABASE') }}", "port": 5439,
                      "schema": "public", "threads": 1, "sslmode": "disable"}
    # every variable it reads is in the episode's environment under this profile
    _, added = _aws(tmp_path, "e").profile_variables()
    for value in target.values():
        if isinstance(value, str) and "env_var(" in value:
            assert value.split("'")[1] in added, value


def test_statement_rows_are_sql_and_containment_probes_the_alias():
    import importlib.util

    import path as harness_path

    assert harness_path.TOOL_KINDS["redshift.statement"] == "sql"
    docker = HERE.parent.parent / "gateway" / "docker"
    sys.path.insert(0, str(docker))
    try:
        spec = importlib.util.spec_from_file_location("containment_for_rs", docker / "containment.py")
        containment = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(containment)
    finally:
        sys.path.remove(str(docker))
    assert containment.NATIVE_BACKENDS["redshift"] == ("redshift.internal", 5439)
