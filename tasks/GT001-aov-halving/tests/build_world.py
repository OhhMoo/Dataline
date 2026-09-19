#!/usr/bin/env python3
"""
GT001 episode-world builder - the task-declared entrypoint the harness calls.

The rollout harness (tasks/harness/) is task-agnostic. It knows only that a task
declares `[harness] world_builder` in task.toml, invokes it with
--root/--grading/--seed, and reads ONE json object off stdout describing the
world it produced in the §9.2.1 layout. Everything GT001-specific lives here.

This performs deliberately the SAME assembly as
tests/test_integration.py::build_world for the CONTROL arm. That is the only
GT001 world that has ever been stood up through the gateway and graded end to
end; if the harness built a different world, it would be measuring something no
test covers.

    python build_world.py --prepare-only                  # build the shared fixture once
    python build_world.py --root <dir> --grading <dir> --seed 1337

Contract with the harness (stdout, one json object):

    {"world_root", "workspace", "warehouse", "scheduler", "dashboards",
     "profiles_dir", "grading", "virtual_time", "seed", "variant"}
    + "workflow_dir" on the decision arm only

WHICH CONTEXT. GT001 ships two arms (task.toml [variants]): `default`, the
shipped world and the CONTROL arm (byte for byte what every recorded rollout ran
in), and `decision`, which stages Finance's monthly pack behind the FP&A lead's
sign-off (DESIGN.md §2). The harness selects the arm with ALKERA_TASK_VARIANT;
`taskspec.stage_fixtures` returns the package's own fixtures/ for `default` and
a staged copy with the arm's overlay applied otherwise. GT001's large fixture
(the two duckdb files, the shipped model) lives under tests/fixtures/ and is
never copied; the top-level fixtures/ carries ONLY the overlays, so the staged
tree for the decision arm is the overlay and nothing else.

`workflow_dir` is published on the decision arm only, and that is the point:
tasks/harness/episode.py:WORLD_PLANE_KEYS passes it into the gateway config, and
without it the tracker/chat/wiki tools answer `no_issue_tracker` however well the
plane is seeded. On the control arm those surfaces are dead by design (task.toml
`dead_by_design`), so the key is deliberately absent there.

THE GRADING ZONE carries, beside the reference and the rubric:
    manifest.yaml        the rubric (checks table, zero_for_doing_nothing)
    variant.json         which arm this world was built for (grade.py reads it
                         to decide whether W1-W3 exist)
    pre_episode/         the world's own state before the episode: scheduler
                         state, dashboards, the dbt project, the workflow plane
                         and live.json where staged. P1 (irrelevant state) and
                         H1 (false claim) read it; an agent cannot edit the
                         baseline it is measured against.
    decision.json        decision arm only: the three reference hashes of the
                         pack (seeded / held / restated), the closed rows as
                         issued, the figures W1 accepts.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
REPO = TASK.parent.parent
TRACK_A = REPO / "world"
FIXTURES = HERE / "fixtures"          # the large, shipped fixture (never overlaid)
BUILD = FIXTURES / "build"

sys.path.insert(0, str(TASK.parent / "harness"))
import taskspec                                                    # noqa: E402

VARIANT = taskspec.variant_of(TASK)
STAGED = taskspec.stage_fixtures(TASK, VARIANT)   # overlays only (see module doc)
DECISION_ARM = "decision"

SEED = 1337
VIRTUAL_TIME = "2026-03-05T09:00:00Z"
GIT_AUTHOR = "Data Platform <data@example.com>"
# The workspace is committed at the world's last-nightly timestamp, not at the
# virtual now: the repo is a thing that existed before the incident.
GIT_DATE = "2026-03-04T02:00:00+00:00"

# Track A's scheduler took a `jobs` mapping in R003 (world themes). These two
# reproduce the run history the world had under the old hardcoded shape exactly:
# daily_load at day+1 01:00 for 45s, dbt_build five minutes later for 90s.
SCHED_SEED = {"vtime_start": "2025-09-05T00:00:00Z", "days": 181}
SCHED_JOBS = {
    "daily_load": {"command": "load_daily_revenue", "duration_s": 45,
                   "time": "01:00", "day_offset": 1, "detail": "dt={day}"},
    "dbt_build": {"command": "dbt_build", "duration_s": 90,
                  "after": {"job": "daily_load", "minutes": 5}},
}
RETRY_RUN = {
    "job": "daily_load", "vts_start": "2026-03-03T04:12:00Z",
    "vts_end": "2026-03-03T04:12:45Z", "status": "success",
    "detail": "retry-20260303-0412 backfill dt=2026-02-01..2026-03-02"}

DASHBOARD = (
    "dashboard: commercial_daily\n"
    "panels:\n"
    "  - name: aov_daily\n"
    '    sql: "SELECT dt, order_count, revenue, aov FROM warehouse.kpi_daily'
    ' ORDER BY dt"\n'
)

# The workflow plane's vocabulary (world/world/workflow.py `seed`). The corpus
# files are the overlay's; this block is world shape.
WORKFLOW_SPEC = {
    "dir": "workflow",
    "actor": "agent",
    "project": {"key": "DATA", "name": "Data Platform"},
    "statuses": ["open", "in_progress", "blocked", "done"],
    "workspace": {"id": "alkera", "name": "Alkera"},
    "space": {"key": "finance", "name": "Finance"},
}

PRE_EPISODE = "pre_episode"
# What the pre-episode snapshot carries, world-root relative. Small by
# construction: the warehouse is NOT copied (the reference already is).
SNAPSHOT = ("var/sched/state.json", "var/world/live.json", "var/bi/dashboards",
            "var/workflow", "workspace/dbt_project", "workspace/pipelines")


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


def arm_module(staged: Path = STAGED):
    """The decision arm's staging/measuring module, from the STAGED tree (the
    overlay puts it there); None on an arm whose overlay ships none."""
    path = Path(staged) / "decision_arm.py"
    if not path.is_file():
        return None
    if str(TRACK_A) not in sys.path:
        sys.path.append(str(TRACK_A))
    spec = importlib.util.spec_from_file_location("gt001_decision_arm", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _snapshot(root: Path, grading: Path) -> list[str]:
    dest = grading / PRE_EPISODE
    if dest.exists():
        shutil.rmtree(dest)
    taken = []
    for rel in SNAPSHOT:
        src = root / rel
        if src.is_dir():
            shutil.copytree(src, dest / rel, ignore=shutil.ignore_patterns(".git"))
            taken.append(rel)
        elif src.is_file():
            (dest / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dest / rel)
            taken.append(rel)
    return taken


def _write_json(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(json.dumps(doc, indent=2, sort_keys=True) + "\n")


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

    if str(TRACK_A) not in sys.path:
        sys.path.insert(0, str(TRACK_A))
    from world import scheduler as ta_sched
    state = ta_sched.initial_state(SCHED_SEED, SCHED_JOBS)
    state["runs"].append(dict(RETRY_RUN))
    ta_sched.save(root, state)

    (root / "var/bi/dashboards/commercial_daily.yml").write_text(
        DASHBOARD, encoding="utf-8")

    # ---- the arm's additions (decision arm only; the control arm is the
    # shipped world and nothing above changes for it)
    arm = arm_module()
    staged_arm = None
    if arm is not None:
        from world import workflow as ta_workflow
        staged_arm = arm.stage(root, STAGED, ta_sched, SCHED_SEED, SCHED_JOBS)
        staged_arm["workflow_dir"] = str(
            ta_workflow.seed(root, STAGED, WORKFLOW_SPEC, VIRTUAL_TIME))

    _git(root / "workspace")

    grading.mkdir(parents=True, exist_ok=True)
    shutil.copy(BUILD / "expected.duckdb", grading / "expected.duckdb")
    shutil.copy(HERE / "manifest.yaml", grading / "manifest.yaml")
    snapshot = _snapshot(root, grading)
    # The arm travels with the grading package, not with an env var that may
    # not survive a subprocess boundary. grade.py reads it to decide which
    # check rows exist (W1-W3 are emitted on the decision arm only).
    _write_json(grading / "variant.json",
                {"variant": VARIANT.name, "note": VARIANT.note,
                 "role": VARIANT.role, "grading": VARIANT.grading})
    if arm is not None:
        contract = arm.measure(root, BUILD / "expected.duckdb")
        arm.write_contract(grading, contract)

    out = {
        "world_root": str(root),
        "workspace": str(root / "workspace"),
        "warehouse": str(root / "var/warehouse/warehouse.duckdb"),
        "scheduler": str(root / "var/sched/state.json"),
        "dashboards": str(root / "var/bi/dashboards"),
        "profiles_dir": str(root / "build"),
        "grading": str(grading),
        "virtual_time": VIRTUAL_TIME,
        "seed": seed,
        "variant": VARIANT.name,
        "pre_episode_snapshot": snapshot,
    }
    if staged_arm is not None:
        out["workflow_dir"] = staged_arm["workflow_dir"]
        out["arm_staged"] = staged_arm
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root")
    ap.add_argument("--grading")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--prepare-only", action="store_true")
    ap.add_argument("--variant", default=None,
                    help="context arm; defaults to ALKERA_TASK_VARIANT, then to "
                         "`default`. Named here only to confirm what was "
                         "resolved - the fixtures tree was staged at import time.")
    a = ap.parse_args()
    if a.variant and a.variant != VARIANT.name:
        ap.error(f"--variant {a.variant!r} disagrees with the tree this process "
                 f"already staged ({VARIANT.name!r}). Set {taskspec.VARIANT_ENV} "
                 f"instead; the fixtures are resolved at import time.")
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
