#!/usr/bin/env python3
"""The one definition of what `workspace/orchestration/schedule.yml` means.

The file is the agent-editable deployment source of truth for WHICH jobs run,
WHEN they start and WHAT script each one executes. It carries no runtime: how
long a job takes is measured by the scheduler and lives in var/sched/state.json
(`jobs.<name>.duration_s`), which no gateway tool can write.

Unlike GT007, the job set is NOT fixed by the world. A migration adds a writer
and retires one, so the agent may:

  * add a job (any name, `script:` under workspace/, `time:` or `after:`);
  * retire a job (`enabled: false`, or delete its block);
  * change when a job starts.

Both tests/build_world.py (which deploys the shipped schedule into the world)
and tests/grade.py (which re-deploys the AGENT's schedule into a throwaway copy
and advances the clock) call `derive_jobs` here, so the world the agent edits
and the world the grader executes cannot be two different things.

Nothing here reads a wall clock, a host path or an environment variable.
"""
from __future__ import annotations

from pathlib import Path

import yaml

# The tokens world/world/executor.py:_kind_script substitutes into `args`.
# `{db}` is the PRIMARY warehouse file (nova), `{workspace}` the repo, `{root}`
# the world root - which is how a script reaches var/warehouse/legacy.duckdb.
ARG_TOKENS = ("db", "workspace", "root")
DEFAULT_ARGS = ["{db}", "{workspace}"]


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


def _check_args(name: str, args) -> list[str]:
    if args is None:
        return list(DEFAULT_ARGS)
    if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise ScheduleError(f"jobs.{name}.args must be a list of strings")
    for arg in args:
        # a `{token}` the executor does not know raises KeyError inside the
        # scheduler and would abort the whole night; refuse it here instead
        try:
            arg.format(**{token: "" for token in ARG_TOKENS})
        except (KeyError, IndexError, ValueError) as exc:
            raise ScheduleError(
                f"jobs.{name}.args entry {arg!r} names a token the scheduler "
                f"does not know (known: {', '.join(ARG_TOKENS)}): {exc}") from exc
    return list(args)


PUBLISH_KINDS = ("table", "file")


def _check_publishes(name: str, raw):
    """`publishes:` - what this job hands to a downstream consumer.

    The WORLD writes one receipt per entry per run into the protected receipts
    ledger (var/ledger/receipts.jsonl); nothing in this task package writes it.
    Shape, from the ledger interface: a list of mappings with `target`, `kind`
    in (table, file) and an optional `consumer`. Absent means the job publishes
    nothing and no receipt is written - which is the one-warehouse default and
    why this key is optional.
    """
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise ScheduleError(f"jobs.{name}.publishes must be a non-empty list")
    out = []
    for i, entry in enumerate(raw):
        where = f"jobs.{name}.publishes[{i}]"
        if not isinstance(entry, dict):
            raise ScheduleError(f"{where} must be a mapping")
        target = entry.get("target")
        if not isinstance(target, str) or not target.strip():
            raise ScheduleError(f"{where} declares no `target:`")
        kind = entry.get("kind", "table")
        if kind not in PUBLISH_KINDS:
            raise ScheduleError(f"{where}.kind is {kind!r}; expected one of "
                                f"{', '.join(PUBLISH_KINDS)}")
        row = {"target": target.strip(), "kind": kind}
        consumer = entry.get("consumer")
        if consumer is not None:
            if not isinstance(consumer, str) or not consumer.strip():
                raise ScheduleError(f"{where}.consumer must be a name")
            row["consumer"] = consumer.strip()
        out.append(row)
    return out


def enabled_jobs(doc: dict) -> dict:
    """The job blocks that are switched on, in file order."""
    raw = doc.get("jobs")
    if not isinstance(raw, dict):
        raise ScheduleError("schedule.yml has no `jobs:` mapping")
    out = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            raise ScheduleError(f"jobs.{name} must be a mapping")
        if spec.get("enabled", True) is False:
            continue
        out[str(name)] = spec
    return out


def derive_jobs(doc: dict, durations: dict, default_duration_s: int) -> dict:
    """schedule.yml + MEASURED durations -> the engine's `jobs` mapping.

    The engine (world/world/scheduler.py:job_starts) wants `time` +
    `day_offset`, or `after: {job, minutes}`. `duration_s` comes from
    `durations` - the scheduler's own measurements - and for a job the
    scheduler has never run, from `default_duration_s`. Never from the file.
    """
    raw = enabled_jobs(doc)
    if not raw:
        raise ScheduleError("schedule.yml enables no jobs at all")
    jobs = {}
    for name, spec in raw.items():
        if not name.replace("_", "").isalnum() or not name[0].isalpha():
            raise ScheduleError(f"job name {name!r} must be [a-z][a-z0-9_]*")
        script = spec.get("script")
        if not isinstance(script, str) or not script.strip():
            raise ScheduleError(f"jobs.{name} declares no `script:`; this world "
                                "runs python scripts under workspace/")
        script = script.replace("\\", "/").lstrip("./")
        if script.startswith("/") or ".." in script.split("/"):
            raise ScheduleError(f"jobs.{name}.script must be a relative path "
                                f"inside workspace/ (got {script!r})")
        out = {"command": name,
               "duration_s": int(durations.get(name, default_duration_s)),
               "detail": "dt={day}",
               "exec": {"kind": "script", "path": script,
                        "args": _check_args(name, spec.get("args")),
                        "detail": name}}
        publishes = _check_publishes(name, spec.get("publishes"))
        if publishes:
            out["publishes"] = publishes
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
            if after["job"] not in raw:
                raise ScheduleError(
                    f"jobs.{name}.after.job is {after['job']!r}, which is not "
                    f"an enabled job in this schedule")
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


def state_jobs(jobs: dict) -> dict:
    """The `jobs` block of var/sched/state.json (frozen shape: command,
    duration_s; plus the deployed start so sched_list shows the schedule)."""
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
