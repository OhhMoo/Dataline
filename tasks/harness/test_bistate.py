"""bistate.py: the BI vendors' published state, staged for the grader.

    cd gateway && uv run pytest ../tasks/harness/test_bistate.py -q

What is pinned, and why each matters:

- stage() copies the services' state documents into `<grading>/bi_state` and
  leaves the server-side caches (import snapshots, extract copies) and the
  lock / temp files behind; a re-stage replaces the copy whole; no state is
  None, never an error.
- `{bi_state}` reaches grade_argv through episode.grade, staged after
  teardown, and is "" when nothing was staged - the `{ledger}` contract.
- The path it stages FROM is where the services really put it, and it is NOT
  under the audit directory (the episode container mounts /audit read-write,
  so state there was forgeable): `<rollout>/service-state/bi` on both
  topologies - the host config the harness writes into a rollout directory
  names it, and the container's native-services mounts
  `<rollout>/service-state` at /service-state (no other container does) and
  its entrypoint, gateway/docker/native_services.py, serves from there.
- A forged `<rollout>/audit/bi` is never staged, with or without real state.
- regrade accepts a grade_argv that names {bi_state}.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
DOCKER = REPO / "gateway" / "docker"
sys.path.insert(0, str(HERE))

import bistate  # noqa: E402
import container_episode  # noqa: E402
import episode  # noqa: E402


def _state(root: Path, bi: Path | None = None) -> Path:
    """A services state dir as the three BI services leave one, under the
    rollout `root` where the harness's services keep it (or at `bi`)."""
    bi = bistate.source_for(root) if bi is None else bi
    (bi / "tableau" / "content").mkdir(parents=True)
    (bi / "tableau" / "manifest.json").write_text('{"datasources": []}', encoding="utf-8")
    (bi / "tableau" / "content" / "ds-1.tds").write_text("<datasource/>", encoding="utf-8")
    (bi / "tableau" / "state.lock").write_text("", encoding="utf-8")
    (bi / "tableau" / "extracts").mkdir()
    (bi / "tableau" / "extracts" / "datasource-1-1.duckdb").write_bytes(b"cache")
    (bi / "sigma").mkdir()
    (bi / "sigma" / "state.json").write_text('{"workbooks": {}}', encoding="utf-8")
    (bi / "sigma" / "state.tmp").write_text("half", encoding="utf-8")
    (bi / "powerbi" / "snapshots").mkdir(parents=True)
    (bi / "powerbi" / "state.json").write_text('{"datasets": {}}', encoding="utf-8")
    (bi / "powerbi" / "snapshots" / "abc.duckdb").write_bytes(b"vertipaq")
    return bi


def test_stage_copies_documents_and_leaves_caches(tmp_path):
    source = _state(tmp_path)
    grading = tmp_path / "grading"
    grading.mkdir()
    staged = bistate.stage(source, grading)
    assert staged == grading / "bi_state"
    got = sorted(p.relative_to(staged).as_posix() for p in staged.rglob("*") if p.is_file())
    assert got == ["powerbi/state.json", "sigma/state.json", "tableau/content/ds-1.tds",
                   "tableau/manifest.json"]
    assert bistate.resolve(grading) == staged


def test_restage_replaces_the_copy_whole(tmp_path):
    source = _state(tmp_path)
    grading = tmp_path / "grading"
    grading.mkdir()
    bistate.stage(source, grading)
    (source / "tableau" / "content" / "ds-1.tds").unlink()
    staged = bistate.stage(source, grading)
    assert not (staged / "tableau" / "content" / "ds-1.tds").exists()


@pytest.mark.parametrize("make", ["missing", "empty", "none"])
def test_no_state_stages_nothing(tmp_path, make):
    grading = tmp_path / "grading"
    grading.mkdir()
    source = bistate.source_for(tmp_path)
    if make == "empty":
        source.mkdir(parents=True)
    assert bistate.stage(None if make == "none" else source, grading) is None
    assert bistate.resolve(grading) is None


def test_refuses_to_stage_into_itself(tmp_path):
    source = _state(tmp_path)
    with pytest.raises(ValueError):
        bistate.stage(source, source)


def test_the_location_is_beside_the_audit_dir_never_in_it(tmp_path):
    rollout = tmp_path / "r0"
    assert bistate.service_state_dir(rollout) == rollout / "service-state"
    assert bistate.source_for(rollout) == rollout / "service-state" / "bi"
    assert not bistate.source_for(rollout).is_relative_to(rollout / "audit")


def _world(tmp_path: Path) -> dict:
    return {"world_root": str(tmp_path / "w"), "workspace": str(tmp_path / "w" / "workspace"),
            "warehouse": str(tmp_path / "w" / "var" / "warehouse" / "warehouse.duckdb"),
            "scheduler": str(tmp_path / "w" / "var" / "sched" / "state.json"),
            "dashboards": str(tmp_path / "w" / "var" / "bi" / "dashboards"),
            "profiles_dir": str(tmp_path / "w" / "build")}


def test_the_host_config_written_into_a_rollout_names_it(tmp_path):
    """The host topology (native_stack.NativeStack, rollout.py's mcp config):
    the config the harness writes into `<rollout>/` beside `<rollout>/audit`
    points the services' `bi_state_dir` where bistate stages from, and the
    gateway loads it so."""
    from alkera_gateway import GatewayConfig

    rollout = tmp_path / "r0"
    rollout.mkdir()
    world = _world(tmp_path)
    for name in ("native_gateway.json", "gateway.json"):
        path = episode.write_gateway_config(rollout / name, world, rollout / "audit",
                                            "native-host", "", service_state=True)
        cfg = GatewayConfig.load(path)
        assert cfg.bi_state_dir == bistate.source_for(rollout).resolve()
        assert not cfg.bi_state_dir.is_relative_to(cfg.audit_dir)


def test_native_stack_config_names_it(tmp_path):
    """NativeStack's own config (the services' --gateway-config on the host)."""
    import native_stack

    rollout = tmp_path / "r0"
    stack = native_stack.NativeStack(world=_world(tmp_path), audit_dir=rollout / "audit",
                                     rollout_dir=rollout, offset=0)
    rollout.mkdir()
    cfg = json.loads(stack._config_path().read_text(encoding="utf-8"))
    assert cfg["bi_state_dir"] == str(bistate.source_for(rollout.resolve()))


def test_other_configs_are_written_as_before(tmp_path):
    """Only the rollout layout: a probe's scratch config, a Harbor-style
    container-path config and the MCP container config name no `bi_state_dir`."""
    world = _world(tmp_path)
    scratch = episode.write_gateway_config(tmp_path / "gateway.json", world,
                                           tmp_path / "r0" / "audit", "e", "")
    assert "bi_state_dir" not in json.loads(scratch.read_text(encoding="utf-8"))
    probe = episode.write_gateway_config(tmp_path / "probe_gateway.json", world,
                                         tmp_path / "probe_audit", "probe", "")
    assert "bi_state_dir" not in json.loads(probe.read_text(encoding="utf-8"))
    (tmp_path / "x").mkdir()
    harbor = episode.write_gateway_config(tmp_path / "x" / "gateway.json", world,
                                          Path("/audit"), "e", "")
    assert "bi_state_dir" not in json.loads(harbor.read_text(encoding="utf-8"))
    inside = container_episode.container_gateway_config(world, "e", "")
    assert "bi_state_dir" not in inside
    # Harbor's REAL call (harbor/alkera_harbor/worldbuild.py): gateway.json with
    # an `audit` directory beside it - the rollout layout, by shape. Sniffing
    # the layout put a host temp path into the exported config (the export's
    # leak gate refused it); placement is now asked for, never inferred.
    staged = tmp_path / "harbor-stage"
    staged.mkdir()
    exported = episode.write_gateway_config(staged / "gateway.json", world,
                                            staged / "audit", "e", "")
    assert "bi_state_dir" not in json.loads(exported.read_text(encoding="utf-8"))
    assert inside["audit_dir"] == container_episode.AUDIT_IN_CONTAINER == "/audit"


def _compose(name: str) -> dict:
    import yaml

    class Loader(yaml.SafeLoader):
        """compose's `!override` / `!reset` tags, read as plain values."""

    Loader.add_multi_constructor("!", lambda loader, suffix, node: (
        loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode)
        else loader.construct_mapping(node) if isinstance(node, yaml.MappingNode)
        else loader.construct_scalar(node)))
    return yaml.load((DOCKER / name).read_text(encoding="utf-8"), Loader=Loader)


def test_container_services_keep_it_in_their_own_mount():
    """compose.native.yml: native-services mounts $ALKERA_EPISODE_SERVICE_STATE
    at /service-state and its entrypoint serves from there; the episode and
    jupyter (which mount /audit read-write) do not mount it; NativeContainerEpisode
    sets the variable to the rollout's service-state directory."""
    services = _compose("compose.native.yml")["services"]
    native = services["native-services"]
    command = " ".join(native["command"])
    assert "native_services.py --state-dir /service-state --" in command
    assert "--audit /audit" in command
    assert "${ALKERA_EPISODE_SERVICE_STATE:?the native services' own state directory}" \
        ":/service-state" in native["volumes"]
    for name in ("episode", "jupyter"):
        volumes = [str(v) for v in services[name].get("volumes") or []]
        assert not any("SERVICE_STATE" in v or "/service-state" in v for v in volumes), name
        assert any(v.endswith(":/audit") for v in volumes), name
    source = (HERE / "container_episode.py").read_text(encoding="utf-8")
    assert '"ALKERA_EPISODE_SERVICE_STATE": self.service_state_dir.as_posix()' in source
    assert "bistate.service_state_dir(Path(self.rollout_dir).resolve())" in source


def test_the_launcher_moves_only_the_state(tmp_path):
    """gateway/docker/native_services.py: serve.py's own derived config, with
    `bi_state_dir` in the state dir; written THERE and not in the audit dir;
    serve gets it as --gateway-config; a caller's --gateway-config is refused."""
    sys.path.insert(0, str(DOCKER))
    import native_services

    from alkera_gateway.native.serve import world_gateway_config

    world, audit, state = tmp_path / "world", tmp_path / "audit", tmp_path / "state"
    (world / "workspace").mkdir(parents=True)
    audit.mkdir()
    config = native_services.services_config(world, audit, state)
    derived = json.loads(world_gateway_config(world, audit))
    assert config["bi_state_dir"] == str(state / "bi")
    # the observability plane's records (samples, alarm transitions, deploys)
    # move with it: under /audit they were writable from the episode too
    assert config["obs_state_dir"] == str(state / "obs")
    moved = {"bi_state_dir", "obs_state_dir"}
    assert {k: v for k, v in config.items() if k not in moved} == \
        {k: v for k, v in derived.items() if k not in moved}
    assert derived["bi_state_dir"] == str((audit / "bi").resolve())   # the old place
    rc = native_services.main(["--state-dir", str(state), "--print-only", "--",
                               "--audit", str(audit), "--world", str(world),
                               "--bind", "0.0.0.0", "--profile", "da/laptop"])
    assert rc == 0
    written = json.loads((state / native_services.CONFIG_NAME).read_text())
    assert written == config
    assert not (audit / "native_gateway.json").exists() and not (audit / "bi").exists()
    with pytest.raises(SystemExit):
        native_services.main(["--state-dir", str(state), "--", "--audit", str(audit),
                              "--world", str(world), "--gateway-config", "/x.json"])
    with pytest.raises(SystemExit):
        native_services.main(["--state-dir", str(state), "--audit", str(audit)])


def _grader(tmp_path: Path) -> Path:
    script = tmp_path / "echo_grade.py"
    script.write_text(
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "print(json.dumps({'total': 0.0, 'checks': [], 'argv': args}))\n",
        encoding="utf-8")
    return script


def test_grade_substitutes_the_staged_copy(tmp_path):
    rollout = tmp_path / "r0"
    _state(rollout)
    grading = tmp_path / "grading"
    grading.mkdir()
    world = {"world_root": str(tmp_path / "world"), "grading": str(grading)}
    (tmp_path / "world").mkdir()
    spec = SimpleNamespace(grade_entrypoint=_grader(tmp_path),
                           grade_argv=["--bi-state", "{bi_state}", "--ledger", "{ledger}"])
    score = episode.grade(spec, world, tmp_path / "final_report.json", rollout)
    assert score["argv"] == ["--bi-state", str(grading / "bi_state"), "--ledger", ""]
    assert (grading / "bi_state" / "sigma" / "state.json").is_file()


def test_grade_without_state_substitutes_empty(tmp_path):
    rollout = tmp_path / "r0"
    rollout.mkdir()
    grading = tmp_path / "grading"
    grading.mkdir()
    world = {"world_root": str(tmp_path / "world"), "grading": str(grading)}
    (tmp_path / "world").mkdir()
    spec = SimpleNamespace(grade_entrypoint=_grader(tmp_path),
                           grade_argv=["--bi-state", "{bi_state}"])
    score = episode.grade(spec, world, tmp_path / "final_report.json", rollout)
    assert score["argv"] == ["--bi-state", ""]


def test_grade_honours_an_explicit_state_dir(tmp_path):
    elsewhere = _state(tmp_path / "elsewhere")
    grading = tmp_path / "grading"
    grading.mkdir()
    world = {"world_root": str(tmp_path / "world"), "grading": str(grading)}
    (tmp_path / "world").mkdir()
    spec = SimpleNamespace(grade_entrypoint=_grader(tmp_path),
                           grade_argv=["{bi_state}"])
    (tmp_path / "r9").mkdir()
    first, stable = episode.grade_twice(spec, world, tmp_path / "a.json", tmp_path / "r9",
                                        bi_state_dir=elsewhere)
    assert stable and first["argv"] == [str(grading / "bi_state")]


def test_a_forged_audit_bi_is_never_staged(tmp_path):
    """What the move closes: a "published" state the subject wrote into the
    audit directory it can write. With real state it is ignored; without any,
    nothing is staged - no fallback to the forgeable place."""
    rollout = tmp_path / "r0"
    forged = _state(tmp_path, rollout / "audit" / "bi")
    (forged / "sigma" / "state.json").write_text('{"workbooks": {"forged": 1}}',
                                                 encoding="utf-8")
    grading = tmp_path / "grading"
    grading.mkdir()
    world = {"world_root": str(tmp_path / "world"), "grading": str(grading)}
    (tmp_path / "world").mkdir()
    spec = SimpleNamespace(grade_entrypoint=_grader(tmp_path), grade_argv=["{bi_state}"])
    assert episode.grade(spec, world, tmp_path / "a.json", rollout)["argv"] == [""]
    _state(rollout)
    score = episode.grade(spec, world, tmp_path / "a.json", rollout)
    assert score["argv"] == [str(grading / "bi_state")]
    staged = (grading / "bi_state" / "sigma" / "state.json").read_text(encoding="utf-8")
    assert "forged" not in staged


def test_regrade_accepts_bi_state_in_grade_argv():
    source = (HERE / "rollout.py").read_text(encoding="utf-8")
    assert '- {"ledger", "bi_state", "artifact"} - set(world)' in source
