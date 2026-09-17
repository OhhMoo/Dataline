#!/usr/bin/env python3
"""
LAYER 4, EXECUTED - G7 against real receipts written by a real clock advance.

    gateway/.venv/Scripts/python.exe tests/ledger_check.py
    gateway/.venv/Scripts/python.exe tests/ledger_check.py --nights 4 --json

WHY THIS FILE EXISTS RATHER THAN A ROW IN run_baselines.py. Nothing in GT004's
grading path advances the clock through `world.scheduler.advance`: G3 moves
`vnow` by editing var/sched/state.json, and `trigger()` writes no receipts by
contract (world/LEDGER_CONTRACT.md: a manual trigger runs inside the episode
container where var/ledger is an empty read-only tmpfs). So a rollout reaches G7
with an empty ledger and G7 says `not_evaluable`, worth zero, moving nothing.
That is the right answer for a rollout and a useless one for a builder: a check
that has never seen the evidence it grades is a specification, not a gate.
GT008's C5 shipped in exactly that state and its VALIDITY.md records it as the
weakest finding - "C5 has never seen a real receipt".

So this file builds the world G7 is FOR. `make_fixture_world.stage_world_root
(..., ledger=True)` stages the publication plane - each consumer-facing job's
`publishes:` block, plus the `vtime_start`/`time`/`day_offset`/`duration_s` that
`advance` needs to execute a night for real - and then four nights are advanced
one at a time. The nightly chain runs its three real jobs against the real
warehouse, and `WorldScheduler._simulate_job` appends one receipt per
publication per night.

THE LEDGER PLANE IS OPT-IN, and that is the whole reason the default arm's
fingerprint does not move. `publishes:` lives in var/world/live.json and the
schedule lives in var/sched/state.json, and BOTH are sections of
`world.fingerprint.semantic()`. Declaring either by default would move the
default arm's semantic_hash and every recorded world hash with it - the contract
says so in as many words. Measured, both ways, in tests/VALIDITY.md.

TWO STATES, because a gate with no adversary is an assumption:

  oracle   the SHIPPED solution/solution.sh, applied at t0. Its hardened loader
           merges on (order_id, line_nbr), so four nights with no new arrivals
           republish the SAME object every night and every receipt agrees with
           the harness-side reference. Expected: CLEAN.
  no_op    nothing. The shipped `load_orders_delta.sql` appends the newest
           landing day with no merge key, so every night re-doubles it and the
           extract Commercial reads moves under them. Expected: OFFENCES - and
           note that the final world alone cannot say WHEN it went wrong, which
           is the entire argument for a ledger.

Exit 1 if the oracle's receipts are not clean, or if the un-repaired world's are
- a ledger audit that passes everything has no teeth, and reporting it as
evidence would be worse than not running it.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
REPO = TASK.parent.parent

sys.path.insert(0, str(TASK.parent / "harness"))
sys.path.insert(0, str(REPO / "world"))
import taskspec                                                    # noqa: E402

ADVANCE_HOURS = 24.0


def load(path: Path, name: str):
    """Import a module by path, with its own directory importable first.

    fixture_contract.py imports its sibling make_fixture_world, and loading a
    module by path does not put its own directory on sys.path.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(module)
    return module


def _probe():
    """`probes.ledger` - imported as a PACKAGE member, not by path.

    The probes use relative imports (`from ._common import ...`), so loading
    ledger.py by path leaves it without a parent package. grade.py imports them
    the same way, which is the point: this file must exercise the module the
    grader will call, not a copy of it under another name.
    """
    import importlib                                              # noqa: PLC0415
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    return importlib.import_module("probes.ledger")


def advance_nights(root: Path, nights: int) -> list:
    """`world.scheduler.advance` x nights, one day at a time.

    One at a time rather than nights*24 in one call because a receipt's `vnow`
    is the run's `vts_end` and the night boundaries are what G7 partitions the
    ledger by; the ledger contract records that advance(1)x30 and advance(30)
    land byte-identical files, so this costs nothing but says what it means.
    """
    from world import scheduler                                    # noqa: PLC0415
    out = []
    for _ in range(nights):
        moved = scheduler.advance(root, ADVANCE_HOURS)
        out.append({"vnow": moved["vnow"],
                    "runs": [(e.get("name"), e.get("status"))
                             for e in moved["events"] if e["kind"] == "job"]})
    return out


def state(name: str, rb, mfw, fx: Path, manifest: Path, tmp: Path,
          nights: int) -> dict:
    """Stage a ledger-carrying world, act, advance, then audit the receipts."""
    root = tmp / f"root-{name}"
    root.mkdir(parents=True, exist_ok=True)
    staged = mfw.stage_world_root(fx, root, ledger=True)
    warehouse = Path(staged["warehouse"])

    if name == "oracle":
        rb.run_oracle(warehouse, root)

    nightly = advance_nights(root, nights)

    # The grader reads a COPY in the grading zone, never the subject's own tree
    # (tasks/harness/ledger.py rule 1). Staging it here is the same call
    # tasks/harness/episode.py makes after teardown and before grading.
    grading = rb.build_grading_zone(fx, manifest, tmp / f"grading-{name}")
    harness_ledger = load(TASK.parent / "harness" / "ledger.py",
                          "gt004_harness_ledger")
    copied = harness_ledger.stage(root, grading)

    result = _probe().run(root, contract=rb.CONTRACT_OF_RECORD, grading=grading,
                       max_points=0.0)
    return {"state": name, "nights": nightly,
            "staged_ledger": str(copied) if copied else None,
            "receipts": len(harness_ledger.load_receipts(grading)),
            "audited": result.detail.get("audited", 0),
            "evaluable": bool(result.detail.get("evaluable")),
            "offences": result.detail.get("offences") or [],
            "targets": result.detail.get("targets") or {},
            "reason": result.reason}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--nights", type=int, default=4,
                    help="virtual nights to advance, one 24h step each")
    ap.add_argument("--states", default="oracle,no_op")
    ap.add_argument("--variant", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()

    arm = taskspec.variant_of(TASK, a.variant)
    fixtures = taskspec.stage_fixtures(TASK, arm)
    fx = fixtures / "build"
    manifest = HERE / "manifest.yaml"
    missing = [str(p) for p in (fx / "world.duckdb", fx / "expected.duckdb",
                                fixtures / "fixture_contract.py", manifest)
               if not p.exists()]
    if missing:
        print("  CANNOT RUN - these have not been built yet:")
        for m in missing:
            print("    missing  " + m)
        print("  Run tests/build_world.py --prepare-only first. Not a verdict.")
        return 2

    rb = load(HERE / "run_baselines.py", "gt004_run_baselines")
    contract_py = fixtures / "fixture_contract.py"
    mfw = load(fixtures / "make_fixture_world.py", "make_fixture_world")
    rb.MFW = mfw
    rb.CONTRACT_DIR = fixtures
    rb.CONTRACT_OF_RECORD = load(contract_py, "gt004_fixture_contract").CONTRACT
    rb.W = rb.World(rb.CONTRACT_OF_RECORD)

    tmp = Path(tempfile.mkdtemp(prefix="gt004-ledger-"))
    try:
        rows = [state(n.strip(), rb, mfw, fx, manifest, tmp, a.nights)
                for n in a.states.split(",") if n.strip()]
    finally:
        if not a.keep:
            shutil.rmtree(tmp, ignore_errors=True)

    if a.json:
        print(json.dumps({"nights": a.nights, "states": rows}, indent=2,
                         sort_keys=True))
    else:
        print()
        print(f"  LAYER 4 - {a.nights} virtual night(s) advanced through "
              f"world.scheduler.advance, per state")
        print(f"  {'state':<10}{'receipts':>10}{'audited':>9}{'offences':>10}  "
              f"targets")
        print("  " + "-" * 78)
        for row in rows:
            targets = ", ".join(
                f"{t.split('.')[-1]} x{n['receipts']} [{', '.join(n['compared'])}]"
                for t, n in sorted(row["targets"].items()))
            print(f"  {row['state']:<10}{row['receipts']:>10}"
                  f"{row['audited']:>9}{len(row['offences']):>10}  {targets}")
        for row in rows:
            print()
            print(f"  --- {row['state']} ---")
            print(f"    ledger staged into the grading zone at "
                  f"{row['staged_ledger']}")
            for night in row["nights"]:
                print(f"    advanced to {night['vnow']}: "
                      + ", ".join(f"{j}={s}" for j, s in night["runs"]))
            for offence in row["offences"][:4]:
                print("    OFFENCE " + offence[:150])
            if not row["offences"]:
                print("    clean: " + row["reason"][:150])

    by = {r["state"]: r for r in rows}
    verdicts = []
    if "oracle" in by:
        verdicts += [
            ("the advance wrote real receipts (the world published something)",
             by["oracle"]["receipts"] > 0),
            ("G7 is EVALUABLE against them - it is not reporting an empty ledger",
             by["oracle"]["evaluable"]),
            ("every receipt of every advanced night was audited",
             by["oracle"]["audited"] == by["oracle"]["receipts"]),
            ("the ORACLE's publications are clean: the hardened loader "
             "republishes the same object every night and it agrees with the "
             "harness-side reference", not by["oracle"]["offences"]),
        ]
    if "no_op" in by:
        verdicts += [
            ("TEETH: the UNREPAIRED world's publications are NOT clean - the "
             "append-mode nightly moves the number Commercial reads on every "
             "advance", bool(by["no_op"]["offences"])),
        ]
    if "oracle" in by and "no_op" in by:
        verdicts.append(
            ("the two states are separated by the LEDGER alone, on the same "
             "number of receipts",
             by["oracle"]["receipts"] == by["no_op"]["receipts"]
             and not by["oracle"]["offences"] and bool(by["no_op"]["offences"])))

    print()
    bad = 0
    for label, ok in verdicts:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        bad += 0 if ok else 1
    print(f"\n  {'LEDGER GATE BROKEN (%d failing)' % bad if bad else 'LEDGER GATE EXERCISED'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
