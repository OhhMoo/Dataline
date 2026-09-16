#!/usr/bin/env python3
"""The one definition of "what the repo's DAG file means".

`workspace/orchestration/schedule.yml` is the agent-editable deployment source
of truth for WHEN each job starts. It deliberately carries no runtime: how long
a job takes is MEASURED by the scheduler and lives in `var/sched/state.json`
(`jobs.<name>.duration_s`), which no gateway tool can write. That split is the
whole mechanism of this unit:

    the only knob in the repo that can move a landing time is a START time.
    `timeout_s` is the allotted window, is documentation, and moves nothing.

So an agent who concludes "the export ran long" has exactly one lever available
(widen `timeout_s`) and it provably cannot help; an agent who concludes "the
export started late" has the lever that can.

Both `tests/build_world.py` (which deploys the shipped schedule into the world)
and `tests/grade.py` (which re-deploys the AGENT's schedule into a throwaway
copy and advances the clock) call `derive_jobs` here, so the world the agent
edits and the world the grader executes cannot be two different things.

Nothing in this module reads a wall clock, a host path or an environment
variable. Same input bytes -> same output dict, on every host.
"""
from __future__ import annotations

from pathlib import Path

import yaml

# The job order is the chain order and is fixed by the world, not by the file:
# a schedule.yml that renames or drops a job is a schedule this world cannot
# deploy, and `validate` says so rather than silently deploying two thirds of a
# pipeline.
JOBS = ("vendor_sync", "build_commercial", "export_commercial")

EXEC = {
    "vendor_sync": {"kind": "script", "path": "pipelines/vendor_sync.py",
                    "args": ["{db}", "{workspace}"], "detail": "vendor_sync"},
    "build_commercial": {"kind": "script", "path": "pipelines/build_commercial.py",
                         "args": ["{db}", "{workspace}"], "detail": "build_commercial"},
    "export_commercial": {"kind": "script", "path": "pipelines/export_commercial.py",
                          "args": ["{db}", "{workspace}"], "detail": "export_commercial"},
}


class ScheduleError(ValueError):
    """A schedule.yml this world cannot deploy. Never a score of zero by
    accident: the caller turns it into a named, reported gate failure."""


def load(path: Path) -> dict:
    text = Path(path).read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        raise ScheduleError("schedule.yml must hold a mapping")
    return doc


def _hhmm(value, where: str) -> tuple[int, int]:
    try:
        hh, mm = (int(x) for x in str(value).split(":"))
    except Exception as exc:                                   # noqa: BLE001
        raise ScheduleError(f"{where}: {value!r} is not HH:MM") from exc
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ScheduleError(f"{where}: {value!r} is not a time of day")
    return hh, mm


def sla(doc: dict) -> tuple[str, str]:
    """(job, deadline HH:MM). The SLA is world policy, not an agent choice:
    callers compare the agent's against the CONTRACT and refuse a rewritten
    deadline rather than grading against it."""
    block = doc.get("sla")
    if not isinstance(block, dict):
        raise ScheduleError("schedule.yml has no `sla:` block")
    job, deadline = block.get("job"), block.get("deadline")
    if job not in JOBS:
        raise ScheduleError(f"sla.job must be one of {list(JOBS)}, got {job!r}")
    _hhmm(deadline, "sla.deadline")
    return str(job), str(deadline)


def derive_jobs(doc: dict, durations: dict) -> dict:
    """schedule.yml + MEASURED durations -> the engine's `jobs` mapping.

    The engine (world/world/scheduler.py:job_starts) wants `time` +
    `day_offset`, or `after: {job, minutes}`. `duration_s` comes from
    `durations` - the scheduler's own measurements - and NEVER from the file,
    which is what stops "declare the job faster" from being a repair.
    """
    raw = doc.get("jobs")
    if not isinstance(raw, dict):
        raise ScheduleError("schedule.yml has no `jobs:` mapping")
    missing = [j for j in JOBS if j not in raw]
    if missing:
        raise ScheduleError(f"schedule.yml no longer declares {missing}; this "
                            f"world deploys exactly {list(JOBS)}")
    extra = [j for j in raw if j not in JOBS]
    if extra:
        raise ScheduleError(f"schedule.yml declares unknown job(s) {extra}; "
                            f"this world deploys exactly {list(JOBS)}")

    jobs = {}
    for name in JOBS:
        spec = raw[name]
        if not isinstance(spec, dict):
            raise ScheduleError(f"jobs.{name} must be a mapping")
        if name not in durations:
            raise ScheduleError(f"no measured duration for {name}")
        out = {"command": name, "duration_s": int(durations[name]),
               # `{day}` is expanded by world.scheduler._simulate_job against
               # the run-day, so an advanced run's `detail` reads the same way
               # as a seeded one ("dt=2026-04-15") instead of leaking the
               # executor's script path into the operator-facing surface.
               "detail": "dt={day}",
               "exec": {k: (list(v) if isinstance(v, list) else v)
                        for k, v in EXEC[name].items()}}
        has_after, has_time = "after" in spec, "time" in spec
        if has_after == has_time:
            raise ScheduleError(
                f"jobs.{name} must declare exactly one of `time:` or `after:` "
                f"(it declares {'both' if has_after else 'neither'})")
        if has_after:
            after = spec["after"]
            if not isinstance(after, dict) or "job" not in after or \
                    "minutes" not in after:
                raise ScheduleError(f"jobs.{name}.after needs `job` and `minutes`")
            if after["job"] not in JOBS:
                raise ScheduleError(
                    f"jobs.{name}.after.job is {after['job']!r}, not a job in "
                    f"this world")
            if after["job"] == name:
                raise ScheduleError(f"jobs.{name} waits on itself")
            try:
                minutes = int(after["minutes"])
            except Exception as exc:                            # noqa: BLE001
                raise ScheduleError(
                    f"jobs.{name}.after.minutes must be an integer") from exc
            if minutes < 0:
                raise ScheduleError(f"jobs.{name}.after.minutes is negative")
            out["after"] = {"job": str(after["job"]), "minutes": minutes}
        else:
            hh, mm = _hhmm(spec["time"], f"jobs.{name}.time")
            out["time"] = f"{hh:02d}:{mm:02d}"
            out["day_offset"] = int(spec.get("day_offset", 0))
        jobs[name] = out
    return jobs


def timeouts(doc: dict) -> dict:
    """`timeout_s` per job, purely so the grader can REPORT that an agent moved
    it. Nothing consumes it: the engine has no timeout, which is exactly why
    widening one cannot make a late-starting job land earlier."""
    raw = doc.get("jobs") or {}
    out = {}
    for name in JOBS:
        spec = raw.get(name)
        if isinstance(spec, dict) and spec.get("timeout_s") is not None:
            try:
                out[name] = int(spec["timeout_s"])
            except (TypeError, ValueError):
                out[name] = spec["timeout_s"]
    return out


def state_jobs(jobs: dict) -> dict:
    """The `jobs` block of var/sched/state.json.

    The frozen state.json shape (agent.md 9.2) carries {command, duration_s};
    this world additionally publishes the deployed start so that `sched_list`
    shows an operator the schedule the scheduler is actually running, which is
    the surface the ran-long/started-late question is asked against.
    """
    out = {}
    for name, spec in jobs.items():
        row = {"command": spec["command"], "duration_s": spec["duration_s"]}
        if "after" in spec:
            row["after"] = dict(spec["after"])
        else:
            row["time"] = spec["time"]
            row["day_offset"] = spec["day_offset"]
        out[name] = row
    return out
