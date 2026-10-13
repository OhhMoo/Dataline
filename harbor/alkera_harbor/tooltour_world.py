#!/usr/bin/env python3
"""The world builder of a tool-tour task: the harness's world-builder contract
(`[harness] world_builder`), so the generator stages a probe's world with the
same code it stages every task's (worldbuild._stage, via tooltour.stage_probe).

    python tooltour_world.py --root <dir>/world --grading <dir>/grading --seed 11
    python tooltour_world.py --prepare-only --seed 11

The profile comes from the environment the generator exports around the call
(PROFILE_ENV, PLUGS_ENV): the builder contract's argv has no slot for it.

WHAT IT BUILDS: exactly the container tour's world (tasks/harness/tool_tour.py
`run_group`): `world_seed(profile, plugs)` - the commerce_analytics theme at
seed 11, the BI001/BI002 seed, with the discovery layer, the parquet exports
when the box has the files slot, and the profile record - built by
`bi_e2e_check.build_world` under `tool_tour.as_profile`. Imported, not
copied. The grading zone stays empty: a probe's truth is read from a sealed
copy of this world, never from a grading file.

Prints ONE JSON object, the builder manifest (`bi_e2e_check.manifest` plus
`seed` and `virtual_time`), as its last stdout line.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HARNESS = REPO / "tasks" / "harness"
#: The probe's profile and plugs (`{slot: [plug, ...]}` as JSON), set by the
#: generator for the duration of one staging (tooltour.tour_world_env).
PROFILE_ENV = "ALKERA_TOOLTOUR_PROFILE"
PLUGS_ENV = "ALKERA_TOOLTOUR_PLUGS"
#: The seed every tour world is built from (the theme's seed.example.yaml).
TOUR_SEED = 11


def build(root: Path, grading: Path, seed: int, profile: str, plugs: dict) -> dict:
    for path in (HARNESS, REPO / "world", REPO / "gateway" / "src"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    import bi_e2e_check as bi
    import tool_tour
    import yaml

    if root.name != "world" or grading.resolve() != (root.parent / "grading").resolve():
        raise SystemExit(f"the tour world is built as <dir>/world beside <dir>/grading "
                         f"(bi_e2e_check.build_world); got --root {root} --grading {grading}")
    text = tool_tour.world_seed(profile, plugs)
    declared = int((yaml.safe_load(text) or {}).get("seed"))
    if declared != seed:
        raise SystemExit(f"the tour world is seed {declared}; asked for --seed {seed}")
    # bi_e2e_check.build_world reports through print(); keep stdout for the manifest
    with contextlib.redirect_stdout(io.StringIO()), \
            tool_tour.as_profile(profile, plugs, text):
        world = bi.build_world(root.parent)
    state = json.loads((root / "var" / "sched" / "state.json").read_text(encoding="utf-8"))
    world.update({"seed": seed, "virtual_time": state.get("vnow")})
    return world


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path)
    parser.add_argument("--grading", type=Path)
    parser.add_argument("--seed", type=int, default=TOUR_SEED)
    parser.add_argument("--prepare-only", action="store_true",
                        help="nothing to prepare: the theme caches nothing")
    args = parser.parse_args(argv)
    if args.prepare_only:
        return 0
    if args.root is None or args.grading is None:
        parser.error("--root and --grading are required")
    profile = os.environ.get(PROFILE_ENV)
    if not profile:
        raise SystemExit(f"{PROFILE_ENV} is not set: the generator names the probe's "
                         f"profile (harbor/alkera_harbor/tooltour.py)")
    plugs = json.loads(os.environ.get(PLUGS_ENV) or "{}")
    world = build(args.root, args.grading, args.seed, profile, plugs)
    print(json.dumps(world))
    return 0


if __name__ == "__main__":
    sys.exit(main())
