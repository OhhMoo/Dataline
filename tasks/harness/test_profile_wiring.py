"""A task's environment profile, end to end through the harness.

    cd gateway && uv run pytest ../tasks/harness/test_profile_wiring.py -q

What a profiled task changes: every gateway config the harness writes carries
`profile`/`plugs`; the native services are launched with `--profile`/`--plug`;
the container topology's episode network has no alias for an excluded service;
the endpoint map the episode is handed and judged against names only what the
profile serves; a planned plug stops the rollout before anything starts. What
an UNPROFILED task changes: nothing, which is asserted against the same
functions called without a profile. No docker, no model.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agent_under_test as aut  # noqa: E402
import container_episode as ce  # noqa: E402
import episode  # noqa: E402
import native_stack  # noqa: E402

PROFILE = "da/laptop"
PLUGS = {"notebook": []}
# da/laptop with no notebook: workspace, duckdb, dbt, metabase_fake, workflow.
SERVICES = ("metabase", "jira", "slack", "confluence")
EXCLUDED = ("prometheus", "alertmanager", "loki", "airbyte", "deploy")


@pytest.fixture
def world(tmp_path: Path) -> dict:
    for name in ("r1", "r2", "r3"):
        (tmp_path / name).mkdir()
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


# ------------------------------------------------------------- the profile
def test_no_profile_resolves_to_none():
    assert episode.environment_profile() is None
    assert episode.environment_profile("", {}) is None
    assert episode.profile_keys() == {}
    assert episode.profile_argv() == []


def test_profile_resolves_and_spells_its_flags():
    profile = episode.environment_profile(PROFILE, PLUGS)
    assert profile.services == SERVICES
    assert episode.profile_keys(PROFILE, PLUGS) == {"profile": PROFILE,
                                                    "plugs": {"notebook": []}}
    assert episode.profile_argv(PROFILE, PLUGS) == [
        "--profile", PROFILE, "--plug", "notebook="]
    assert episode.profile_argv("de/laptop", {"warehouse": ["duckdb", "postgres"]}) \
        == ["--profile", "de/laptop", "--plug", "warehouse=duckdb,postgres"]


def test_planned_plug_is_a_harness_error_with_the_profile_text():
    # de/aws plugs redshift and tableau, both planned
    with pytest.raises(episode.HarnessError, match="planned, not built: redshift"):
        episode.environment_profile("de/aws")
    with pytest.raises(episode.HarnessError, match="--plug|profile"):
        episode.environment_profile("", {"notebook": []})


def test_rollout_stops_on_a_planned_plug_before_the_world(monkeypatch, tmp_path):
    import rollout
    import taskspec

    spec = SimpleNamespace(variant="", profile="de/aws", plugs={})
    monkeypatch.setattr(taskspec, "load", lambda *a, **k: spec)

    def no_world(*a, **k):
        raise AssertionError("the world was built for an unservable profile")

    monkeypatch.setattr(episode, "build_world", no_world)
    args = SimpleNamespace(tasks_dir=None, task="T", variant=None,
                           out=str(tmp_path / "r"))
    with pytest.raises(episode.HarnessError, match="planned, not built: redshift"):
        rollout.run_rollout(args)
    assert not (tmp_path / "r").exists()


def test_profile_toolbelt():
    belt = {"sql_query": "q", "bi_list": "b", "sched_list": "s", "bash": "sh"}
    assert episode.profile_toolbelt(belt, None) is belt
    assert episode.profile_toolbelt(belt, episode.environment_profile(PROFILE, PLUGS)) \
        == {"sql_query": "q", "bi_list": "b", "bash": "sh"}


# ------------------------------------------------------------ gateway configs
def test_write_gateway_config_unprofiled_is_byte_identical(world, tmp_path):
    a = episode.write_gateway_config(tmp_path / "a.json", world, tmp_path / "audit",
                                     "ep", "")
    b = episode.write_gateway_config(tmp_path / "b.json", world, tmp_path / "audit",
                                     "ep", "", profile="", plugs={})
    assert a.read_bytes() == b.read_bytes()
    cfg = json.loads(a.read_text())
    assert "profile" not in cfg and "plugs" not in cfg


def test_write_gateway_config_profiled(world, tmp_path):
    plain = json.loads(episode.write_gateway_config(
        tmp_path / "a.json", world, tmp_path / "audit", "ep", "").read_text())
    cfg = json.loads(episode.write_gateway_config(
        tmp_path / "b.json", world, tmp_path / "audit", "ep", "",
        profile=PROFILE, plugs=PLUGS).read_text())
    assert cfg == {**plain, "profile": PROFILE, "plugs": {"notebook": []}}


def test_probe_config_follows_the_spec():
    # probe_surfaces reads the profile off the spec it is given
    assert episode.spec_profile_keys(SimpleNamespace()) == {}
    assert episode.spec_profile_keys(SimpleNamespace(profile="", plugs={})) == {}
    assert episode.spec_profile_keys(SimpleNamespace(profile=PROFILE, plugs=PLUGS)) \
        == {"profile": PROFILE, "plugs": {"notebook": []}}


def test_container_gateway_config(world):
    plain = ce.container_gateway_config(world, "ep", "")
    assert ce.container_gateway_config(world, "ep", "", profile="", plugs={}) == plain
    assert list(plain) == list(ce.container_gateway_config(world, "ep", ""))
    assert "profile" not in plain and "plugs" not in plain
    assert ce.container_gateway_config(world, "ep", "", profile=PROFILE, plugs=PLUGS) \
        == {**plain, "profile": PROFILE, "plugs": {"notebook": []}}


def test_native_stack_config(world, tmp_path):
    plain = native_stack.NativeStack(world=world, audit_dir=tmp_path / "a1",
                                     rollout_dir=tmp_path / "r1")
    spec0 = SimpleNamespace(profile="", plugs={})
    nospec = native_stack.NativeStack(world=world, audit_dir=tmp_path / "a1",
                                      rollout_dir=tmp_path / "r2", spec=spec0)
    base = json.loads(plain._config_path().read_text())
    assert json.loads(nospec._config_path().read_text()) == base
    spec = SimpleNamespace(profile=PROFILE, plugs=PLUGS)
    prof = native_stack.NativeStack(world=world, audit_dir=tmp_path / "a1",
                                    rollout_dir=tmp_path / "r3", spec=spec)
    assert json.loads(prof._config_path().read_text()) == {
        **base, "profile": PROFILE, "plugs": {"notebook": []}}


# ----------------------------------------------------------------- serve argv
def test_native_stack_serve_argv(world, tmp_path):
    plain = native_stack.NativeStack(world=world, audit_dir=tmp_path / "a",
                                     rollout_dir=tmp_path / "r1", offset=0)
    argv = plain._serve_argv()
    assert "--profile" not in argv and "--plug" not in argv
    assert argv[-2] == "--gateway-config"
    spec = SimpleNamespace(profile=PROFILE, plugs=PLUGS)
    prof = native_stack.NativeStack(world=world, audit_dir=tmp_path / "a",
                                    rollout_dir=tmp_path / "r2", spec=spec, offset=0)
    pargv = prof._serve_argv()
    assert pargv[:len(argv) - 1] == argv[:-1]
    assert pargv[-4:] == ["--profile", PROFILE, "--plug", "notebook="]


def test_native_stack_awaits_only_the_profiles_ports():
    from alkera_gateway.native import serve

    assert native_stack.service_ports(None) == native_stack.service_ports()
    ports = native_stack.service_ports(episode.environment_profile(PROFILE, PLUGS))
    assert ports == sorted({*(serve.STUB_PORTS[s] for s in SERVICES),
                            serve.AGGREGATE_PORT})
    with_store = native_stack.service_ports(
        episode.environment_profile("de/laptop"))
    assert serve.MINIO_AUDIT_PORT in with_store


def test_native_stack_endpoints(world, tmp_path):
    plain = native_stack.NativeStack(world=world, audit_dir=tmp_path / "a",
                                     rollout_dir=tmp_path / "r1", offset=0)
    ep = plain._write_endpoints(None)
    assert set(aut.NATIVE_SERVICES) <= set(ep)
    spec = SimpleNamespace(profile=PROFILE, plugs=PLUGS)
    prof = native_stack.NativeStack(world=world, audit_dir=tmp_path / "a",
                                    rollout_dir=tmp_path / "r2", spec=spec, offset=0)
    pep = prof._write_endpoints(None)
    assert {n for n in aut.NATIVE_SERVICES if n in pep} == set(SERVICES)
    written = json.loads(prof.endpoints_file.read_text())
    assert set(written) == set(SERVICES)
    assert "sched" not in pep and "s3" not in pep


# ---------------------------------------------------------- the endpoint map
def test_profile_endpoints_unprofiled_is_unchanged():
    ep = aut.resolve_endpoints(None)
    assert aut.profile_endpoints(ep, None) == ep


def test_profile_endpoints_narrow_to_the_profile():
    ep = aut.profile_endpoints(aut.resolve_endpoints(None),
                               episode.environment_profile(PROFILE, PLUGS))
    assert {n for n in aut.NATIVE_SERVICES if n in ep} == set(SERVICES)
    assert not any(n in ep for n in EXCLUDED)
    # no orchestrator, no object store in da/laptop
    assert "sched" not in ep
    assert not any(k.startswith("s3") for k in ep)
    assert "postgres" in ep
    de = aut.profile_endpoints(aut.resolve_endpoints(None),
                               episode.environment_profile("de/laptop"))
    assert de == aut.resolve_endpoints(None)


def test_allowed_destinations(tmp_path):
    full = aut.resolve_endpoints(None)
    narrow = aut.profile_endpoints(full, episode.environment_profile(PROFILE, PLUGS))
    allowed = aut.allowed_destinations(narrow)
    assert ("jira.internal", 8080) in allowed
    assert ("loki.internal", 3100) not in allowed
    assert ("sched.internal", 8120) not in allowed
    assert ("minio.internal", 9000) not in allowed
    assert ("postgres.internal", 5432) in allowed
    assert allowed < aut.allowed_destinations(full)


def test_native_env(tmp_path):
    full = aut.resolve_endpoints(None)
    env = aut.native_env(endpoints=full, audit_dir=tmp_path, profiles_dir=tmp_path)
    narrow = aut.profile_endpoints(full, episode.environment_profile(PROFILE, PLUGS))
    penv = aut.native_env(endpoints=narrow, audit_dir=tmp_path, profiles_dir=tmp_path)
    assert "LOKI_URL" in env and "AWS_ENDPOINT_URL" in env
    assert "LOKI_URL" not in penv and "DEPLOY_URL" not in penv
    assert not any(k.startswith("AWS_") for k in penv)
    assert penv["JIRA_URL"] == env["JIRA_URL"]
    assert {k: v for k, v in env.items() if k in penv} == penv


# --------------------------------------------------- the container topology
def _stack(world, tmp_path, name, **kw):
    return ce.NativeContainerEpisode(project="t", world=world,
                                     audit_dir=tmp_path / "audit",
                                     rollout_dir=tmp_path / name, **kw)


@pytest.fixture
def no_docker(monkeypatch):
    monkeypatch.setattr(ce, "image_path", lambda image: "/usr/bin:/bin")


def test_overlay_unprofiled_is_byte_identical(world, tmp_path, no_docker):
    a = _stack(world, tmp_path, "r1").write_overlay().read_bytes()
    b = _stack(world, tmp_path, "r2", profile="", plugs={}).write_overlay().read_bytes()
    assert a == b
    assert b"!override" not in a and b"!reset" not in a


def _block(text: str, service: str) -> str:
    """One top-level service's block of the generated overlay."""
    m = re.search(rf"^  {re.escape(service)}:\n((?:    .*\n|\n)*)", text, re.M)
    assert m, f"no {service} block in\n{text}"
    return m.group(1)


def test_overlay_profiled_aliases(world, tmp_path, no_docker):
    plain = _stack(world, tmp_path, "r1").write_overlay().read_text()
    text = _stack(world, tmp_path, "r2", profile=PROFILE,
                  plugs=PLUGS).write_overlay().read_text()
    # everything the unprofiled overlay says, it still says, in order
    kept = "\n".join(line for line in text.splitlines() if "!reset null" not in line)
    assert plain.split("services:\n", 1)[1] in kept + "\n"
    services = _block(text, "native-services")
    aliases = re.findall(r"^          - (\S+)$", services, re.M)
    assert "aliases: !override" in services
    assert aliases == ["native-services.internal",
                       *(f"{s}.internal" for s in SERVICES)]
    assert "sched.internal" not in aliases
    minio = _block(text, "minio")
    assert "aliases: !reset []" in minio
    episode_env = _block(text, "episode")
    for var in ("LOKI_URL", "LOKI_ADDR", "DEPLOY_URL", "DEPLOY_API_TOKEN",
                "AIRBYTE_URL", "PROMETHEUS_URL", "ALERTMANAGER_URL",
                "SCHED_URL", "AWS_ENDPOINT_URL"):
        assert f"      {var}: !reset null" in episode_env, var
    for var in ("JIRA_URL", "METABASE_URL", "SLACK_BASE_URL", "PGHOST"):
        assert var not in episode_env


def test_overlay_keeps_sched_and_minio_when_plugged(world, tmp_path, no_docker):
    text = _stack(world, tmp_path, "r", profile="de/laptop",
                  plugs={"bi": []}).write_overlay().read_text()
    services = _block(text, "native-services")
    assert "- sched.internal" in services
    assert "- metabase.internal" not in services
    assert "minio:" not in text
    assert "AWS_ENDPOINT_URL" not in text and "SCHED_URL" not in text


def test_reset_env_names_exist_in_the_static_overlay():
    static = ce.NATIVE_OVERLAY.read_text(encoding="utf-8")
    for names in ce.EPISODE_SERVICE_ENV.values():
        for var in names:
            assert re.search(rf"^      {var}: ", static, re.M), var
    for name in aut.NATIVE_SERVICES:
        assert name in ce.EPISODE_SERVICE_ENV
        assert f"- {name}.internal" in static


def test_compose_env_carries_the_serve_flags(world, tmp_path):
    assert _stack(world, tmp_path, "r1").compose_env()[
        "ALKERA_NATIVE_PROFILE_ARGS"] == ""
    env = _stack(world, tmp_path, "r2", profile=PROFILE, plugs=PLUGS).compose_env()
    assert env["ALKERA_NATIVE_PROFILE_ARGS"] == "--profile da/laptop --plug notebook="
    static = ce.NATIVE_OVERLAY.read_text(encoding="utf-8")
    command = static.split("\n  native-services:\n", 1)[1].split("stop_grace_period", 1)[0]
    assert "${ALKERA_NATIVE_PROFILE_ARGS:-}" in command


def test_episode_endpoints(world, tmp_path):
    assert _stack(world, tmp_path, "r1").episode_endpoints() \
        == aut.resolve_endpoints(None)
    ep = _stack(world, tmp_path, "r2", profile=PROFILE,
                plugs=PLUGS).episode_endpoints()
    assert {n for n in aut.NATIVE_SERVICES if n in ep} == set(SERVICES)
    assert "sched" not in ep and "s3" not in ep


def test_surface_gate_reports_a_profiled_out_surface_without_dialling():
    # surface_audit reads `endpoints[surface]` directly; a profiled endpoint map
    # (profile_endpoints) no longer has the excluded keys, so the gate must say
    # "excluded_by_profile" instead of raising KeyError mid-rollout.
    import surface_audit

    live, code, _ = surface_audit._run_native_check(
        "jira", {}, {}, [], 0.1)
    assert (live, code) == (False, "excluded_by_profile")
    live, code, _ = surface_audit._run_native_check(
        "sched", {}, {}, [], 0.1, served_planes=frozenset({"sched"}))
    assert (live, code) == (False, "excluded_by_profile")


# ------------------------------------------------------ the notebook plug
def test_overlay_without_notebook_has_no_jupyter(world, tmp_path, no_docker):
    for name, kw in (("r1", {}), ("r2", {"profile": PROFILE, "plugs": PLUGS})):
        text = _stack(world, tmp_path, name, **kw).write_overlay().read_text()
        assert "jupyter" not in text.split("services:\n", 1)[1]
    assert "COMPOSE_PROFILES" not in {
        k: v for k, v in _stack(world, tmp_path, "r3").compose_env().items()
        if k not in os.environ}


def test_overlay_with_notebook_gives_jupyter_the_episodes_box(world, tmp_path,
                                                             no_docker):
    stack = _stack(world, tmp_path, "r", profile="da/laptop", plugs={})
    text = stack.write_overlay().read_text()
    episode_block, jupyter = _block(text, "episode"), _block(text, "jupyter")
    # the same shim PATH, watch list, workspace and warehouse FILE binds
    for line in episode_block.splitlines():
        if line.strip().startswith(("PATH:", "ALKERA_SHIM_", "ALKERA_WORKSPACE:",
                                    "- \"")):
            assert line in jupyter.splitlines(), line
    # the same endpoint resets: an excluded service is absent from both
    for var in ("LOKI_URL", "SCHED_URL", "AWS_ENDPOINT_URL"):
        assert f"      {var}: !reset null" in jupyter, var
    assert '      JUPYTER_URL: "http://jupyter.internal:8888"' in episode_block
    assert "notebook" in stack.compose_env()["COMPOSE_PROFILES"].split(",")


def test_compose_profiles_keep_the_callers(world, tmp_path, monkeypatch):
    monkeypatch.setenv("COMPOSE_PROFILES", "airflow")
    env = _stack(world, tmp_path, "r", profile="da/laptop").compose_env()
    assert env["COMPOSE_PROFILES"].split(",") == ["airflow", "notebook"]
    assert _stack(world, tmp_path, "r2").compose_env()["COMPOSE_PROFILES"] == "airflow"


def test_static_jupyter_service_is_profile_gated():
    static = ce.NATIVE_OVERLAY.read_text(encoding="utf-8")
    block = static.split("\n  jupyter:\n", 1)[1].split("\n  # The nine fake", 1)[0]
    assert "profiles: [notebook]" in block
    assert "environment: *native-endpoints" in block
    assert "- jupyter.internal" in block


# --------------------------------------------- the world agrees with the task
def _world_with_profile(tmp_path, record):
    root = tmp_path / "w"
    (root / "var" / "world").mkdir(parents=True)
    if record is not None:
        (root / "var" / "world" / "profile.json").write_text(json.dumps(record))
    return root


def test_world_profile_check_unprofiled_world_and_task(tmp_path):
    episode.check_world_profile(_world_with_profile(tmp_path, None), None)


def test_world_profile_check_matching(tmp_path):
    profile = episode.environment_profile(PROFILE, PLUGS)
    root = _world_with_profile(tmp_path, {"name": PROFILE,
                                          "slots": sorted(profile.slots)})
    episode.check_world_profile(root, profile)


@pytest.mark.parametrize("record,task,match", [
    (None, (PROFILE, PLUGS), "built without"),
    ({"name": PROFILE, "slots": ["files"]}, None, "declares no profile"),
    ({"name": PROFILE, "slots": ["files", "warehouse"]}, (PROFILE, PLUGS), "slots"),
    ({"name": "de/laptop", "slots": None}, (PROFILE, PLUGS), "de/laptop"),
])
def test_world_profile_check_refuses_disagreement(tmp_path, record, task, match):
    profile = episode.environment_profile(*task) if task else None
    if record and record["slots"] is None:
        record["slots"] = sorted(profile.slots)
    with pytest.raises(episode.HarnessError, match=match):
        episode.check_world_profile(_world_with_profile(tmp_path, record), profile)


def test_jupyter_strips_what_the_episode_strips():
    import yaml

    static = ce.NATIVE_OVERLAY.read_text(encoding="utf-8")
    plain = re.sub(r"!(override|reset)\b", "", static)
    entry = yaml.safe_load(plain)["services"]["jupyter"]["entrypoint"]
    stripped = [entry[i + 1] for i, arg in enumerate(entry) if arg == "--strip"]
    assert stripped == list(ce.STRIP_PATHS)
