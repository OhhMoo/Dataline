#!/usr/bin/env python3
"""
Does the world `build_world.py` hands the harness grade like the world the
baseline suite validates?

The task-validity gate (`run_baselines.py`) stages its own world: the cached
fixture copied to `var/warehouse/warehouse.duckdb` with `fixtures/workspace`
beside it, the fixture directory doubling as the grading zone. That staging is
the ONLY GT003 world any baseline has ever been scored against. `build_world.py`
stages a different-looking one - a scheduler, a git repo, a dashboard, a
separate grading zone - and the harness grades THAT. If the two disagree by so
much as one check, every recorded GT003 baseline is a statement about a world no
rollout is run in, which is the shape of F622: GT001 printed TASK VALID for weeks
while its oracle aborted on its first statement at the deployed path.

So this runs each baseline agent twice, once against each staging, grades both
with identical argv, and diffs every check. Zero differences is the claim
`tests/VALIDITY.md` records; this is the command that produces it.

    python build_world_equivalence.py [--json <path>]

`reference_copy` is deliberately absent: it returns `(artifact, reachable)`
rather than an artifact, and what it measures is the TOPOLOGY of the grading
zone - which differs between the two stagings on purpose, because the fixture
directory doubling as a grading zone is exactly the reachability
`run_baselines.py` prints a NOTE about. Comparing it here would compare two
things that are meant to differ.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import build_world                                  # noqa: E402
import run_baselines as rb                          # noqa: E402

AGENTS = ["oracle", "no_op", "full_refresh", "rebuild_only", "checklist",
          "checklist_joined", "schema_vandal"]
BUILD = HERE / "fixtures" / "build"


def stage_baselines(tmp: Path):
    """Exactly `run_baselines.main`: the fixture dir doubles as the grading zone."""
    shutil.copy(HERE / "manifest.yaml", BUILD / "manifest.yaml")
    warehouse, workspace = rb.fresh(BUILD, tmp)
    return warehouse, workspace, BUILD


def stage_builder(tmp: Path):
    """Exactly what the harness gets: `build_world.build`'s own json manifest."""
    world = build_world.build(tmp / "world", tmp / "grading")
    return (Path(world["warehouse"]), Path(world["workspace"]),
            Path(world["grading"]))


def run(agent: str, stage) -> dict:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        warehouse, workspace, grading = stage(tmp)
        artifact = rb.AGENTS[agent](warehouse, workspace, grading)
        return rb.grade(warehouse, grading, artifact, tmp, root=workspace)


def compare(a: dict, b: dict) -> list[str]:
    ca = {c["id"]: c for c in a.get("checks", [])}
    cb = {c["id"]: c for c in b.get("checks", [])}
    diffs = []
    if set(ca) != set(cb):
        diffs.append(f"check ids differ: {sorted(set(ca) ^ set(cb))}")
    for cid in sorted(set(ca) & set(cb)):
        if ca[cid]["points"] != cb[cid]["points"]:
            diffs.append(f"{cid} points: {ca[cid]['points']} vs {cb[cid]['points']}")
        if ca[cid].get("passed") != cb[cid].get("passed"):
            diffs.append(f"{cid} passed: {ca[cid].get('passed')} vs "
                         f"{cb[cid].get('passed')}")
    if a.get("total") != b.get("total"):
        diffs.append(f"total: {a.get('total')} vs {b.get('total')}")
    return diffs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None,
                    help="write the per-check results here as well")
    args = ap.parse_args()

    rb.prime_good_artifact(BUILD)
    rows, bad = [], 0
    print(f"\n  {'baseline':<18}{'run_baselines':>14}{'build_world':>14}"
          f"{'differences':>14}")
    print("  " + "-" * 60)
    for agent in AGENTS:
        a = run(agent, stage_baselines)
        b = run(agent, stage_builder)
        diffs = compare(a, b)
        bad += len(diffs)
        rows.append({"agent": agent, "run_baselines": a, "build_world": b,
                     "diffs": diffs})
        print(f"  {agent:<18}{a['total']:>14}{b['total']:>14}{len(diffs):>14}")

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2), encoding="utf-8",
                                   newline="")
    print()
    if bad:
        for row in rows:
            for d in row["diffs"]:
                print(f"  [DIFF] {row['agent']}: {d}")
        print(f"\n  NOT EQUIVALENT ({bad} difference(s)). The harness would be "
              f"grading a world no baseline covers.")
        return 1
    print(f"  EQUIVALENT: {len(AGENTS)} baselines, every check, zero differences.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
