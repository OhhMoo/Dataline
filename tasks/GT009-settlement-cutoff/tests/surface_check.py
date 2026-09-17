#!/usr/bin/env python3
"""
GT009 surface gate - the half of task validity `run_baselines.py` cannot see.

    python surface_check.py            # build a world, probe it, run the gate
    python surface_check.py --keep     # keep the staged world

`run_baselines.py` proves the RUBRIC separates the oracle's decision from every
named alternative. It proves nothing about the world a model is actually
handed, because it drives the gateway in-process through `probes/gateway.py`.
The rollout harness does not: it stands a real gateway up over a throwaway
copy, probes every tool in the toolbelt, and QUARANTINES the run if a
`required_surfaces` entry is dead (`tasks/harness/surface_audit.gate_error`). A
task whose probes are wrong fails there, at K=5, after the quota is spent.

So this runs exactly that pipeline, with no model:

    build_world.py  ->  episode.probe_surfaces (real gateway, real copy)
                    ->  surface_audit.all_probes over the whole toolbelt
                    ->  surface_audit.audit  ->  gate_error

and additionally refuses a package where a `required_surfaces` name is not a
probe label, or where a declared probe is dead without being declared
`dead_by_design`.

GT009 declares `[harness].warehouse_schemas`, so the content-bearing gate
(tasks/harness/NATIVE_DRIVER.md) holds `sql_*` to a table with a row in it
rather than to `SELECT 1`. That is honest here and was not for GT008: this
world SHIPS a populated warehouse, because building it is not the task.

Reads tasks/harness/ and gateway/ and writes neither.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
FIXTURES = TASK / "fixtures"
REPO = TASK.parent.parent
HARNESS = REPO / "tasks" / "harness"

sys.path.insert(0, str(FIXTURES))
sys.path.insert(0, str(HARNESS))
from fixture_contract import CONTRACT                              # noqa: E402
import episode                                                     # noqa: E402
import surface_audit                                               # noqa: E402
import taskspec                                                    # noqa: E402

OK: list[str] = []
BAD: list[str] = []


def check(condition: bool, message: str) -> bool:
    (OK if condition else BAD).append(message)
    print(f"  {'ok  ' if condition else 'FAIL'}  {message}")
    return bool(condition)


def build(root: Path, grading: Path) -> dict:
    result = subprocess.run(
        [sys.executable, str(HERE / "build_world.py"), "--root", str(root),
         "--grading", str(grading), "--seed", str(CONTRACT["seed"])],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(REPO / "world"), "PYTHONUTF8": "1"})
    if result.returncode != 0:
        raise SystemExit(f"build_world.py failed:\n{result.stdout}{result.stderr}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    print("=" * 74)
    print("GT009-settlement-cutoff  -  surface gate (no model)")
    print("=" * 74)

    spec = taskspec.load(TASK)
    print("\n[1/4] the package declares what the harness needs")
    labels = {p.get("label") or p["tool"] for p in spec.surface_probes}
    required = list(getattr(spec, "required_surfaces", []) or [])
    check(bool(spec.world_builder), "a world builder")
    check(bool(spec.grade_argv), "a grade argv")
    check(bool(required), f"{len(required)} required surface(s)")
    unknown = sorted(set(required) - labels)
    check(not unknown, f"every required surface is a declared probe label "
                       f"{'' if not unknown else '(unknown: ' + str(unknown) + ')'}")

    scratch = Path(tempfile.mkdtemp(prefix="gt009-surface-"))
    try:
        print("\n[2/4] world")
        world = build(scratch / "root", scratch / "grading")
        for key in ("warehouse", "workspace", "scheduler", "dashboards",
                    "observability", "workflow_dir", "profiles_dir"):
            check(bool(world.get(key)), f"the manifest carries {key}")
        check(not world.get("warehouses"),
              "one warehouse: no key here needs a harness-side patch")

        print("\n[3/4] probe - a real gateway over a throwaway copy, "
              "every tool in the toolbelt")
        toolbelt = episode.gateway_tool_names()
        probes = surface_audit.all_probes(toolbelt, spec.surface_probes)
        probed = episode.probe_surfaces(spec, world, scratch, probes)
        surfaces = probed["surfaces"]
        live = sorted(k for k, v in surfaces.items() if v.get("live"))
        dead = sorted(k for k, v in surfaces.items() if not v.get("live"))
        print(f"  {len(toolbelt)} tool(s) in the toolbelt; {len(probes)} probe(s); "
              f"{len(live)} live, {len(dead)} dead")
        print(f"  dead: {dead}")

        print("\n[4/4] the gate the harness runs before the subject is handed "
              "the terminal")
        audit = surface_audit.audit(spec=spec, surfaces=surfaces, toolbelt=toolbelt)
        gate = surface_audit.gate_error(audit)
        check(gate is None, f"surface_audit.gate_error: "
                            f"{'clean' if gate is None else gate}")
        missing = [s for s in required
                   if not (surfaces.get(s) or {}).get("live")]
        check(not missing, f"every required surface is LIVE "
                           f"{'' if not missing else '(dead: ' + str(missing) + ')'}")
        check((surfaces.get("bi_render") or {}).get("live"),
              f"bi_render is live: {CONTRACT['bi']['dashboard']} renders what "
              f"the clearing bank reads")
        check((surfaces.get("kb") or {}).get("live"),
              "kb is live: the corrections runbook and the precedence page are "
              "the two surfaces the decision is settled from")
        declared_dead = {d.get("label") for d in (spec.dead_by_design or [])}
        undeclared = sorted(d for d in dead
                            if d in labels and d not in declared_dead)
        check(not undeclared, f"every DECLARED probe that is dead is declared "
                              f"dead_by_design {'' if not undeclared else undeclared}")
        if a.keep:
            print(f"\n  kept: {scratch}")
    finally:
        if not a.keep:
            shutil.rmtree(scratch, ignore_errors=True)

    print()
    if BAD:
        print(f"SURFACE GATE NOT CLEAN - {len(BAD)} problem(s)")
        return 1
    print(f"SURFACE GATE CLEAN - {len(OK)} check(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
