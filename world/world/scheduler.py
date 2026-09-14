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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .hashing import json_canon

STATE = Path("var/sched/state.json")


def parse_vtime(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _path(root: Path) -> Path:
    return Path(root) / STATE


def load(root: Path) -> dict:
    return json.loads(_path(root).read_text(encoding="utf-8"))


def save(root: Path, state: dict) -> None:
    _path(root).write_bytes(json_canon(state) + b"\n")


def initial_state(seed: dict, jobs: dict) -> dict:
    """vnow = start + days; one successful run per manifest job per past day.

    jobs maps name -> {command, duration_s, time, day_offset, after, detail}
    (theme.yaml). Run start = day + day_offset days + time, except `after`
    jobs start that many minutes after the referenced job's start (which must
    precede them in the mapping). detail formats {day} with the ISO date.
    """
    start = parse_vtime(seed["vtime_start"])
    runs = []
    for i in range(seed["days"]):
        day = start + timedelta(days=i)
        starts = {}
        for name, spec in jobs.items():
            if spec.get("after"):
                at = (starts[spec["after"]["job"]]
                      + timedelta(minutes=spec["after"]["minutes"]))
            else:
                hh, mm = (int(x) for x in spec["time"].split(":"))
                at = (day + timedelta(days=spec["day_offset"],
                                      hours=hh, minutes=mm))
            starts[name] = at
            detail = (spec["detail"].format(day=day.date().isoformat())
                      if spec.get("detail") else None)
            runs.append({"job": name, "vts_start": iso(at),
                         "vts_end": iso(at + timedelta(
                             seconds=spec["duration_s"])),
                         "status": "success", "detail": detail})
    job_defs = {n: {"command": s["command"], "duration_s": s["duration_s"]}
                for n, s in jobs.items()}
    return {"vnow": iso(start + timedelta(days=seed["days"])),
            "jobs": job_defs, "runs": runs}


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
