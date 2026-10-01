"""Exact Shapley attribution of the no-op twin's damage over the fault set (H4).

Spec: design section 3 H4 and section 5 ("closure as a validity gate").
Players are the world's faults: each latent defect (`defect:<name>`, a key of
solution/apply.py FIXES) and each shock kind that executed in the schedule
(`shock:<kind>`). The value of a coalition S is J of a no-op twin in which
exactly the faults in S are present:

- a defect NOT in S is absent from t0 on: its fix is copied in and the world
  is restated (the load and dbt build re-run by hand, scheduler state
  untouched) BEFORE the first tick, so no tick ever samples the defective
  code. When no defect is disabled nothing is applied or restated, so
  v(all players) is J_noop itself;
- a shock kind NOT in S is removed from the installed schedule. Shocks of
  other kinds keep their `suppressed` flags: the realisation is fixed, and
  removing the kind that won an occurrence does not make the loser happen.

Shapley values are exact over all 2^n coalitions:

    phi_i = sum_{S not containing i} |S|! (n-|S|-1)! / n! * (v(S+i) - v(S))

Efficiency makes sum(phi) == v(N) - v(empty) an identity, so `closure` only
checks the arithmetic. The non-trivial half of the gate is v(empty) ~ 0: with
no fault present the world must take no damage; anything else is damage no
fault owns, i.e. a world bug.

When `players` names a subset, every fault outside it is disabled throughout
(the game is played on the subset; tests use this to keep runs short).
Values are cached per coalition in <workdir>/attribution_values.json, keyed
by a fingerprint of the schedule, ticks, players, gamma and task.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import twins  # noqa: E402

DEFECT, SHOCK = "defect:", "shock:"
CACHE = "attribution_values.json"


# --------------------------------------------------------------------------
# the game
# --------------------------------------------------------------------------

def key(S) -> str:
    return "|".join(sorted(S))


def shapley(players: list[str], v: Callable[[frozenset], float]) -> dict:
    """Exact Shapley values; `v` is called once per subset."""
    players = list(players)
    n = len(players)
    values = {}
    for r in range(n + 1):
        for combo in itertools.combinations(players, r):
            S = frozenset(combo)
            values[S] = float(v(S))
    fact = math.factorial
    phi = {}
    for i in players:
        rest = [p for p in players if p != i]
        total = 0.0
        for r in range(n):
            w = fact(r) * fact(n - r - 1) / fact(n)
            for combo in itertools.combinations(rest, r):
                S = frozenset(combo)
                total += w * (values[S | {i}] - values[S])
        phi[i] = total
    res = {"players": players, "phi": phi,
           "values": {key(S): x for S, x in values.items()},
           "v_empty": values[frozenset()], "v_full": values[frozenset(players)]}
    return recheck(res)


def recheck(res: dict) -> dict:
    """(Re)compute sum_phi and closure = |sum(phi) - (v(N) - v(empty))|."""
    res["sum_phi"] = sum(res["phi"].values())
    res["closure"] = abs(res["sum_phi"] - (res["v_full"] - res["v_empty"]))
    return res


def gate(res: dict, rel_tol: float = 1e-9) -> dict:
    """H4: closure <= tol AND v(empty) <= tol, tol = rel_tol * max(1, |v(N)|)."""
    tol = rel_tol * max(1.0, abs(res["v_full"]))
    closure_ok = res["closure"] <= tol
    null_ok = abs(res["v_empty"]) <= tol
    return {"tol": tol, "closure_ok": closure_ok, "null_ok": null_ok,
            "unattributed": res["v_empty"], "passed": closure_ok and null_ok}


# --------------------------------------------------------------------------
# coalitions -> twin worlds
# --------------------------------------------------------------------------

def players(task: twins.Task, schedule: dict) -> list[str]:
    kinds = sorted({s["kind"] for s in schedule.get("shocks") or []
                    if not s.get("suppressed")})
    return [DEFECT + d for d in task.defects] + [SHOCK + k for k in kinds]


_all_players = players


def coalition_schedule(schedule: dict, enabled_kinds: set) -> dict:
    out = dict(schedule)
    out["shocks"] = [dict(s) for s in schedule.get("shocks") or []
                     if s["kind"] in enabled_kinds]
    return out


class FixAtStart:
    """No-op policy whose world lacks the given defects from t0 on."""
    name = "coalition"

    def __init__(self, solution, disabled: tuple[str, ...]):
        self.solution, self.disabled, self.done = solution, tuple(disabled), False

    def act(self, root, tick, vnow):
        if self.done or not self.disabled:
            return []
        self.done = True
        return self.solution.apply(root, self.disabled) + self.solution.restate(root)


def coalition_value(task_dir: Path, schedule: dict, tick_schedule: dict,
                    enabled: frozenset, workdir: Path, gamma: float = 1.0,
                    universe: list[str] | None = None) -> float:
    """J of the no-op twin with exactly the faults in `enabled` present."""
    task = twins.load_task(task_dir)
    disabled = tuple(d for d in task.defects if DEFECT + d not in enabled)
    kinds = {p[len(SHOCK):] for p in enabled if p.startswith(SHOCK)}
    name = "coal-" + (hashlib.sha256(key(enabled).encode()).hexdigest()[:12])
    res = twins.run_twin(task.seed, coalition_schedule(schedule, kinds), tick_schedule,
                         FixAtStart(task.solution, disabled), task.damage_spec,
                         workdir, name=name, gamma=gamma)
    return res["J"]


def _fingerprint(task_dir, schedule, tick_schedule, plist, gamma) -> str:
    doc = {"task": str(Path(task_dir).resolve()), "schedule": schedule,
           "ticks": tick_schedule, "players": sorted(plist), "gamma": gamma}
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def _run_one(args):
    task_dir, schedule, ticks, enabled, workdir, gamma, fn = args
    return key(enabled), fn(task_dir, schedule, ticks, frozenset(enabled),
                            workdir, gamma)


def attribute(task_dir: Path, schedule: dict, tick_schedule: dict, workdir: Path, *,
              players: list[str] | None = None, gamma: float = 1.0, jobs: int = 1,
              value_fn: Callable | None = None, check_restate: bool = False) -> dict:
    """Exact Shapley attribution of J over the fault set, with the H4 gate.

    Returns {players, phi, values, v_empty, v_full, sum_phi, closure, gate,
    J_noop?, restate_only?}. Coalition values are computed (in `jobs`
    processes), cached on disk, and reused across calls.
    check_restate also runs a twin that restates with NO fix applied: if the
    restate itself moved J, disabling a defect would mix the fix's effect with
    the restate's, and the attribution would not mean what it says.
    """
    task = twins.load_task(task_dir)
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    plist = list(players) if players is not None else _all_players(task, schedule)
    fn = value_fn or coalition_value
    fp = _fingerprint(task_dir, schedule, tick_schedule, plist, gamma)
    cache_path = workdir / CACHE
    cache = {}
    if cache_path.is_file():
        doc = json.loads(cache_path.read_text(encoding="utf-8"))
        if doc.get("fingerprint") == fp:
            cache = doc["values"]
    subsets = [frozenset(c) for r in range(len(plist) + 1)
               for c in itertools.combinations(plist, r)]
    todo = [S for S in subsets if key(S) not in cache]
    twins.base_world(task.seed, workdir)          # build once, before any fork

    def save():
        cache_path.write_text(json.dumps({"fingerprint": fp, "values": cache},
                                         indent=1, sort_keys=True) + "\n")

    work = [(task_dir, schedule, tick_schedule, sorted(S), workdir, gamma, fn)
            for S in todo]
    if jobs > 1 and len(work) > 1:
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            for k, val in pool.map(_run_one, work):
                cache[k] = val
                save()
    else:
        for item in work:
            k, val = _run_one(item)
            cache[k] = val
            save()
    res = shapley(plist, lambda S: cache[key(S)])
    res["gate"] = gate(res)
    if check_restate:
        r = twins.run_twin(task.seed, schedule, tick_schedule,
                           _RestateOnly(task.solution), task.damage_spec, workdir,
                           name="restate-only", gamma=gamma)
        res["restate_only_J"] = r["J"]
        full = set(_all_players(task, schedule))
        if set(plist) == full:
            res["restate_neutral"] = r["J"] == res["v_full"]
    return res


class _RestateOnly:
    name = "restate_only"

    def __init__(self, solution):
        self.solution, self.done = solution, False

    def act(self, root, tick, vnow):
        if self.done:
            return []
        self.done = True
        return self.solution.restate(root)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Shapley attribution of J_noop (H4)")
    ap.add_argument("--task", type=Path, default=twins.REPO / "tasks" / "DW001-live-revenue")
    ap.add_argument("--schedule", type=Path, help="shock_schedule.json; default: "
                    "hawkes.sample of the task's shock_spec on a fresh build")
    ap.add_argument("--ticks", type=Path, help="tick_schedule.json; default: "
                    "post ticks of --h over --T")
    ap.add_argument("--h", type=float, default=24.0)
    ap.add_argument("--T", type=float, default=168.0)
    ap.add_argument("--workdir", type=Path, required=True)
    ap.add_argument("--jobs", type=int, default=1)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    from world import hawkes, scheduler
    task = twins.load_task(a.task)
    base = twins.base_world(task.seed, a.workdir)
    sched = (json.loads(a.schedule.read_text()) if a.schedule
             else hawkes.sample(base, task.shock_spec))
    ticks = (json.loads(a.ticks.read_text()) if a.ticks
             else twins.post_tick_schedule(scheduler.load(base)["vnow"], a.h, a.T))
    res = attribute(a.task, sched, ticks, a.workdir, jobs=a.jobs, check_restate=True)
    out = {k: res[k] for k in ("players", "phi", "v_empty", "v_full", "sum_phi",
                               "closure", "gate", "values")}
    out.update({k: res[k] for k in ("restate_only_J", "restate_neutral") if k in res})
    out["shocks"] = [(s["id"], s["key"], s["suppressed"]) for s in sched["shocks"]]
    text = json.dumps(out, indent=1, sort_keys=True)
    if a.out:
        a.out.write_text(text + "\n")
    print(text)
    return 0 if res["gate"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
