#!/usr/bin/env python3
"""
GT006c/d (STACKED) episode-world builder - the task-declared entrypoint the harness calls.

Identical to GT006's. Which corpus it stages (trap or neutral) is decided by
fixtures/workflow/*.yaml; nothing here knows which package it is in.

The rollout harness is task-agnostic: it reads `[harness] world_builder` out of
task.toml, runs it with --root/--grading/--seed, and parses ONE json object off
stdout. Everything GT006-specific lives here.

    python build_world.py --prepare-only --seed 6006     # build the shared fixture once
    python build_world.py --root <dir> --grading <dir> --seed 6006

Contract with the harness (stdout, one json object):

    {"world_root", "workspace", "warehouse", "scheduler", "dashboards",
     "profiles_dir", "grading", "virtual_time", "seed",
     "workflow_dir", "observability", "obs_samples", "ingest"}

THE LAST FOUR ARE NOT OPTIONAL HERE. `GatewayConfig`'s defaults for those planes
are absolute paths outside any world root (config.py:19-23), so a config that
does not name them leaves the gateway looking somewhere the world is not and
issues_*/chat_*/kb_*/metrics_*/alarms_*/deploy_*/ingest_* answer
`no_issue_tracker` / `no_observability` / `no_ingest_state` NO MATTER WHAT IS
SEEDED. `tasks/harness/episode.py:WORLD_PLANE_KEYS` passes exactly these four
through. GT004 measured five Opus rollouts in a half-absent world for want of
this line.

It stages the world the SAME way the baseline suite does - through
`make_fixture_world.stage_world_root`, `stage_surfaces.stage` and
`write_grading_zone`, in that order - because a harness that built the world by
any other route would be measuring something no baseline covers.
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
from collateral import snapshot                                    # noqa: E402

SEED = int(CONTRACT["seed"])
VIRTUAL_TIME = str(CONTRACT["virtual_now"])

# The repo existed before the incident; it is not an artefact of the episode
# starting. The rule shipped on RULE_DEPLOY, so the last commit is that day.
GIT_DATE = CONTRACT["rule"]["deployed"] + "T09:21:00+00:00"

PROFILES = """commercial_payments:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: "{warehouse}"
      threads: 1
"""


def prepare(seed: int) -> None:
    """Build the shared fixture once, before any rollout forks.

    K rollouts racing to generate the same fixture is how a stale generator once
    overwrote a validated reference while every verdict still printed PASS.
    """
    if not (BUILD / "world.duckdb").is_file():
        mfw.main_build(BUILD, seed)
    verify(BUILD, quiet=True)


def git_init(workspace: Path) -> None:
    """A workspace an engineer would recognise: history, not a pile of files."""
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "Data Platform", "GIT_AUTHOR_EMAIL": "data@example.com",
           "GIT_COMMITTER_NAME": "Data Platform", "GIT_COMMITTER_EMAIL": "data@example.com",
           "GIT_AUTHOR_DATE": GIT_DATE, "GIT_COMMITTER_DATE": GIT_DATE}

    def g(*a):
        subprocess.run(["git", *a], cwd=workspace, env=env,
                       capture_output=True, text=True, check=False)
    g("init", "-q", "-b", "main")
    g("add", "-A")
    g("-c", "commit.gpgsign=false", "commit", "-q", "-m",
      f"payments marts: nightly load, range backfill, BI extract "
      f"({CONTRACT['rule']['ticket']} exclusion)")


def build(root: Path, grading: Path, seed: int) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    grading.mkdir(parents=True, exist_ok=True)
    # Ground rule 1, enforced by the harness too: the grader does not exist
    # during the episode. write_grading_zone refuses a zone inside the root.
    mfw.stage_world_root(BUILD, root)

    # The human-context and operational halves of the world, staged BEFORE the
    # grading zone is written and for that reason: collateral.snapshot records
    # which planes exist, and a plane seeded after the baseline is one the
    # collateral check can never notice being deleted.
    surfaces = stage_surfaces.stage(root, VIRTUAL_TIME)

    mfw.write_grading_zone(BUILD, root, grading, snapshot_fn=snapshot)
    problems = stage_surfaces.check_staged_before_baseline(root, grading)
    if problems:
        raise SystemExit("staged surfaces are not in the collateral baseline:\n  "
                         + "\n  ".join(problems))

    # The rubric travels with the grading package, not with the repo checkout.
    shutil.copy(HERE / "manifest.yaml", grading / "manifest.yaml")

    warehouse = root / CONTRACT["layout"]["deployed_world_relpath"]
    workspace = root / "workspace"
    profiles_dir = root / "var" / "dbt"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    (profiles_dir / "profiles.yml").write_text(
        PROFILES.replace("{warehouse}", warehouse.as_posix()),
        encoding="utf-8", newline="")

    git_init(workspace)

    return {
        "world_root": str(root),
        "workspace": str(workspace),
        "warehouse": str(warehouse),
        "scheduler": str(root / CONTRACT["layout"]["scheduler_relpath"]),
        "dashboards": str(root / CONTRACT["layout"]["dashboards_relpath"]),
        "profiles_dir": str(profiles_dir),
        "grading": str(grading),
        "virtual_time": VIRTUAL_TIME,
        "seed": seed,
        # The four planes GatewayConfig does NOT default into the world root.
        # `observability` names definitions.yaml directly rather than the
        # observability.yml the default expects; the gateway reads whichever
        # single definitions file the config points at, and a world carrying two
        # is refused with invalid_observability.
        "workflow_dir": str(root / CONTRACT["layout"]["workflow_relpath"]),
        "observability": str(root / CONTRACT["layout"]["obs_definitions_relpath"]),
        "obs_samples": str(root / CONTRACT["layout"]["obs_samples_relpath"]),
        "ingest": str(root / CONTRACT["layout"]["ingest_relpath"]),
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
