#!/usr/bin/env python3
"""
GT004 episode-world builder - the task-declared entrypoint the harness calls.

The rollout harness is task-agnostic: it reads `[harness] world_builder` out of
task.toml, runs it with --root/--grading/--seed, and parses ONE json object off
stdout. Everything GT004-specific lives here.

It stages the world the SAME way the baseline suite does - through
`make_fixture_world.stage_world_root` and `write_grading_zone`, the two entry
points CONTRACT["layout"] names - because a harness that built the world by any
other route would be measuring something no baseline covers. That is not a
hypothetical: GT004's own suite hand-rolled a partial root, omitted
var/bi/dashboards and var/sched/state.json, and silently zeroed two of the six
gates for every baseline including the oracle. The suite now delegates here's
sibling; so does this.

    python build_world.py --prepare-only --seed 4004     # build the shared fixture once
    python build_world.py --root <dir> --grading <dir> --seed 4004

Contract with the harness (stdout, one json object):

    {"world_root", "workspace", "warehouse", "scheduler", "dashboards",
     "profiles_dir", "grading", "virtual_time", "seed"}
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

sys.path.insert(0, str(TASK.parent / "harness"))
import taskspec                                                    # noqa: E402

# WHICH CONTEXT. GT004 ships eight named variants (task.toml [variants]); the
# harness selects one with --variant and hands it down as ALKERA_TASK_VARIANT,
# because this process is a plain subprocess whose argv the harness does not
# own. `fixtures_dir` returns the package's own fixtures/ for an arm with no
# overlay - so `default` reads exactly the bytes a build with no variant at all
# reads - and a staged copy with the arm's overlay applied for the others.
VARIANT = taskspec.variant_of(TASK)
FIXTURES = taskspec.stage_fixtures(TASK, VARIANT)
BUILD = FIXTURES / "build"

sys.path.insert(0, str(FIXTURES))
import make_fixture_world as mfw                                   # noqa: E402
from fixture_contract import CONTRACT, verify                      # noqa: E402

sys.path.insert(0, str(HERE))
import stage_surfaces                                              # noqa: E402

SEED = int(CONTRACT["seed"])
VIRTUAL_TIME = str(CONTRACT["virtual_now"])

# The repo existed before the incident; it is not an artifact of the episode
# starting. The retry that caused the damage ran 2026-03-01 02:17, so the last
# commit predates it.
GIT_AUTHOR = "Data Platform <data@example.com>"
GIT_DATE = "2026-02-27T18:40:00+00:00"

PROFILES = """commercial:
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
    if BUILD.is_file() or not (BUILD / "world.duckdb").is_file():
        mfw.main(["--out", str(BUILD), "--seed", str(seed)])
    verify(BUILD, quiet=True)


def git_init(workspace: Path) -> None:
    """A workspace an engineer would recognise: history, not a pile of files.

    .git is excluded from the workspace fingerprint (fingerprint.py hashes the
    tree with exclude=(".git",)), so this cannot move the world's identity.
    """
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
      "commercial marts: nightly delta load, window backfill helper, BI extract")


def build(root: Path, grading: Path, seed: int) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    grading.mkdir(parents=True, exist_ok=True)
    # Ground rule 1, enforced by the harness too: the grader does not exist
    # during the episode. write_grading_zone refuses a zone inside the root.
    mfw.stage_world_root(BUILD, root)

    # The human-context and operational halves of the world, staged BEFORE the
    # grading zone is written and for that reason. probes/collateral.py scores
    # `obs` (CONTRACT overrides its advisory default) and `ingest` (the probe's
    # own default), and it compares against grading/world_sections.json: a
    # section present in the world and absent from that baseline reads as "the
    # section appeared" and costs -2 for every agent, the oracle included.
    # Seeding after write_grading_zone would do exactly that.
    surfaces = stage_surfaces.stage(root, VIRTUAL_TIME)

    mfw.write_grading_zone(BUILD, root, grading)
    problems = stage_surfaces.check_staged_before_baseline(root, grading)
    if problems:
        raise SystemExit("staged surfaces are not in the collateral baseline:\n  "
                         + "\n  ".join(problems))

    # The rubric travels with the grading package, not with the repo checkout.
    shutil.copy(HERE / "manifest.yaml", grading / "manifest.yaml")
    # ...and so does the arm this world was built for. grade.py reads it from
    # here rather than from the environment: the grader runs in its own process,
    # sometimes on another host, and an arm carried by an env var that did not
    # survive a subprocess boundary would score the wrong rubric while printing
    # a number that looked exactly like the right one.
    (grading / "variant.json").write_text(
        json.dumps({"variant": VARIANT.name, "note": VARIANT.note,
                    "grading": VARIANT.grading}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="")
    # ...and so do the false-cause families, which are grading policy. Without
    # them grade.py raises PackagingError rather than silently scoring without
    # the penalty that closes F620's unit-free half.
    fc = FIXTURES / "workflow" / "false_causes.yaml"
    if fc.is_file():
        shutil.copy(fc, grading / "false_causes.yaml")

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
        "variant": VARIANT.name,
        # The three surfaces GatewayConfig does NOT default into the world root.
        # Its defaults are absolute (`/var/workflow`, `/var/obs/observability.yml`,
        # `/var/ingest/state.json`, config.py:22-31), which resolve outside any
        # world tree, so a gateway config that does not name these serves
        # no_issue_tracker / no_observability / no_ingest_state no matter what is
        # seeded. They are published here so the config writer can pass them
        # through; `observability` names observability.yml deliberately - it does
        # not exist, and the gateway falls back to definitions.yaml beside it
        # (observability.py:518-531), which is the one definitions file this
        # world ships.
        "workflow_dir": str(root / "var" / "workflow"),
        "observability": str(root / "var" / "obs" / "observability.yml"),
        "ingest": str(root / "var" / "ingest" / "state.json"),
        "surfaces_staged": surfaces,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root")
    ap.add_argument("--grading")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--prepare-only", action="store_true")
    ap.add_argument("--variant", default=None,
                    help="context variant; defaults to ALKERA_TASK_VARIANT, "
                         "then to `default`. Named here only to report what was "
                         "resolved - the tree was staged at import time.")
    a = ap.parse_args()
    if a.variant and a.variant != VARIANT.name:
        ap.error(f"--variant {a.variant!r} disagrees with the tree this process "
                 f"already staged ({VARIANT.name!r}). Set {taskspec.VARIANT_ENV} "
                 f"instead; the fixtures are resolved at import time so that "
                 f"make_fixture_world and fixture_contract come from the arm.")
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
