#!/usr/bin/env python3
"""
GT007 surface staging - the human half of the world.

WHY IT EXISTS. GT004's first real K=5 rollout was scored and reported with
eight surfaces dead, because the seeded corpus was never staged into the world
root and the harness never told the gateway where to look. In this unit the
scattered human context is not decoration: the FOLK EXPLANATION - "the export
has outgrown its window" - lives in the tracker, the channel and the wiki, and
it is the thing the run history has to overrule. A world served without them is
a world in which the trap does not exist and the number measured is not the
number this unit claims to measure.

THE ONE RULE. Nothing here is invented. Every ticket, message and page is read
from `fixtures/workflow/*.yaml` and assembled by `world/world/workflow.py` - the
same assembler and the same validator every theme uses - so a corpus this
accepts is a corpus the fifteen gateway tools can read and write back to.

THE OBSERVABILITY PLANE is staged by `make_fixture_world.stage_world_root`, not
here: it is world state under var/obs on the same footing as var/bi and
var/sched, and it is what the DEPLOY axis is graded on. What matters about it
belongs next to this file's own rule, because it is the same rule: the plane
watches what Commercial READ - how many days the extract covers, what it totals,
whether it has fallen behind the mart - and it watches NO CLOCK. There is no
job_duration, no job_error_rate and no freshness metric, and
`fixture_contract._problems_obs` refuses a definitions file that adds one,
because any of the three would hand the agent the ran-long/started-late
discrimination off a dashboard and the unit would stop measuring its own
mechanism. That absence is also the world's own fact: DATA-7655 - "alert when
commercial_extract has not landed by 04:00" - has been open since February, and
it is why Commercial find out at 06:00.

WHAT IS DELIBERATELY NOT SEEDED, and declared `dead_by_design` in task.toml:

  * No recorded sample store. var/obs/samples.jsonl does not exist: the world's
    writer appends only after the scheduler executes a run, and nothing has run
    inside the episode yet. `metrics_series` and `alarms_history` are LIVE and
    answer empty, which is the truth about a plane nobody has been watching.
  * No ingestion connector plane. The vendor drop is files in the repo, which
    is what the pipeline actually reads.
  * No dbt project. The three jobs are python scripts and there is no dbt in
    this world to compile.

DETERMINISM. The assembler writes canonical json; every offset in the corpus is
resolved against the world clock, never a wall clock. K rollouts from one seed
stage identical bytes.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
FIXTURES = TASK / "fixtures"
REPO = TASK.parent.parent

# The vocabulary half of the workflow contract (the corpus owns the content).
WORKFLOW_SPEC = {
    "dir": "workflow",
    "actor": "agent",
    "project": {"key": "DATA", "name": "Data platform"},
    "statuses": ["open", "triage", "in_progress", "blocked", "done", "wontfix"],
    "workspace": "commercial-data",
    "space": {"key": "DE", "name": "Data Engineering"},
}


class StagingError(SystemExit):
    """A world these surfaces cannot honestly describe. Fails the build."""


def stage_workflow(root: Path, vnow: str) -> dict:
    sys.path.insert(0, str(REPO / "world"))
    try:
        from world import workflow as world_workflow
    finally:
        sys.path.pop(0)
    # FIXTURES/build, not FIXTURES: the generator copies the authored corpus
    # into the build, fixture_contract.verify checks it THERE, and --falsify
    # mutates it there. Seeding from the authored source instead would put the
    # one part of the world nobody verifies in front of the agent.
    directory = world_workflow.seed(root, FIXTURES / "build", WORKFLOW_SPEC, vnow)
    documents = sorted(p.name for p in directory.glob("*.json"))
    if documents != ["chat.json", "issues.json", "kb.json"]:
        raise StagingError(f"workflow staging produced {documents}, expected "
                           "chat.json, issues.json and kb.json")
    return {"dir": str(directory), "documents": documents,
            "project": WORKFLOW_SPEC["project"]["key"],
            "workspace": WORKFLOW_SPEC["workspace"],
            "space": WORKFLOW_SPEC["space"]["key"],
            "actor": WORKFLOW_SPEC["actor"]}


def stage(root: Path, vnow: str) -> dict:
    return {"workflow": stage_workflow(Path(root), vnow)}


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
