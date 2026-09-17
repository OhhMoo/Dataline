#!/usr/bin/env python3
"""
EQUIVALENCE RUN: the lifted contracts against GT004's REAL fixture world.

    gateway/.venv/Scripts/python.exe tasks/contracts/equivalence_gt004.py

A library lifted out of a task is worth nothing until it agrees with the task it
was lifted from, on that task's own world.  So this script:

  1. stages GT004's fixture the way tests/run_baselines.py stages it - through
     make_fixture_world.stage_world_root, which CONTRACT["layout"] names as the
     way a world root is laid out.  A suite that stages the world differently
     from the harness is not testing the world the harness ships: staging it by
     hand once omitted var/bi and var/sched and silently zeroed two of six gates
     for every baseline INCLUDING the oracle.
  2. produces two states: the world as shipped (no_op) and the world after the
     SHIPPED solution/solution.sh (oracle).
  3. grades each with GT004's OWN tests/grade.py, and separately evaluates
     tasks/contracts' exactly_once and liveness against the same world root.
  4. prints the comparison.

It EDITS NOTHING under tasks/GT004-doubling-window/.  Everything it writes lands
in a temporary directory.

MAPPING (what is being claimed equal):

    GT004 G1b  == exactly_once.no_duplicate_keys
    GT004 G3   == exactly_once.{late_incorporated_once, no_re_doubling,
                               stable_on_replay}
    GT004 G2   == liveness.{ingests, no_collateral, idempotent}

G1b and G3 both land in exactly_once because they are two stressors on ONE
invariant: a key that is in the target twice, and a redelivery that lands twice.
GT004 scores them as separate rubric lines; the contract scores them as one
invariant with four clauses.  The claim under test is that the VERDICTS agree,
not that the points do.
"""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GT004 = REPO / "tasks" / "GT004-doubling-window"
sys.path.insert(0, str(REPO))

from tasks.contracts import evaluate                                # noqa: E402
from tasks.contracts._types import Context                          # noqa: E402


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(mod)
    return mod


NO_OP_ARTIFACT = {"primary_cause": "", "damage_window": [],
                  "supporting_evidence": [], "repair_strategy": "",
                  "remaining_risks": [], "additional_causes": []}


def contract_params(contract: dict, manifest: dict) -> tuple:
    """GT004's own probe configuration, re-expressed as contract params.

    Nothing is invented here: every key is read from
    fixtures/fixture_contract.py::CONTRACT, overlaid by tests/manifest.yaml the
    same way probes/_common.probe_config overlays it.
    """
    probes = contract["probes"]
    world = probes["world"]

    def overlay(key, manifest_key):
        base = copy.deepcopy(probes.get(key) or {})
        over = (manifest.get(manifest_key) or {})
        for k, v in over.items():
            if isinstance(v, dict) and isinstance(base.get(k), dict):
                base[k] = {**base[k], **v}
            else:
                base[k] = v
        return base

    shared = {
        "table": world["fact_table"],
        "business_key": world["business_key"],
        "date_column": world["date_column"],
        "audit_columns": world.get("audit_columns") or [],
        "window": world["window"],
        "pipeline": probes["pipeline"],
    }
    late_cfg = overlay("late_arrival", "late_arrival_probe")
    idem_cfg = overlay("idempotency", "idempotency_probe")

    payload = dict(late_cfg["payload"])
    exactly_once = dict(shared)
    exactly_once["late"] = {
        "predicate": payload.pop("predicate"),
        "advance_to": late_cfg.get("advance_to"),
        "payload": payload,
    }
    liveness = dict(shared)
    liveness["plant"] = idem_cfg["plant"]
    return exactly_once, liveness


def run_state(rb, fx, manifest_path, grader, tmp: Path, name: str,
              params: tuple) -> dict:
    """Stage a fresh world, run GT004's own baseline agent on it, then grade the
    result BOTH ways.  The agents are GT004's, unmodified - using anything else
    would compare the contracts against a state no baseline covers."""
    warehouse, root = rb.stage_root(fx, tmp, f"root-{name}")
    grading = rb.build_grading_zone(fx, manifest_path, tmp / f"grading-{name}")
    artifact = rb.AGENTS[name](warehouse, root, fx)
    score = rb.grade(grader, warehouse, grading, artifact, tmp, root, False)
    checks = {c["id"]: c for c in score["checks"]}

    exactly_once_params, liveness_params = params
    before = {"duplicate_keys_before":
              contract_of(rb)["defect_1"]["duplicated_business_keys"]}
    out = {"gt004": checks, "contracts": {}}
    for cname, cparams in (("exactly_once", exactly_once_params),
                           ("liveness", liveness_params)):
        ctx = Context(world_root=root, before=before, params=cparams)
        try:
            out["contracts"][cname] = evaluate(cname, ctx)
        finally:
            ctx.close()
    return out


_CONTRACT = {}


def contract_of(rb):
    return _CONTRACT["c"]


MAPPING = [
    ("G1b", ["exactly_once.no_duplicate_keys"]),
    ("G3", ["exactly_once.late_incorporated_once", "exactly_once.no_re_doubling",
            "exactly_once.stable_on_replay"]),
    ("G2", ["liveness.ingests", "liveness.no_collateral", "liveness.idempotent"]),
]


def report(states: dict) -> int:
    disagreements = 0
    width = 74
    print()
    print("EQUIVALENCE - GT004's own grade.py vs tasks/contracts, same world")
    print("=" * width)
    for state, data in states.items():
        print()
        print(f"[{state}]")
        print(f"  {'check':<5} {'GT004 verdict':<16} {'contract verdict':<18} agree")
        print("  " + "-" * (width - 2))
        for cid, clause_ids in MAPPING:
            check = data["gt004"].get(cid)
            if check is None:
                print(f"  {cid:<5} (not emitted)")
                continue
            gt_pass = bool(check["passed"])
            clauses = {}
            for result in data["contracts"].values():
                clauses.update({c.id: c for c in result.clauses})
            got = [clauses.get(x) for x in clause_ids]
            if any(c is None for c in got):
                verdict = "MISSING CLAUSE"
                agree = False
            else:
                verdict = all(c.passed for c in got)
                agree = verdict == gt_pass
            disagreements += 0 if agree else 1
            shown = "{} ({}/{})".format(gt_pass, check["points"], check["max"])
            print(f"  {cid:<5} {shown:<16} {str(verdict):<18} "
                  f"{'yes' if agree else 'NO'}")
        for cname, result in data["contracts"].items():
            print(f"    {cname}: {result.points}/{result.max}")
            for c in result.clauses:
                print(f"      [{'PASS' if c.passed else 'FAIL'}] {c.id}"
                      f"{' (fatal)' if c.fatal else ''}: {c.detail[:150]}")
    print()
    print(f"disagreements: {disagreements}")
    return disagreements


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true",
                    help="leave the staged worlds on disk for inspection")
    ap.add_argument("--states", default="oracle,no_op,permanent_skip,"
                                        "repair_before_late_arrival,"
                                        "defect_one_only,full_refresh")
    a = ap.parse_args()

    fx = GT004 / "fixtures" / "build"
    contract_py = GT004 / "fixtures" / "fixture_contract.py"
    manifest_path = GT004 / "tests" / "manifest.yaml"
    grader = GT004 / "tests" / "grade.py"
    for p in (fx / "world.duckdb", fx / "expected.duckdb", contract_py,
              manifest_path, grader):
        if not p.exists():
            print(f"CANNOT RUN - {p} has not been built. "
                  f"Run tests/build_world.py --prepare-only first.")
            return 2

    rb = load(GT004 / "tests" / "run_baselines.py", "gt004_run_baselines")
    fixture_mod = rb.load_module(contract_py, "gt004_fixture_contract")
    contract = fixture_mod.CONTRACT
    _CONTRACT["c"] = contract
    rb.MFW = rb.load_module(contract_py.parent / "make_fixture_world.py",
                            "make_fixture_world")
    rb.W = rb.World(contract)
    rb.CONTRACT_DIR = contract_py.parent

    import yaml
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    params = contract_params(contract, manifest)

    tmp = Path(tempfile.mkdtemp(prefix="contracts-equiv-gt004-"))
    rb.SHIM = rb.write_shim(contract_py, tmp)
    try:
        # the oracle's own report, primed once, is the artifact every other
        # baseline submits - so a baseline is only ever penalised for what it
        # DID and never for what it wrote.  Same rule run_baselines.main uses.
        warehouse, root = rb.stage_root(fx, tmp, "root-prime")
        rb._GOOD = rb.run_oracle(warehouse, root)

        states = {}
        for name in a.states.split(","):
            name = name.strip()
            if not name:
                continue
            if name not in rb.AGENTS:
                print(f"  unknown baseline {name!r}; have {sorted(rb.AGENTS)}")
                return 2
            print(f"  staging + grading [{name}] ...", file=sys.stderr)
            states[name] = run_state(rb, fx, manifest_path, grader, tmp, name,
                                     params)
        bad = report(states)
        print(json.dumps({"tmp": str(tmp)} if a.keep else {}, indent=1))
        return 0 if bad == 0 else 1
    finally:
        if not a.keep:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
