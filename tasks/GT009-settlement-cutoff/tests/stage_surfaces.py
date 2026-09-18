#!/usr/bin/env python3
"""GT009 surface staging - the human half of the world.

Every ticket, message and page is read from `fixtures/build/workflow/*.yaml`
and assembled by `world/world/workflow.py` - the same assembler and the same
validator every theme uses. Nothing is invented here.

Why it matters in this unit: the three sources that disagree about whether a
closed settlement day may be restated live on three different surfaces. The
closed ticket is in the tracker, the runbook that supersedes it is in the wiki,
and the argument neither of them settles is in chat. A world served without the
workflow plane is a world in which the decision has only one side.

The observability and BI planes are staged by
`make_fixture_world.stage_world_root`: they are world state under var/obs and
var/bi, on the same footing as var/sched.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
FIXTURES = TASK / "fixtures"
REPO = TASK.parent.parent

sys.path.insert(0, str(FIXTURES))
from fixture_contract import WORKFLOW_SPEC                         # noqa: E402


class StagingError(SystemExit):
    """A world these surfaces cannot honestly describe. Fails the build."""


def stage_workflow(root: Path, vnow: str, fixtures: Path | None = None) -> dict:
    sys.path.insert(0, str(REPO / "world"))
    try:
        from world import workflow as world_workflow
    finally:
        sys.path.pop(0)
    # <fixtures>/build, not <fixtures>: the generator copies the authored
    # corpus into the build, fixture_contract.verify checks it THERE, and
    # --falsify mutates it there. The caller passes the RESOLVED fixtures tree
    # (a staged overlay copy when an arm is in play); the module default is the
    # package's own.
    fixtures = Path(fixtures) if fixtures is not None else FIXTURES
    directory = world_workflow.seed(root, fixtures / "build", WORKFLOW_SPEC, vnow)
    documents = sorted(path.name for path in directory.glob("*.json"))
    if documents != ["chat.json", "issues.json", "kb.json"]:
        raise StagingError(f"workflow staging produced {documents}, expected "
                           "chat.json, issues.json and kb.json")
    return {"dir": str(directory), "documents": documents,
            "project": WORKFLOW_SPEC["project"]["key"],
            "workspace": WORKFLOW_SPEC["workspace"],
            "space": WORKFLOW_SPEC["space"]["key"],
            "actor": WORKFLOW_SPEC["actor"]}


def stage(root: Path, vnow: str, fixtures: Path | None = None) -> dict:
    return {"workflow": stage_workflow(Path(root), vnow, fixtures=fixtures)}


def _cli(argv=None) -> int:
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("--vnow", required=True)
    a = ap.parse_args(argv)
    print(json.dumps(stage(Path(a.root), a.vnow), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
