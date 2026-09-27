#!/usr/bin/env python3
"""
D2's "definitions edited" gate against the native harness's sanitiser.

The native harness strips the leading `#` comment lines from
var/obs/definitions.yaml before every episode (native_stack.sanitize_world_files).
D2 used to compare the file's raw digest against CONTRACT.obs.digest, so every
native rollout, the oracle's included, was GATED as "definitions edited" whatever
the agent did (K=1 opus-decision-k1-20260926T054914Z r0: 22 comment lines
removed, D2 0/6). The gate now compares the parsed document.

The two halves that must both hold:
  - a comment-only difference (the sanitiser's own output) is NOT an edit;
  - a real edit to what deploy_run reads (a canary's alarm dropped, a guard
    threshold moved) still IS one, and so is a file that no longer parses.

    python test_gt007_definitions_sanitised.py     (or under pytest)
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
REPO = TASK.parent.parent


def _grade():
    os.environ["ALKERA_TASK_VARIANT"] = "decision"
    # every task's grader imports its own `fixture_contract` & co. by bare
    # name; drop another task's copies so one pytest process can load both
    for name in ("fixture_contract", "make_fixture_world", "schedule_model"):
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("gt007_grade", HERE / "grade.py")
    module = importlib.util.module_from_spec(spec)
    argv, sys.argv = sys.argv, [str(HERE / "grade.py")]
    try:
        spec.loader.exec_module(module)
    finally:
        sys.argv = argv
    return module


G = _grade()
SHIPPED = G.FIXTURES / "obs" / "definitions.yaml"


def _world(tmp: Path) -> Path:
    """A world root holding the shipped definitions, run through the harness's
    real sanitiser. Returns the world's definitions path."""
    obs = tmp / G.cget("obs.relpath")
    obs.parent.mkdir(parents=True)
    shutil.copyfile(SHIPPED, obs)
    sys.path.insert(0, str(REPO / "tasks" / "harness"))
    import native_stack                                            # noqa: PLC0415
    removed = native_stack.sanitize_world_files({"world_root": str(tmp)})
    assert removed["obs_comment_lines_removed"] > 0, removed
    return obs


def test_sanitised_definitions_are_not_an_edit():
    with tempfile.TemporaryDirectory() as tmp:
        obs = _world(Path(tmp))
        # the old gate's failure, kept visible: the bytes DO differ
        assert G.sha256_bytes(obs.read_bytes()) != G.cget("obs.digest")
        assert G.definitions_intact(obs)


def test_a_real_edit_is_still_an_edit():
    with tempfile.TemporaryDirectory() as tmp:
        obs = _world(Path(tmp))
        doc = yaml.safe_load(obs.read_text(encoding="utf-8"))
        deploy = next(d for d in doc["deploys"] if (d.get("canary") or {}).get("alarms"))
        deploy["canary"]["alarms"] = deploy["canary"]["alarms"][1:]
        obs.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        assert not G.definitions_intact(obs)


def test_a_guard_threshold_moved_is_an_edit():
    with tempfile.TemporaryDirectory() as tmp:
        obs = _world(Path(tmp))
        doc = yaml.safe_load(obs.read_text(encoding="utf-8"))
        guard = next(g for d in doc["deploys"]
                     for g in ((d.get("canary") or {}).get("guards") or [])
                     if isinstance(g, dict))
        key = next(k for k, v in guard.items()
                   if isinstance(v, (int, float)) and not isinstance(v, bool))
        guard[key] = guard[key] + 1000
        obs.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
        assert not G.definitions_intact(obs)


def test_unparseable_definitions_are_an_edit():
    with tempfile.TemporaryDirectory() as tmp:
        obs = _world(Path(tmp))
        obs.write_text(obs.read_text(encoding="utf-8") + "\n  : [unclosed\n",
                       encoding="utf-8")
        assert not G.definitions_intact(obs)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
        print(f"  ok  {test.__name__}")
    print(f"{len(tests)} passed")
