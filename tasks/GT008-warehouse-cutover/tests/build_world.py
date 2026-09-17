#!/usr/bin/env python3
"""
GT008 episode-world builder - the task-declared entrypoint the harness calls.

    python build_world.py --prepare-only --seed 8008
    python build_world.py --root <dir> --grading <dir> --seed 8008

Stages the world through `make_fixture_world.stage_world_root` and
`write_grading_zone`, seeds the workflow plane, lays down the git history, and
prints ONE json object on stdout.

Contract with the harness (stdout):

    {"world_root", "workspace", "warehouse", "scheduler", "dashboards",
     "profiles_dir", "grading", "virtual_time", "seed", "workflow_dir",
     "observability", "obs_samples",
     "warehouse_name", "warehouses"}

THE TWO KEYS THE HARNESS DOES NOT YET READ. `warehouse_name` and `warehouses`
are what make this a two-warehouse episode: the gateway serves the `warehouse`
field on sql_query/sql_execute only when `GatewayConfig.warehouses` is
non-empty, and the consumer view in nova resolves only when the legacy catalog
is attached. `tasks/harness/episode.py:write_gateway_config` passes only
WORLD_PLANE_KEYS plus the four fixed keys, so a rollout built by the harness
today would stand a ONE-warehouse gateway up over this world and `bi_render`
would fail on the consumer view. The one-hunk patch is in DESIGN.md; until it
lands, tests/run_baselines.py (which builds its own gateway config from this
manifest) is the only driver that can run this world.
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

PROFILE = """revenue:
  target: dev
  outputs:
    dev:
      type: duckdb
      path: "{warehouse}"
      threads: 1
"""

COMMITS = [
    ("2026-03-02T10:00:00+00:00", "Tobi Akande", "takande@example.invalid",
     "revenue-mart: nightly load, contract v1"),
    ("2026-04-02T15:30:00+00:00", "Jamie North", "jnorth@example.invalid",
     "orders feed: contract v2 (versions, deletes); adjustments file (DATA-8088)"),
    ("2026-05-09T16:45:00+00:00", "Rosa Keane", "rkeane@example.invalid",
     "nova: register nova_nightly with a placeholder loader (DATA-8110)"),
]


def prepare(seed: int) -> None:
    needed = (BUILD / "legacy.duckdb", BUILD / "world.duckdb",
              BUILD / "obs" / "definitions.yaml", BUILD / "workflow" / "issues.yaml",
              BUILD / "expected" / "checkpoint_0.json")
    if not all(path.is_file() for path in needed):
        mfw.main(["--out", str(BUILD), "--seed", str(seed)])
    verify(BUILD, quiet=True)


def git_history(workspace: Path) -> None:
    """Three commits over the SAME tree. The history is flavour, not
    evidence: nothing the grader reads depends on it, and .git is excluded
    from the workspace fingerprint."""
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

    workspace = root / "workspace"
    profiles_dir = root / "var" / "dbt"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    with (profiles_dir / "profiles.yml").open("w", encoding="utf-8",
                                              newline="") as fh:
        fh.write(PROFILE.replace("{warehouse}",
                                 Path(staged["warehouse"]).as_posix()))
    git_history(workspace)

    wh = CONTRACT["warehouses"]
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
        # THE SECOND WAREHOUSE. See the module docstring: the harness does not
        # read these two keys yet; run_baselines.py does.
        "warehouse_name": wh["primary_name"],
        "warehouses": [{"name": wh["legacy_name"], "backend": "duckdb",
                        "path": staged["legacy_warehouse"],
                        "read_only": wh["legacy_read_only"],
                        "attach": wh["legacy_attach"]}],
        "legacy_warehouse": staged["legacy_warehouse"],
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
