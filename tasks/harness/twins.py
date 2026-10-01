"""Twin runner, score S and per-fault timeline for dynamic worlds (plan Task 5).

Spec: docs/implementation_plan/experimental_ideas/2026-09-30-dynamic-world-design.md
sections 2 (objective J, score S) and 4.5. After an episode the harness holds
the agent world's three ledgers (Task 6 copies them out):

    shock_schedule.json   the Hawkes realisation installed in the agent world
    tick_schedule.json    every tick's recorded virtual times
    damage.jsonl          one damage row per tick of the agent world

A **twin** is a fresh build of the same seed with the same shock schedule
installed, driven through the same recorded ticks by a scripted policy
(`clock.replay`), with damage sampled after every tick exactly as in the
episode. The no-op and oracle twins share the agent's shock realisation, so

    S = (J_noop - J_agent) / (J_noop - J_oracle)

is a paired (common-random-numbers) estimator. Nothing here calls a model or a
container; every number comes from `world.scheduler.advance` running the real
loader and dbt, and `world.damage.sample` measuring the result.

`J` weights each tick by its own duration (vnow_before -> vnow_after from the
tick schedule) rather than a constant h: the last post-episode tick is
shortened to land on T, and a policy-triggered job can stretch a step. With
uniform ticks it equals `damage.integral` exactly.

Fresh build: the seed is built once per work directory (`base_world`) and
copied per twin, the pattern DW-1's own tests use; a build is a pure function
of the seed, so the copy IS a fresh build.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
if str(REPO / "world") not in sys.path:
    sys.path.insert(0, str(REPO / "world"))

import yaml  # noqa: E402

from world import build as wbuild  # noqa: E402
from world import clock, damage, hawkes, scheduler  # noqa: E402
from world import faults as faultlib  # noqa: E402

HOUR = timedelta(hours=1)
DEGENERATE_S = ("S is undefined when J_noop == J_oracle: the oracle does no "
                "better than doing nothing on this shock realisation, so the "
                "denominator (the room for improvement) is zero and every "
                "agent would score both 0 and 1. Reported as None, never a "
                "division by zero.")


# --------------------------------------------------------------------------
# the task
# --------------------------------------------------------------------------

def _load_module(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@dataclass
class Task:
    dir: Path
    seed: Path
    damage_spec: dict
    shock_spec: dict
    policies: object
    solution: object
    defects: dict = field(default_factory=dict)       # defect name -> contract
    shock_owner: dict = field(default_factory=dict)   # shock kind -> contract

    @property
    def weights(self) -> dict:
        return dict(self.damage_spec.get("weights") or {})


def load_task(task_dir: Path) -> Task:
    """A dynamic-world task from its task.toml.

    Defects are the solution's FIXES (named by the contract they damage);
    each `[[latent_defects]]` entry's `contract` and the fault kind its
    `shock` text starts with give the shock -> owning-contract map.
    """
    task_dir = Path(task_dir).resolve()
    meta = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
    w = meta["world"]
    tag = hashlib.sha256(str(task_dir).encode()).hexdigest()[:8]
    sol = _load_module(f"dw_solution_{tag}", task_dir / "solution" / "apply.py")
    if task_dir.name == "DW001-live-revenue":
        # policies.py imports the solution under this fixed name
        sys.modules.setdefault("dw001_solution", sol)
    pol = _load_module(f"dw_policies_{tag}", task_dir / w["policies"])
    owner = {}
    for d in meta.get("latent_defects") or []:
        kind = str(d.get("shock", "")).split(" ", 1)[0].strip()
        if kind in faultlib.KINDS:
            owner[kind] = d["contract"]
    return Task(dir=task_dir, seed=(REPO / w["seed"]).resolve(),
                damage_spec=damage.load_spec(task_dir / w["damage_spec"]),
                shock_spec=hawkes.load_spec(task_dir / w["shock_spec"]),
                policies=pol, solution=sol,
                defects={name: name for name in sol.FIXES}, shock_owner=owner)


# --------------------------------------------------------------------------
# worlds
# --------------------------------------------------------------------------

def base_world(seed_path: Path, workdir: Path) -> Path:
    """The built seed, once per work directory (keyed by the seed's bytes)."""
    seed_path = Path(seed_path).resolve()
    tag = hashlib.sha256(seed_path.read_bytes()).hexdigest()[:12]
    base = Path(workdir) / "_base" / tag
    if not (base / ".built").is_file():
        shutil.rmtree(base, ignore_errors=True)
        base.parent.mkdir(parents=True, exist_ok=True)
        wbuild.build(seed_path, base)
        (base / ".built").write_text(str(seed_path) + "\n", encoding="utf-8")
    return base


def fresh_world(seed_path: Path, workdir: Path, name: str) -> Path:
    base = base_world(seed_path, workdir)
    dst = Path(workdir) / name
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(base, dst, symlinks=True)
    (dst / ".built").unlink()
    return dst


def post_tick_schedule(vnow_start: str, h_hours: float, T_hours: float) -> dict:
    """The tick schedule of an episode with no commands: `Clock.finish` from
    `vnow_start`, computed without a world (h-hour steps, the last shortened)."""
    clock.TickSpec(m=1, h_hours=h_hours, T_hours=T_hours)      # validates
    t = start = scheduler.parse_vtime(vnow_start)
    end = start + timedelta(hours=T_hours)
    ticks = []
    while t < end:
        nxt = min(t + timedelta(hours=h_hours), end)
        ticks.append({"tick": len(ticks), "phase": "post", "after_command": None,
                      "vnow_before": scheduler.iso(t), "vnow_after": scheduler.iso(nxt)})
        t = nxt
    return {"m": 1, "h_hours": h_hours, "T_hours": T_hours,
            "vnow_start": scheduler.iso(start), "ticks": ticks}


# --------------------------------------------------------------------------
# J and S
# --------------------------------------------------------------------------

def wv(row: dict, weights: dict) -> float:
    """w . V for one damage row (a contract without a weight weighs 1.0)."""
    return sum(float(weights.get(c, 1.0)) * float(v)
               for c, v in (row.get("V") or {}).items())


def _rows_by_tick(rows: list[dict], tick_schedule: dict) -> list[dict]:
    by = {}
    for r in rows:
        if "tick" in r:
            by[int(r["tick"])] = r
    out = []
    for t in tick_schedule["ticks"]:
        if t["tick"] not in by:
            raise ValueError(f"no damage row for tick {t['tick']}")
        out.append(by[t["tick"]])
    return out


def _duration(rec: dict) -> float:
    return (scheduler.parse_vtime(rec["vnow_after"])
            - scheduler.parse_vtime(rec["vnow_before"])) / HOUR


def objective(rows: list[dict], weights: dict, tick_schedule: dict,
              gamma: float = 1.0) -> float:
    """J = sum_k dt_k * w.V_k + gamma * w.V_T over the tick schedule.

    Rows are matched to ticks by `tick`; rows for other ticks (e.g. a t0
    sample) are ignored, a tick without a row is an error.
    """
    if not tick_schedule["ticks"]:
        return 0.0
    rows = _rows_by_tick(rows, tick_schedule)
    total = sum(_duration(t) * wv(r, weights)
                for t, r in zip(tick_schedule["ticks"], rows))
    return total + gamma * wv(rows[-1], weights)


def score(J_agent: float, J_noop: float, J_oracle: float) -> float | None:
    """S = (J_noop - J_agent) / (J_noop - J_oracle); None when undefined.

    Degenerate case: |J_noop - J_oracle| <= 1e-12 * max(1, |J_noop|) (see
    DEGENERATE_S). A negative denominator (the oracle worse than the no-op)
    is not degenerate and is returned as computed; it means the oracle is
    wrong for this world and the report flags it.
    """
    den = J_noop - J_oracle
    if abs(den) <= 1e-12 * max(1.0, abs(J_noop)):
        return None
    return (J_noop - J_agent) / den


def curve(rows: list[dict], weights: dict) -> list[dict]:
    """The compact per-tick curve stored in twins.json."""
    return [{"tick": r["tick"], "phase": r.get("phase"), "vnow": r.get("vnow"),
             "V": dict(r.get("V") or {}), "wV": wv(r, weights)} for r in rows]


# --------------------------------------------------------------------------
# twins
# --------------------------------------------------------------------------

def run_twin(seed_path: Path, schedule: dict, tick_schedule: dict, policy,
             damage_spec: dict, workdir: Path, *, name: str | None = None,
             gamma: float = 1.0, keep: bool = False) -> dict:
    """Fresh build + `hawkes.install(schedule)` + `clock.replay(policy)`.

    Returns {"rows": damage rows (one per tick, with tick/phase), "J",
    "actions": the replay records' actions}. The world is removed afterwards
    unless `keep`.
    """
    name = name or f"twin-{getattr(policy, 'name', 'policy')}"
    root = fresh_world(seed_path, workdir, name)
    try:
        hawkes.install(root, schedule)
        rows = []

        def on_tick(r: Path, record: dict) -> None:
            row = damage.sample(r, damage_spec)
            row["tick"], row["phase"] = record["tick"], record["phase"]
            rows.append(row)

        records = clock.replay(root, tick_schedule, policy, on_tick)
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)
    weights = dict(damage_spec.get("weights") or {})
    return {"rows": rows, "J": objective(rows, weights, tick_schedule, gamma),
            "actions": [{"tick": r["tick"], "actions": r["actions"],
                         **({"actions_initial": r["actions_initial"]}
                            if "actions_initial" in r else {})} for r in records]}


def _policy(task: Task, name: str, tick_schedule: dict):
    return task.policies.make(name, horizon_hours=float(tick_schedule["T_hours"]))


def scripted_episode(task: Task, schedule: dict, tick_schedule: dict,
                     policy_name: str, episode_dir: Path, workdir: Path,
                     gamma: float = 1.0) -> dict:
    """Play a scripted policy as if it were the agent and write the three
    ledgers an episode leaves (for tests and the scripted ladder)."""
    res = run_twin(task.seed, schedule, tick_schedule, _policy(task, policy_name, tick_schedule),
                   task.damage_spec, workdir, name=f"episode-{policy_name}", gamma=gamma)
    ep = Path(episode_dir)
    ep.mkdir(parents=True, exist_ok=True)
    (ep / "shock_schedule.json").write_text(json.dumps(schedule, sort_keys=True) + "\n")
    (ep / "tick_schedule.json").write_text(json.dumps(tick_schedule, sort_keys=True) + "\n")
    ledger = ep / "damage.jsonl"
    ledger.unlink(missing_ok=True)
    for r in res["rows"]:
        damage.append(ledger, r)
    return res


# --------------------------------------------------------------------------
# per-fault timeline
# --------------------------------------------------------------------------

def _V(row: dict, contract: str) -> float:
    return float((row.get("V") or {}).get(contract, 0.0))


def _executed(schedule: dict) -> list[dict]:
    return [s for s in schedule.get("shocks") or [] if not s.get("suppressed")]


def _after(tick_schedule: dict, i: int) -> str:
    return tick_schedule["ticks"][i]["vnow_after"]


def _hours(a: str, b: str) -> float:
    return (scheduler.parse_vtime(b) - scheduler.parse_vtime(a)) / HOUR


def _agent_course(entry: dict, onset: int | None, agent: list[dict],
                  tick_schedule: dict, exposures: list[str] | None) -> dict:
    """Fill first_zero / mitigated / status for one fault from the agent curve.

    exposures: vts of later shocks owned by the same contract; None means the
    contract is exposed on every tick (a shock-free, continuous defect).
    """
    c = entry["contract"]
    entry.update(first_zero_tick=None, mitigated_tick=None,
                 time_to_mitigate_hours=None)
    if onset is None:
        entry["status"] = "inert"
        return entry
    n = len(agent)
    first_zero = next((k for k in range(onset, n) if _V(agent[k], c) == 0.0), None)
    entry["first_zero_tick"] = first_zero
    if first_zero is None:
        entry["status"] = "missed"
        return entry
    last_pos = max((k for k in range(first_zero, n) if _V(agent[k], c) > 0.0),
                   default=None)
    if last_pos is not None:
        entry["status"] = "patched"                    # back to 0, then recurred
        if last_pos + 1 < n:                          # ... and later held 0 again
            entry["mitigated_tick"] = last_pos + 1
    else:
        entry["mitigated_tick"] = first_zero
        held_from = _after(tick_schedule, first_zero)
        if exposures is None:
            exposed = first_zero < n - 1
        else:
            exposed = any(v > held_from for v in exposures)
        entry["status"] = "fixed" if exposed else "mitigated"
    if entry["status"] != "patched":
        entry["time_to_mitigate_hours"] = _hours(
            _after(tick_schedule, onset), _after(tick_schedule, entry["mitigated_tick"]))
    return entry


def timeline(schedule: dict, tick_schedule: dict, noop_rows: list[dict],
             agent_rows: list[dict], defects: dict, shock_owner: dict,
             onsets: dict | None = None) -> list[dict]:
    """One entry per executed shock and per latent defect.

    onset (no-op twin):
      shock  - the first tick ending at/after the shock's vts at which the
               owning contract's V rises above its level at the tick before
               (its `baseline`; 0 before tick 0). A heuristic on a shared
               contract: two shocks of one owner inside one tick share an onset.
      defect - the first tick at which its contract's V > 0.
    agent course (agent curve, from onset):
      first_zero_tick  the first tick with owning V == 0;
      status           inert (no onset in the no-op twin) | not_pivotal
                       (counterfactual onset None: removing this shock alone
                       avoids no damage) | missed (never 0) |
                       patched (0, then > 0 again) | fixed (0 and held through
                       a later shock of the same owner; every later tick for a
                       contract no shock owns) | mitigated (0 and held, but no
                       later shock tested it);
      mitigated_tick   the tick from which V stays 0 to the end;
      time_to_mitigate_hours  onset tick end -> mitigated tick end (fixed /
                       mitigated only).
    Suppressed shocks never happened and get no entry.

    `onsets` ({shock id: tick | None}, from `counterfactual_onsets`) replaces
    the heuristic for the shocks it names (`onset_method: counterfactual`).
    """
    noop = _rows_by_tick(noop_rows, tick_schedule)
    agent = _rows_by_tick(agent_rows, tick_schedule)
    ticks = tick_schedule["ticks"]
    shocks = _executed(schedule)
    owned = {}
    for s in shocks:
        c = shock_owner.get(s["kind"])
        if c:
            owned.setdefault(c, []).append(s["vts"])
    out = []
    for s in shocks:
        c = shock_owner.get(s["kind"])
        e = {"fault": s["id"], "type": "shock", "kind": s["kind"], "contract": c,
             "vts": s["vts"], "onset_tick": None, "onset_vnow": None, "baseline": None}
        if c is None:
            e["status"] = "unowned"
            out.append(e)
            continue
        first = next((k for k, t in enumerate(ticks) if t["vnow_after"] >= s["vts"]), None)
        onset = None
        e["onset_method"] = "heuristic"
        if onsets is not None and s["id"] in onsets:
            onset, e["onset_method"] = onsets[s["id"]], "counterfactual"
        elif first is not None:
            base = _V(noop[first - 1], c) if first > 0 else 0.0
            e["baseline"] = base
            onset = next((k for k in range(first, len(ticks))
                          if _V(noop[k], c) > base), None)
        if onset is not None:
            e["onset_tick"], e["onset_vnow"] = onset, ticks[onset]["vnow_after"]
        later = [v for v in owned[c] if v > s["vts"]]
        e = _agent_course(e, onset, agent, tick_schedule, later)
        if onset is None and e["onset_method"] == "counterfactual":
            # no damage that removing this shock ALONE avoids; it may still
            # share damage with another shock (overdetermination) - the
            # Shapley attribution, not the timeline, splits that
            e["status"] = "not_pivotal"
        out.append(e)
    for name, c in defects.items():
        e = {"fault": f"defect:{name}", "type": "defect", "kind": name, "contract": c,
             "vts": None, "onset_tick": None, "onset_vnow": None, "baseline": 0.0}
        onset = next((k for k in range(len(ticks)) if _V(noop[k], c) > 0.0), None)
        if onset is not None:
            e["onset_tick"], e["onset_vnow"] = onset, ticks[onset]["vnow_after"]
        out.append(_agent_course(e, onset, agent, tick_schedule, owned.get(c)))
    return out


def _without_one(task_dir, schedule, ticks, shock_id, workdir, gamma):
    task = load_task(task_dir)
    sched = dict(schedule, shocks=[s for s in schedule["shocks"] if s["id"] != shock_id])
    tag = hashlib.sha256(shock_id.encode()).hexdigest()[:10]
    return shock_id, run_twin(task.seed, sched, ticks, _policy(task, "noop", ticks),
                              task.damage_spec, workdir, name=f"minus-{tag}",
                              gamma=gamma)["rows"]


def counterfactual_onsets(task: Task, schedule: dict, tick_schedule: dict,
                          noop_rows: list[dict], workdir: Path, gamma: float = 1.0,
                          jobs: int = 1) -> dict:
    """Exact per-shock onset: {shock id: first tick at which the owning V of
    the no-op twin EXCEEDS that of the no-op twin WITHOUT that one shock, or
    None (the shock added no damage to its contract)}. Strictly greater, not
    different: on DW-1 one late day can shield another (removing it raises
    liveness damage), which is an interaction, not the shock's onset. One extra twin per executed
    owned shock; the heuristic in `timeline` cannot separate two shocks that
    land on one contract, this can."""
    noop = _rows_by_tick(noop_rows, tick_schedule)
    shocks = [s for s in _executed(schedule) if task.shock_owner.get(s["kind"])]
    base_world(task.seed, workdir)
    args = [(task.dir, schedule, tick_schedule, s["id"], workdir, gamma) for s in shocks]
    if jobs > 1 and len(args) > 1:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(max_workers=jobs) as pool:
            minus = dict(pool.map(_without_one, *zip(*args)))
    else:
        minus = dict(_without_one(*a) for a in args)
    out = {}
    for s in shocks:
        c = task.shock_owner[s["kind"]]
        rows = _rows_by_tick(minus[s["id"]], tick_schedule)
        out[s["id"]] = next((k for k in range(len(noop))
                             if _V(noop[k], c) > _V(rows[k], c)), None)
    return out


# --------------------------------------------------------------------------
# time to detect (gateway audit log)
# --------------------------------------------------------------------------

def _touched(row: dict) -> list[str]:
    out = list(row.get("targets") or []) + list(row.get("read_targets") or [])
    if row.get("target"):
        out.append(row["target"])
    return [str(t).lower() for t in out]


def _matches(row: dict, footprint: list[str]) -> bool:
    touched = _touched(row)
    summary = str(row.get("args_summary") or "").lower()
    for f in (str(x).lower() for x in footprint):
        for t in touched:
            if (t == f or t.endswith("/" + f) or t.endswith("." + f)
                    or f.endswith("/" + t) or f.endswith("." + t)):
                return True
        if f and f in summary:
            return True
    return False


def time_to_detect(entries: list[dict], actions_path: Path,
                   footprints: dict) -> list[dict]:
    """Add detect_seq / detect_vts / time_to_detect_hours to timeline entries.

    The first audit row (actions.jsonl, gateway schema: seq, vts, target(s),
    read_targets, args_summary) at or after the onset tick's end that touches
    the fault's footprint. A footprint is looked up by the fault id, then by
    its kind (shock kind, or `defect:<name>` for a defect). Entries with no
    onset or no footprint get None.
    """
    rows = []
    path = Path(actions_path)
    if path.is_file():
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()
                if l.strip()]
    rows.sort(key=lambda r: r.get("seq", 0))
    for e in entries:
        e.update(detect_seq=None, detect_vts=None, time_to_detect_hours=None)
        fp = footprints.get(e["fault"]) or footprints.get(e["kind"]) \
            or footprints.get(f"defect:{e['kind']}" if e["type"] == "defect" else "")
        if not fp or e.get("onset_vnow") is None:
            continue
        for r in rows:
            vts = r.get("vts")
            if not vts or scheduler.parse_vtime(vts) < scheduler.parse_vtime(e["onset_vnow"]):
                continue
            if _matches(r, fp):
                e.update(detect_seq=r.get("seq"), detect_vts=vts,
                         time_to_detect_hours=_hours(e["onset_vnow"], vts))
                break
    return entries


# --------------------------------------------------------------------------
# per episode
# --------------------------------------------------------------------------

def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines()
            if l.strip()]


def episode_scores(episode_dir: Path, task_dir: Path, workdir: Path | None = None, *,
                   actions: Path | None = None, footprints: dict | None = None,
                   gamma: float = 1.0, write: bool = True,
                   exact_onset: bool = False, jobs: int = 1) -> dict:
    """Run the no-op and oracle twins for one episode and write twins.json.

    episode_dir holds the agent world's shock_schedule.json,
    tick_schedule.json and damage.jsonl. `actions` (an actions.jsonl) and
    `footprints` ({fault id | kind | defect:<name>: [tables / paths]}) turn on
    time-to-detect. `exact_onset` replaces the per-shock onset heuristic
    with `counterfactual_onsets` (one extra no-op twin per shock).
    """
    ep = Path(episode_dir)
    task = load_task(task_dir)
    schedule = json.loads((ep / "shock_schedule.json").read_text(encoding="utf-8"))
    ticks = json.loads((ep / "tick_schedule.json").read_text(encoding="utf-8"))
    agent_rows = _rows_by_tick(_read_jsonl(ep / "damage.jsonl"), ticks)
    workdir = Path(workdir) if workdir is not None else ep / "_twins"
    workdir.mkdir(parents=True, exist_ok=True)
    w = task.weights
    noop = run_twin(task.seed, schedule, ticks, _policy(task, "noop", ticks),
                    task.damage_spec, workdir, name="twin-noop", gamma=gamma)
    oracle = run_twin(task.seed, schedule, ticks, _policy(task, "oracle", ticks),
                      task.damage_spec, workdir, name="twin-oracle", gamma=gamma)
    J_agent = objective(agent_rows, w, ticks, gamma)
    S = score(J_agent, noop["J"], oracle["J"])
    onsets = (counterfactual_onsets(task, schedule, ticks, noop["rows"], workdir,
                                    gamma, jobs) if exact_onset else None)
    tl = timeline(schedule, ticks, noop["rows"], agent_rows, task.defects,
                  task.shock_owner, onsets)
    if actions is not None:
        tl = time_to_detect(tl, actions, footprints or {})
    out = {"task": task.dir.name, "J_agent": J_agent, "J_noop": noop["J"],
           "J_oracle": oracle["J"], "S": S, "gamma": gamma, "weights": w,
           "S_note": DEGENERATE_S if S is None else None,
           "oracle_worse_than_noop": oracle["J"] > noop["J"],
           "tick_schedule": ticks,
           "curves": {"agent": curve(agent_rows, w), "noop": curve(noop["rows"], w),
                      "oracle": curve(oracle["rows"], w)},
           "timeline": tl}
    if write:
        (ep / "twins.json").write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("episode_dir", type=Path)
    ap.add_argument("--task", type=Path, default=REPO / "tasks" / "DW001-live-revenue")
    ap.add_argument("--workdir", type=Path)
    ap.add_argument("--actions", type=Path)
    ap.add_argument("--footprints", type=Path, help="yaml/json {fault: [tables]}")
    ap.add_argument("--exact-onset", action="store_true")
    ap.add_argument("--jobs", type=int, default=1)
    a = ap.parse_args(argv)
    fps = yaml.safe_load(a.footprints.read_text()) if a.footprints else None
    out = episode_scores(a.episode_dir, a.task, a.workdir, actions=a.actions, footprints=fps,
                         exact_onset=a.exact_onset, jobs=a.jobs)
    print(json.dumps({k: out[k] for k in ("J_agent", "J_noop", "J_oracle", "S")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
