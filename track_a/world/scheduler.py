"""Deterministic scheduler state machine + the world's virtual clock.

/var/sched/state.json is the single time authority: `vnow` plus job run
history. Triggering a job appends a run and advances vnow by the job's
seeded duration — never by measured wall time.

Placeholder: job `command`s record intent only and have no side effects.
Track C wires real load/dbt behavior through the same state transitions.
"""
import argparse
import json
import sys
from pathlib import Path

from .gen import iso, parse_vtime
from .hashing import json_canon
from datetime import timedelta

STATE = Path("var/sched/state.json")


def _path(root: Path) -> Path:
    return Path(root) / STATE


def load(root: Path) -> dict:
    return json.loads(_path(root).read_text(encoding="utf-8"))


def save(root: Path, state: dict) -> None:
    _path(root).write_bytes(json_canon(state) + b"\n")


def initial_state(seed: dict) -> dict:
    """vnow = start + days; one successful daily_load + dbt_build per past day."""
    start = parse_vtime(seed["vtime_start"])
    jobs = {
        "daily_load": {"command": "load_daily_revenue", "duration_s": 45},
        "dbt_build": {"command": "dbt_build", "duration_s": 90},
    }
    runs = []
    for i in range(seed["days"]):
        day = start + timedelta(days=i)
        load_at = day + timedelta(days=1, hours=1)
        runs.append({"job": "daily_load", "vts_start": iso(load_at),
                     "vts_end": iso(load_at + timedelta(seconds=45)),
                     "status": "success", "detail": f"dt={day.date()}"})
        build_at = load_at + timedelta(minutes=5)
        runs.append({"job": "dbt_build", "vts_start": iso(build_at),
                     "vts_end": iso(build_at + timedelta(seconds=90)),
                     "status": "success", "detail": None})
    return {"vnow": iso(start + timedelta(days=seed["days"])),
            "jobs": jobs, "runs": runs}


def trigger(root: Path, job: str) -> dict:
    state = load(root)
    if job not in state["jobs"]:
        raise SystemExit(f"unknown job: {job} (have: {', '.join(state['jobs'])})")
    start = parse_vtime(state["vnow"])
    end = start + timedelta(seconds=state["jobs"][job]["duration_s"])
    run = {"job": job, "vts_start": iso(start), "vts_end": iso(end),
           "status": "success", "detail": None}
    state["runs"].append(run)
    state["vnow"] = iso(end)
    save(root, state)
    return run


def advance(root: Path, hours: float) -> str:
    state = load(root)
    state["vnow"] = iso(parse_vtime(state["vnow"]) + timedelta(hours=hours))
    save(root, state)
    return state["vnow"]


def main(argv=None):
    p = argparse.ArgumentParser(prog="world.scheduler")
    p.add_argument("root", type=Path)
    p.add_argument("cmd", choices=["state", "list", "trigger", "advance", "logs"])
    p.add_argument("arg", nargs="?")
    a = p.parse_args(argv)
    if a.cmd == "state":
        print(json.dumps(load(a.root), indent=2, sort_keys=True))
    elif a.cmd == "list":
        print("\n".join(load(a.root)["jobs"]))
    elif a.cmd == "trigger":
        print(json.dumps(trigger(a.root, a.arg), sort_keys=True))
    elif a.cmd == "advance":
        print(advance(a.root, float(a.arg)))
    elif a.cmd == "logs":
        for r in load(a.root)["runs"]:
            if a.arg is None or r["job"] == a.arg:
                print(f"{r['vts_start']} {r['job']:<12} {r['status']}")


if __name__ == "__main__":
    sys.exit(main())
