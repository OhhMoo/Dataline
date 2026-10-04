"""bistate.py: the BI vendors' published state, staged for the grader.

    cd gateway && uv run pytest ../tasks/harness/test_bistate.py -q

What is pinned, and why each matters:

- stage() copies the services' state documents into `<grading>/bi_state` and
  leaves the server-side caches (import snapshots, extract copies) and the
  lock / temp files behind; a re-stage replaces the copy whole; no state is
  None, never an error.
- `{bi_state}` reaches grade_argv through episode.grade, staged after
  teardown, and is "" when nothing was staged - the `{ledger}` contract.
- The path it stages FROM is where the services really put it, on every
  topology: the host's GatewayConfig default (`<audit_dir>/bi`), and the
  container's native-services, which serves `--audit /audit` with /audit the
  bind of the rollout's own audit directory and no `bi_state_dir` override in
  any config the harness writes.
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
sys.path.insert(0, str(HERE))

import bistate  # noqa: E402
import container_episode  # noqa: E402
import episode  # noqa: E402


def _state(root: Path) -> Path:
    """A services state dir as the three BI services leave one."""
    bi = root / "audit" / "bi"
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
    source = tmp_path / "audit" / "bi"
    if make == "empty":
        source.mkdir(parents=True)
    assert bistate.stage(None if make == "none" else source, grading) is None
    assert bistate.resolve(grading) is None


def test_refuses_to_stage_into_itself(tmp_path):
    source = _state(tmp_path)
    with pytest.raises(ValueError):
        bistate.stage(source, source)


def test_the_host_default_is_the_gateway_default(tmp_path):
    from alkera_gateway import GatewayConfig

    rollout = tmp_path / "r0"
    world = tmp_path / "world"
    (world / "workspace").mkdir(parents=True)
    cfg = GatewayConfig(workspace=world / "workspace", warehouse=world / "w.duckdb",
                        audit_dir=rollout / "audit", world_root=world)
    assert cfg.bi_state_dir == bistate.source_for(rollout).resolve()


def test_no_harness_config_overrides_bi_state_dir(tmp_path):
    """The default is the only place it can be: neither config the harness
    writes names `bi_state_dir`, on either topology."""
    world = {"world_root": str(tmp_path / "w"), "workspace": str(tmp_path / "w" / "workspace"),
             "warehouse": str(tmp_path / "w" / "var" / "warehouse" / "warehouse.duckdb"),
             "scheduler": str(tmp_path / "w" / "var" / "sched" / "state.json"),
             "dashboards": str(tmp_path / "w" / "var" / "bi" / "dashboards"),
             "profiles_dir": str(tmp_path / "w" / "build")}
    host = episode.write_gateway_config(tmp_path / "gateway.json", world,
                                        tmp_path / "r0" / "audit", "e", "")
    assert "bi_state_dir" not in json.loads(host.read_text(encoding="utf-8"))
    inside = container_episode.container_gateway_config(world, "e", "")
    assert "bi_state_dir" not in inside
    assert inside["audit_dir"] == container_episode.AUDIT_IN_CONTAINER == "/audit"


def test_container_services_keep_it_under_the_rollouts_audit_bind():
    """compose.native.yml: native-services runs `serve --audit /audit` with
    /audit the bind of $ALKERA_EPISODE_AUDIT, which NativeContainerEpisode sets
    to the rollout's own audit directory - so `/audit/bi` in the container is
    `<rollout>/audit/bi` on the host, bistate.source_for(rollout)."""
    import yaml

    class Loader(yaml.SafeLoader):
        """compose's `!override` / `!reset` tags, read as plain values."""

    Loader.add_multi_constructor("!", lambda loader, suffix, node: (
        loader.construct_sequence(node) if isinstance(node, yaml.SequenceNode)
        else loader.construct_mapping(node) if isinstance(node, yaml.MappingNode)
        else loader.construct_scalar(node)))
    compose = yaml.load((REPO / "gateway" / "docker" / "compose.native.yml")
                        .read_text(encoding="utf-8"), Loader=Loader)
    services = compose["services"]["native-services"]
    command = " ".join(services["command"])
    assert "--audit /audit" in command
    assert any(str(v).startswith("${ALKERA_EPISODE_AUDIT") and str(v).endswith(":/audit")
               for v in services["volumes"])
    source = (HERE / "container_episode.py").read_text(encoding="utf-8")
    assert '"ALKERA_EPISODE_AUDIT": Path(self.audit_dir).resolve().as_posix()' in source


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


def test_regrade_accepts_bi_state_in_grade_argv():
    source = (HERE / "rollout.py").read_text(encoding="utf-8")
    assert '- {"ledger", "bi_state", "artifact"} - set(world)' in source
