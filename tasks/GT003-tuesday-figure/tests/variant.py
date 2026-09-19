#!/usr/bin/env python3
"""Which ARM this process is running in. Imported by every script here.

GT003 is ONE context with two `[variants]` declared in task.toml: `default`
(the control arm, the world every stored run was measured in) and `decision`
(the same world plus an owner, a boundary and a consumer, laid over it by
`fixtures/variants/decision/`). The arm has to be known BEFORE the world is
assembled and before the grader reads its first check, so it is resolved once,
here, and pinned into the environment for every child process.

    from variant import VARIANT, FIXTURES, IS_DECISION

WHERE THE ARM COMES FROM, in order (the same rule GT006's tests/variant.py
states, kept identical so the two packages cannot disagree about it):

  1. `--variant NAME` on this process's argv, read directly rather than through
     argparse, because argparse runs after the imports that need the answer.
  2. the `variant.json` that build_world.py wrote into the grading zone named
     by `--grading`. This is how the GRADER learns which arm built the world it
     was handed: an arm carried only in an environment variable is one lost
     `env=` away from grading a decision world against the control rubric.
  3. $ALKERA_TASK_VARIANT, which `taskspec` reads when the first two are silent.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
REPO = TASK.parent.parent

sys.path.insert(0, str(REPO / "tasks" / "harness"))
import taskspec                                                    # noqa: E402


def _flag(name: str) -> str | None:
    """`--name VALUE` / `--name=VALUE` off argv, without argparse."""
    argv, found = sys.argv[1:], None
    for i, arg in enumerate(argv):
        if arg == f"--{name}" and i + 1 < len(argv):
            found = argv[i + 1]
        elif arg.startswith(f"--{name}="):
            found = arg.split("=", 1)[1]
    return found


def requested() -> str | None:
    """The arm this process was asked for, by the rules in the module docstring."""
    explicit = _flag("variant")
    if explicit:
        return explicit
    grading = _flag("grading")
    if grading:
        marker = Path(grading) / "variant.json"
        if marker.is_file():
            name = json.loads(marker.read_text(encoding="utf-8")).get("variant")
            if name:
                return str(name)
    return None


VARIANT = taskspec.variant_of(TASK, requested())
os.environ[taskspec.VARIANT_ENV] = VARIANT.name
FIXTURES = taskspec.fixtures_dir(TASK)
IS_DECISION = VARIANT.name == "decision"
