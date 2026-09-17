#!/usr/bin/env python3
"""Layer 3 of the contract-maintenance generator: the mechanical coupling filter.

THE RULE (implementation_structure/TASK_DESIGN_2026-09-15.md, "Framework
proposal", layer 3):

    Two stressors compound only if they share state -- same table, checkpoint,
    repair window, or metric. Mechanically checkable: perturb defect A; if the
    correct repair of B changes, they are coupled. The generator auto-rejects
    disconnected compositions (two easy tasks in a trench coat) before any model
    token is spent.

Why this filter has to exist at all: agent.md 8.10 measured GT001/GT005/GT006/
GT007 at 1.0 each. Stacking them multiplies ones. GT006c/GT006d then ran the
experiment directly -- an easy defect A stacked on top of defect B moved the
pass rate by zero, 28/28 x5 in both context conditions. That is what an
uncoupled composition looks like from the outside, and it costs 10 episodes of
quota to find out. This module is meant to find it out for free.

TWO ENTRY POINTS
----------------
`predeclared_overlap(a, b, registry)` -- the cheap pre-filter. Pure set
intersection over `tasks/stressors/registry.yaml`: shared tables, checkpoints,
window, metrics, contracts. No world is built. It is NECESSARY, not sufficient:
sharing a table is exactly the condition under which coupling is possible, and
says nothing about whether it happens. Use it to skip the expensive test on
pairs that cannot possibly couple, never to accept a pair.

`measured_coupling(task_dir, a, b, workdir, clock=False)` -- the real test,
executed:

    1. build base, base+{a}, base+{b}, base+{a,b}
    2. apply B's oracle repair to base+{b} and to base+{a,b}
    2a. WITH A CLOCK: advance each staged world to its next planted event
    3. evaluate B's contract clauses on both
    4. footprint(A), MEASURED not declared: every (table, key) that differs
       between base and base+{a}, plus every (table, key) that A's own oracle
       repair of base+{a} moves. This is "A's own footprint" -- the state A is
       entitled to change.
    5. VERDICT, coupled if either:
         (i)  every B clause passes on base+{b} and at least one fails on
              base+{a,b}                         -- B's repair stops working
         (ii) the post-repair canonical state differs between the two worlds at
              some (table, key) OUTSIDE footprint(A)
                                                 -- A reached state it never
                                                    touched, through B's repair
    6. plus a diagnostic that separates the two flavours of (i):
       `repair_order_dependent` -- does applying A's repair BEFORE B's rescue
       the failing clauses? If yes, B's correct repair genuinely acquired a
       prerequisite, which is the compounding the framework is after. If the
       clauses fail either way, B's repair is insufficient for a reason the
       pair does not explain, and the pair should be read as a clause bug
       before it is read as coupling.

Coupling is DIRECTED. (a -> b) asks whether A perturbs B's repair; (b -> a) is a
separate measurement and routinely comes out differently. A composition is worth
building if either direction couples; reporting only the unordered verdict hides
which defect is the gate.

THE CLOCK (`clock=True`), and why this module needed one. COUPLING.md's
limitation 2 said it plainly: "No clock -- the weakest thing here. Worlds are
compared at t0; `late_arrival` is staged as rows already in the landing stream,
a static stand-in for an event defined by arriving AFTER a repair." A stressor
whose whole nature is WHEN it happens cannot be measured by diffing two
warehouses that have never been run. With `clock=True` the adapter stages a
WORLD ROOT rather than a bare duckdb file, the repair is applied at t0 exactly
as an agent would apply it, and then `world.scheduler.advance` moves the world
over the next planted event, executing the world's own jobs for real. B's
clauses are evaluated after that.

The verdict criteria are unchanged; what changes is the STATE they are computed
on. A pair that is uncoupled at t0 and coupled after one advance is the finding
the filter could not previously express, and it is not a different rule - it is
the same rule asked at a moment when both stressors exist.

The clocked path needs three more methods on the adapter (`clocked`, `world`,
`warehouse`, `advance`); an adapter without them returns `executed: False` with
`why_not_executed` naming the gap, exactly as a missing adapter does.

STRENGTH, recorded in the evidence rather than collapsed into the verdict:
    strong   criterion (ii) fired -- A propagated into a table it does not own
    ordered  only (i) fired and A's repair rescues it -- a real prerequisite
    weak     only (i) fired and A's repair does not rescue it
    none     neither fired

NEW FILE. Edits nothing under tasks/GT*/, world/, gateway/, or the rest of
tasks/harness/. Runs no model.

CLI
    python tasks/harness/coupling.py --task GT004-doubling-window \
        --pairs all --workdir %TEMP%/coupling --out tasks/stressors/measured
    python tasks/harness/coupling.py --task GT009-settlement-cutoff \
        --pairs all --clock --out tasks/stressors/measured
"""
from __future__ import annotations

import argparse
import itertools
import json
import shutil
import sys
import time
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO / "tasks" / "stressors" / "registry.yaml"

# The substrate keys: state through which one stressor can actually reach
# another. `window` is deliberately NOT one of them. Two stressors in the same
# world almost always share calendar dates -- GT004's whole history is 181 days
# and every stressor lives inside it -- so a date-range intersection accepts
# nearly every pair and carries no information. A window only matters as a
# refinement of a shared TABLE, and is reported that way.
SUBSTRATE_KEYS = ("tables", "checkpoints", "metrics")


# ===========================================================================
# registry + the cheap pre-filter
# ===========================================================================
def load_registry(path: Path = REGISTRY_PATH) -> dict:
    """tasks/stressors/registry.yaml -> {name: entry}."""
    import yaml
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return {s["name"]: s for s in doc.get("stressors", [])}


def _state(entry: dict, key: str) -> set:
    """The declared state of one kind.

    `tables` folds in `derived_tables`. It has to: the pre-filter is only
    allowed to REJECT, so it must not miss a reachable relation. GT004's
    doubling writes warehouse.fct_orders and its stale-layout defect writes
    warehouse.fct_revenue -- disjoint direct writes, and measurably coupled,
    because both are carried into warehouse.kpi_daily. A direct-writes-only
    pre-filter would have rejected the one pair in this repo that couples.
    """
    vals = set((entry.get("state") or {}).get(key) or [])
    if key == "tables":
        vals |= set(entry.get("derived_tables") or [])
    return vals


def _window_overlap(a: dict, b: dict) -> list:
    """`window` is a list of [start, end] ISO date ranges. Intersect them."""
    out = []
    for wa in (a.get("state") or {}).get("window") or []:
        for wb in (b.get("state") or {}).get("window") or []:
            lo, hi = max(str(wa[0]), str(wb[0])), min(str(wa[1]), str(wb[1]))
            if lo <= hi:
                out.append([lo, hi])
    return out


def predeclared_overlap(a: str, b: str, registry: dict) -> dict:
    """Cheap pre-filter on declared shared state. NECESSARY, NOT SUFFICIENT.

    `overlap` True means the pair *may* couple and is worth measuring. False is
    a safe reject: two stressors with no shared table, checkpoint, window or
    metric cannot change each other's repair, because there is no state through
    which one could reach the other.
    """
    ea, eb = registry.get(a), registry.get(b)
    if ea is None or eb is None:
        missing = [n for n, e in ((a, ea), (b, eb)) if e is None]
        return {"a": a, "b": b, "overlap": None, "shared": {},
                "error": "not in registry: " + ", ".join(missing)}
    shared = {k: sorted(_state(ea, k) & _state(eb, k)) for k in SUBSTRATE_KEYS}
    overlap = any(shared[k] for k in SUBSTRATE_KEYS)
    shared["window"] = _window_overlap(ea, eb) if overlap else []
    shared_contracts = sorted(set(ea.get("stresses") or [])
                              & set(eb.get("stresses") or []))

    # A stressor that writes no relation at all -- GT005's .dbtignore, GT007's
    # widened offset -- acts on whether another stressor is OBSERVED or
    # SCHEDULED, not on rows. No intersection of data state can see that, so an
    # empty intersection involving one is `undecidable`, never a reject.
    # GT005 is the worked example: the ignored assertion is precisely why nobody
    # saw the inner-join row loss, and the substrate test calls them disjoint.
    control_plane = [n for n, e in ((a, ea), (b, eb)) if not _state(e, "tables")]
    undecidable = bool(control_plane) and not overlap
    return {
        "a": a, "b": b,
        "same_task": ea.get("task") == eb.get("task"),
        "overlap": None if undecidable else overlap,
        "shared": shared,
        "shared_contracts": shared_contracts,
        "kinds": [ea.get("kind"), eb.get("kind")],
        "control_plane_stressors": control_plane,
        "verdict": (
            "undecidable -- %s writes no relation; the substrate test cannot "
            "see observation or scheduling coupling" % ", ".join(control_plane)
            if undecidable else
            "may couple -- measure it" if overlap else
            "disconnected -- reject without measuring"),
    }


# ===========================================================================
# canonical state and measured footprints
# ===========================================================================
def _attach(paths: dict):
    con = duckdb.connect(":memory:", config={"threads": 1})
    for alias, p in paths.items():
        con.execute("ATTACH '" + Path(p).as_posix() + "' AS " + alias
                    + " (READ_ONLY)")
    return con


def _exists(con, alias: str, table: str) -> bool:
    schema, name = table.split(".", 1)
    return bool(con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_catalog = ?"
        " AND table_schema = ? AND table_name = ?",
        [alias, schema, name]).fetchone()[0])


def differing_keys(db_x: Path, db_y: Path, tables: dict, limit: int = 2_000_000):
    """{table: set(key tuples)} whose row MULTISET differs between two worlds.

    Symmetric EXCEPT ALL in both directions, so a duplicated row counts as a
    difference even when the distinct rows agree -- which is the whole point
    when one of the stressors is a doubling.
    """
    con = _attach({"x": db_x, "y": db_y})
    out, notes = {}, {}
    try:
        for table, (keycols, cols) in tables.items():
            hx, hy = _exists(con, "x", table), _exists(con, "y", table)
            if not (hx and hy):
                notes[table] = "present in x=%s y=%s" % (hx, hy)
                out[table] = {("<table-missing>",)}
                continue
            sel = ", ".join(cols)
            key = ", ".join(keycols)
            rows = con.execute(
                "SELECT DISTINCT " + key + " FROM ("
                "  (SELECT " + sel + " FROM x." + table +
                "   EXCEPT ALL SELECT " + sel + " FROM y." + table + ")"
                "  UNION ALL"
                "  (SELECT " + sel + " FROM y." + table +
                "   EXCEPT ALL SELECT " + sel + " FROM x." + table + ")"
                ") LIMIT " + str(limit)).fetchall()
            out[table] = {tuple(str(v) for v in r) for r in rows}
    finally:
        con.close()
    return out, notes


def _flatten(d: dict) -> set:
    return {(t,) + k for t, keys in d.items() for k in keys}


# ===========================================================================
# the measured test
# ===========================================================================
def _stage(adapter, subset, dest, *, repair=None, clock: bool = False):
    """One world, optionally repaired at t0 and optionally advanced.

    Returns the warehouse path. The ORDER is the whole point of the clocked
    mode: the world is built, the agent's repair lands at t0, and only then
    does the clock move - so an event that arrives during the advance arrives
    AFTER the repair, which is what an event stressor actually is.
    """
    if not clock:
        db = adapter.build(frozenset(subset), Path(dest))
        if repair:
            adapter.repair(repair, db)
        return db
    root = adapter.world(frozenset(subset), Path(dest))
    db = adapter.warehouse(root)
    if repair:
        adapter.repair(repair, db)
    adapter.advance(root)
    return db


def measured_coupling(task_dir, a: str, b: str, workdir, *,
                      registry: dict = None, adapter=None,
                      keep_worlds: bool = False, clock: bool = False) -> dict:
    """Measure whether stressor `a` changes the correct repair of stressor `b`.

    Returns the JSON record described in the module docstring. `executed` is
    False (and `coupled` None) when no adapter can stage the subset worlds --
    the pre-filter result is still returned, so a registry-only run degrades to
    "designed, not executed" rather than to a guess.

    `clock=True` stages world ROOTS, applies the repair at t0 and advances over
    the next planted event before evaluating. See the module docstring.
    """
    t0 = time.time()
    task = Path(task_dir).name
    registry = registry if registry is not None else load_registry()
    pre = predeclared_overlap(a, b, registry)
    rec = {"task": task, "a": a, "b": b, "predeclared_overlap": pre,
           "clock": bool(clock), "coupled": None, "evidence": {},
           "executed": False}

    if adapter is None:
        from coupling_adapters import get_adapter          # noqa: E402
        adapter = get_adapter(task_dir, clock=clock)
    if adapter is None:
        rec["evidence"]["why_not_executed"] = (
            "no adapter for task %r; the measured test needs a subset builder"
            % task)
        return rec
    for name in (a, b):
        if name not in getattr(adapter, "stressors", []):
            rec["evidence"]["why_not_executed"] = (
                "%r is not stageable by the %s adapter" % (name, task))
            return rec
    if clock and not getattr(adapter, "clocked", False):
        rec["evidence"]["why_not_executed"] = (
            "the %s adapter stages no world root; a clocked measurement needs "
            "`world`, `warehouse` and `advance`" % task)
        return rec

    work = Path(workdir) / (task + "__" + a + "__" + b + ("__clock" if clock else ""))
    if work.exists():
        shutil.rmtree(work)
    tables = adapter.canonical_tables

    def stage(subset, name, repair=None):
        return _stage(adapter, subset, work / name, repair=repair, clock=clock)

    # ---- 1. stage the four worlds ---------------------------------------
    w_base = stage(frozenset(), "base")
    w_a = stage(frozenset([a]), "a")
    stage(frozenset([b]), "b")
    stage(frozenset([a, b]), "ab")
    t_build = time.time() - t0

    # ---- 2. footprint(A): MEASURED, not declared -------------------------
    planted, _ = differing_keys(w_base, w_a, tables)
    w_a_rep = stage(frozenset([a]), "a_repaired", repair=a)
    repaired, _ = differing_keys(w_a, w_a_rep, tables)
    footprint = _flatten(planted) | _flatten(repaired)

    # ---- 3. B's oracle repair on both worlds -----------------------------
    w_b_rep = stage(frozenset([b]), "b_repaired", repair=b)
    cl_b = adapter.clauses(b, w_b_rep)

    w_ab_rep = stage(frozenset([a, b]), "ab_repaired", repair=b)
    cl_ab = adapter.clauses(b, w_ab_rep)

    # ---- 4. criterion (i): B's repair stops working ----------------------
    b_ok = all(c["ok"] for c in cl_b)
    ab_failed = [c["clause"] for c in cl_ab if not c["ok"]]
    crit_i = bool(b_ok and ab_failed)

    # ---- 5. criterion (ii): state moved outside A's footprint ------------
    post, notes = differing_keys(w_b_rep, w_ab_rep, tables)
    outside = {t: sorted(k for k in keys if (t,) + k not in footprint)
               for t, keys in post.items()}
    outside = {t: k for t, k in outside.items() if k}
    crit_ii = bool(outside)

    # ---- 6. the order diagnostic ----------------------------------------
    order_dependent = None
    cl_ab_ordered = None
    if crit_i:
        if clock:
            root = adapter.world(frozenset([a, b]), work / "ab_ordered")
            w_ord = adapter.warehouse(root)
            adapter.repair(a, w_ord)
            adapter.repair(b, w_ord)
            adapter.advance(root)
        else:
            w_ord = adapter.build(frozenset([a, b]), work / "ab_ordered")
            adapter.repair(a, w_ord)
            adapter.repair(b, w_ord)
        cl_ab_ordered = adapter.clauses(b, w_ord)
        order_dependent = all(c["ok"] for c in cl_ab_ordered)

    coupled = crit_i or crit_ii
    strength = ("strong" if crit_ii else
                ("ordered" if crit_i and order_dependent else
                 ("weak" if crit_i else "none")))

    rec.update({
        "coupled": coupled,
        "executed": True,
        "evidence": {
            "strength": strength,
            "criterion_i_repair_stops_working": crit_i,
            "criterion_ii_state_outside_a_footprint": crit_ii,
            "b_clauses_on_base_plus_b": cl_b,
            "b_clauses_on_base_plus_a_plus_b": cl_ab,
            "b_clauses_failing_with_a_present": ab_failed,
            "repair_order_dependent": order_dependent,
            "b_clauses_after_repair_a_then_repair_b": cl_ab_ordered,
            "a_footprint_size": len(footprint),
            "a_footprint_tables": sorted({k[0] for k in footprint}),
            "post_repair_diff_keys": {t: len(k) for t, k in post.items() if k},
            "post_repair_diff_outside_a_footprint": {
                t: {"keys": len(k), "sample": k[:5]} for t, k in outside.items()},
            "table_notes": notes,
            "a_repair_writes": getattr(adapter, "repair_writes", {}).get(a),
            "b_repair_writes": getattr(adapter, "repair_writes", {}).get(b),
            "seconds_build": round(t_build, 2),
        },
        "seconds": round(time.time() - t0, 2),
    })
    if not keep_worlds:
        shutil.rmtree(work, ignore_errors=True)
    return rec


def selfcheck(task_dir, workdir, *, adapter=None, clock: bool = False) -> list:
    """Validity check on the clauses themselves, run before any verdict is read.

    A clause set that passes on the unrepaired world is vacuous, and a filter
    built on vacuous clauses returns "uncoupled" for everything and looks like
    it works. For each stressor s this asserts:

        base                -> every clause of s passes   (the world is clean)
        base + {s}          -> at least one clause fails  (the clause has teeth)
        base + {s} repaired -> every clause passes        (the repair is right)

    The first assertion is skipped for `kind: event`. An event's clauses are
    obligations created BY the event -- "the late lines are present exactly
    once" is false on a world where they never arrived -- so a clean world
    failing them is correct, not a bug. The coupling verdict never uses the
    clean world; its baseline is base+{b}, where B's obligation is live.

    THE SECOND ASSERTION HAS THE SAME SHAPE OF HOLE, and GT009 is what found
    it. "base + {s} fails at least one clause" presumes a stressor that is
    harmful ALONE - and a stressor that is harmful only beside another is
    precisely a COUPLED stressor, which is the thing this filter exists to
    find. Re-sending version 1 of six payments breaks nothing in a world where
    version 2 was never delivered. An adapter may therefore declare
    `conditional_on = {stressor: [partner, ...]}`, and the planted and repaired
    legs are staged with those partners present and UNREPAIRED. `staged_with`
    records it on the row, so a green self-check never silently means something
    weaker than it did before.

    Any row with ok=False invalidates every verdict involving that stressor.
    """
    task = Path(task_dir).name
    if adapter is None:
        from coupling_adapters import get_adapter              # noqa: E402
        adapter = get_adapter(task_dir, clock=clock)
    work = Path(workdir) / (task + "__selfcheck" + ("__clock" if clock else ""))
    if work.exists():
        shutil.rmtree(work)
    if clock and not getattr(adapter, "clocked", False):
        return []
    base = _stage(adapter, frozenset(), work / "base", clock=clock)
    conditional = getattr(adapter, "conditional_on", {}) or {}
    rows = []
    for s in adapter.stressors:
        t0 = time.time()
        partners = list(conditional.get(s) or [])
        staged = frozenset([s, *partners])
        on_base = adapter.clauses(s, base)
        w = _stage(adapter, staged, work / s, clock=clock)
        planted = adapter.clauses(s, w)
        w_rep = _stage(adapter, staged, work / (s + "__rep"), repair=s,
                       clock=clock)
        after = adapter.clauses(s, w_rep)
        kind = getattr(adapter, "kinds", {}).get(s, "defect")
        clean = (None if kind == "event"
                 else all(c["ok"] for c in on_base))
        row = {
            "stressor": s,
            "kind": kind,
            "clock": bool(clock),
            "staged_with": partners,
            "clean_world_passes": clean,
            "planted_world_fails": any(not c["ok"] for c in planted),
            "repaired_world_passes": all(c["ok"] for c in after),
            "clauses_with_teeth": [c["clause"] for c in planted if not c["ok"]],
            "clauses_failing_on_clean_world":
                [c["clause"] for c in on_base if not c["ok"]],
            "clauses_still_failing_after_repair":
                [c["clause"] for c in after if not c["ok"]],
            "seconds": round(time.time() - t0, 2),
        }
        row["ok"] = (clean is not False and row["planted_world_fails"]
                     and row["repaired_world_passes"])
        rows.append(row)
    shutil.rmtree(work, ignore_errors=True)
    return rows


def summarise(records: list) -> str:
    """The verdict table, as markdown."""
    w = ["| a (perturbed) | b (repaired) | clock | predeclared | coupled | "
         "strength | B clauses failing with A present | s |",
         "|---|---|---|---|---|---|---|---:|"]
    for r in records:
        pre = r["predeclared_overlap"].get("overlap")
        ev = r.get("evidence", {})
        w.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["a"], r["b"], "yes" if r.get("clock") else "no",
            {True: "overlap", False: "disconnected", None: "?"}[pre],
            {True: "**yes**", False: "no", None: "not executed"}[r["coupled"]],
            ev.get("strength", "-"),
            ", ".join(ev.get("b_clauses_failing_with_a_present") or []) or "-",
            r.get("seconds", "-")))
    return "\n".join(w)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--task", default="GT004-doubling-window")
    ap.add_argument("--pairs", default="all",
                    help="'all' for every ordered pair, else a;b,c;d list")
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--out", default=None, help="directory for the JSON record")
    ap.add_argument("--registry", default=str(REGISTRY_PATH))
    ap.add_argument("--keep-worlds", action="store_true")
    ap.add_argument("--selfcheck", action="store_true",
                    help="validate the clause sets, then exit")
    ap.add_argument("--clock", action="store_true",
                    help="stage world roots, repair at t0, then ADVANCE over "
                         "the next planted event before evaluating")
    args = ap.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from coupling_adapters import get_adapter                # noqa: E402

    task_dir = REPO / "tasks" / args.task
    adapter = get_adapter(task_dir, clock=args.clock)
    registry = load_registry(Path(args.registry))
    workdir = Path(args.workdir) if args.workdir else (
        Path(__file__).resolve().parents[2] / "var" / "coupling")

    if args.selfcheck:
        bad = 0
        for row in selfcheck(task_dir, workdir, adapter=adapter,
                             clock=args.clock):
            print("%-34s ok=%-5s clean=%-5s teeth=%-5s repaired=%-5s %s" % (
                row["stressor"], row["ok"], row["clean_world_passes"],
                row["planted_world_fails"], row["repaired_world_passes"],
                ",".join(row["clauses_with_teeth"]) or "-"))
            bad += 0 if row["ok"] else 1
        return 1 if bad else 0

    if args.pairs == "all":
        if adapter is None:
            print("no adapter for %s and --pairs all needs one" % args.task)
            return 2
        pairs = list(itertools.permutations(adapter.stressors, 2))
    else:
        pairs = [tuple(p.split(";", 1)) for p in args.pairs.split(",")]

    records = []
    for a, b in pairs:
        rec = measured_coupling(task_dir, a, b, workdir, registry=registry,
                                adapter=adapter, keep_worlds=args.keep_worlds,
                                clock=args.clock)
        ev = rec.get("evidence", {})
        print("%-34s -> %-34s coupled=%-5s %-8s %5ss" % (
            a, b, rec["coupled"], ev.get("strength", "-"),
            rec.get("seconds", "-")))
        records.append(rec)

    table = summarise(records)
    print("\n" + table)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        p = out / (args.task + ("-coupling-clock.json" if args.clock
                                else "-coupling.json"))
        checks = (selfcheck(task_dir, workdir, adapter=adapter, clock=args.clock)
                  if adapter is not None else [])
        with p.open("w", encoding="utf-8", newline="") as fh:
            json.dump({"task": args.task, "clock": bool(args.clock),
                       "clause_selfcheck": checks, "pairs": records},
                      fh, indent=2, sort_keys=True, default=str)
            fh.write("\n")
        print("wrote " + str(p))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
