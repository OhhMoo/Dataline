"""The BI graders read the agent's repository without running it.

In the measured container topology episode.grade() ran each task's grader ON
THE HOST, as the host user, against the world the agent wrote - and the agent
owns its workspace repository's `.git`. BI001's bi_eval / BI002's pbi_eval read
HEAD (and every other branch) of that repository; with `git archive` a
committed `* filter=pwn` plus `filter.pwn.smudge` in `.git/config` ran the
agent's command on the host. They now read through alkera_gateway.safegit, and
(decision D4) that topology's grader now runs in a no-network, read-only box;
the host topology's still runs on the host.

Each case runs a task's own oracle baseline exactly as its run_baselines.py
does (fresh world, the real service modules in process, the scripted agent),
except that between the agent's last action and grading the workspace
repository is weaponized (gateway/tests/hostile_git.py: a committed
.gitattributes, smudge/clean/process filters, fsmonitor, hooks, textconv, ...;
every payload touches a marker), and it is graded the way a rollout grades it:
episode.grade (bistate staging, the task's grade_argv, grade.py), in BOTH
grading sandboxes - `host`, and `container` (test_grade_sandbox.py), where the
markers live in the rollout directory, the one place the box can write, so a
payload that ran inside it would still be seen. Then:

  - grading fires nothing, and the score is check-for-check the one recorded
    in the task's tests/baselines/<arm>.json (graded on a benign repository);
  - the decision arms read a second branch (BI001 S3, BI002 M3 and H1's
    `_on_branches`), so branch reads are covered too;
  - control: the old read, `git archive HEAD bi`, run afterwards on the same
    repository DOES fire - the wiring is live, the silence is the fix. In the
    container case it is run inside the same box first, and fires there too.

Each case is a subprocess: the two tasks' test modules share names
(contract, grade, build_world, run_baselines) and pick their arm at import.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
HOSTILE = REPO / "gateway" / "tests" / "hostile_git.py"
TASKS = {"BI001": REPO / "tasks" / "BI001-revenue-disagrees-across-bi",
         "BI002": REPO / "tasks" / "BI002-powerbi-share-and-stale-refresh"}

DRIVER = r'''
import importlib.util, json, os, shutil, subprocess, sys
from pathlib import Path

task, hostile, harness, out, sandbox, base = (Path(sys.argv[1]), Path(sys.argv[2]),
                                              Path(sys.argv[3]), Path(sys.argv[4]),
                                              sys.argv[5], Path(sys.argv[6]))
spec = importlib.util.spec_from_file_location("hostile_git", hostile)
H = importlib.util.module_from_spec(spec)
spec.loader.exec_module(H)
sys.path.insert(0, str(task / "tests"))
import run_baselines as RB
sys.path.insert(0, str(harness))
import episode, taskspec

fn, _ = RB.BASELINES["oracle"]
world = RB.build_at(base, "oracle")
rollout = base / "oracle"                     # world/, grading/, audit/: a rollout's layout
make = getattr(RB, "bi_services", None)
services = (RB.bi_services.Services if make else RB.pbi_service.PowerBI)(
    world, rollout / "audit")
services.seed()
fn(world, services)
workspace = Path(world["workspace"])
markers = rollout / "markers"
H.weaponize(workspace, markers)
arm = os.environ.get(taskspec.VARIANT_ENV) or "default"
score = episode.grade(taskspec.load(task.name, task.parent, variant=arm), world,
                      workspace / "final_report.json", rollout,
                      bi_state_dir=services.bi_state, sandbox=sandbox)
head = RB.E.tree_bytes(RB.E.head_tree(workspace, base / "head"))
branches = RB.E.other_branches(workspace)
graded = H.fired(markers)
archive = ["git", "-c", "safe.directory=*", "-C", str(workspace), "archive",
           "--format=tar", "HEAD", "bi"]
in_box = None
if sandbox == "container":
    subprocess.run(episode.sandbox_command(archive, read_only=[world["world_root"]],
                                           writable=rollout),
                   capture_output=True, timeout=600)
    in_box = H.fired(markers)
    shutil.rmtree(markers)
    markers.mkdir()
subprocess.run(archive, capture_output=True, env=H.trusted_env())
out.write_text(json.dumps({
    "total": score["total"],
    "checks": {c["id"]: c["points"] for c in score["checks"]},
    "passed": {c["id"]: c["passed"] for c in score["checks"]},
    "head_files": len(head), "branches": branches,
    "fired_while_grading": graded, "fired_by_git_archive_in_box": in_box,
    "fired_by_git_archive": H.fired(markers)}))
'''


@pytest.mark.parametrize("sandbox", ["host", "container"])
@pytest.mark.parametrize("task, arm", [("BI001", "default"), ("BI001", "decision"),
                                       ("BI002", "default"), ("BI002", "decision")])
def test_grading_a_weaponized_repository_runs_nothing_and_scores_the_same(
        task, arm, sandbox, tmp_path):
    if sandbox == "container":
        sys.path.insert(0, str(HERE))
        import episode                                             # noqa: PLC0415
        ok, why = episode.grade_sandbox_available()
        if not ok:
            pytest.skip(f"container grading not exercised: {why}")
    folder = TASKS[task]
    driver = tmp_path / "driver.py"
    driver.write_text(DRIVER, encoding="utf-8")
    out = tmp_path / "out.json"
    work = tmp_path / "work"          # under pytest's basetemp, which pytest prunes
    work.mkdir()
    env = {**os.environ, "ALKERA_TASK_VARIANT": arm, "PYTHONUTF8": "1"}
    if arm == "default":
        env.pop("ALKERA_TASK_VARIANT")
    done = subprocess.run([sys.executable, str(driver), str(folder), str(HOSTILE), str(HERE),
                           str(out), sandbox, str(work)],
                          capture_output=True, text=True, env=env, timeout=900, check=False)
    assert done.returncode == 0, done.stderr[-4000:]
    got = json.loads(out.read_text(encoding="utf-8"))
    recorded = json.loads((folder / "tests" / "baselines" / f"{arm}.json")
                          .read_text(encoding="utf-8"))["baselines"]["oracle"]

    assert got["fired_while_grading"] == [], got
    assert set(got["fired_by_git_archive"]) & {"smudge", "process"}, \
        f"control: git archive on this repository should run the filter: {got}"
    if sandbox == "container":
        assert set(got["fired_by_git_archive_in_box"]) & {"smudge", "process"}, \
            f"control: inside the box too, and its markers reach the host: {got}"
    assert got["head_files"] > 0
    assert got["total"] == recorded["total"]
    assert got["checks"] == {cid: c["points"] for cid, c in recorded["checks"].items()}
    if arm == "decision":
        assert got["branches"], "the decision oracle prepares its fix on a branch"
        branch_check = {"BI001": "S3", "BI002": "M3"}[task]
        assert got["passed"][branch_check], got
