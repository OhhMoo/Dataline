#!/usr/bin/env python3
"""
GT005 episode-world builder - the entrypoint `[harness] world_builder` names.

The rollout harness is task-agnostic: it reads the key out of task.toml, runs
this with --root/--grading/--seed, and parses ONE json object off stdout.
Everything GT005-specific lives here and in the two modules it delegates to.

    python build_world.py --prepare-only --seed 5005      # build the shared fixture
    python build_world.py --root <dir> --grading <dir> --seed 5005

Contract with the harness (stdout, one json object):

    {"world_root", "workspace", "warehouse", "scheduler", "dashboards",
     "profiles_dir", "grading", "virtual_time", "seed",
     "workflow_dir", "observability"}

The last two are not decoration. `tasks/harness/episode.py:WORLD_PLANE_KEYS`
passes exactly ("workflow_dir", "observability", "obs_samples", "ingest")
through into the gateway config, and GatewayConfig's own defaults for those are
ABSOLUTE paths outside any world tree. A builder that does not publish them
serves `no_issue_tracker` / `no_observability` no matter what is seeded - which
is precisely how GT004 shipped five scored rollouts with eight dead surfaces.
`observability` names observability.yml deliberately: it does not exist, and the
gateway falls back to definitions.yaml beside it, which is the file this world
ships. `ingest` is NOT published, and task.toml declares ingest dead_by_design
with the reason.
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

# The world is BUILT by running dbt, so this package must be executed by an
# interpreter that has it. Measured the hard way: the shell's `python` on this
# host is not always the venv that carries dbt, and the failure without this
# guard is an importlib traceback from three frames inside run_baselines, which
# reads like a bug in the task.
try:
    from importlib.metadata import version as _pkg_version
    _pkg_version("dbt-core")
except Exception as _exc:                                          # noqa: BLE001
    raise SystemExit(
        f"{sys.executable} has no dbt-core ({_exc}). GT005 builds its world by "
        "running dbt; run this package with the interpreter that has it, e.g. "
        "world/.venv/Scripts/python.exe.")

SEED = int(CONTRACT["seed"])
VIRTUAL_TIME = str(CONTRACT["virtual_now"])

# The repo existed before the episode. The last commit is the one that retired
# the v1 billing models and pointed .dbtignore at the directory - which is the
# commit that silenced the assertion, sitting in the history where an engineer
# would look for it, saying exactly what its author believed it did.
GIT_AUTHOR_DATE = "2026-01-14T12:12:00+00:00"

PROFILES = """commercial:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: "{warehouse}"
      schema: warehouse
      threads: 1
"""


def prepare(seed: int) -> None:
    """Build the shared fixture once, before any rollout forks.

    K rollouts racing to generate the same fixture is how a stale generator once
    overwrote a validated reference while every verdict still printed PASS.
    """
    if not (BUILD / "warehouse.duckdb").is_file():
        mfw.main(["--out", str(BUILD), "--seed", str(seed)])
    verify(BUILD, quiet=False)


def git_init(workspace: Path) -> None:
    """A workspace an engineer would recognise: history, not a pile of files.

    Two commits, because the second one is evidence. `.git` is excluded from
    every fingerprint this task takes, so it cannot move the world's identity.
    """
    env = {**os.environ,
           "GIT_AUTHOR_NAME": "Li Chen", "GIT_AUTHOR_EMAIL": "lchen@example.com",
           "GIT_COMMITTER_NAME": "Li Chen", "GIT_COMMITTER_EMAIL": "lchen@example.com",
           "GIT_AUTHOR_DATE": GIT_AUTHOR_DATE, "GIT_COMMITTER_DATE": GIT_AUTHOR_DATE}

    def g(*a, **kw):
        subprocess.run(["git", *a], cwd=workspace, env={**env, **kw.get("env", {})},
                       capture_output=True, text=True, check=False)

    base = {"GIT_AUTHOR_NAME": "Data Platform",
            "GIT_AUTHOR_EMAIL": "data@example.com",
            "GIT_COMMITTER_NAME": "Data Platform",
            "GIT_COMMITTER_EMAIL": "data@example.com",
            "GIT_AUTHOR_DATE": "2025-12-02T10:00:00+00:00",
            "GIT_COMMITTER_DATE": "2025-12-02T10:00:00+00:00"}
    g("init", "-q", "-b", "main")
    # Commit one: the project as it was before the migration.
    ignore = workspace / "dbt_project" / ".dbtignore"
    kept = ignore.read_bytes()
    ignore.unlink()
    g("add", "-A", env=base)
    g("-c", "commit.gpgsign=false", "commit", "-q", "-m",
      "commercial marts: close pack extract, revenue by month", env=base)
    # Commit two: RFC-118. Retire the v1 models, exclude the directory.
    ignore.write_bytes(kept)
    g("add", "-A")
    g("-c", "commit.gpgsign=false", "commit", "-q", "-m",
      "RFC-118: retire the v1 billing staging models\n\n"
      "The v1 raw tables still land so the models still compile, but nothing\n"
      "consumes them and parsing them costs ~4s on every run. Excluded from\n"
      "the parse until DATA-5107 deletes the directory in 2026.2.\n")


def build(root: Path, grading: Path, seed: int) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    grading.mkdir(parents=True, exist_ok=True)
    # Ground rule: the grader does not exist during the episode.
    # write_grading_zone refuses a zone inside the root.
    mfw.stage_world_root(BUILD, root)

    # The human-context and observability planes, staged BEFORE the grading zone
    # and for that reason: the collateral baseline has to contain them, or a
    # section that was always there reads as "the agent stood up monitoring
    # nobody asked for" and costs every agent, the oracle included.
    surfaces = stage_surfaces.stage(root, VIRTUAL_TIME)

    mfw.write_grading_zone(BUILD, root, grading)
    problems = stage_surfaces.check_staged_before_baseline(root, grading)
    if problems:
        raise SystemExit("staged surfaces are not in the collateral baseline:\n  "
                         + "\n  ".join(problems))

    # The rubric travels with the grading package, not with the repo checkout.
    shutil.copy(HERE / "manifest.yaml", grading / "manifest.yaml")

    warehouse = root / CONTRACT["layout"]["deployed_world_relpath"]
    workspace = root / CONTRACT["layout"]["workspace_relpath"]
    profiles_dir = root / CONTRACT["layout"]["profiles_relpath"]
    profiles_dir.mkdir(parents=True, exist_ok=True)
    with open(profiles_dir / "profiles.yml", "w", encoding="utf-8", newline="") as handle:
        handle.write(PROFILES.replace("{warehouse}", warehouse.as_posix()))

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
        # See the module docstring: without these two the workflow and
        # observability planes are dead however well they are seeded.
        "workflow_dir": str(root / "var" / "workflow"),
        "observability": str(root / "var" / "obs" / "observability.yml"),
        "surfaces_staged": surfaces,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root")
    ap.add_argument("--grading")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--prepare-only", action="store_true")
    a = ap.parse_args(argv)
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
