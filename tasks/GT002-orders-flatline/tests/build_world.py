#!/usr/bin/env python3
"""
GT002 episode-world builder - the task-declared entrypoint the harness calls.

The rollout harness (tasks/harness/) is task-agnostic. It knows only that a task
declares `[harness] world_builder` in task.toml, invokes it with
--root/--grading/--seed, and reads ONE json object off stdout describing the
world it produced in the agent.md §9.2.1 layout. Everything GT002-specific
lives here.

This assembles deliberately the SAME world the baseline suite validates: the
warehouse is deployed through `warehouse_conn.deployed_copy`, and the workspace
and scheduler come from `fixtures/workspace_files.py`. If the harness built a
world by any other route, it would be measuring something no baseline covers -
which is exactly how GT001 shipped an oracle that aborted on its first
statement while printing TASK VALID (F622).

    python build_world.py --prepare-only                  # build the shared fixture once
    python build_world.py --root <dir> --grading <dir> --seed 2029

Contract with the harness (stdout, one json object):

    {"world_root", "workspace", "warehouse", "scheduler", "dashboards",
     "profiles_dir", "grading", "virtual_time", "seed"}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
BUILD = FIXTURES / "build"

SEED = 2029
VIRTUAL_TIME = "2026-03-13T09:00:00Z"
GIT_AUTHOR = "Data Platform <data@example.com>"
# The workspace is committed BEFORE the virtual now: the repo is a thing that
# existed before the incident, not an artifact of the episode starting.
GIT_DATE = "2026-03-12T02:00:00+00:00"

# Declared as a `bi_render` surface probe in task.toml and defined nowhere, so
# the probe failed against every world the harness could build. The dashboard is
# the consumer-facing surface the ticket's symptom is reported from, so its
# absence was not cosmetic - it removed the surface the episode starts on.
DASHBOARD = """dashboard: exec_revenue
panels:
  - name: daily_orders_by_channel
    sql: "SELECT dt, channel_group, order_count, order_value
          FROM warehouse.kpi_orders_daily ORDER BY dt, channel_group"
  - name: daily_totals
    sql: "SELECT dt, SUM(order_count) AS order_count, SUM(order_value) AS order_value
          FROM warehouse.kpi_orders_daily GROUP BY dt ORDER BY dt"
"""


def prepare(seed: int = SEED) -> None:
    """Build the shared, cached fixture world once, then verify it.

    Rollouts copy from here. Building per rollout would be slow AND would put K
    generator processes in a race over one output directory - which is how a
    stale generator silently overwrote a validated expected.duckdb before (see
    fixture_contract.py's own docstring).
    """
    BUILD.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(HERE))
    from filelock import FileLock
    from fixture_contract import verify as verify_fixture

    with FileLock(str(BUILD.parent / "fixture.lock"), timeout=600):
        if (not (BUILD / "world.duckdb").exists()
                or not (BUILD / "expected.duckdb").exists()):
            subprocess.run(
                [sys.executable, str(FIXTURES / "make_fixture_world.py"),
                 "--out", str(BUILD), "--seed", str(seed)],
                check=True, capture_output=True)
        verify_fixture(BUILD, quiet=True)      # raises FixtureDrift on drift


def _git(workspace: Path) -> None:
    """A real git repo, so git_log / git_diff / git_commit are live surfaces.

    Fixed author and dates: a commit sha that depends on the wall clock gives
    two rollouts of the same seed two different worlds.
    """
    m = re.match(r"(.*)<(.*)>", GIT_AUTHOR)
    env = {**os.environ, "GIT_AUTHOR_DATE": GIT_DATE, "GIT_COMMITTER_DATE": GIT_DATE}

    def run(*args):
        subprocess.run(["git", *args], cwd=workspace, env=env, check=True,
                       capture_output=True)

    run("init", "-q")
    run("config", "user.name", m.group(1).strip())
    run("config", "user.email", m.group(2).strip())
    run("config", "commit.gpgsign", "false")
    run("add", "-A")
    run("commit", "-q", "--no-verify", "-m", f"world seed {SEED}")


def build(root: Path, grading: Path, seed: int = SEED) -> dict:
    root, grading = Path(root).resolve(), Path(grading).resolve()
    if grading.is_relative_to(root):
        # ground rule 1: the grading zone is never reachable from the episode
        raise SystemExit("grading zone must not live inside the world root")
    prepare(seed)

    sys.path.insert(0, str(HERE))
    from warehouse_conn import deployed_copy
    sys.path.insert(0, str(FIXTURES))
    import workspace_files

    for d in ("var/sched", "var/bi/dashboards", "build"):
        (root / d).mkdir(parents=True, exist_ok=True)

    # Deployed at the CONTRACT filename via the task's own helper. Only
    # `warehouse.*` collides with the catalog named after the file stem, which is
    # why this defect hides from any suite that stages a differently-named file.
    warehouse = deployed_copy(BUILD / "world.duckdb", root)

    workspace_files.write_workspace(root / "workspace")
    workspace_files.write_scheduler(root / "var/sched/state.json")
    (root / "var/bi/dashboards/exec_revenue.yml").write_text(
        DASHBOARD, encoding="utf-8")

    _git(root / "workspace")

    grading.mkdir(parents=True, exist_ok=True)
    shutil.copy(BUILD / "expected.duckdb", grading / "expected.duckdb")
    shutil.copy(HERE / "manifest.yaml", grading / "manifest.yaml")
    truth = BUILD / "harness_truth.json"
    if truth.exists():                       # per-cause credit reference
        shutil.copy(truth, grading / "harness_truth.json")

    return {
        "world_root": str(root),
        "workspace": str(root / "workspace"),
        "warehouse": str(warehouse),
        "scheduler": str(root / "var/sched/state.json"),
        "dashboards": str(root / "var/bi/dashboards"),
        "profiles_dir": str(root / "build"),
        "grading": str(grading),
        "virtual_time": VIRTUAL_TIME,
        "seed": seed,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root")
    ap.add_argument("--grading")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--prepare-only", action="store_true")
    a = ap.parse_args()
    if a.prepare_only:
        prepare(a.seed)
        print(json.dumps({"prepared": str(BUILD)}))
        return 0
    if not a.root or not a.grading:
        ap.error("--root and --grading are required unless --prepare-only")
    print(json.dumps(build(Path(a.root), Path(a.grading), a.seed)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
