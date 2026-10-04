"""instruction.md == the prompt the harness sends, except the profiles path.

Contract 1.9: canary line + exactly what agent_under_test.build_prompt composes
for `--driver native --topology container`; the only difference allowed is the
dbt profile living in the agent's HOME (/home/engineer) instead of /root.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest

import test_export_support as support
from alkera_harbor import harness, render, worldbuild
from alkera_harbor.constants import AGENT_HOME, CANARY_MD
from alkera_harbor.naming import ArmRef

# harbor/models/task/task.py strip_canary (0.23.0), verbatim logic
_CANARY_LINE_RE = re.compile(r"^(<!--.*canary.*-->|#.*canary.*)$", re.IGNORECASE)


def strip_canary(text: str) -> str:
    lines = text.split("\n")
    idx = 0
    while idx < len(lines) and _CANARY_LINE_RE.match(lines[idx].strip()):
        idx += 1
    while idx < len(lines) and lines[idx].strip() == "":
        idx += 1
    return "\n".join(lines[idx:])


def _workspace(tmp_path, with_profiles=True):
    ws = tmp_path / "world" / "workspace"
    (ws / "docs").mkdir(parents=True)
    (ws / ".envrc").write_text("export X=1\n")
    (ws / "docs" / "onboarding.md").write_text("# week one\n")
    profiles = tmp_path / "world" / "var" / "dbt"
    profiles.mkdir(parents=True)
    if with_profiles:
        (profiles / "profiles.yml").write_text("p: {}\n")
    return ws, profiles


def _prompt(ws, profiles, **kw):
    aut = harness.modules().aut
    return aut.build_prompt("## The ticket body\n", {}, "/workspace/final_report.json",
                            600, 5400, "native", workspace=ws, profiles_dir=profiles,
                            container_shell=True, **kw)


def test_only_the_profiles_path_differs(tmp_path):
    ws, profiles = _workspace(tmp_path)
    harness_prompt = _prompt(ws, profiles)               # what rollout.py sends
    ours = _prompt(ws, profiles, home=AGENT_HOME)
    assert "/root/.dbt/profiles.yml" in harness_prompt
    assert ours == harness_prompt.replace("/root/.dbt/profiles.yml",
                                          "/home/engineer/.dbt/profiles.yml")
    assert "/root" not in ours
    assert str(tmp_path) not in ours                      # no host path


def test_without_a_profile_the_prompts_are_identical(tmp_path):
    ws, profiles = _workspace(tmp_path, with_profiles=False)
    assert _prompt(ws, profiles, home=AGENT_HOME) == _prompt(ws, profiles)


def test_canary_is_stripped_back_to_the_exact_prompt(tmp_path):
    ws, profiles = _workspace(tmp_path)
    prompt = _prompt(ws, profiles, home=AGENT_HOME)
    text = render.instruction_md(prompt)
    assert text.split("\n", 1)[0] == CANARY_MD
    assert strip_canary(text) == prompt


def test_harbor_strip_canary_agrees(tmp_path):
    ws, profiles = _workspace(tmp_path)
    prompt = _prompt(ws, profiles, home=AGENT_HOME)
    script = ("import json, sys\nfrom harbor.models.task.task import strip_canary\n"
              "print(json.dumps(strip_canary(json.loads(sys.stdin.read()))))")
    assert support.run_harbor(script, render.instruction_md(prompt)) == prompt


def test_a_prompt_starting_with_blank_lines_is_refused():
    with pytest.raises(render.RenderError):
        render.instruction_md("\nYou are ...")


def test_real_arm_parity_with_the_harness():
    """GT007 ships a dbt profile (var/dbt/profiles.yml), so the profiles line
    is present and is the ONLY difference from the harness's own prompt."""
    m = harness.modules()
    ref = ArmRef("GT007-ran-long-or-started-late", "default")
    with tempfile.TemporaryDirectory(prefix="alkera-parity-") as tmp:
        base = Path(tmp)
        with harness.variant_env(ref.arm):          # rollout.py's own sequence
            spec = m.taskspec.load(ref.task_id, variant=ref.arm)
            m.episode.prepare_task(spec)
            world = m.episode.build_world(spec, base / "world", base / "grading")
            m.native_stack.install_workspace_cli(world, topology="container")
            m.native_stack.stage_discovery(world)
            m.native_stack.sanitize_world_files(world)
        harness_prompt = m.aut.build_prompt(
            spec.instruction, {}, spec.artifact_in_world, spec.max_tool_calls,
            spec.wall_clock_seconds, "native", False, shell=False,
            workspace=world["workspace"], profiles_dir=world["profiles_dir"],
            container_shell=True)
    staged = worldbuild.stage_arm(ref)
    try:
        assert harness_prompt.count("/root/.dbt/profiles.yml") == 1
        assert staged.prompt == harness_prompt.replace(
            "/root/.dbt/profiles.yml", "/home/engineer/.dbt/profiles.yml")
        assert staged.prompt.endswith(spec.instruction)
        envrc = (staged.workspace_dir / ".envrc").read_text()
        assert 'export DBT_PROFILES_DIR="/home/engineer/.dbt"' in envrc
        profiles = (staged.home_dir / ".dbt" / "profiles.yml").read_text()
        assert "/world/var/warehouse/" in profiles
    finally:
        staged.cleanup()
