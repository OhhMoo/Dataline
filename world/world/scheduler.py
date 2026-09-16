"""The world's scheduler: a virtual clock that actually runs the jobs.

Two files, and the split is deliberate.

`var/sched/state.json` is the agent-visible surface and the single time
authority: `vnow` plus run history. Its shape is FROZEN (agent.md 9.2) - the
gateway's `sched_state` / `sched_list` / `sched_logs` / `sched_trigger` read it,
and three built tasks depend on it. Nothing here changes `jobs` or `vnow`, and
a seeded history row is byte-for-byte what it always was.

`var/world/live.json` is the live-world control plane: the schedule, each job's
`exec` block, the arrival stream, and the traffic parameters carried over from
the seed. It lives OUTSIDE the agent-visible surface on purpose - no gateway
tool reads /var/world - so making the world evolve does not hand the agent the
generator's parameters. It is conditionally fingerprinted (fingerprint.py), so
it is still tamper-evident.

What `trigger` and `advance` now do:

- `trigger(root, job)` runs the job's real work through `executor.execute` and
  derives `status` from the result. A broken dbt model fails, carrying dbt's
  own error. A job with no `exec` block fails as `unsupported` - the one thing
  this module will never do is report a status it did not observe.
- `advance(root, hours)` materialises the span: new source partitions land and
  scheduled jobs fire, in virtual-time order, executing for real.

Determinism (agent.md 7.4), all of it load-bearing because state.json is
fingerprinted:

- one PRNG per (seed, day_index), drawn inside the theme's `write_day` hook, so
  day N's bytes do not depend on how you got to day N. `advance(1)` x 24 lands
  the same world as `advance(24)`.
- `vnow` after `advance` is exactly start + hours - never a function of how
  long a job ran - so advancing is path-independent.
- occurrences are deduplicated by (job, vts_start) against existing history, so
  re-advancing over a span already simulated is a no-op rather than a replay.
- ordered iteration everywhere: events sort on (vts, arrivals-before-jobs,
  name); jobs iterate in manifest order.
- no wall clock is read anywhere in this module or in executor.py.
- faults (faults.py) draw from one PRNG keyed on (fault_seed, fault_id,
  occurrence) and nothing else, so enabling a fault shifts no other fault's
  draws and `advance(1)` x N still lands the world `advance(N)` does. A late
  arrival whose new time falls past the span is *deferred* by absolute vts in
  live.json rather than recomputed, which is what makes the two paths agree.
- observability samples (observe.py) are appended after every executed run, so
  a fault leaves a trace in the record as well as in the world.
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import executor, faults as faultlib, observe
from .hashing import json_canon

STATE = Path("var/sched/state.json")
LIVE = Path("var/world/live.json")

MAX_EVENTS = 2000                      # refuse to simulate an unbounded span
MAX_HOURS = 24 * 366


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


# --------------------------------------------------------------------------
# live-world control plane
# --------------------------------------------------------------------------

def live_path(root: Path) -> Path:
    return Path(root) / LIVE


def load_live(root: Path) -> dict | None:
    path = live_path(root)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_live(root: Path, live: dict) -> None:
    path = live_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_canon(live) + b"\n")


def live_seed(live: dict | None) -> dict | None:
    """The seed-shaped dict a theme's gen hooks expect, rebuilt from live.json.

    None when the live block does not describe a generated stream. A TASK world
    carries a live block for one reason - to give its jobs `exec` specs, which
    is what `_execute` reads - while its data is pre-built by a fixture
    generator rather than landed day by day. Such a block has no `vtime_start`
    and no `days_landed`, and demanding them raised KeyError before any job
    could run, which is why a task's jobs could not execute at all.
    """
    if not live or "vtime_start" not in live or "days_landed" not in live:
        return None
    return {**live.get("params", {}), "vtime_start": live["vtime_start"],
            "days": live["days_landed"]}


def live_gen(live: dict | None):
    """The theme's gen hooks, or None when the live block names no theme.

    Only `_kind_theme` uses these; a job that is `sql`, `script`, `dbt` or
    `scan` never touches them. `_execute` builds them eagerly though, so a live
    block without a `theme` - which is what a task world has, since a task is
    not a registered theme - used to raise KeyError before the executor was
    ever reached.
    """
    if not live or not live.get("theme"):
        return None
    from .themes import load_theme          # local: gen hooks import this module
    return load_theme(live["theme"]).gen


def initial_live(seed: dict, theme) -> dict | None:
    """Seed var/world/live.json from the theme manifest, or None if it has no
    `live:` block (a theme without one builds a static world, as before)."""
    spec = theme.manifest.get("live")
    if not spec:
        return None
    live = {
        "theme": theme.name,
        "vtime_start": seed["vtime_start"],
        "days_landed": seed["days"],
        # only the seed keys the theme DECLARES it needs are carried into the
        # world - an explicit, auditable list, not the whole seed
        "params": {k: seed[k] for k in spec.get("params", []) if k in seed},
        "arrival": spec.get("arrival"),
        "jobs": theme.manifest["jobs"],
    }
    # The fault catalogue and its bookkeeping appear ONLY when the theme
    # declares faults, on the same conditional-section rule as everything else
    # here: a theme with no `faults:` block produces a byte-identical live.json
    # to the one it produced before this layer existed.
    if spec.get("faults"):
        live["faults"] = faultlib.catalogue(spec["faults"], seed.get("faults"))
        live["fault_seed"] = seed["seed"]
        # days_landed counts days whose data actually landed; days_scheduled is
        # the arrival stream's cursor. They diverge the moment a partition goes
        # missing, and conflating them is how a gap silently heals itself.
        live["days_scheduled"] = seed["days"]
        live["deferred_arrivals"] = {}
        live["deferred_runs"] = {}
        live["fault_log"] = []
    return live


# --------------------------------------------------------------------------
# schedule
# --------------------------------------------------------------------------

def job_starts(jobs: dict, day: datetime) -> dict:
    """Each job's start time for the run-day `day`.

    `time` + `day_offset` place a job relative to the day it processes; an
    `after` job starts that many minutes after the job it names. This is the
    single definition of the schedule - `initial_state` (history) and `advance`
    (future) both call it, so a simulated run lands on exactly the slot the
    history would have used.

    Dependencies are resolved by repeated passes, NOT by mapping order: the
    manifest preserves author order but live.json is canonical JSON, which
    sorts keys, and `dbt_build` sorting ahead of the job it waits on must not
    change the schedule. Order-independence here is what makes the two agree.
    """
    starts, pending = {}, dict(jobs)
    while pending:
        progressed = False
        for name in sorted(pending):
            after = pending[name].get("after")
            if after:
                if after["job"] not in starts:
                    continue
                at = starts[after["job"]] + timedelta(minutes=after["minutes"])
            else:
                spec = pending[name]
                hh, mm = (int(x) for x in spec["time"].split(":"))
                at = day + timedelta(days=spec["day_offset"], hours=hh, minutes=mm)
            starts[name] = at
            del pending[name]
            progressed = True
        if not progressed:
            raise SystemExit("unresolvable job schedule (missing or circular "
                             "`after`): " + ", ".join(sorted(pending)))
    return starts


# --------------------------------------------------------------------------
# fault-shaped run text. These strings land in var/sched/state.json, which the
# agent reads through sched_state / sched_logs, so they must read like the
# engine that would really have produced them - and they must NOT name the
# fault that caused them. A real scheduler says "killed at the timeout"; it
# does not say "fault vendor_late_delivery fired". The ledger that names the
# fault is var/world/live.json's `fault_log`, which no gateway tool reads.
# --------------------------------------------------------------------------

RETRY_ERROR = "connection reset by peer"


def _timeout_error(timeout_s: int) -> str:
    return (f"job exceeded its {timeout_s}s timeout and was killed (SIGKILL) "
            f"before it produced any output")


def _overrun_seconds(duration_s: int, params: dict) -> int:
    return (int(duration_s * float(params.get("multiplier", 1.0)))
            + int(params.get("extra_minutes", 0)) * 60)


def _history_run(name, at, duration_s, detail, status="success", error=None,
                 try_number=None, max_tries=None) -> dict:
    run = {"job": name, "vts_start": iso(at),
           "vts_end": iso(at + timedelta(seconds=duration_s)),
           "status": status, "detail": detail}
    # Extra keys appear ONLY on a faulted row, so an unfaulted history is
    # byte-identical to the one this function produced before faults existed -
    # which is what GT001 (which calls initial_state directly) depends on.
    if error is not None:
        run["error"] = error
    if try_number is not None:
        run["try_number"] = try_number
        run["max_tries"] = max_tries
    return run


def initial_state(seed: dict, jobs: dict, faults: list | None = None,
                  fault_seed=0) -> dict:
    """vnow = start + days; one successful run per manifest job per past day.

    This is the world's backstory, not an execution: the history describes a
    pipeline that ran every night before the episode began. Its shape is frozen
    and unchanged - `jobs` carries only {command, duration_s}, so `sched_list`
    returns exactly what it always did.

    `faults` is opt-in (a theme's `live.faults_in_history`) and carries the
    faults whose end state is the landing zone the builder actually wrote: the
    JOB faults timeout / retry / overrun, and the one ARRIVAL fault that heals,
    `late_arrival`. `missing_arrival`, `duplicate_arrival`, `torn_write` and
    `partial_write` each leave a permanent data divergence the seeded world
    does not carry, so they are skipped here - their draw is still consumed, so
    enabling one shifts nothing else, and the night records clean. faults.py's
    docstring carries the full argument and what carrying them would cost.

    What is carried coheres with the built data for one reason: all four themes
    reload every partition each night, so a night that failed - because the
    scheduler killed the job, or because the delivery was not there yet - heals
    on the next night, which is exactly what the final warehouse shows.

    History day `i` IS arrival day_index `i`: `_arrival_at` and this loop both
    count days from `vtime_start` and let the arrival and the job add their own
    `day_offset`. So the same draw governs the same calendar day in the
    backstory and in `advance`, and the two never overlap - `advance` starts its
    cursor at `days_scheduled`, which is `seed["days"]`.

    With `faults=None` (the default, and every world that does not opt in) this
    is byte-for-byte the function it has always been.
    """
    start = parse_vtime(seed["vtime_start"])
    ctx = {"faults": faults or [], "fault_seed": fault_seed}
    if faults:
        faultlib.check_history(faults, jobs)
    runs = []
    for i in range(seed["days"]):
        day = start + timedelta(days=i)
        starts = job_starts(jobs, day)
        # One decision per day, shared by every job that read the delivery -
        # one late vendor upload is one incident, not one per consumer.
        arrival = faultlib.arrival_decision(ctx, i) if faults else None
        for name, spec in jobs.items():
            at = starts[name]
            detail = (spec["detail"].format(day=day.date().isoformat())
                      if spec.get("detail") else None)
            fault = (faultlib.job_decision(ctx, name, iso(at))
                     if faults else None)
            kind = fault["kind"] if fault else None
            params = fault["params"] if fault else {}
            # What this job found when it read the delivery, or None if the
            # day's arrival was fine or did not concern it. A run that really
            # executed and found nothing FAILS; one the scheduler never started
            # is untouched, because it never got as far as looking.
            missed = faultlib.missed_by(arrival, name,
                                        executor.day_tokens(day.date()))
            status = "failed" if missed else "success"
            if kind == "job_timeout":
                timeout_s = int(params.get("timeout_s", spec["duration_s"]))
                runs.append(_history_run(
                    name, at, timeout_s, detail, "failed",
                    params.get("error") or _timeout_error(timeout_s)))
                continue
            if kind == "job_retry":
                attempts = max(2, int(params.get("attempts", 2)))
                delay = int(params.get("retry_delay_minutes", 10))
                message = params.get("error") or RETRY_ERROR
                for n in range(1, attempts):
                    runs.append(_history_run(
                        name, at, int(params.get("fail_after_s", 5)), detail,
                        "failed", message, n, attempts))
                    at = at + timedelta(minutes=delay)
                # The attempt that really reached the work is the one the
                # missing delivery can bite.
                runs.append(_history_run(name, at, spec["duration_s"], detail,
                                         status, missed,
                                         try_number=attempts,
                                         max_tries=attempts))
                continue
            if kind == "job_overrun":
                runs.append(_history_run(
                    name, at, _overrun_seconds(spec["duration_s"], params),
                    detail, status, missed))
                continue
            runs.append(_history_run(name, at, spec["duration_s"], detail,
                                     status, missed))
    job_defs = {n: {"command": s["command"], "duration_s": s["duration_s"]}
                for n, s in jobs.items()}
    return {"vnow": iso(start + timedelta(days=seed["days"])),
            "jobs": job_defs, "runs": runs}


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------

def _run_record(job: str, at: datetime, duration_s: int, result: dict,
                trigger: str, detail: str | None,
                execution_date: str | None = None) -> dict:
    """The run record. `status` is derived from `result`, never asserted, and
    `executed` says whether the job's declared work actually ran - the
    difference between "ran and failed" and "never got as far as running".

    Three things set `executed: false`, and the `error` text is what separates
    them: the job declares no executable work ("...declares no executable
    work..."), the scheduler killed it at its timeout, or a transient transport
    failure ended the attempt before it reached the work. In every one of them
    `action` is null and `effects` empty, so the record never implies output
    that was not produced.
    """
    return {
        "job": job,
        "vts_start": iso(at),
        "vts_end": iso(at + timedelta(seconds=duration_s)),
        "status": "success" if result["ok"] else "failed",
        "detail": detail if detail is not None else result.get("detail"),
        "trigger": trigger,
        "execution_date": execution_date,
        "executed": result.get("error_kind") != "unsupported",
        "action": result.get("detail"),
        "error": result.get("error"),
        "effects": result.get("effects") or {},
    }


def _execute(root: Path, state: dict, live: dict | None, job: str,
             variables: dict | None = None, day: str | None = None) -> dict:
    """Run one job's declared work. `day` is the EXECUTION day, not the wall
    date - the day whose delivery this occurrence is responsible for.

    It is the same value that goes into the run record's `execution_date` and
    into `detail`'s `{day}`, and a `scan` job scopes its glob with it. Without
    it every scan globbed its delivery tree cumulatively, which is why no poll
    in this world could fail after day one.
    """
    spec = ((live or {}).get("jobs", {}).get(job) or {}).get("exec")
    return executor.execute(
        root, job, spec, gen=live_gen(live) if spec else None,
        theme=(live or {}).get("theme"), seed=live_seed(live),
        variables=variables, day=day,
        log_name=f"run-{len(state['runs']) + 1:06d}")


def trigger(root: Path, job: str, variables: dict | None = None,
            execution_date: str | None = None) -> dict:
    """Run `job` now, for real, and record what happened.

    A manual trigger scopes the same way a scheduled run does: `execution_date`
    IS the logical day (`sched_trigger`: "Execute a job for an explicit logical
    date"), so it supplies the execution day a `scan` job polls for. An
    explicit `variables["day"]` wins, so an agent can aim a poll at a date
    other than the one it is filing the run under.
    """
    root = Path(root)
    state = load(root)
    if job not in state["jobs"]:
        raise SystemExit(f"unknown job: {job} (have: {', '.join(state['jobs'])})")
    live = load_live(root)
    start = parse_vtime(state["vnow"])
    day = (variables or {}).get("day") or (
        str(execution_date)[:10] if execution_date else None)
    result = _execute(root, state, live, job, variables, day)
    run = _run_record(job, start, state["jobs"][job]["duration_s"], result,
                      "manual", None, execution_date)
    state["runs"].append(run)
    state["vnow"] = run["vts_end"]
    save(root, state)
    observe.record(root, state, parse_vtime(run["vts_end"]), "trigger",
                   observe.run_id(len(state["runs"]), job))
    return run


# --------------------------------------------------------------------------
# time and traffic
# --------------------------------------------------------------------------

def _arrival_at(live: dict, day_index: int) -> datetime:
    at = live["arrival"]["at"]
    hh, mm = (int(x) for x in at.get("time", "00:00").split(":"))
    return (parse_vtime(live["vtime_start"])
            + timedelta(days=day_index + at.get("day_offset", 0),
                        hours=hh, minutes=mm))


def _fault_by_id(live: dict, fault_id: str) -> dict | None:
    for fault in faultlib.declared(live):
        if fault["id"] == fault_id:
            return fault
    return None


def _plan_arrivals(live: dict, start: datetime, target: datetime):
    """Arrival events for (start, target], plus the cursor and deferral map.

    Pure: computes, mutates nothing - `advance` commits what it returns.

    Two cursors, and the split is the whole trick to path-independence. A late
    arrival whose new time lands past the span is written into
    `deferred_arrivals` as an ABSOLUTE vts and replayed from there, so
    `advance(1)` thirty times lands it on exactly the instant `advance(30)`
    does. A missing arrival advances `days_scheduled` and not `days_landed`,
    so the generator does not quietly fill the gap on the next pass.
    """
    name = live["arrival"].get("name", "arrival")
    events = []
    cursor = int(live.get("days_scheduled", live["days_landed"]))
    deferred = dict(live.get("deferred_arrivals") or {})

    for key in sorted(deferred, key=int):
        vts = parse_vtime(deferred[key]["vts"])
        if vts <= target:
            events.append({"kind": "arrival", "vts": vts, "name": name,
                           "day_index": int(key), "late": True,
                           "fault": _fault_by_id(live, deferred[key]["fault"])})
            del deferred[key]

    while _arrival_at(live, cursor) <= target:
        base = _arrival_at(live, cursor)
        fault = faultlib.arrival_decision(live, cursor)
        kind = fault["kind"] if fault else None
        if kind == "missing_arrival":
            # No data, no event to execute: a gap, recorded as one.
            events.append({"kind": "arrival_missed", "vts": base, "name": name,
                           "day_index": cursor, "fault": fault, "late": False})
        elif kind == "late_arrival":
            vts = base + faultlib.delay_for(fault)
            if vts <= target:
                events.append({"kind": "arrival", "vts": vts, "name": name,
                               "day_index": cursor, "late": True, "fault": fault})
            else:
                deferred[str(cursor)] = {"vts": iso(vts), "fault": fault["id"]}
        else:
            events.append({"kind": "arrival", "vts": base, "name": name,
                           "day_index": cursor, "late": False, "fault": fault})
        cursor += 1
        if len(events) > MAX_EVENTS:
            break
    return events, cursor, deferred


def _run_key(event: dict) -> str:
    return f"{event['name']}@{iso(event['vts'])}"


def _plan_deferred_runs(live: dict, target: datetime):
    """Retry attempts parked past a previous span's end. Returns (due, still)."""
    parked = dict(live.get("deferred_runs") or {})
    due = []
    for key in sorted(parked):
        entry = parked[key]
        vts = parse_vtime(entry["vts"])
        if vts <= target:
            due.append({"kind": "job", "vts": vts, "name": entry["job"],
                        "day_index": entry["day_index"],
                        "retry": entry["retry"]})
            del parked[key]
    return due, parked


def _plan_jobs(state: dict, live: dict, start: datetime,
               target: datetime) -> list:
    vstart = parse_vtime(live["vtime_start"])
    seen = {(r["job"], r["vts_start"]) for r in state["runs"]}
    events = []
    first = max(0, (start - vstart).days - 2)
    last = (target - vstart).days + 2
    for day_index in range(first, last + 1):
        day = vstart + timedelta(days=day_index)
        starts = job_starts(live["jobs"], day)
        for name in live["jobs"]:
            at = starts[name]
            if start < at <= target and (name, iso(at)) not in seen:
                events.append({"kind": "job", "vts": at, "name": name,
                               "day_index": day_index})
    return events


def _ordered(events: list) -> list:
    # arrivals sort before jobs at the same instant: data lands, then loads run
    events.sort(key=lambda e: (e["vts"], 1 if e["kind"] == "job" else 0,
                               e["name"]))
    if len(events) > MAX_EVENTS:
        raise SystemExit(
            f"advance would simulate {len(events)} events (cap {MAX_EVENTS}); "
            "advance in smaller steps")
    return events


def plan(state: dict, live: dict, start: datetime, target: datetime) -> list:
    """The ordered event list for (start, target]. Pure - runs nothing."""
    arrivals = _plan_arrivals(live, start, target)[0] if live.get("arrival") else []
    return _ordered(arrivals + _plan_deferred_runs(live, target)[0]
                    + _plan_jobs(state, live, start, target))


def _fault_log(live: dict, vts: datetime, fault: dict, target: str,
               detail: dict) -> None:
    """The author-side ledger of what fired, in var/world/live.json.

    Deliberately NOT agent-visible: no gateway tool reads /var/world. The agent
    meets the incident the way a real operator does - a failed run, a gap in a
    landing zone, a truncated file - and has to work out what happened. The
    ledger is how the task author proves afterwards that the incident was the
    one they declared, and it is inside the semantic fingerprint, so it is
    tamper-evident.
    """
    live.setdefault("fault_log", []).append({
        "vts": iso(vts), "fault": fault["id"], "kind": fault["kind"],
        "target": target, "detail": detail})


def _land(root: Path, live: dict, gen, seed: dict, event: dict, day):
    """Run the arrival hook, then apply any fault to the bytes it just wrote.

    Which files the day produced is learned by diffing the set of file PATHS
    under workspace/data across the hook - never mtimes, never sizes, never
    directory order. That keeps the fault layer generic over themes without
    every theme having to declare its own output paths.
    """
    workspace = root / "workspace"
    fault = event.get("fault")
    alters_bytes = bool(fault) and fault["kind"] in ("duplicate_arrival",
                                                     "torn_write")
    watch = workspace / "data"
    before = faultlib.file_index(watch) if alters_bytes else set()
    produced = getattr(gen, live["arrival"]["op"])(
        workspace, seed, event["day_index"], day)
    live["days_landed"] = max(live["days_landed"], event["day_index"] + 1)
    effect = {}
    if alters_bytes:
        effect = faultlib.apply_arrival_effect(
            watch, fault, before, faultlib.file_index(watch))
    if fault:
        _fault_log(live, event["vts"], fault, event["name"],
                   {"day": day.isoformat(), "late": bool(event.get("late")),
                    **effect})
    return (produced if isinstance(produced, dict) else None), effect


def _simulate_job(root: Path, state: dict, live: dict, event: dict,
                  day) -> list:
    """Run one job occurrence, under whatever fault applies.

    Appends to state["runs"] and returns `(runs_appended, followup_event)`.
    `followup_event` is a retry attempt that has to be scheduled back into the
    event queue at its own time; it is None for every other outcome.

    Every branch derives its status from something: a real execution, or the
    fault the world declared. None of them asserts success.
    """
    job = event["name"]
    spec = live["jobs"][job]
    day_iso = day.isoformat()
    detail = (spec["detail"].format(day=day_iso) if spec.get("detail") else None)
    duration = int(spec["duration_s"])
    at = event["vts"]
    retry = event.get("retry")
    # A retry attempt is not a fresh occurrence: it does not redraw. Drawing
    # again would let a retry retry itself, and would make the outcome depend
    # on how many attempts had already happened rather than on the seed.
    fault = None if retry else faultlib.job_decision(live, job, iso(at))
    kind = fault["kind"] if fault else None
    params = fault["params"] if fault else {}
    made = []

    def emit(run):
        if retry:
            run["try_number"] = retry["try_number"]
            run["max_tries"] = retry["max_tries"]
        state["runs"].append(run)
        made.append(run)
        return run

    if retry and retry["try_number"] < retry["max_tries"]:
        # More attempts to burn before the one that really runs.
        declared = _fault_by_id(live, retry["fault"]) or {"params": {}}
        rparams = declared["params"]
        result = {"ok": False, "detail": None, "effects": {},
                  "error_kind": "transient",
                  "error": rparams.get("error") or RETRY_ERROR}
        run = _run_record(job, at, int(rparams.get("fail_after_s", 5)), result,
                          "schedule", detail, day_iso)
        run["executed"] = False
        emit(run)
        return made, {**event,
                      "vts": at + timedelta(
                          minutes=int(rparams.get("retry_delay_minutes", 10))),
                      "retry": {**retry, "try_number": retry["try_number"] + 1}}

    if kind == "job_timeout":
        # The work does NOT run: the scheduler killed it. `executed: false`
        # with an empty `action` and empty `effects` says exactly that, and no
        # part of the record claims output that was never produced.
        timeout_s = int(params.get("timeout_s", duration))
        result = {"ok": False, "detail": None, "effects": {},
                  "error_kind": "timeout",
                  "error": params.get("error") or _timeout_error(timeout_s)}
        run = _run_record(job, at, timeout_s, result, "schedule", detail, day_iso)
        run["executed"] = False
        emit(run)
        _fault_log(live, at, fault, job, {"timeout_s": timeout_s})
        return made, None

    if kind == "job_retry":
        # One attempt that never reached the work, then a follow-up event at
        # vts + retry_delay. The follow-up goes back into the event queue
        # rather than running here, so it interleaves with everything else at
        # its real time - a downstream job scheduled inside the retry window
        # genuinely runs before the retry lands, which is the whole reason a
        # retry is interesting. If it falls past the span it is deferred by
        # absolute vts, exactly as a late arrival is.
        attempts = max(2, int(params.get("attempts", 2)))
        delay = int(params.get("retry_delay_minutes", 10))
        message = params.get("error") or RETRY_ERROR
        result = {"ok": False, "detail": None, "effects": {},
                  "error_kind": "transient", "error": message}
        run = _run_record(job, at, int(params.get("fail_after_s", 5)),
                          result, "schedule", detail, day_iso)
        run["executed"] = False
        run["try_number"] = 1
        run["max_tries"] = attempts
        emit(run)
        _fault_log(live, at, fault, job,
                   {"attempts": attempts, "retry_delay_minutes": delay})
        return made, {"kind": "job", "vts": at + timedelta(minutes=delay),
                      "name": job, "day_index": event["day_index"],
                      "retry": {"try_number": 2, "max_tries": attempts,
                                "fault": fault["id"]}}

    if kind == "job_overrun":
        # The work runs for real and its status is derived as always; only the
        # recorded window stretches, over the next occurrence or past vnow.
        # This is the staged interleave: the evidence of a race, never a race.
        result = _execute(root, state, live, job, day=day_iso)
        stretched = _overrun_seconds(duration, params)
        emit(_run_record(job, at, stretched, result, "schedule", detail, day_iso))
        _fault_log(live, at, fault, job,
                   {"duration_s": stretched, "nominal_duration_s": duration})
        return made, None

    if kind == "partial_write":
        result = _execute(root, state, live, job, day=day_iso)
        trimmed = faultlib.apply_partial_write(root, params) if result["ok"] else None
        if trimmed:
            tables = (spec.get("exec") or {}).get("tables") or []
            after = executor.table_rows(root, tables)
            effects = {k: dict(v) if isinstance(v, dict) else v
                       for k, v in (result.get("effects") or {}).items()}
            for name in tables:
                row = effects.get(name)
                if isinstance(row, dict):
                    row["rows_after"] = after.get(name)
            result = {**result, "ok": False, "error_kind": "partial_write",
                      "effects": effects,
                      "error": params.get("error")
                      or "loader process exited with code 137 (killed)"}
        emit(_run_record(job, at, duration, result, "schedule", detail, day_iso))
        _fault_log(live, at, fault, job, {"trimmed": trimmed})
        return made, None

    result = _execute(root, state, live, job, day=day_iso)
    emit(_run_record(job, at, duration, result, "schedule", detail, day_iso))
    return made, None


def advance(root: Path, hours: float) -> dict:
    """Move the clock by `hours`, materialising everything in the span.

    Returns {"vnow", "live", "events": [...]}. With no live-world control plane
    (var/world/live.json absent - a hand-built fixture world, for instance) the
    clock still moves, `live` is False and `events` is empty: the world says it
    simulated nothing rather than pretending it did.
    """
    root = Path(root)
    if not 0 <= hours <= MAX_HOURS:
        raise SystemExit(f"advance hours must be within [0, {MAX_HOURS}]")
    state = load(root)
    live = load_live(root)
    start = parse_vtime(state["vnow"])
    target = start + timedelta(hours=hours)

    if live is None:
        state["vnow"] = iso(target)
        save(root, state)
        return {"vnow": state["vnow"], "live": False, "events": [],
                "note": "no live-world control plane (var/world/live.json): "
                        "the clock moved, nothing was executed"}

    gen, seed = live_gen(live), live_seed(live)
    arrivals, cursor, deferred = ([], None, None)
    if live.get("arrival"):
        arrivals, cursor, deferred = _plan_arrivals(live, start, target)
    # Retry attempts left hanging past the end of a previous span, replayed by
    # absolute vts. Without this a retry that straddles an advance boundary
    # would be lost and the occurrence would never complete - the job would
    # have failed once and then silently never run again.
    pending_runs, runs_later = _plan_deferred_runs(live, target)
    log = []
    queue = _ordered(arrivals + pending_runs
                     + _plan_jobs(state, live, start, target))
    index = 0
    while index < len(queue):
        event = queue[index]
        index += 1
        day = (parse_vtime(live["vtime_start"])
               + timedelta(days=event["day_index"])).date()
        if event["kind"] == "arrival_missed":
            _fault_log(live, event["vts"], event["fault"], event["name"],
                       {"day": day.isoformat(), "day_index": event["day_index"]})
            log.append({"kind": "arrival_missed", "vts": iso(event["vts"]),
                        "name": event["name"], "day": day.isoformat(),
                        "produced": None})
        elif event["kind"] == "arrival":
            produced, effect = _land(root, live, gen, seed, event, day)
            seed = live_seed(live)
            log.append({"kind": "arrival", "vts": iso(event["vts"]),
                        "name": event["name"], "day": day.isoformat(),
                        "late": bool(event.get("late")), "produced": produced,
                        **({"fault_effect": effect} if effect else {})})
        else:
            base_index = len(state["runs"])
            runs, followup = _simulate_job(root, state, live, event, day)
            for offset, run in enumerate(runs, start=1):
                observe.record(root, state, parse_vtime(run["vts_end"]),
                               "advance",
                               observe.run_id(base_index + offset, run["job"]))
                log.append({"kind": "job", "vts": run["vts_start"],
                            "name": run["job"], "status": run["status"],
                            "error": run["error"],
                            **({"try_number": run["try_number"]}
                               if run.get("try_number") else {})})
            if followup is not None:
                if followup["vts"] <= target:
                    # back into the queue at its real time, so anything
                    # scheduled inside the retry window runs first
                    queue = queue[:index] + _ordered(queue[index:] + [followup])
                else:
                    runs_later[_run_key(followup)] = {
                        "vts": iso(followup["vts"]), "job": followup["name"],
                        "day_index": followup["day_index"],
                        "retry": followup["retry"]}
    # vnow is exactly start + hours: never a function of how long a job ran, so
    # advance(1) x N and advance(N) leave the same clock.
    state["vnow"] = iso(target)
    if faultlib.declared(live):
        live["days_scheduled"] = (cursor if cursor is not None
                                  else live["days_landed"])
        live["deferred_arrivals"] = deferred or {}
        live["deferred_runs"] = runs_later
    save(root, state)
    save_live(root, live)
    return {"vnow": state["vnow"], "live": True, "events": log}


def main(argv=None):
    p = argparse.ArgumentParser(prog="world.scheduler")
    p.add_argument("root", type=Path)
    p.add_argument("cmd", choices=["state", "list", "trigger", "advance",
                                   "logs", "plan", "live"])
    p.add_argument("arg", nargs="?")
    a = p.parse_args(argv)
    if a.cmd == "state":
        print(json.dumps(load(a.root), indent=2, sort_keys=True))
    elif a.cmd == "list":
        print("\n".join(load(a.root)["jobs"]))
    elif a.cmd == "trigger":
        print(json.dumps(trigger(a.root, a.arg), indent=2, sort_keys=True))
    elif a.cmd == "advance":
        print(json.dumps(advance(a.root, float(a.arg)), indent=2, sort_keys=True))
    elif a.cmd == "live":
        print(json.dumps(load_live(a.root), indent=2, sort_keys=True))
    elif a.cmd == "plan":
        state, live = load(a.root), load_live(a.root)
        if live is None:
            print("no live-world control plane; advance moves the clock only")
            return
        start = parse_vtime(state["vnow"])
        for e in plan(state, live, start, start + timedelta(hours=float(a.arg))):
            print(f"{iso(e['vts'])} {e['kind']:<8} {e['name']}")
    elif a.cmd == "logs":
        for r in load(a.root)["runs"]:
            if a.arg is None or r["job"] == a.arg:
                print(f"{r['vts_start']} {r['job']:<12} {r['status']}"
                      + (f"  {r['error'].splitlines()[0]}" if r.get("error") else ""))


if __name__ == "__main__":
    sys.exit(main())
