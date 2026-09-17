#!/usr/bin/env python3
"""
GT003 episode-world builder - the task-declared entrypoint the harness calls.

The rollout harness (tasks/harness/) is task-agnostic. It knows only that a task
declares `[harness] world_builder` in task.toml, invokes it with
--root/--grading/--seed, and reads ONE json object off stdout describing the
world it produced in the agent.md 9.2.1 layout. Everything GT003-specific lives
here.

This performs deliberately the SAME assembly `tests/run_baselines.py` performs -
the shared fixture staged at the CONTRACT filename `var/warehouse/warehouse.duckdb`
with `fixtures/workspace` beside it - because that staging is the only GT003
world any baseline has ever been scored against. A harness that built a
different world would be measuring something no baseline covers, which is
exactly how GT001 shipped an oracle that aborted on its first statement while
printing TASK VALID (F622). `tests/VALIDITY.md` carries the executed proof: the
shipped oracle graded against a world built here and against run_baselines'
own staging, check by check, with zero differences.

Three things the baseline staging does NOT set up, because nothing in the
baseline suite reads them, are set up here because the EPISODE does:

  * `var/sched/state.json` - the 777-run history SEED_SPEC section 3 freezes.
    The 08:14:58-08:15:39 rebuild against the 08:15:00 extract is the ordinal
    evidence for the primary cause; without the scheduler the task is not
    solvable from the world, only guessable.
  * `workspace/.git` - the DATA-5518 commit SEED_SPEC section 3 freezes, so
    `git_log`/`git_diff` are live surfaces and the compensating change has an
    author and a date the way it does in the ticket's story. The grader's
    compensating-change check still reads the WORKING TREE file only; the git
    half of A043's check stays deferred (tests/manifest.yaml
    compensating_change.note). Nothing here changes what is graded.
  * `var/bi/dashboards/board_pack.yml` - the board pack the ticket's symptom is
    reported from. instruction.md promises "the BI layer the board pack is
    built from" and GT002 shipped a `bi_render` probe for a dashboard defined
    nowhere; a brief that names a surface the world does not have removes the
    surface the episode starts on.

    python build_world.py --prepare-only                  # build the shared fixture once
    python build_world.py --root <dir> --grading <dir> --seed 4211

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
WORKSPACE = FIXTURES / "workspace"

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

# The publish script the DATA-5518 commit changed. The pre-change revision is
# derived from the shipped one by removing the predicate and the two comment
# lines that introduce it - see `_before_data_5518`.
REBUILD_SQL = "pipelines/mart_rebuild.sql"
DATA_5518_COMMENT = "-- DATA-5518 (rkulkarni 2026-03-03):"
DATA_5518_PREDICATE = "where l.customer_segment <> 'RESELLER'"

# The board pack. `rev_snapshot` is the record Finance quotes from and the
# thing the ticket's "revenue has looked low since" is read off, so the panels
# address it and the mart it is derived from. `warehouse.<table>` is
# unambiguous through the gateway, which attaches the file AS `wh` - it is
# ambiguous only to a client that opens the contract filename directly, which
# is the collision SEED_SPEC section 3 makes every other component ATTACH
# around.
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


def prepare(seed: int = SEED) -> None:
    """Build the shared, cached fixture world once, then verify it.

    Rollouts copy from here. Building it per rollout would be slow AND would put
    K generator processes in a race over the same output directory - which is
    exactly how a stale generator silently overwrote a validated expected.duckdb
    before (see fixture_contract.py's own docstring).
    """
    BUILD.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(HERE))
    from filelock import FileLock
    from fixture_contract import verify as verify_fixture

    with FileLock(str(BUILD.parent / "fixture.lock"), timeout=600):
        if (not (BUILD / "world.duckdb").exists()
                or not (BUILD / "expected.duckdb").exists()
                or not (BUILD / "sched_state.json").exists()):
            subprocess.run(
                [sys.executable, str(FIXTURES / "make_fixture_world.py"),
                 "--out", str(BUILD), "--seed", str(seed)],
                check=True, capture_output=True)
        verify_fixture(BUILD, quiet=True)      # raises FixtureDrift on drift


def _before_data_5518(shipped: str) -> str:
    """`pipelines/mart_rebuild.sql` as it stood before the compensating change.

    The shipped file carries the predicate under a two-line comment naming the
    ticket. Removing exactly those three lines is the diff the DATA-5518 commit
    has to introduce, and asserting the count means a future edit to the fixture
    file breaks the build loudly instead of committing a parent revision that
    quietly still filters the segment out.
    """
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
    """A real git repo carrying the history SEED_SPEC section 3 freezes.

    Two commits: the repo as it stood before the incident, then the DATA-5518
    commit on its own branch, fast-forwarded onto main so the branch name
    survives in the refs. Author, message and both dates are fixed, so the shas
    are a function of the seed and not of the wall clock.
    """
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
    reads it.

    The history comes from the task's own fixture generator rather than from
    `world.scheduler.initial_state`. That helper produces ONE run per job per
    day; GT003's story needs 24 hourly rebuilds and 96 quarter-hour extracts a
    day, three failed retries of one load, one off-schedule event-driven rebuild
    at 08:14:58, and every rebuild from 09:07 onward failing on the leftover
    relation - 777 runs whose ORDERING is the evidence for the primary cause.
    Rebuilding that through a day loop would be a second, divergent source for
    the one artifact the diagnosis rests on. Track A's module still owns the
    write, so the state file is serialised exactly as every other world's is.
    """
    sys.path.insert(0, str(TRACK_A))
    from world import scheduler as ta_sched

    state = json.loads((BUILD / "sched_state.json").read_text(encoding="utf-8"))
    ta_sched.save(root, state)


def build(root: Path, grading: Path, seed: int = SEED) -> dict:
    root, grading = Path(root).resolve(), Path(grading).resolve()
    if grading.is_relative_to(root):
        # ground rule 1: the grading zone is never reachable from the episode
        raise SystemExit("grading zone must not live inside the world root")
    prepare(seed)

    for d in ("var/warehouse", "var/sched", "var/bi/dashboards", "build"):
        (root / d).mkdir(parents=True, exist_ok=True)

    # Deployed under the CONTRACT name. DuckDB names the catalog after the file
    # stem there, so catalog and schema collide and every bare
    # `warehouse.<table>` reference raises "Ambiguous reference to catalog or
    # schema" for any client that opens the file directly. run_baselines.py
    # stages every baseline at this same path for exactly that reason.
    shutil.copy(BUILD / "world.duckdb", root / "var/warehouse/warehouse.duckdb")
    shutil.copytree(WORKSPACE, root / "workspace")
    # Where the vendor's rate redeliveries land (SEED_SPEC section 3). Empty,
    # and git does not track it: the unmerged JPY backfill is in
    # raw.fx_rates_inbox, which is where the loader left it.
    (root / "workspace/data/landing/fx").mkdir(parents=True, exist_ok=True)

    _scheduler(root)
    (root / "var/bi/dashboards" / f"{DASHBOARD_ID}.yml").write_text(
        DASHBOARD, encoding="utf-8", newline="")
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
