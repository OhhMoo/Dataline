#!/usr/bin/env python3
"""
GT001 episode-world builder - the task-declared entrypoint the harness calls.

The rollout harness (tasks/harness/) is task-agnostic. It knows only that a task
declares `[harness] world_builder` in task.toml, invokes it with
--root/--grading/--seed, and reads ONE json object off stdout describing the
world it produced in the §9.2.1 layout. Everything GT001-specific lives here.

This performs deliberately the SAME assembly as
tests/test_integration.py::build_world. That is the only GT001 world that has
ever been stood up through the gateway and graded end to end; if the harness
built a different world, it would be measuring something no test covers.

    python build_world.py --prepare-only                  # build the shared fixture once
    python build_world.py --root <dir> --grading <dir> --seed 1337

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
REPO = HERE.parents[2]
TRACK_A = REPO / "world"
FIXTURES = HERE / "fixtures"
BUILD = FIXTURES / "build"

SEED = 1337
VIRTUAL_TIME = "2026-03-05T09:00:00Z"
GIT_AUTHOR = "Data Platform <data@example.com>"
# The workspace is committed at the world's last-nightly timestamp, not at the
# virtual now: the repo is a thing that existed before the incident.
GIT_DATE = "2026-03-04T02:00:00+00:00"

# Track A's scheduler took a `jobs` mapping in R003 (world themes). These two
# reproduce the run history the world had under the old hardcoded shape exactly:
# daily_load at day+1 01:00 for 45s, dbt_build five minutes later for 90s.
SCHED_JOBS = {
    "daily_load": {"command": "load_daily_revenue", "duration_s": 45,
                   "time": "01:00", "day_offset": 1, "detail": "dt={day}"},
    "dbt_build": {"command": "dbt_build", "duration_s": 90,
                  "after": {"job": "daily_load", "minutes": 5}},
}

DASHBOARD = (
    "dashboard: commercial_daily\n"
    "panels:\n"
    "  - name: aov_daily\n"
    '    sql: "SELECT dt, order_count, revenue, aov FROM warehouse.kpi_daily'
    ' ORDER BY dt"\n'
)


def prepare(seed: int = SEED) -> None:
    """Build the shared, cached fixture world once, then verify it.

    Rollouts copy from here. Building it per rollout would be slow AND would put
    K generator processes in a race over the same output directory - which is
    exactly how a stale generator silently overwrote expected.duckdb once before
    (see fixture_contract.py).
    """
    BUILD.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(HERE))
    from filelock import FileLock
    from fixture_contract import verify as verify_fixture

    # K rollouts start at once and every one of them arrives here. Generation
    # must happen exactly once: two generators writing the same expected.duckdb
    # is precisely how a stale reference got written under a validated run
    # before (see fixture_contract.py's own docstring).
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

    Fixed author and dates: the commit sha must not depend on wall clock, or two
    rollouts of the same seed get different worlds.
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

    for d in ("workspace/dbt_project/models/warehouse", "var/warehouse",
              "var/sched", "var/bi/dashboards", "build"):
        (root / d).mkdir(parents=True, exist_ok=True)

    # deployed under the CONTRACT name - this is what exposes the duckdb
    # catalog/schema collision that made GT001 unsolvable end to end
    shutil.copy(BUILD / "world.duckdb", root / "var/warehouse/warehouse.duckdb")
    shutil.copy(FIXTURES / "workspace/dbt_project/models/warehouse/fct_orders.sql",
                root / "workspace/dbt_project/models/warehouse/fct_orders.sql")

    sys.path.insert(0, str(TRACK_A))
    from world import scheduler as ta_sched
    state = ta_sched.initial_state(
        {"vtime_start": "2025-09-05T00:00:00Z", "days": 181}, SCHED_JOBS)
    state["runs"].append({
        "job": "daily_load", "vts_start": "2026-03-03T04:12:00Z",
        "vts_end": "2026-03-03T04:12:45Z", "status": "success",
        "detail": "retry-20260303-0412 backfill dt=2026-02-01..2026-03-02"})
    ta_sched.save(root, state)

    (root / "var/bi/dashboards/commercial_daily.yml").write_text(
        DASHBOARD, encoding="utf-8")

    _git(root / "workspace")

    grading.mkdir(parents=True, exist_ok=True)
    shutil.copy(BUILD / "expected.duckdb", grading / "expected.duckdb")
    shutil.copy(HERE / "manifest.yaml", grading / "manifest.yaml")

    return {
        "world_root": str(root),
        "workspace": str(root / "workspace"),
        "warehouse": str(root / "var/warehouse/warehouse.duckdb"),
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
