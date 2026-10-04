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
    # de/aws plugs redshift (planned) and tableau (partial since the BI build)
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


def _env(block: str) -> dict:
    """The `environment:` mapping of one service block, comments skipped."""
    out = {}
    for name, value in re.findall(r"^      ([A-Z][A-Z0-9_]*): (.*)$", block, re.M):
        out[name] = json.loads(value) if value.startswith('"') else value
    return out


def test_overlay_profiled_aliases(world, tmp_path, no_docker):
    plain = _stack(world, tmp_path, "r1").write_overlay().read_text()
    text = _stack(world, tmp_path, "r2", profile=PROFILE,
                  plugs=PLUGS).write_overlay().read_text()
    # every variable the unprofiled overlay sets, the profiled one sets the same
    assert _env(_block(plain, "episode")).items() <= _env(_block(text, "episode")).items()
    assert _block(plain, "episode").split("    volumes:\n", 1)[1] in _block(text, "episode")
    services = _block(text, "native-services")
    aliases = re.findall(r"^          - (\S+)$", services, re.M)
    assert "aliases: !override" in services
    assert aliases == ["native-services.internal",
                       *(f"{s}.internal" for s in SERVICES)]
    assert "sched.internal" not in aliases
    minio = _block(text, "minio")
    assert "aliases: !reset []" in minio
    # the WHOLE mapping, restated (`!override`): an excluded service's
    # variables are absent, not `!reset` (a no-op under Compose v5.6)
    episode_block = _block(text, "episode")
    assert "    environment: !override\n" in episode_block
    assert "!reset" not in episode_block
    episode_env = _env(episode_block)
    for var in ("LOKI_URL", "LOKI_ADDR", "DEPLOY_URL", "DEPLOY_API_TOKEN",
                "AIRBYTE_URL", "PROMETHEUS_URL", "ALERTMANAGER_URL",
                "SCHED_URL", "AWS_ENDPOINT_URL",
                "PGHOST"):                      # a DuckDB box: no Postgres warehouse
        assert var not in episode_env, var
    for var in ("JIRA_URL", "METABASE_URL", "SLACK_BASE_URL", "HOME",
                "ALKERA_NATIVE_AUDIT", "DBT_PROFILES_DIR"):
        assert var in episode_env, var


def test_overlay_keeps_sched_and_minio_when_plugged(world, tmp_path, no_docker):
    text = _stack(world, tmp_path, "r", profile="de/laptop",
                  plugs={"bi": []}).write_overlay().read_text()
    services = _block(text, "native-services")
    assert "- sched.internal" in services
    assert "- metabase.internal" not in services
    assert "minio:" not in text
    episode_env = _env(_block(text, "episode"))
    static = ce.static_environment("episode")
    # kept, with the static value: the orchestrator and the object store are plugged
    for var in ("AWS_ENDPOINT_URL", "SCHED_URL"):
        assert episode_env[var] == static[var], var
    assert "METABASE_URL" not in episode_env and "METABASE_API_KEY" not in episode_env


def test_reset_env_names_exist_in_the_static_overlay():
    static = ce.NATIVE_OVERLAY.read_text(encoding="utf-8")
    for name, names in ce.EPISODE_SERVICE_ENV.items():
        for var in names:
            # the nine are in the static file (and reset away); a BI vendor's
            # are NOT - the per-rollout overlay adds them when it is plugged
            present = re.search(rf"^      {var}: ", static, re.M) is not None
            assert present == (name in aut.NATIVE_SERVICES), (name, var)
    for name in aut.NATIVE_SERVICES:
        assert name in ce.EPISODE_SERVICE_ENV
        assert f"- {name}.internal" in static
    for name in aut.PLUG_SERVICES:
        assert ce.EPISODE_SERVICE_ENV[name] == tuple(aut.plug_service_env(name, "u"))
        assert f"- {name}.internal" not in static   # no alias (a comment may name it)


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
    # the same profile: an excluded service is absent from both, both restate
    # their whole mapping
    jupyter_env = _env(jupyter)
    for var in ("LOKI_URL", "SCHED_URL", "AWS_ENDPOINT_URL"):
        assert var not in jupyter_env and var not in _env(episode_block), var
    assert "    environment: !override\n" in jupyter
    assert jupyter_env["JIRA_URL"] == ce.static_environment("jupyter")["JIRA_URL"]
    assert "JUPYTER_URL" not in jupyter_env
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


# ------------------------------------------------ the explicit-only BI vendors
# powerbi, tableau and sigma are served only when a profile plugs them by name.
# Everything above that is unprofiled stays byte-identical (the tests above);
# these are the profiled cases.
BI_PLUGS = {"notebook": [], "bi": ["powerbi"]}
BI_SERVICES = ("powerbi", "jira", "slack", "confluence")
POWERBI_ENV = ("POWERBI_API_URL", "FABRIC_API_URL", "AZURE_AUTHORITY_HOST",
               "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID")


def test_no_bi_vendor_in_any_unprofiled_map():
    ep = aut.resolve_endpoints(None)
    assert not set(aut.PLUG_SERVICES) & set(ep)
    assert not set(aut.PLUG_SERVICES) & set(aut.NATIVE_SERVICES)
    for profile in (None, episode.environment_profile("de/laptop")):
        assert not set(aut.PLUG_SERVICES) & set(aut.profile_endpoints(ep, profile))


def test_profile_endpoints_add_a_plugged_vendor():
    ep = aut.profile_endpoints(aut.resolve_endpoints(None),
                               episode.environment_profile(PROFILE, BI_PLUGS))
    assert ep["powerbi"] == "http://powerbi.internal:8095"
    assert "metabase" not in ep and "tableau" not in ep and "sigma" not in ep
    sigma = aut.profile_endpoints(aut.resolve_endpoints(None),
                                  episode.environment_profile("de/snowflake"))
    assert sigma["sigma"] == "http://sigma.internal:8097"


def test_endpoint_file_may_relay_a_vendor_but_only_a_plugged_one(tmp_path):
    path = tmp_path / "ep.json"
    path.write_text(json.dumps({"powerbi": "http://127.0.0.1:18095",
                                "sigma": "http://127.0.0.1:18097"}))
    ep = aut.resolve_endpoints(path)
    narrowed = aut.profile_endpoints(ep, episode.environment_profile(PROFILE, BI_PLUGS))
    assert narrowed["powerbi"] == "http://127.0.0.1:18095"   # the relay, kept
    assert "sigma" not in narrowed
    path.write_text(json.dumps({"lookr": "http://x"}))
    with pytest.raises(episode.HarnessError, match="lookr"):
        aut.resolve_endpoints(path)


def test_allowed_destinations_and_native_env_with_a_vendor(tmp_path):
    ep = aut.profile_endpoints(aut.resolve_endpoints(None),
                               episode.environment_profile(PROFILE, BI_PLUGS))
    allowed = aut.allowed_destinations(ep)
    assert ("powerbi.internal", 8095) in allowed
    assert ("metabase.internal", 3000) not in allowed
    assert ("sigma.internal", 8097) not in allowed
    env = aut.native_env(endpoints=ep, audit_dir=tmp_path, profiles_dir=tmp_path)
    assert {k: env[k] for k in POWERBI_ENV} == aut.plug_service_env(
        "powerbi", "http://powerbi.internal:8095")
    assert env["AZURE_CLIENT_SECRET"] == aut.NATIVE_FAKE_CREDENTIAL
    assert "METABASE_URL" not in env and "SIGMA_BASE_URL" not in env
    plain = aut.native_env(endpoints=aut.resolve_endpoints(None), audit_dir=tmp_path,
                           profiles_dir=tmp_path)
    assert not set(POWERBI_ENV) & set(plain)


def test_plug_service_env_per_vendor():
    assert aut.plug_service_env("tableau", "U") == {
        "TABLEAU_SERVER_URL": "U", "TABLEAU_PAT_NAME": aut.NATIVE_FAKE_CREDENTIAL,
        "TABLEAU_PAT_SECRET": aut.NATIVE_FAKE_CREDENTIAL, "TABLEAU_SITE": ""}
    assert aut.plug_service_env("sigma", "U") == {
        "SIGMA_BASE_URL": "U", "SIGMA_CLIENT_ID": aut.NATIVE_FAKE_CREDENTIAL,
        "SIGMA_CLIENT_SECRET": aut.NATIVE_FAKE_CREDENTIAL}
    with pytest.raises(KeyError):
        aut.plug_service_env("metabase", "U")


def test_overlay_with_a_vendor_adds_its_alias_and_variables(world, tmp_path, no_docker):
    stack = _stack(world, tmp_path, "r", profile=PROFILE, plugs=BI_PLUGS)
    text = stack.write_overlay().read_text()
    services = _block(text, "native-services")
    aliases = re.findall(r"^          - (\S+)$", services, re.M)
    assert aliases == ["native-services.internal",
                       *(f"{s}.internal" for s in BI_SERVICES)]
    episode_env = _block(text, "episode")
    for var, value in aut.plug_service_env(
            "powerbi", "http://powerbi.internal:8095").items():
        assert f"      {var}: {json.dumps(value)}" in episode_env, var
    # metabase is dropped like any excluded service; no other vendor appears
    assert "METABASE_URL" not in _env(episode_env)
    assert "TABLEAU" not in text and "SIGMA" not in text
    assert "jupyter" not in text.split("services:\n", 1)[1]
    assert stack.episode_endpoints()["powerbi"] == "http://powerbi.internal:8095"


def test_overlay_with_a_vendor_and_the_notebook(world, tmp_path, no_docker):
    stack = _stack(world, tmp_path, "r", profile="da/laptop", plugs={"bi": ["sigma"]})
    text = stack.write_overlay().read_text()
    jupyter = _block(text, "jupyter")
    for var, value in aut.plug_service_env(
            "sigma", "http://sigma.internal:8097").items():
        assert f"      {var}: {json.dumps(value)}" in jupyter, var
    assert "METABASE_URL" not in _env(jupyter)
    assert "- sigma.internal" in _block(text, "native-services")


# ------------------------------------- the world's clock in the containers
def _clocked(world: dict, vnow: str | None = "2026-03-02T00:00:00Z") -> dict:
    state = Path(world["scheduler"])
    state.parent.mkdir(parents=True, exist_ok=True)
    if vnow is not None:
        state.write_text(json.dumps({"vnow": vnow, "jobs": {}}), encoding="utf-8")
    return world


@pytest.mark.parametrize("kw", [{}, {"profile": PROFILE, "plugs": BI_PLUGS},
                                {"profile": "da/laptop", "plugs": {}}])
def test_overlay_carries_the_world_clock(world, tmp_path, no_docker, kw):
    """The shims (episode) and the notebook kernel (jupyter) stamp rows with
    `native.audit.virtual_time`; `var/sched` is not mounted there (P7), so the
    overlay hands both the world's `vnow`, not GatewayConfig's literal."""
    from alkera_gateway.config import GatewayConfig

    _clocked(world)
    text = _stack(world, tmp_path, "r", **kw).write_overlay().read_text()
    for service in ("episode", *(("jupyter",) if "jupyter:" in text else ())):
        env = _env(_block(text, service))
        assert env[ce.CLOCK_ENV] == "2026-03-02T00:00:00Z", service
    assert ce.CLOCK_ENV == "ALKERA_VIRTUAL_TIME"
    assert GatewayConfig().virtual_time != "2026-03-02T00:00:00Z"


def test_the_clock_variable_is_the_one_the_row_writer_reads(monkeypatch):
    from alkera_gateway.native import audit

    for name in ("ALKERA_VTS", "ALKERA_SCHEDULER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(ce.CLOCK_ENV, "2026-03-02T00:00:00Z")
    assert audit.virtual_time() == "2026-03-02T00:00:00Z"


def test_world_clock_sources(world):
    assert ce.world_clock(world) == "2026-03-08T09:00:00Z"      # manifest fallback
    _clocked(world, "2026-03-02T00:00:00Z")
    assert ce.world_clock(world) == "2026-03-02T00:00:00Z"      # vnow wins
    bare = {k: v for k, v in world.items() if k != "virtual_time"}
    Path(world["scheduler"]).unlink()
    assert ce.world_clock(bare) is None


def test_overlay_without_a_clock_writes_no_clock_line(world, tmp_path, no_docker):
    bare = {k: v for k, v in world.items() if k != "virtual_time"}
    text = _stack(bare, tmp_path, "r").write_overlay().read_text()
    assert ce.CLOCK_ENV not in _env(_block(text, "episode"))


# --------------------- the profile's environment: !override, checked by compose
def test_profiled_environment_is_static_minus_excluded_plus_plugged(world, tmp_path,
                                                                   no_docker):
    stack = _stack(world, tmp_path, "r", profile=PROFILE, plugs=BI_PLUGS)
    text = stack.write_overlay().read_text()
    dropped, added = stack.profile_variables()
    plain = _env(_block(_stack(world, tmp_path, "r0").write_overlay().read_text(),
                        "episode"))
    static = ce.static_environment("episode")
    expect = {**{k: v for k, v in static.items() if k not in dropped},
              **added, **plain}
    assert _env(_block(text, "episode")) == expect
    # the static table is compose.yml's episode environment plus the anchor
    assert {"ALKERA_POSTGRES_DSN", "JIRA_URL", "SCHED_URL", "HOME"} <= set(static)
    assert set(ce.static_environment("jupyter")) < set(static)


BI001_PLUGS = {"bi": ["powerbi", "tableau", "sigma"]}


def _compose_config(stack) -> dict:
    """`docker compose config` over the stack's own files and environment.
    Client-side only: no daemon, no containers."""
    import shutil
    import subprocess

    if not shutil.which("docker"):
        pytest.skip("no docker CLI")
    argv = ["docker", "compose", "-p", "cfgcheck"]
    for path in stack.compose_files():
        argv += ["-f", str(path)]
    done = subprocess.run([*argv, "config", "--format", "json"], capture_output=True,
                          text=True, env=stack.compose_env(), timeout=120)
    if done.returncode != 0 and "is not a docker command" in done.stderr:
        pytest.skip("no docker compose plugin")
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_compose_config_drops_excluded_variables(world, tmp_path, no_docker):
    """Measured by compose itself, the property the `!reset null` lines claimed
    and did not have under Compose v5.6.0: a da/laptop box's episode AND jupyter
    environments name no excluded service, and are the unprofiled ones minus
    exactly what the profile drops, plus exactly what it plugs."""
    _clocked(world)
    plain = _stack(world, tmp_path, "r0")
    plain.write_overlay()
    unprofiled = _compose_config(plain)["services"]["episode"]["environment"]
    # BI001's box: da/laptop (notebook plugged), all three BI vendors
    stack = _stack(world, tmp_path, "r1", profile=PROFILE, plugs=BI001_PLUGS)
    stack.write_overlay()
    services = _compose_config(stack)["services"]
    dropped, added = stack.profile_variables()
    for service in ("episode", "jupyter"):
        env = services[service]["environment"]
        for var in ("SCHED_URL", "METABASE_URL", "PROMETHEUS_URL", "LOKI_URL",
                    "AIRBYTE_URL", "DEPLOY_URL", "ALERTMANAGER_URL",
                    "AWS_ENDPOINT_URL"):
            assert var not in env, (service, var)
        assert env[ce.CLOCK_ENV] == "2026-03-02T00:00:00Z"
        assert env["POWERBI_API_URL"] == "http://powerbi.internal:8095"
        assert env["TABLEAU_SITE"] == "" and env["SIGMA_BASE_URL"]
    episode_env = services["episode"]["environment"]
    assert set(episode_env) == (set(unprofiled) - set(dropped)) | set(added) \
        | {"JUPYTER_URL"}
    assert {k: v for k, v in episode_env.items() if k in unprofiled} == \
        {k: v for k, v in unprofiled.items() if k not in dropped}
    # unprofiled: the plain merge, every variable of the nine still there
    assert {"SCHED_URL", "METABASE_URL", "AWS_ENDPOINT_URL"} <= set(unprofiled)


# ------------------------------- the services' own state (bistate.py, P7 H)
def test_compose_env_names_the_rollouts_service_state(world, tmp_path):
    import bistate

    stack = _stack(world, tmp_path, "r")
    env = stack.compose_env()
    assert env["ALKERA_EPISODE_SERVICE_STATE"] == \
        bistate.service_state_dir((tmp_path / "r").resolve()).as_posix()
    assert bistate.source_for(tmp_path / "r") == \
        Path(env["ALKERA_EPISODE_SERVICE_STATE"]) / "bi"


def test_service_state_is_mounted_into_native_services_only(world, tmp_path, no_docker):
    """episode, jupyter, subject, egress-proxy, postgres, minio: none of them."""
    stack = _stack(world, tmp_path, "r", profile=PROFILE, plugs=BI001_PLUGS,
                   subject_sandbox=True)
    stack.write_overlay()
    services = _compose_config(stack)["services"]
    assert {"episode", "jupyter", "subject", "egress-proxy"} <= set(services)
    state = stack.service_state_dir.as_posix()
    mounts = {name: [(v.get("source"), v.get("target")) for v in svc.get("volumes", [])]
              for name, svc in services.items()}
    assert (state, "/service-state") in mounts["native-services"]
    for name, rows in mounts.items():
        if name == "native-services":
            continue
        assert not any(src == state or tgt == "/service-state" for src, tgt in rows), name
    command = " ".join(services["native-services"]["command"])
    assert "native_services.py --state-dir /service-state --" in command


def test_compose_env_carries_the_vendor_plug(world, tmp_path):
    env = _stack(world, tmp_path, "r", profile=PROFILE, plugs=BI_PLUGS).compose_env()
    assert env["ALKERA_NATIVE_PROFILE_ARGS"] == (
        "--profile da/laptop --plug notebook= --plug bi=powerbi")


def test_world_profile_check_bi_vendor_matching(tmp_path):
    profile = episode.environment_profile(PROFILE, BI_PLUGS)
    root = _world_with_profile(tmp_path, {"name": PROFILE,
                                          "slots": sorted(profile.slots),
                                          "plugs": {"bi": ["powerbi"]}})
    episode.check_world_profile(root, profile)


def test_world_profile_check_without_a_vendor_ignores_absent_plugs(tmp_path):
    # metabase_fake is not an explicit-only vendor: a record without `plugs`
    # (every world built before the key existed) still matches
    profile = episode.environment_profile(PROFILE, PLUGS)
    root = _world_with_profile(tmp_path, {"name": PROFILE,
                                          "slots": sorted(profile.slots)})
    episode.check_world_profile(root, profile)
    root2 = _world_with_profile(tmp_path / "2", {
        "name": PROFILE, "slots": sorted(profile.slots),
        "plugs": {"bi": ["metabase_fake"]}})
    episode.check_world_profile(root2, profile)


@pytest.mark.parametrize("world_bi,task_plugs,match", [
    # the task plugs Power BI, the world never said which BI it was built for
    (None, BI_PLUGS, r"plugs: \{bi: \['powerbi'\]\}"),
    # built for another vendor
    (["tableau"], BI_PLUGS, "tableau"),
    # built for Power BI, but the task serves Metabase
    (["powerbi"], PLUGS, "metabase_fake"),
])
def test_world_profile_check_refuses_a_bi_disagreement(tmp_path, world_bi,
                                                       task_plugs, match):
    profile = episode.environment_profile(PROFILE, task_plugs)
    record = {"name": PROFILE, "slots": sorted(profile.slots)}
    if world_bi is not None:
        record["plugs"] = {"bi": world_bi}
    with pytest.raises(episode.HarnessError, match=match):
        episode.check_world_profile(_world_with_profile(tmp_path, record), profile)


def test_world_built_without_a_profile_names_the_bi_plugs_it_needs(tmp_path):
    profile = episode.environment_profile(PROFILE, BI_PLUGS)
    with pytest.raises(episode.HarnessError, match="plugs: {bi: \\['powerbi'\\]}"):
        episode.check_world_profile(_world_with_profile(tmp_path, None), profile)


def test_gate_relays_a_plugged_vendor_and_nothing_new_unprofiled(world, tmp_path):
    # ServiceGate binds loopback listeners only; docker is dialled per
    # connection, and none is made here.
    def gate(name, **kw):
        stack = _stack(world, tmp_path, name, **kw)
        stack.services_container = "never-dialled"
        try:
            return stack.gate_endpoints()
        finally:
            stack.gate.stop()

    plain = gate("r1")
    assert not set(aut.PLUG_SERVICES) & set(plain)
    assert set(aut.NATIVE_SERVICES) <= set(plain)
    ep = gate("r2", profile=PROFILE, plugs=BI_PLUGS)
    assert ep["powerbi"].startswith("http://127.0.0.1:")
    assert {n for n in (*aut.NATIVE_SERVICES, *aut.PLUG_SERVICES) if n in ep} \
        == set(BI_SERVICES)


def test_surface_gate_sweeps_a_plugged_vendor_only(tmp_path):
    import http.server
    import threading

    import surface_audit

    class Health(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - the stdlib's spelling
            body = json.dumps({"ok": True, "service": "powerbi", "items": 2}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        assert surface_audit.native_check_kind("powerbi") == "http_health"
        swept = surface_audit.probe_native(probes=[], endpoints={"powerbi": url},
                                           world={}, timeout=2)
        row = swept["surfaces"]["~powerbi"]
        assert (row["live"], row["check"], row["declared"]) == (True, "http_health", False)
        # an unprofiled map never carries a vendor, so the sweep is the nine's
        plain = surface_audit.probe_native(probes=[], endpoints={"jira": url},
                                           world={}, timeout=2)
        assert set(plain["surfaces"]) == {"~jira"}
    finally:
        server.shutdown()
    assert set(surface_audit.NATIVE_SERVICE_NAMES) == set(aut.NATIVE_SERVICES)
    assert set(surface_audit.NATIVE_PLUG_SERVICE_NAMES) == set(aut.PLUG_SERVICES)


# ------------------------------------------- BI vendors on the host topology
def test_host_stack_awaits_and_addresses_a_plugged_bi_vendor(world, tmp_path):
    from alkera_gateway.native import serve

    profile = episode.environment_profile("da/laptop", {"bi": ["powerbi"]})
    ports = native_stack.service_ports(profile)
    assert serve.PLUG_PORTS["powerbi"] in ports
    assert serve.PLUG_PORTS["tableau"] not in ports
    assert serve.PLUG_PORTS["powerbi"] not in native_stack.service_ports(None)

    spec = SimpleNamespace(profile="da/laptop", plugs={"bi": ["powerbi"]})
    (tmp_path / "r").mkdir()
    stack = native_stack.NativeStack(world=world, audit_dir=tmp_path / "a",
                                     rollout_dir=tmp_path / "r", spec=spec, offset=7)
    endpoints = stack._write_endpoints(None)
    assert endpoints["powerbi"] == f"http://127.0.0.1:{serve.PLUG_PORTS['powerbi'] + 7}"
    assert "tableau" not in endpoints and "metabase" not in endpoints


# ------------------------------------------------- ./ops/sched and the profile
DA_SLOTS = ["files", "notebook", "warehouse", "transform", "bi", "human"]
DE_SLOTS = ["files", "warehouse", "transform", "orchestrator", "object_store", "ingest",
            "bi", "observability", "release", "human"]
CLI_KEYS = {"cli", "topology", "interpreter", "pythonpath", "files"}


def _record_profile(world: dict, record: dict) -> None:
    path = Path(world["world_root"]) / episode.WORLD_PROFILE_RECORD
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")


@pytest.mark.parametrize("topology", ["host", "container"])
def test_no_orchestrator_no_workspace_cli(world, topology):
    """A profile that plugs no orchestrator (da/*) has no scheduler on the
    box: `./ops/sched` is not installed into the working tree on either
    topology (on the host its local mode listed, read and RAN jobs)."""
    _record_profile(world, {"name": "da/laptop", "slots": DA_SLOTS,
                            "plugs": {"bi": ["powerbi", "tableau", "sigma"]}})
    result = native_stack.install_workspace_cli(world, topology)
    workspace = Path(world["workspace"])
    assert not (workspace / native_stack.SCHED_CLI_PATH).exists()
    assert not (workspace / "ops").exists()
    assert result["cli"] is None and result["files"] == []
    assert result["topology"] == topology
    assert "orchestrator" in result["skipped"]
    # the keys a caller reads (harbor/alkera_harbor/worldbuild.py) are all there
    assert CLI_KEYS <= set(result)


def test_an_orchestrator_that_is_not_ops_sched_gets_no_cli(world):
    _record_profile(world, {"name": "de/laptop", "slots": DE_SLOTS,
                            "plugs": {"orchestrator": ["airflow"]}})
    result = native_stack.install_workspace_cli(world, "host")
    assert result["cli"] is None
    assert not (Path(world["workspace"]) / native_stack.SCHED_CLI_PATH).exists()


def test_a_profile_with_the_scheduler_gets_the_cli(world):
    _record_profile(world, {"name": "de/laptop", "slots": DE_SLOTS})
    result = native_stack.install_workspace_cli(world, "host")
    assert result["cli"] == native_stack.SCHED_CLI_PATH
    assert (Path(world["workspace"]) / native_stack.SCHED_CLI_PATH).is_file()
    assert set(result) == CLI_KEYS


def test_unprofiled_workspace_cli_is_unchanged(world, tmp_path):
    """No profile record: installed exactly as before profiles existed - the
    same record and the same bytes a profiled box with ops_sched gets."""
    result = native_stack.install_workspace_cli(world, "host")
    target = Path(world["workspace"]) / native_stack.SCHED_CLI_PATH
    assert set(result) == CLI_KEYS
    assert result["cli"] == native_stack.SCHED_CLI_PATH
    assert (result["interpreter"], result["pythonpath"]) == (str(episode.PY),
                                                             str(episode.TRACK_A))
    assert target.is_file()
    unprofiled = target.read_bytes()
    _record_profile(world, {"name": "de/laptop", "slots": DE_SLOTS})
    again = native_stack.install_workspace_cli(world, "host")
    assert again == result and target.read_bytes() == unprofiled


# ------------------------- backend variables follow the profile's plugs
PG = {"PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"}
SNOW = {"SNOWFLAKE_HOST", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD"}
AIRFLOW = {"AIRFLOW_API_URL", "AIRFLOW_USER", "AIRFLOW_PASSWORD"}


def test_a_duckdb_box_has_no_postgres_snowflake_or_airflow_variables(world, tmp_path,
                                                                     no_docker):
    # The discovery files of a profiled world already name only the warehouse
    # the profile plugs (world/native_workspace.py); the process environment
    # said otherwise - PGHOST=postgres.internal on a DuckDB analyst box.
    stack = _stack(world, tmp_path, "r", profile=PROFILE, plugs=BI_PLUGS)
    dropped, _ = stack.profile_variables()
    assert PG | SNOW | AIRFLOW <= set(dropped)
    episode_env = _env(_block(stack.write_overlay().read_text(), "episode"))
    assert not (PG | SNOW | AIRFLOW) & set(episode_env)


def test_a_postgres_or_snowflake_box_keeps_its_warehouse_variables(world, tmp_path,
                                                                   no_docker):
    pg, _ = _stack(world, tmp_path, "p", profile="de/postgres").profile_variables()
    assert not PG & set(pg) and SNOW <= set(pg)
    sf, _ = _stack(world, tmp_path, "s", profile="de/snowflake").profile_variables()
    assert not SNOW & set(sf) and PG <= set(sf)


# ------------------------------------- the episode image follows the profile
def _build_episode():
    import importlib.util

    path = HERE.parent.parent / "gateway" / "docker" / "build_episode.py"
    spec = importlib.util.spec_from_file_location("build_episode_for_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_default_image_becomes_the_profiles_role_image():
    # BI001 (da/laptop) needs the analyst image: the jupyter service runs the
    # kernel from it and the de image has no jupyter-server. Left at the
    # default, --image used to start every profiled task on the de image.
    build = _build_episode()
    default = ce.DEFAULT_IMAGE
    cases = [("da/laptop", None, "da", ""), ("ds/laptop", {"compute": []}, "ds", ""),
             ("de/laptop", None, "de", ""), ("de/snowflake", None, "de", "snowflake"),
             ("da/snowflake", None, "da", "snowflake")]
    for name, plugs, role, clients in cases:
        profile = episode.environment_profile(name, plugs)
        assert ce.profile_image(default, profile) == build.image_tag(default, role, clients), name
    assert ce.profile_image(default, None) == default                 # unprofiled: unchanged
    explicit = "alkera-episode:mine"
    assert ce.profile_image(explicit, episode.environment_profile("da/laptop")) == explicit
