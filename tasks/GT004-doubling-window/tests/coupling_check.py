#!/usr/bin/env python3
"""
LAYER 3 REGRESSION - GT004's coupling verdicts, re-measured against the record.

    gateway/.venv/Scripts/python.exe tests/coupling_check.py          # ~3 min
    gateway/.venv/Scripts/python.exe tests/coupling_check.py --clock  # ~2 min
    gateway/.venv/Scripts/python.exe tests/run_baselines.py --coupling

WHAT IT PROTECTS. The coupling rule (tasks/harness/COUPLING.md, and
implementation_structure/TASK_DESIGN_2026-09-15.md layer 3) says two stressors
compound only if they share state, and that this is MECHANICALLY CHECKABLE:
perturb defect A, and if the correct repair of B changes, they are coupled.
GT004's twelve ordered pairs were measured on 2026-09-16 - two accepted, ten
rejected - and that measurement is the evidence that this task is one composed
task rather than two easy ones in a trench coat. GT006c/GT006d cost twenty
episodes of quota to learn what the filter would have said for free.

A measurement is evidence about the world it was taken in. GT004's fixture is
still edited: a builder who changes `revenue_sql` so the stale-layout repair
stops reading `fct_orders`, or hardens `load_orders_delta.sql` in the shipped
workspace, would DECOUPLE the defects and nothing else in this package would
notice - every baseline would still score exactly what it scores, and the task
would quietly become the thing the filter exists to reject. This file is the
thing that notices.

HOW. It re-runs `coupling.measured_coupling` over exactly the ordered pairs the
record carries, with the same adapter and the same registry, and compares
`coupled` and `strength` pair by pair. Any change is a failure, in EITHER
direction: a pair that starts coupling is as much a change to what this task
measures as one that stops.

It also re-runs the clause self-check first, and that is not politeness. The
self-check asserts that each stressor's clauses pass on a clean world, fail on a
planted one and pass again after the repair. Vacuous clauses return "uncoupled"
for every pair and look exactly like a working filter, so a green matrix on top
of a red self-check is worth nothing.

IT IS OPT-IN because it costs about three minutes - five 181-day world builds
per ordered pair - and run_baselines.py is run far more often than the fixture
is edited. `--coupling` turns it on there.

NOTHING HERE IS RE-DERIVED. The rule, the adapter and the criteria are
tasks/harness/coupling.py's; this file supplies GT004's record and the
comparison.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
REPO = TASK.parent.parent
MEASURED = REPO / "tasks" / "stressors" / "measured"

sys.path.insert(0, str(TASK.parent / "harness"))


def record_path(clock: bool) -> Path:
    return MEASURED / (TASK.name + ("-coupling-clock.json" if clock
                                    else "-coupling.json"))


def load_record(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def key(row: dict) -> tuple:
    return (row["a"], row["b"])


def verdict(row: dict) -> tuple:
    """(coupled, strength) - what this file asserts has not moved.

    `strength` comes from the evidence block and is `none` when the pair is
    uncoupled. Row counts and key counts deliberately are NOT compared: they are
    world-size facts that a legitimate fixture change may move, while the
    verdict is the thing the composition rests on.
    """
    return (bool(row.get("coupled")),
            str((row.get("evidence") or {}).get("strength") or "none"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--clock", action="store_true",
                    help="re-measure the CLOCKED record instead: worlds are "
                         "advanced over the next planted event before B's "
                         "clauses are evaluated")
    ap.add_argument("--record", default=None,
                    help="override the measured JSON to compare against")
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--skip-selfcheck", action="store_true",
                    help="skip the clause self-check. Only for a re-run that "
                         "has just seen it green; a green matrix on top of a "
                         "red self-check is worth nothing.")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    path = Path(a.record) if a.record else record_path(a.clock)
    if not path.is_file():
        print(f"  CANNOT RUN - no measured record at {path}. Produce one with\n"
              f"    coupling.py --task {TASK.name}"
              f"{' --clock' if a.clock else ''} --pairs all --out {MEASURED}\n"
              f"  This is a 'not yet', not a verdict.")
        return 2

    import coupling                                               # noqa: PLC0415
    from coupling_adapters import get_adapter                     # noqa: PLC0415

    record = load_record(path)
    adapter = get_adapter(TASK, clock=a.clock)
    if adapter is None:
        print(f"  CANNOT RUN - tasks/harness/coupling_adapters.py has no adapter "
              f"for {TASK.name}{' with a clock' if a.clock else ''}.")
        return 2
    registry = coupling.load_registry()
    workdir = Path(a.workdir) if a.workdir else Path(
        tempfile.mkdtemp(prefix="gt004-coupling-"))

    problems, checks = [], []
    if not a.skip_selfcheck:
        for row in coupling.selfcheck(TASK, workdir, adapter=adapter,
                                      clock=a.clock):
            checks.append(row)
            if not row["ok"]:
                problems.append(
                    f"self-check RED for {row['stressor']}: clean="
                    f"{row['clean_world_passes']} planted_fails="
                    f"{row['planted_world_fails']} repaired_passes="
                    f"{row['repaired_world_passes']}. Clauses with no teeth "
                    f"return 'uncoupled' for every pair and look like a working "
                    f"filter.")

    rows = []
    for was in record.get("pairs") or []:
        now = coupling.measured_coupling(TASK, was["a"], was["b"], workdir,
                                         registry=registry, adapter=adapter,
                                         clock=a.clock)
        before, after = verdict(was), verdict(now)
        rows.append({"a": was["a"], "b": was["b"],
                     "recorded": before, "measured": after,
                     "seconds": now.get("seconds"),
                     "clauses_failing":
                         (now.get("evidence") or {}).get(
                             "b_clauses_failing_with_a_present") or []})
        if before != after:
            problems.append(
                f"{was['a']} -> {was['b']}: recorded coupled={before[0]} "
                f"strength={before[1]}, now coupled={after[0]} "
                f"strength={after[1]}. A fixture edit changed what this "
                f"composition measures.")

    if a.json:
        print(json.dumps({"record": str(path), "clock": bool(a.clock),
                          "selfcheck": checks, "pairs": rows,
                          "problems": problems}, indent=2, sort_keys=True,
                         default=str))
    else:
        print()
        print(f"  LAYER 3 - re-measured against {path.name}"
              f"{' (clocked)' if a.clock else ''}")
        print(f"  {'a (perturbed)':<34}{'b (repaired)':<34}"
              f"{'recorded':>12}{'measured':>12}{'s':>7}")
        print("  " + "-" * 99)
        for row in rows:
            show = lambda v: f"{v[0]}/{v[1]}"                     # noqa: E731
            flag = "" if row["recorded"] == row["measured"] else "  <-- MOVED"
            print(f"  {row['a']:<34}{row['b']:<34}"
                  f"{show(row['recorded']):>12}{show(row['measured']):>12}"
                  f"{row['seconds']:>7}{flag}")
        coupled = [r for r in rows if r["measured"][0]]
        print(f"\n  {len(coupled)} accepted, {len(rows) - len(coupled)} rejected"
              f" - and GT004 is a ONE-DIRECTIONAL gate, which is what makes it a"
              f" composition")
        for row in coupled:
            print(f"    {row['a']} -> {row['b']}: B's clause(s) "
                  f"{row['clauses_failing'] or ['-']} fail once A is present")
        for problem in problems:
            print("\n  " + problem)
        print(f"\n  {'COUPLING CHANGED (%d)' % len(problems) if problems else 'COUPLING UNCHANGED'}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
