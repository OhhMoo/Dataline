#!/usr/bin/env python3
"""
PROOF that the GT004 probes both pass and fail correctly.

    python -m probes.selftest            # from tasks/GT004-doubling-window/tests
    python selftest.py --verbose

Every probe is exercised against at least two purpose-built worlds: one where it
must score full marks and one where it must not.  A probe that has never been
shown to FAIL is not a probe.

The adversaries are the ones the design names, not convenient ones:

  G2  a load "fixed" into a permanent skip ("only load when the target is
      empty").  Perfectly idempotent, leaves the window untouched, repairs
      nothing.  It must score ZERO, not full marks.
  G3  a repair that hardcoded "delete the window, reinsert the snapshot I saw".
      It passes G2 outright - which is the point: G3 is the only gate that sees
      it, and it is why the task is long-horizon.
  G4  a dashboard whose panel still reads the snapshot table nobody rebuilt.  The
      mart is right; the exposure is stale; that is a partial score, not a pass.
  G5  an agent that also rewrote a dashboard definition and dropped a staging
      table nobody asked it to touch - and, separately, an agent that did nothing
      at all, which must NOT collect the gate's points for free.
  G6  a test that passes on both snapshots, which proves nothing and scores half.

Exit code 0 only when every expectation holds.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):                      # allow `python selftest.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "probes"

from . import _worlds, collateral, exposure, idempotency, late_arrival, regression
from ._common import ProbeResult, open_world, warehouse_path, write_text

W0, W1 = _worlds.WINDOW


def repair(root: Path, *, with_test: str | None = "discriminating") -> None:
    """What a competent agent leaves behind: the window de-duplicated, the write
    mode changed from append to merge, and a regression test added."""
    con = open_world(warehouse_path(root))
    try:
        con.execute("""
            CREATE TABLE warehouse._fct_dedup AS
            SELECT event_id,
                   ANY_VALUE(revenue_dt)  AS revenue_dt,
                   ANY_VALUE(customer_id) AS customer_id,
                   ANY_VALUE(amount)      AS amount,
                   ANY_VALUE(load_ts)     AS load_ts,
                   ANY_VALUE(batch_id)    AS batch_id
              FROM warehouse.fct_revenue GROUP BY event_id""")
        con.execute("DROP TABLE warehouse.fct_revenue")
        con.execute("ALTER TABLE warehouse._fct_dedup RENAME TO fct_revenue")
    finally:
        con.close()
    write_text(root / "workspace" / "pipelines" / "fct_upsert.sql", _worlds.PIPE_MERGE)
    if with_test == "discriminating":
        write_text(root / "workspace" / "tests" / "test_no_doubling.py",
                   _worlds.TEST_DISCRIMINATING)
    elif with_test == "vacuous":
        write_text(root / "workspace" / "tests" / "test_no_doubling.py",
                   _worlds.TEST_VACUOUS)


def vandalise(root: Path) -> None:
    """Collateral the task never asked for: a rewritten dashboard DEFINITION and
    a dropped staging table."""
    p = root / "var" / "bi" / "dashboards" / "revenue_daily.yml"
    write_text(p, p.read_text(encoding="utf-8").replace("Daily revenue",
                                                        "Revenue (tidied)"))
    con = open_world(warehouse_path(root))
    try:
        con.execute("DROP TABLE staging.stg_revenue")
    finally:
        con.close()


# --------------------------------------------------------------------- cases
def cases(tmp: Path):
    """(gate, label, expectation, thunk) - expectation is (op, value)."""
    out = []

    def world(name, **kw):
        root = tmp / name
        nums = _worlds.build(root, **kw)
        return root, _worlds.contract(nums), nums

    # ---------------- G2 idempotency, max 6
    for label, pipe, expect in (
            ("merge (a genuine upsert)", "merge", ("==", 6.0)),
            ("PERMANENT SKIP (load only when empty)", "skip", ("==", 0.0)),
            ("append-only (the unrepaired defect)", "append", ("<", 6.0)),
            ("hardcoded window restate", "hardcoded", ("==", 6.0)),
    ):
        def mk(pipe=pipe, label=label):
            root, c, _ = world(f"g2-{pipe}", pipeline=pipe, doubled=False)
            return idempotency.run(root, contract=c, max_points=6.0)
        out.append(("G2", label, expect, mk))

    # ---------------- G3 late arrivals, max 5
    for label, pipe, expect in (
            ("merge (a genuine upsert)", "merge", ("==", 5.0)),
            ("HARDCODED window restate", "hardcoded", ("==", 0.0)),
            ("PERMANENT SKIP", "skip", ("==", 0.0)),
            ("append-only", "append", ("<", 5.0)),
    ):
        def mk(pipe=pipe):
            root, c, _ = world(f"g3-{pipe}", pipeline=pipe, doubled=False)
            return late_arrival.run(root, contract=c, max_points=5.0)
        out.append(("G3", label, expect, mk))

    # ---------------- G4 exposure, max 3
    for label, dash, expect in (
            ("live dashboard over the repaired mart", "live", ("==", 3.0)),
            ("STALE dashboard (reads the snapshot table)", "stale", ("<", 3.0)),
            ("BROKEN dashboard (panel table dropped)", "broken", ("==", 0.0)),
    ):
        def mk(dash=dash):
            root, c, _ = world(f"g4-{dash}", pipeline="merge", doubled=False,
                               dashboard=dash)
            return exposure.run(root, contract=c, max_points=3.0)
        out.append(("G4", label, expect, mk))

    # ---------------- G5 collateral, max 4
    def g5(kind):
        base = tmp / f"g5-{kind}-pristine"
        nums = _worlds.build(base, pipeline="append", doubled=True, test=None)
        c = _worlds.contract(nums)
        # the repair's tables only.  staging.stg_revenue was never asked to move.
        c["probes"]["collateral"]["may_move"]["warehouse"] = ["warehouse.fct_revenue"]
        agent = tmp / f"g5-{kind}-agent"
        shutil.copytree(base, agent)
        if kind != "idle":
            repair(agent)
        if kind == "damaged":
            vandalise(agent)
        return collateral.run(agent, baseline=base, contract=c, max_points=4.0)

    out.append(("G5", "repaired, nothing else touched", ("==", 4.0),
                lambda: g5("clean")))
    out.append(("G5", "ALSO rewrote a dashboard and dropped a staging table",
                ("==", 0.0), lambda: g5("damaged")))
    out.append(("G5", "DID NOTHING AT ALL (must not be free points)", ("==", 0.0),
                lambda: g5("idle")))

    # ---------------- G6 regression, max 3
    def g6(kind, route="pristine"):
        base = tmp / f"g6-{kind}-{route}-pre"
        nums = _worlds.build(base, pipeline="append", doubled=True, test=None)
        c = _worlds.contract(nums)
        agent = tmp / f"g6-{kind}-{route}-agent"
        shutil.copytree(base, agent)
        repair(agent, with_test=None if kind == "none" else kind)
        return regression.run(agent, baseline=base,
                              pre_repair_world=base if route == "pristine" else None,
                              contract=c, max_points=3.0)

    out.append(("G6", "test fails pre-repair, passes post (pristine route)",
                ("==", 3.0), lambda: g6("discriminating")))
    out.append(("G6", "same test, PRE-REPAIR REBUILT BY REINJECTION",
                ("==", 3.0), lambda: g6("discriminating", "reinjure")))
    out.append(("G6", "VACUOUS test (passes on both snapshots)", ("==", 1.5),
                lambda: g6("vacuous")))
    out.append(("G6", "no test left behind", ("==", 0.0), lambda: g6("none")))

    # ---------------- cross-cutting: the graded world must survive untouched,
    # and the score must not move when the grader runs twice on it
    # (episode.grade_twice compares the two runs and calls a drift a grader bug).
    def untouched_and_stable():
        root = tmp / "invariants"
        nums = _worlds.build(root, pipeline="merge", doubled=False)
        c = _worlds.contract(nums)
        pre = collateral.snapshot(root)
        first = [idempotency.run(root, contract=c, max_points=6.0).points,
                 late_arrival.run(root, contract=c, max_points=5.0).points,
                 exposure.run(root, contract=c, max_points=3.0).points,
                 regression.run(root, contract=c, max_points=3.0).points]
        mid = collateral.snapshot(root)
        second = [idempotency.run(root, contract=c, max_points=6.0).points,
                  late_arrival.run(root, contract=c, max_points=5.0).points,
                  exposure.run(root, contract=c, max_points=3.0).points,
                  regression.run(root, contract=c, max_points=3.0).points]
        post = collateral.snapshot(root)
        moved = [s for s in pre["sections"]
                 if pre["sections"][s] != post["sections"][s]]
        ok = not moved and pre == mid and first == second
        why = (f"ran G2,G3,G4,G6 twice against the graded world: sections that "
               f"moved = {moved or 'none'}; points run 1 = {first}, run 2 = "
               f"{second}")
        return ProbeResult("INV", 1.0 if ok else 0.0, 1.0, why, {})

    out.append(("INV", "no probe mutates the graded world; score is stable "
                       "across a repeat grading", ("==", 1.0), untouched_and_stable))
    return out


def check(expect, points) -> bool:
    op, v = expect
    return {"==": points == v, "<": points < v, ">": points > v}[op]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="probes.selftest")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--keep", type=Path, help="build the worlds here and keep them")
    a = ap.parse_args(argv)

    tmp = a.keep or Path(tempfile.mkdtemp(prefix="gt004-selftest-"))
    tmp.mkdir(parents=True, exist_ok=True)
    bad = 0
    try:
        for gate, label, expect, thunk in cases(tmp):
            r = thunk()
            ok = check(expect, r.points)
            bad += 0 if ok else 1
            print(f"[{'ok ' if ok else 'BAD'}] {gate} {r.points:>4}/{r.max}  "
                  f"(expect {expect[0]} {expect[1]})  {label}")
            if a.verbose or not ok:
                print(f"        {r.reason}")
    finally:
        if a.keep is None:
            shutil.rmtree(tmp, ignore_errors=True)
    print(("ALL EXPECTATIONS HELD" if not bad else
           f"{bad} EXPECTATION(S) VIOLATED"))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
