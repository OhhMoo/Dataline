#!/usr/bin/env python3
"""
GT003 episode-world builder - the task-declared entrypoint the harness calls.

The rollout harness (tasks/harness/) is task-agnostic. It knows only that a task
declares `[harness] world_builder` in task.toml, invokes it with
--root/--grading/--seed, and reads ONE json object off stdout describing the
world it produced in the agent.md 9.2.1 layout. Everything GT003-specific lives
here.

This performs deliberately the SAME assembly `tests/run_baselines.py` performs -
in fact run_baselines.py now calls `build()` for every baseline - the shared
fixture staged at the CONTRACT filename `var/warehouse/warehouse.duckdb` with
`fixtures/workspace` beside it, because that staging is the only GT003 world
any baseline has ever been scored against (F622).

Three things the fixture alone does not set up are set up here because the
EPISODE reads them:

  * `var/sched/state.json` - the 777-run history SEED_SPEC section 3 freezes.
  * `workspace/.git` - the DATA-5518 commit SEED_SPEC section 3 freezes.
  * `var/bi/dashboards/board_pack.yml` - the board pack the ticket's symptom is
    reported from.

CONTEXT VARIANTS (2026-09-19, tasks/contracts/DECISION_TEMPLATE.md). GT003 is
ONE context with two `[variants]` (task.toml): `default`, the CONTROL arm -
the world above and nothing else, the world every stored run was measured in -
and `decision`, the same world plus the overlay `fixtures/variants/decision/`
resolved by tests/variant.py. On the decision arm `tests/decision_arm.py`
additionally seeds the finance schema (the board pack's book), the pack job
and its history, the live plan a scheduler night runs, the tracker / chat /
wiki, and the consumer's last receipt. The resolved arm is written into the
grading zone as `variant.json`, the staged decision as `decision.json`, and a
copy of the world as shipped as `pre_repair_world/`, which the grader's
whole-object and irrelevant-state checks compare against.

    python build_world.py --prepare-only                  # build the shared fixture once
    python build_world.py --root <dir> --grading <dir> --seed 4211 [--variant decision]

Contract with the harness (stdout, one json object):

    {"world_root", "workspace", "warehouse", "scheduler", "dashboards",
     "profiles_dir", "grading", "virtual_time", "seed", "variant"
     [, "workflow_dir"]}
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
TASK = HERE.parent
REPO = TASK.parent.parent
TRACK_A = REPO / "world"

sys.path.insert(0, str(HERE))
from variant import VARIANT, FIXTURES                              # noqa: E402
import decision_arm                                                # noqa: E402

# The fixture cache lives in the arm's resolved fixtures tree, as every other
# reformed task keeps it: the package's own fixtures/build on the control arm,
# the staged copy's build/ on the decision arm (taskspec.stage_fixtures copies
# an existing build along, so the generator runs once per machine).
BUILD = FIXTURES / "build"
GENERATOR = TASK / "fixtures" / "make_fixture_world.py"
WORKSPACE = FIXTURES / "workspace"          # arm-resolved: the overlay's files ride along

SEED = 4211
VIRTUAL_TIME = "2026-03-05T09:00:00Z"

# The repo as it stood before the incident, and the commit the incident
# provoked. Both are frozen in SEED_SPEC.md section 3; both are pinned to a
# fixed author, message and date, because a commit sha that depends on the wall
# clock gives two rollouts of the same seed two different worlds.
BASE_AUTHOR = "Data Platform <data@example.com>"
BASE_DATE = "2026-03-01T02:00:00+00:00"
FIX_AUTHOR = "rkulkarni <rkulkarni@example.com>"
FIX_DATE = "2026-03-03T16:40:00+00:00"
FIX_BRANCH = "fix/DATA-5518-reseller-double-count"
FIX_MESSAGE = """DATA-5518: exclude reseller lines from the revenue mart

Mart was double counting the reseller segment on 03-03. Filtering it out
until the source feed is fixed.
"""

REBUILD_SQL = "pipelines/mart_rebuild.sql"
DATA_5518_COMMENT = "-- DATA-5518 (rkulkarni 2026-03-03):"
DATA_5518_PREDICATE = "where l.customer_segment <> 'RESELLER'"

DASHBOARD_ID = "board_pack"
DASHBOARD = """dashboard: board_pack
title: Revenue board pack
owner: finance-systems
panels:
  - name: mtd_revenue_by_region
    title: Month-to-date revenue by region, last 24 hours of ticks
    sql: "SELECT snapshot_ts, region, mtd_revenue_usd, mtd_order_count,
                 active_accounts
          FROM warehouse.rev_snapshot
          WHERE snapshot_ts >= TIMESTAMP '2026-03-04 09:00:00'
          ORDER BY snapshot_ts, region"
  - name: daily_revenue
    title: Revenue by day from the mart
    sql: "SELECT order_dt, ROUND(SUM(revenue_usd), 2) AS revenue_usd,
                 SUM(order_count) AS order_count
          FROM warehouse.mart_revenue_fact
          WHERE order_dt >= DATE '2026-02-01'
          GROUP BY 1 ORDER BY 1"
"""

# What pre_repair_world keeps of the built world: everything the grader's
# whole-object, irrelevant-state and boundary checks compare against. The
# git history is left out (the working tree is what the checks read).
PRE_REPAIR_IGNORE = shutil.ignore_patterns(".git", "*.wal", "__pycache__")


def prepare(seed: int = SEED) -> None:
    """Build the shared, cached fixture world once, then verify it."""
    BUILD.mkdir(parents=True, exist_ok=True)
    from filelock import FileLock
    from fixture_contract import verify as verify_fixture

    with FileLock(str(BUILD.parent / "fixture.lock"), timeout=600):
        if (not (BUILD / "world.duckdb").exists()
                or not (BUILD / "expected.duckdb").exists()
                or not (BUILD / "sched_state.json").exists()):
            subprocess.run(
                [sys.executable, str(GENERATOR), "--out", str(BUILD),
                 "--seed", str(seed)],
                check=True, capture_output=True)
        verify_fixture(BUILD, quiet=True)      # raises FixtureDrift on drift


def _before_data_5518(shipped: str) -> str:
    """`pipelines/mart_rebuild.sql` as it stood before the compensating change."""
    out, dropped = [], 0
    lines = shipped.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith(DATA_5518_COMMENT):
            while i < len(lines) and lines[i].lstrip().startswith("--"):
                i += 1
                dropped += 1
            if i < len(lines) and lines[i].strip() == DATA_5518_PREDICATE:
                i += 1
                dropped += 1
            continue
        out.append(lines[i])
        i += 1
    if dropped != 3:
        raise SystemExit(
            f"{REBUILD_SQL} no longer carries the DATA-5518 comment and "
            f"predicate as three consecutive lines (removed {dropped}). The "
            f"parent revision of the compensating change cannot be derived, so "
            f"the git history would misrepresent what Ravi changed.")
    return "".join(out)


def _git(workspace: Path) -> None:
    """A real git repo carrying the history SEED_SPEC section 3 freezes."""
    shipped = (workspace / REBUILD_SQL).read_text(encoding="utf-8")

    def run(*args, author: str = "", date: str = ""):
        env = dict(os.environ)
        if date:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
        cmd = ["git"]
        if author:
            m = re.match(r"(.*)<(.*)>", author)
            cmd += ["-c", f"user.name={m.group(1).strip()}",
                    "-c", f"user.email={m.group(2).strip()}"]
        subprocess.run([*cmd, *args], cwd=workspace, env=env, check=True,
                       capture_output=True)

    run("init", "-q")
    run("symbolic-ref", "HEAD", "refs/heads/main")
    run("config", "commit.gpgsign", "false")
    run("config", "core.autocrlf", "false")

    (workspace / REBUILD_SQL).write_text(_before_data_5518(shipped),
                                         encoding="utf-8", newline="")
    run("add", "-A")
    run("commit", "-q", "--no-verify", "-m", "revenue mart and BI extract",
        author=BASE_AUTHOR, date=BASE_DATE)

    run("checkout", "-q", "-b", FIX_BRANCH)
    (workspace / REBUILD_SQL).write_text(shipped, encoding="utf-8", newline="")
    run("add", "-A")
    run("commit", "-q", "--no-verify", "-m", FIX_MESSAGE,
        author=FIX_AUTHOR, date=FIX_DATE)

    run("checkout", "-q", "main")
    run("merge", "-q", "--ff-only", FIX_BRANCH)


def _scheduler(root: Path) -> None:
    """The run history SEED_SPEC section 3 freezes, written where the gateway
    reads it, through Track A's own module so it is serialised as every other
    world's is."""
    sys.path.insert(0, str(TRACK_A))
    from world import scheduler as ta_sched

    state = json.loads((BUILD / "sched_state.json").read_text(encoding="utf-8"))
    ta_sched.save(root, state)


def build(root: Path, grading: Path, seed: int = SEED) -> dict:
    root, grading = Path(root).resolve(), Path(grading).resolve()
    if grading.is_relative_to(root):
        # ground rule 1: the grading zone is never reachable from the episode
        raise SystemExit("grading zone must not live inside the world root")
    problem = decision_arm.role_problem()
    if problem:
        raise SystemExit(problem)
    prepare(seed)

    for d in ("var/warehouse", "var/sched", "var/bi/dashboards", "build"):
        (root / d).mkdir(parents=True, exist_ok=True)

    # Deployed under the CONTRACT name (catalog == schema == "warehouse" for
    # anyone who opens the file directly; every component ATTACHes around it).
    shutil.copy(BUILD / "world.duckdb", root / "var/warehouse/warehouse.duckdb")
    shutil.copytree(WORKSPACE, root / "workspace")
    (root / "workspace/data/landing/fx").mkdir(parents=True, exist_ok=True)

    _scheduler(root)
    (root / "var/bi/dashboards" / f"{DASHBOARD_ID}.yml").write_text(
        DASHBOARD, encoding="utf-8", newline="")

    # THE DECISION ARM: the owner, the boundary and the consumer, staged into
    # the world BEFORE the git history (so the pack script has a commit) and
    # BEFORE pre_repair_world is copied (so the grader's baseline carries the
    # same finance book, live plan and corpus the agent sees).
    seeded = decision_arm.seed_world(root, VIRTUAL_TIME) if decision_arm.IS_DECISION else None

    _git(root / "workspace")

    grading.mkdir(parents=True, exist_ok=True)
    shutil.copy(BUILD / "expected.duckdb", grading / "expected.duckdb")
    shutil.copy(HERE / "manifest.yaml", grading / "manifest.yaml")
    (grading / "variant.json").write_text(
        json.dumps({"variant": VARIANT.name, "note": VARIANT.note,
                    "role": VARIANT.role or "control",
                    "grading": VARIANT.grading,
                    "fixtures": FIXTURES.as_posix()}, indent=2) + "\n",
        encoding="utf-8", newline="")
    pre = grading / "pre_repair_world"
    if pre.exists():
        shutil.rmtree(pre)
    shutil.copytree(root / "var", pre / "var", ignore=PRE_REPAIR_IGNORE)
    shutil.copytree(root / "workspace", pre / "workspace", ignore=PRE_REPAIR_IGNORE)

    record = decision_arm.grading_record()
    if record is not None:
        (grading / "decision.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n",
            encoding="utf-8", newline="")
        problems = decision_arm.verify_world(root)
        if problems:
            raise SystemExit("the built world is not the decision world "
                             "decision.yaml describes:\n  " + "\n  ".join(problems))

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
    }
    if seeded is not None:
        out["workflow_dir"] = str(root / "var" / "workflow")
        out["decision_seeded"] = seeded
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root")
    ap.add_argument("--grading")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--prepare-only", action="store_true")
    ap.add_argument("--variant", default=None,
                    help="context variant; also read from ALKERA_TASK_VARIANT. "
                         "Resolved at import time by tests/variant.py; this "
                         "declaration documents it and rejects a typo.")
    a = ap.parse_args()
    if a.prepare_only:
        prepare(a.seed)
        print(json.dumps({"prepared": str(BUILD), "variant": VARIANT.name}))
        return 0
    if not a.root or not a.grading:
        ap.error("--root and --grading are required unless --prepare-only")
    print(json.dumps(build(Path(a.root), Path(a.grading), a.seed)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
