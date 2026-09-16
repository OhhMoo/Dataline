#!/usr/bin/env python3
"""
GT007 episode-world builder - the task-declared entrypoint the harness calls.

The rollout harness is task-agnostic: it reads `[harness] world_builder` out of
task.toml, runs it with --root/--grading/--seed, and parses ONE json object off
stdout. Everything GT007-specific lives here.

    python build_world.py --prepare-only --seed 7007
    python build_world.py --root <dir> --grading <dir> --seed 7007

It stages the world through `make_fixture_world.stage_world_root` and
`write_grading_zone` - the two entry points CONTRACT["layout"] names - because
the baseline suite stages it the same way, and a harness that built the world by
any other route would be measuring something no baseline covers.

Contract with the harness (stdout, one json object):

    {"world_root", "workspace", "warehouse", "scheduler", "dashboards",
     "profiles_dir", "grading", "virtual_time", "seed", "workflow_dir"}

`workflow_dir` is not optional decoration. episode.py:WORLD_PLANE_KEYS passes it
into the gateway config, and GatewayConfig's default is the ABSOLUTE
`/var/workflow`, which resolves outside any world tree: a config that does not
name it makes issues_*/chat_*/kb_* answer `no_issue_tracker` no matter what is
seeded. That is exactly how GT004's first five Opus rollouts were scored in a
half-absent world. `observability` and `ingest` are deliberately NOT published:
this world seeds neither, they are declared `dead_by_design` in task.toml with
reasons, and handing the gateway a path to nothing is worse than saying so.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
FIXTURES = TASK / "fixtures"
BUILD = FIXTURES / "build"

sys.path.insert(0, str(FIXTURES))
import make_fixture_world as mfw                                   # noqa: E402
from fixture_contract import CONTRACT, verify                      # noqa: E402

sys.path.insert(0, str(HERE))
import stage_surfaces                                              # noqa: E402

SEED = int(CONTRACT["seed"])
VIRTUAL_TIME = str(CONTRACT["virtual_now"])

PROFILE = """commercial:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: "{warehouse}"
      threads: 1
"""


def prepare(seed: int) -> None:
    """Build the shared fixture once, before any rollout forks.

    K rollouts racing to generate the same fixture is how a stale generator
    overwrites a validated reference while every verdict still prints PASS.
    """
    if not (BUILD / "world.duckdb").is_file():
        mfw.main(["--out", str(BUILD), "--seed", str(seed)])
    verify(BUILD, quiet=True)


def git_history(workspace: Path) -> None:
    """Two commits, and the second one is evidence.

    The repo existed before the incident, and the change that caused the breach
    is a commit with an author and a date - which is the whole point of ADR
    0001. `git log -p orchestration/schedule.yml` shows the wait before and
    after; nothing else in the world states both numbers. .git is excluded from
    the workspace fingerprint, so this cannot move the world's identity.
    """
    schedule = workspace / "orchestration" / "schedule.yml"
    shipped = schedule.read_text(encoding="utf-8")
    pre = (BUILD / "workspace_pre" / "orchestration" / "schedule.yml").read_text(
        encoding="utf-8")

    def run(*args, when: str, who: str, email: str):
        env = {**os.environ,
               "GIT_AUTHOR_NAME": who, "GIT_AUTHOR_EMAIL": email,
               "GIT_COMMITTER_NAME": who, "GIT_COMMITTER_EMAIL": email,
               "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
        subprocess.run(["git", *args], cwd=workspace, env=env,
                       capture_output=True, text=True, check=False)

    run("init", "-q", "-b", "main", when=mfw.PRE_CHANGE_COMMIT,
        who="Paul Diallo", email="pdiallo@example.invalid")
    with schedule.open("w", encoding="utf-8", newline="") as fh:
        fh.write(pre)
    run("add", "-A", when=mfw.PRE_CHANGE_COMMIT,
        who="Paul Diallo", email="pdiallo@example.invalid")
    run("-c", "commit.gpgsign=false", "commit", "-q", "-m",
        "commercial nightly: vendor drop moved to 01:00 (DATA-7690)",
        when=mfw.PRE_CHANGE_COMMIT,
        who="Paul Diallo", email="pdiallo@example.invalid")

    with schedule.open("w", encoding="utf-8", newline="") as fh:
        fh.write(shipped)
    run("add", "-A", when=mfw.CHANGE_COMMIT,
        who="Paul Diallo", email="pdiallo@example.invalid")
    run("-c", "commit.gpgsign=false", "commit", "-q", "-m",
        "export_commercial: give it a bigger head start after the build "
        "(DATA-7710)\n\nThe build ran long on the 8th and the export walked "
        "into a half-built mart.\nWiden the wait and raise the timeout so it "
        "cannot happen again.",
        when=mfw.CHANGE_COMMIT,
        who="Paul Diallo", email="pdiallo@example.invalid")


def build(root: Path, grading: Path, seed: int) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    grading.mkdir(parents=True, exist_ok=True)
    # Ground rule 1: the grader does not exist during the episode.
    # write_grading_zone refuses a zone inside the root.
    staged = mfw.stage_world_root(BUILD, root)
    surfaces = stage_surfaces.stage(root, VIRTUAL_TIME)
    mfw.write_grading_zone(BUILD, root, grading)

    # The rubric travels with the grading package, not with the repo checkout.
    shutil.copy(HERE / "manifest.yaml", grading / "manifest.yaml")

    workspace = root / "workspace"
    profiles_dir = root / "var" / "dbt"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    with (profiles_dir / "profiles.yml").open("w", encoding="utf-8",
                                              newline="") as fh:
        fh.write(PROFILE.replace("{warehouse}",
                                 Path(staged["warehouse"]).as_posix()))

    git_history(workspace)

    return {
        "world_root": str(root),
        "workspace": str(workspace),
        "warehouse": staged["warehouse"],
        "scheduler": staged["scheduler"],
        "dashboards": staged["dashboards"],
        "profiles_dir": str(profiles_dir),
        "grading": str(grading),
        "virtual_time": VIRTUAL_TIME,
        "seed": seed,
        # The one plane GatewayConfig would otherwise resolve to an absolute
        # path outside this world. See the module docstring.
        "workflow_dir": str(root / "var" / "workflow"),
        "surfaces_staged": surfaces,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root")
    ap.add_argument("--grading")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--prepare-only", action="store_true")
    a = ap.parse_args()
    prepare(a.seed)
    if a.prepare_only:
        return 0
    if not (a.root and a.grading):
        ap.error("--root and --grading are required unless --prepare-only")
    print(json.dumps(build(Path(a.root).resolve(),
                           Path(a.grading).resolve(), a.seed)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
