#!/usr/bin/env python3
"""Which CONTEXT this process is running in. Imported by every script here.

GT006 is ONE context with four `[variants]` declared in task.toml: `default`,
`trap-ticket`, `stacked-neutral` and `stacked-ticket`. The arm decides which
`fixture_contract.py` and `make_fixture_world.py` are imported, so it has to be
known BEFORE the first import and argparse is too late. That is the only reason
this module exists: build_world.py, stage_surfaces.py, grade.py and
run_baselines.py all need the same answer at the same moment, and one copy of
three lines cannot disagree with itself.

    from variant import VARIANT, FIXTURES

`VARIANT` is the resolved arm (`taskspec.Variant`); `FIXTURES` is the tree to
put on sys.path and to read `build/` from - the package's own `fixtures/` for an
arm with no overlay, and a cached copy with `fixtures/variants/<arm>/` laid over
it otherwise.

WHERE THE ARM COMES FROM, in order:

  1. `--variant NAME` on this process's argv, read directly rather than through
     argparse.
  2. the `variant.json` that build_world.py wrote into the grading zone named by
     `--grading`. This is how the GRADER learns which arm built the world it was
     handed: an arm carried only in an environment variable is one lost `env=`
     away from grading a stacked world against the unstacked rubric and printing
     a number for it.
  3. $ALKERA_TASK_VARIANT, which `taskspec` reads for us when the first two are
     silent.

Resolving also PINS the answer into the environment, so every child process -
the world builder, the grader, dbt, git - agrees with this one by construction
rather than by being passed a second flag.
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
