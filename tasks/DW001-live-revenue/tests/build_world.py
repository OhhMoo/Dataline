#!/usr/bin/env python3
"""DW001 episode-world builder - the entrypoint `[harness] world_builder` names.

    python build_world.py --root <dir> --grading <dir> --seed <n>
    python build_world.py --prepare-only --seed <n>      # nothing to cache

Builds the `revenue_live` world with `world.build` (the same call
tests/test_dw001.py makes) and prints the one json manifest the harness reads.
The shocks are NOT installed here: the harness samples and installs them from
`[dynamic].shocks` (tasks/harness/dynamic.stage), so one builder serves any
shock realisation. The grading zone holds nothing: a dynamic task is scored
from the damage ledger the harness writes, not from a reference world.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
REPO = TASK.parents[1]
sys.path.insert(0, str(REPO / "world"))

SEED = REPO / "world" / "world" / "themes" / "revenue_live" / "seed.example.yaml"


def build(root: Path, grading: Path, seed: int) -> dict:
    from world import build as wbuild, scheduler     # noqa: E402

    wbuild.build(SEED, root)
    grading.mkdir(parents=True, exist_ok=True)
    return {
        "world_root": str(root),
        "workspace": str(root / "workspace"),
        "warehouse": str(root / "var" / "warehouse" / "warehouse.duckdb"),
        "scheduler": str(root / "var" / "sched" / "state.json"),
        "dashboards": str(root / "var" / "bi" / "dashboards"),
        "profiles_dir": str(root / "build"),
        "observability": str(root / "var" / "obs" / "definitions.yaml"),
        "obs_samples": str(root / "var" / "obs" / "samples.jsonl"),
        "grading": str(grading),
        "virtual_time": scheduler.load(root)["vnow"],
        "seed": seed,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root")
    ap.add_argument("--grading")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--prepare-only", action="store_true")
    a = ap.parse_args()
    if a.prepare_only:
        return 0
    print(json.dumps(build(Path(a.root).resolve(), Path(a.grading).resolve(),
                           a.seed)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
