#!/usr/bin/env python3
"""
GT009 episode-world builder - the task-declared entrypoint the harness calls.

    python build_world.py --prepare-only --seed 9009
    python build_world.py --root <dir> --grading <dir> --seed 9009

Stages the world through `make_fixture_world.stage_world_root` and
`write_grading_zone`, seeds the workflow plane, lays down the git history, and
prints ONE json object on stdout.

Contract with the harness (stdout):

    {"world_root", "workspace", "warehouse", "scheduler", "dashboards",
     "profiles_dir", "grading", "virtual_time", "seed", "workflow_dir",
     "observability", "obs_samples"}

One warehouse, so every key here is one the harness already carries
(`tasks/harness/episode.py:WORLD_PLANE_KEYS`). Unlike GT008 this world needs no
harness-side patch.
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

PROFILE = """settlement:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: "{warehouse}"
      threads: 1
"""

COMMITS = [
    ("2026-02-11T09:30:00+00:00", "Dev Patel", "dpatel@example.invalid",
     "settlement nightly: feed loader, publisher, source contract v2"),
    ("2026-04-02T11:05:00+00:00", "Dev Patel", "dpatel@example.invalid",
     "source contract v3: versions, voids, the clearing window"),
    ("2026-05-28T14:20:00+00:00", "Rosa Keane", "rkeane@example.invalid",
     "correction register and correction_ref, per the corrections runbook "
     "(PAY-3271)"),
]


def prepare(seed: int) -> None:
    needed = (BUILD / "world.duckdb", BUILD / "obs" / "definitions.yaml",
              BUILD / "workflow" / "issues.yaml", BUILD / "expected" / "t0.json")
    if not all(path.is_file() for path in needed):
        mfw.main(["--out", str(BUILD), "--seed", str(seed)])
    verify(BUILD, quiet=True)


def git_history(workspace: Path) -> None:
    """Three commits over the SAME tree. The history is flavour, not evidence:
    nothing the grader reads depends on it, and .git is excluded from the
    workspace fingerprint."""
    def run(*args, when: str, who: str, email: str):
        env = {**os.environ,
               "GIT_AUTHOR_NAME": who, "GIT_AUTHOR_EMAIL": email,
               "GIT_COMMITTER_NAME": who, "GIT_COMMITTER_EMAIL": email,
               "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
        subprocess.run(["git", *args], cwd=workspace, env=env,
                       capture_output=True, text=True, check=False)

    when, who, email, _ = COMMITS[0]
    run("init", "-q", "-b", "main", when=when, who=who, email=email)
    for when, who, email, message in COMMITS:
        run("add", "-A", when=when, who=who, email=email)
        run("-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty",
            "-m", message, when=when, who=who, email=email)


def build(root: Path, grading: Path, seed: int) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    grading.mkdir(parents=True, exist_ok=True)
    staged = mfw.stage_world_root(BUILD, root)
    surfaces = stage_surfaces.stage(root, VIRTUAL_TIME)
    mfw.write_grading_zone(BUILD, root, grading)
    shutil.copy(HERE / "manifest.yaml", grading / "manifest.yaml")
    # The platform's clearing-window daemon, kept where the grader can restore
    # it before the nights run. It is in the workspace so the agent can read
    # it; it is here so that editing it changes nothing the world executes.
    platform = grading / "platform"
    if platform.exists():
        shutil.rmtree(platform)
    shutil.copytree(BUILD / "workspace" / CONTRACT["layout"]["platform_relpath"],
                    platform)

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
        "observability": staged["observability"],
        "obs_samples": staged["obs_samples"],
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
