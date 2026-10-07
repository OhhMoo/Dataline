"""The backend registry in the container topology (alkera_gateway.backends).

    gateway/.venv/bin/python -m pytest tasks/harness/test_backends.py -q

Two kinds of test. The GOLDENS pin the per-rollout overlay byte for byte: an
unprofiled GT001-shaped world, the `da/snowflake` box and BI001's `da/laptop`
box, captured with the code as it stood before the registry existed
(`python tasks/harness/test_backends.py --regen` rewrites them; a diff in
testdata/ is then the whole review). The REGISTRY tests say what a backend
entry buys a profile that plugs it - an allowed destination, an image tag,
its statement tool counted as SQL - and that two TLS backends in one profile
are refused. No docker, no model.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import agent_under_test as aut  # noqa: E402
import container_episode as ce  # noqa: E402

TESTDATA = HERE / "testdata"
#: What `ce.image_path` reads off a built episode image (`docker image
#: inspect`), as the GT001 container rollouts recorded it.
IMAGE_PATH = ("/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:"
              "/sbin:/bin:/opt/toolbelt/bin")
#: The host path every golden's world lives under, after normalisation.
ROOT = "/rollouts/r0"

#: name -> (testdata file, NativeContainerEpisode keywords, world clock, variant)
GOLDENS = {
    # GT001 (unprofiled): one warehouse file, the scheduler's vnow, an arm.
    "unprofiled": ("overlay_unprofiled.yml",
                   {"image": "alkera-episode:head"},
                   "2026-03-05T00:00:00Z", "default"),
    # The da/snowflake box snowflake_e2e_check.py brings up.
    "da_snowflake": ("overlay_da_snowflake.yml",
                     {"image": "alkera-episode:head-da-snowflake",
                      "profile": "da/snowflake", "plugs": {}},
                     "2026-03-02T00:00:00Z", ""),
    # BI001's box: da/laptop with all three BI vendors plugged.
    "da_laptop": ("overlay_da_laptop.yml",
                  {"image": "alkera-episode:head-da", "profile": "da/laptop",
                   "plugs": {"bi": ["powerbi", "tableau", "sigma"]}},
                  "2026-03-02T00:00:00Z", "default"),
}


def _world(base: Path, clock: str, variant: str) -> dict:
    root = base / "world"
    (root / "var" / "warehouse").mkdir(parents=True, exist_ok=True)
    (root / "var" / "warehouse" / "warehouse.duckdb").write_bytes(b"x")
    (root / "var" / "sched").mkdir(parents=True, exist_ok=True)
    (root / "var" / "sched" / "state.json").write_text(
        json.dumps({"vnow": clock, "jobs": {}}), encoding="utf-8")
    (root / "workspace").mkdir(exist_ok=True)
    return {"world_root": str(root), "workspace": str(root / "workspace"),
            "warehouse": str(root / "var" / "warehouse" / "warehouse.duckdb"),
            "scheduler": str(root / "var" / "sched" / "state.json"),
            "dashboards": str(root / "var" / "bi" / "dashboards"),
            "profiles_dir": str(root / "build"),
            "variant": variant}


def render(name: str, base: Path) -> str:
    """The overlay `write_overlay` writes for golden `name`, with the
    temporary root spelled ROOT so the bytes do not depend on where it ran."""
    _, kw, clock, variant = GOLDENS[name]
    world = _world(base, clock, variant)
    stack = ce.NativeContainerEpisode(project="t", world=world,
                                      audit_dir=base / "audit",
                                      rollout_dir=base / "rollout",
                                      variant=variant, **kw)
    original = ce.image_path
    ce.image_path = lambda image: IMAGE_PATH
    try:
        text = stack.write_overlay().read_text(encoding="utf-8")
    finally:
        ce.image_path = original
    return text.replace(Path(base).resolve().as_posix(), ROOT)


def _golden(name: str) -> str:
    return (TESTDATA / GOLDENS[name][0]).read_text(encoding="utf-8")


# ------------------------------------------------------------------ goldens
def test_unprofiled_overlay_is_byte_identical(tmp_path):
    assert render("unprofiled", tmp_path) == _golden("unprofiled")


def test_snowflake_overlay_is_byte_identical(tmp_path):
    assert render("da_snowflake", tmp_path) == _golden("da_snowflake")


def test_da_laptop_overlay_is_byte_identical(tmp_path):
    # BI001/BI002's box: no backend is plugged, so the registry adds nothing.
    assert render("da_laptop", tmp_path) == _golden("da_laptop")


# ----------------------------------------------------------------- registry
def _stack(tmp_path, name, **kw):
    world = _world(tmp_path / name, "2026-03-02T00:00:00Z", "")
    return ce.NativeContainerEpisode(project="t", world=world,
                                     audit_dir=tmp_path / name / "audit",
                                     rollout_dir=tmp_path / name / "rollout", **kw)


def test_backend_endpoint_allowed_only_when_plugged(tmp_path):
    from alkera_gateway import backends

    snowflake = backends.BACKENDS["snowflake"]
    pair = (snowflake.host, snowflake.port)
    assert pair == ("snowflake.internal", 8443)
    plugged = _stack(tmp_path, "s", profile="da/snowflake").episode_endpoints()
    assert plugged["snowflake"] == backends.endpoint(snowflake)
    assert pair in aut.allowed_destinations(plugged)
    laptop = _stack(tmp_path, "d", profile="da/laptop").episode_endpoints()
    assert "snowflake" not in laptop
    assert pair not in aut.allowed_destinations(laptop)
    unprofiled = _stack(tmp_path, "u").episode_endpoints()
    assert pair not in aut.allowed_destinations(unprofiled)


def test_two_tls_backends_refused(monkeypatch):
    from alkera_gateway import backends
    from alkera_gateway.plugs import ProfileError

    monkeypatch.setitem(backends.BACKENDS, "x", backends.Backend("x", tls=True))
    with pytest.raises(ProfileError) as raised:
        backends.plugged({"warehouse": ("snowflake", "x")})
    assert "snowflake" in str(raised.value) and "'x'" in str(raised.value)
    # one TLS backend is fine, whichever it is
    assert [b.name for b in backends.plugged({"warehouse": ("x",)})] == ["x"]


def test_two_tls_backends_refused_at_profile_resolution(monkeypatch):
    """The harness resolves a task's profile before it builds a world: two TLS
    backends stop the rollout there, naming both."""
    import episode

    from alkera_gateway import backends, plugs

    monkeypatch.setitem(backends.BACKENDS, "x", backends.Backend("x", port=1, tls=True))
    monkeypatch.setitem(plugs.PLUGS, "x", plugs.Plug("x", "warehouse", "partial"))
    with pytest.raises(episode.HarnessError, match="'snowflake', 'x'"):
        episode.environment_profile("da/laptop", {"warehouse": ["snowflake", "x"]})
    assert episode.environment_profile("da/laptop", {"warehouse": ["x"]}) is not None


def test_two_tls_backends_refused_before_the_overlay(tmp_path, monkeypatch):
    """The container topology asks the registry before it writes a byte."""
    from alkera_gateway import backends
    from alkera_gateway.plugs import ProfileError

    monkeypatch.setitem(backends.BACKENDS, "x", backends.Backend("x", tls=True))
    stack = _stack(tmp_path, "t", profile="da/snowflake")
    profile = stack.environment_profile()
    two = SimpleNamespace(name=profile.name, services=profile.services,
                          slots={**profile.slots, "warehouse": ("snowflake", "x")})
    monkeypatch.setattr(stack, "environment_profile", lambda: two)
    with pytest.raises(ProfileError, match="snowflake"):
        stack.write_overlay()
    assert not stack.overlay.exists()


def _build_episode():
    import importlib.util

    path = HERE.parent.parent / "gateway" / "docker" / "build_episode.py"
    spec = importlib.util.spec_from_file_location("build_episode_for_backends", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_profile_image_matches_build_tag_for_every_client():
    from alkera_gateway import backends

    build = _build_episode()
    clients = {b.clients for b in backends.BACKENDS.values() if b.clients}
    assert clients, "no backend names a client set"
    assert set(build.WAREHOUSE_CLIENTS) == clients
    for name, backend in backends.BACKENDS.items():
        if not backend.clients:
            continue
        for role in ("de", "da", "ds"):
            profile = SimpleNamespace(name=f"{role}/laptop",
                                      slots={"warehouse": (name,)})
            assert ce.profile_image(ce.DEFAULT_IMAGE, profile) == build.image_tag(
                ce.DEFAULT_IMAGE, role, backend.clients), (name, role)


def test_statement_tools_are_sql_kind():
    import path as harness_path

    from alkera_gateway import backends

    tools = [b.statement_tool for b in backends.BACKENDS.values() if b.statement_tool]
    assert "snowflake.statement" in tools
    for tool in tools:
        assert harness_path.TOOL_KINDS.get(tool) == "sql", tool


def test_snowflake_names_are_the_registrys():
    """The constants the checks read (snowflake_e2e_check, test_profile_wiring)
    are what the registry derives, and the static table's Snowflake variables
    are the row's own."""
    from alkera_gateway import backends

    sf = backends.BACKENDS["snowflake"]
    assert ce.SNOWFLAKE_TRUST == backends.trust_dir(sf)
    assert ce.SNOWFLAKE_BUNDLE == backends.bundle(sf)
    assert ce.SNOWFLAKE_DESTINATIONS == backends.destinations_file(sf)
    assert ce.SNOWFLAKE_SEED_IN_CONTAINER == ce.SEED_IN_CONTAINER
    assert set(ce.BACKEND_ENV["snowflake"]) <= set(sf.env)
    static = ce.static_environment("episode")
    assert {k: static[k] for k in ce.BACKEND_ENV["snowflake"]} == \
        {k: sf.env[k] for k in ce.BACKEND_ENV["snowflake"]}
    assert ce.STATIC_BACKENDS <= set(backends.BACKENDS)


def test_every_plugged_backend_destination_is_folded(tmp_path):
    """`down` folds every plugged backend's own destination file, and a
    profile without one leaves destinations.jsonl alone."""
    from alkera_gateway import backends

    stack = _stack(tmp_path, "f", profile="da/snowflake")
    audit = Path(stack.audit_dir)
    audit.mkdir(parents=True)
    (audit / "destinations.jsonl").write_text(
        '{"count":0,"host":"jira.internal","port":8080}\n', encoding="utf-8")
    (audit / backends.destinations_file(backends.BACKENDS["snowflake"])).write_text(
        '{"count":3,"host":"snowflake.internal","port":8443}\n', encoding="utf-8")
    folded = stack.fold_destinations()
    assert [(r["host"], r["count"]) for r in folded] == [
        ("jira.internal", 0), ("snowflake.internal", 3)]
    plain = _stack(tmp_path, "g", profile="da/laptop")
    Path(plain.audit_dir).mkdir(parents=True)
    assert plain.fold_destinations() == []


def _overlay_yaml(text: str) -> dict:
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    def tagged(loader, node):
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        return loader.construct_scalar(node)

    for tag in ("!override", "!reset"):
        Loader.add_constructor(tag, tagged)
    return yaml.load(text, Loader=Loader)


def _plugging(monkeypatch, stack, name):
    """`stack`, re-profiled as da/laptop with `name` as its warehouse plug."""
    profile = stack.environment_profile()
    fake = SimpleNamespace(name=profile.name, services=profile.services,
                           slots={**profile.slots, "warehouse": (name,)})
    monkeypatch.setattr(stack, "environment_profile", lambda: fake)
    monkeypatch.setattr(ce, "image_path", lambda image: IMAGE_PATH)
    return stack


def test_a_new_backend_gets_a_complete_service_block(tmp_path, monkeypatch):
    """A registry row is all a container backend needs: the overlay declares
    its service whole - no compose.yml or compose.native.yml stanza."""
    from alkera_gateway import backends

    row = backends.Backend("pgx", port=5439, scheme="postgres", hosted=False,
                           dockerfile="Dockerfile.pgx", seeds_world=True,
                           statement_tool="pgx.statement",
                           env={"PGX_HOST": "pgx.internal", "PGX_PORT": "5439"})
    monkeypatch.setitem(backends.BACKENDS, "pgx", row)
    stack = _plugging(monkeypatch, _stack(tmp_path, "n", profile="da/laptop"), "pgx")
    overlay = _overlay_yaml(stack.write_overlay().read_text(encoding="utf-8"))
    service = overlay["services"]["pgx"]
    assert service["profiles"] == ["pgx"]
    assert service["build"] == {"context": "../..", "dockerfile": "gateway/docker/Dockerfile.pgx",
                                "args": {"BASE": stack.image}}
    assert service["networks"] == {"episode_net": {"aliases": ["pgx.internal"]}}
    audit = Path(stack.audit_dir).resolve().as_posix()
    assert service["volumes"][:2] == [f"{audit}:/audit", "pgxdata:/pgx-data"]
    assert service["volumes"][2].endswith(":/seed/warehouse.duckdb:ro")
    assert service["environment"]["ALKERA_PGX_DESTINATIONS"] == \
        "/audit/destinations.pgx.jsonl"
    assert service["environment"]["ALKERA_PGX_SEED"] == "/seed"
    assert ":153F'" in service["healthcheck"]["test"][-1]           # 5439, LISTEN
    assert overlay["volumes"] == {"pgxdata": None}
    episode_block = overlay["services"]["episode"]
    assert episode_block["depends_on"] == {"pgx": {"condition": "service_healthy"}}
    assert episode_block["environment"]["PGX_HOST"] == "pgx.internal"
    assert "REQUESTS_CA_BUNDLE" not in episode_block["environment"]
    assert "pgx" in stack.compose_env()["COMPOSE_PROFILES"].split(",")
    endpoints = stack.episode_endpoints()
    assert endpoints["pgx"] == "postgres://pgx.internal:5439"
    assert ("pgx.internal", 5439) in aut.allowed_destinations(endpoints)


def test_a_hosted_tls_backend_publishes_through_native_services(tmp_path, monkeypatch):
    """A module inside native-services: no service of its own, an alias on
    native-services, and its trust volume there (writable) and in the
    episode (read-only, REQUESTS_CA_BUNDLE pointing into it)."""
    from alkera_gateway import backends

    url = "https://hx.internal:8098"
    row = backends.Backend("hx", port=8098, scheme="https", tls=True,
                           env={"HX_HOST": url, "HX_TOKEN": "t"})
    monkeypatch.setitem(backends.BACKENDS, "hx", row)
    stack = _plugging(monkeypatch, _stack(tmp_path, "h", profile="da/laptop"), "hx")
    overlay = _overlay_yaml(stack.write_overlay().read_text(encoding="utf-8"))
    assert "hx" not in overlay["services"]
    services = overlay["services"]["native-services"]
    assert "hx.internal" in services["networks"]["episode_net"]["aliases"]
    assert services["volumes"] == ["hxtrust:/etc/alkera/hx-tls"]
    episode_block = overlay["services"]["episode"]
    assert "hxtrust:/etc/alkera/hx-tls:ro" in episode_block["volumes"]
    assert episode_block["environment"]["REQUESTS_CA_BUNDLE"] == "/etc/alkera/hx-tls/bundle.pem"
    assert episode_block["environment"]["HX_HOST"] == url
    assert "depends_on" not in episode_block
    assert overlay["volumes"] == {"hxtrust": None}
    assert "hx" not in stack.compose_env().get("COMPOSE_PROFILES", "")
    assert stack.episode_endpoints()["hx"] == url
    # the host topology re-bases the same variables onto its own address
    assert aut.plug_service_env("hx", "https://127.0.0.1:18098") == {
        "HX_HOST": "https://127.0.0.1:18098", "HX_TOKEN": "t"}


def test_a_plugged_backend_env_wins_over_the_static_table(tmp_path, monkeypatch):
    """A hosted row standing in for Airflow: the static table's AIRFLOW_API_URL
    (8080) gives way, in place, to the row's (8100) - one variable, one
    address - and the episode may dial only that one."""
    from alkera_gateway import backends

    url = "http://airflow.internal:8100/api/v2"
    monkeypatch.setitem(backends.BACKENDS, "airflow", backends.Backend(
        "airflow", port=8100, env={"AIRFLOW_API_URL": url,
                                   "AIRFLOW_USER": "alkera-local-dev",
                                   "AIRFLOW_PASSWORD": "alkera-local-dev"}))
    stack = _stack(tmp_path, "a", profile="de/laptop")
    profile = stack.environment_profile()
    fake = SimpleNamespace(name=profile.name, services=profile.services,
                           slots={**profile.slots, "orchestrator": ("airflow",)})
    monkeypatch.setattr(stack, "environment_profile", lambda: fake)
    monkeypatch.setattr(ce, "image_path", lambda image: IMAGE_PATH)
    text = stack.write_overlay().read_text(encoding="utf-8")
    overlay = _overlay_yaml(text)
    episode_env = overlay["services"]["episode"]["environment"]
    assert episode_env["AIRFLOW_API_URL"] == url
    assert "airflow.internal:8080" not in text and text.count("AIRFLOW_API_URL:") == 1
    assert "airflow.internal" in \
        overlay["services"]["native-services"]["networks"]["episode_net"]["aliases"]
    endpoints = stack.episode_endpoints()
    assert endpoints["airflow"] == "http://airflow.internal:8100"
    allowed = aut.allowed_destinations(endpoints)
    assert ("airflow.internal", 8100) in allowed and ("airflow.internal", 8080) not in allowed


def test_hosted_backends_are_registry_only():
    """A registry backend is in none of the BI vendors' tables: serve.py mounts
    it, the harness aliases and exports it, from its row alone."""
    import surface_audit

    from alkera_gateway import backends
    from alkera_gateway.native import serve, services

    sys.path.insert(0, str(HERE.parent.parent / "world"))
    from world import native_workspace as nw

    names = set(backends.BACKENDS)
    for table in (services.PLUG_NAMES, serve.PLUG_PORTS, aut.PLUG_SERVICES,
                  {s.name for s in nw.PLUG_SERVICES}, ce.EPISODE_SERVICE_ENV,
                  surface_audit.NATIVE_PLUG_SERVICE_NAMES):
        assert not names & set(table), table


# ------------------------------------------- the box's dbt profile (Task 2)
def _staged(tmp_path, monkeypatch, name, **kw) -> tuple[str, str]:
    """(world profile re-rooted, what `_stage_profiles` staged) for a world
    whose build/profiles.yml is the commerce theme's DuckDB `dev` target."""
    stack = _stack(tmp_path, name, **kw)
    build = Path(stack.world["profiles_dir"])
    build.mkdir(parents=True, exist_ok=True)
    (build / "profiles.yml").write_text(
        "world_dbt:\n  outputs:\n    dev:\n"
        f"      path: {stack.world['warehouse']}\n      threads: 1\n      type: duckdb\n"
        "  target: dev\n", encoding="utf-8")
    monkeypatch.setattr(stack, "exec_", lambda *a, **k: None)
    monkeypatch.setattr(stack, "copy_in", lambda *a, **k: None)
    assert stack._stage_profiles() == ["world_dbt.dev"]
    rerooted, _ = ce.reroot_profiles((build / "profiles.yml").read_text(encoding="utf-8"),
                                     stack.world_root)
    return rerooted, (stack.container_dir / "profiles.yml").read_text(encoding="utf-8")


@pytest.mark.parametrize("kw", [{}, {"profile": "da/laptop",
                                     "plugs": {"bi": ["powerbi", "tableau", "sigma"]}}])
def test_box_profile_unplugged_is_todays_bytes(tmp_path, monkeypatch, kw):
    rerooted, staged = _staged(tmp_path, monkeypatch, "u", **kw)
    assert staged == rerooted


def test_box_profile_of_a_snowflake_box_defaults_to_snowflake(tmp_path, monkeypatch):
    import yaml

    rerooted, staged = _staged(tmp_path, monkeypatch, "s", profile="da/snowflake")
    profile = yaml.safe_load(staged)["world_dbt"]
    assert profile["target"] == "snowflake"
    assert profile["outputs"]["dev"] == yaml.safe_load(rerooted)["world_dbt"]["outputs"]["dev"]
    assert profile["outputs"]["snowflake"]["type"] == "snowflake"
    assert profile["outputs"]["snowflake"]["database"] == "{{ env_var('SNOWFLAKE_DATABASE') }}"
    # every variable it reads is in the episode's environment under this profile
    stack = _stack(tmp_path, "s2", profile="da/snowflake")
    _, added = stack.profile_variables()
    env = {**ce.static_environment("episode"), **added}
    for value in profile["outputs"]["snowflake"].values():
        if isinstance(value, str) and "env_var(" in value:
            assert value.split("'")[1] in env, value


def test_surface_gate_accepts_a_box_profile_defaulting_to_the_backend(tmp_path, monkeypatch):
    """The dbt gate reads the world-side profile and asks for an output whose
    path is the warehouse: a box profile whose default is the plugged
    warehouse still carries the DuckDB target, so it passes."""
    import surface_audit

    sys.path.insert(0, str(HERE.parent.parent / "world"))
    from world.native_workspace import box_profiles_yaml

    warehouse = tmp_path / "var" / "warehouse" / "warehouse.duckdb"
    warehouse.parent.mkdir(parents=True)
    warehouse.write_bytes(b"x")
    world_profile = ("world_dbt:\n  outputs:\n    dev:\n"
                     f"      path: {warehouse}\n      threads: 1\n      type: duckdb\n"
                     "  target: dev\n")
    profiles = tmp_path / "box"
    profiles.mkdir()
    (profiles / "profiles.yml").write_text(box_profiles_yaml(world_profile, ["snowflake"]),
                                           encoding="utf-8")
    monkeypatch.setattr(surface_audit, "dbt_executable", lambda: "/usr/bin/dbt")
    ok, code, detail = surface_audit._dbt_toolchain(str(profiles), str(warehouse))
    assert ok and code is None and "target 'dev'" in detail, detail


if __name__ == "__main__":
    import tempfile

    if sys.argv[1:] != ["--regen"]:
        raise SystemExit("usage: python tasks/harness/test_backends.py --regen")
    TESTDATA.mkdir(exist_ok=True)
    for golden in GOLDENS:
        with tempfile.TemporaryDirectory() as temp:
            text = render(golden, Path(temp))
        out = TESTDATA / GOLDENS[golden][0]
        with open(out, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        print(f"wrote {out} ({len(text)} bytes)")
