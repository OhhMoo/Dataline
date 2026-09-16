"""Headless Airflow, driven by this world's virtual clock (R004 section 7).

THE LOAD-BEARING RULE
---------------------
`var/sched/state.json` is the single time authority and the single record of
what executed. Nothing in this module writes it, and nothing in this module is
hashed by `fingerprint.semantic()`. The Airflow metadata DB is a **derived
catalogue and an intent queue**. If it drifts, it is rebuilt from the ledger.

WHAT THIS MODULE IS FOR
-----------------------
Two jobs, and the first one is the important one.

`due()` asks *Airflow's own timetable* which runs fall in a virtual span. That
buys cron semantics, `catchup`, `start_date` restriction, DST and any custom
timetable a theme ships, for free and correctly - and, decisively, it means the
DAG file is the schedule. An agent that edits `schedule="0 1 * * *"` changes
what runs. Under a projection from `theme.yaml` it would not, and "editing a
DAG does nothing" is the single worst thing this substrate could teach.

`run_one()` drives `DAG.test()` - Airflow's own in-process, scheduler-free task
loop, whose source comments call it exactly that - inside `vclock.at()`.

WHY catchup=True IS FORCED IN THE RESTRICTION
---------------------------------------------
`CronTriggerTimetable.next_dagrun_info` has exactly one wall-clock read and it
is on the `catchup=False` branch (R004 section 2.3):

    else:
        start_time_candidates = [self._align_to_prev(coerce_datetime(utcnow()))]

So we always enumerate with `catchup=True`, which is clock-free, and express a
DAG's declared `catchup=False` by *truncating the enumeration to the latest due
run* rather than by taking Airflow's `catchup=False` code path. Taking that
branch would work under `vclock` too, but it would make the result a function
of how many times something else had already called `utcnow()`.

WHERE `catchup=False` MAY BE HONOURED, AND WHERE IT MUST NOT BE
---------------------------------------------------------------
All four themes declare `catchup=False`. Measured, both majors, over the seven
virtual days after each world's `vnow`: the timetable yields **7** occurrences,
and truncating to the latest yields **1**. Today's `scheduler.advance()` runs
all 7, i.e. it behaves as `catchup=True`.

**`honour_catchup=True` must NOT be the default on the `advance()` path**, for
a reason that is structural rather than aesthetic:

  * it breaks path-independence. `advance(1)` seven times would truncate seven
    times and produce 7 runs; `advance(168)` would truncate once and produce 1.
    `advance(1) x N == advance(N)` is agent.md 7.4's load-bearing property and
    the acceptance battery checks it.
  * it desynchronises runs from arrivals. `scheduler._plan_arrivals` lands a
    partition per day over the whole span regardless; truncating only the jobs
    leaves six days of data on the floor with no run record - a hole the seeded
    history has no analogue for, so the agent would be reading an incoherent
    past.
  * it models the wrong situation. In a real deployment with the scheduler
    *running*, `catchup=True` and `catchup=False` produce identical run sets.
    The flag only differs on a **cold start** behind `start_date`: a scheduler
    waking from downtime, a DAG being unpaused, a `start_date` moved backwards.
    `advance(h)` is time passing with the scheduler up, not a cold start.

So: honour `catchup` at the cold-start boundary - when the ledger holds **no**
run at all for a dag_id, `catchup=False` starts at the latest occurrence rather
than at `start_date` - and ignore it on every subsequent `advance`. That is
Airflow's own meaning, it keeps the DAG file load-bearing (an agent that moves
`start_date` back a year and flips `catchup` sees 1 run versus 365), and it
fires only where the ledger is empty, so it cannot break path-independence.
`honour_catchup` therefore stays a parameter, defaulting True for the read-only
`agreement()` proof and passed False by `advance()`.

ALWAYS PASS logical_date
------------------------
`DagRun.generate_run_id` appends `get_random_string()` when `logical_date` is
None (R004 section 2.2, reproduced live: `manual__...+00:00_lDjsbpNJ`). That is
the one way to lose determinism by accident.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

MAX_ITER = 20000        # refuse to walk an unbounded timetable

# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def dagbag(dags_dir):
    """Parse a theme workspace's `dags/` folder. Returns the DagBag.

    `bag.import_errors` is part of the answer, not an exception: a theme whose
    DAG does not parse is a finding, and real Airflow surfaces exactly this in
    its `import_error` table and at `GET /api/v2/importErrors`.
    """
    from airflow.models.dagbag import DagBag
    return DagBag(dag_folder=str(dags_dir), include_examples=False,
                  read_dags_from_db=False)


# --------------------------------------------------------------------------
# what Airflow thinks is due
# --------------------------------------------------------------------------


def _restriction(dag, catchup=True):
    from airflow.timetables.base import TimeRestriction
    return TimeRestriction(earliest=dag.start_date, latest=dag.end_date,
                           catchup=catchup)


def airflow_occurrences(dag, start, target, honour_catchup=True):
    """Every DagRunInfo whose `run_after` lies in (start, target].

    Walks the timetable from the DAG's own `start_date` with `catchup=True`,
    which is the clock-free branch. When the DAG declares `catchup=False` and
    `honour_catchup`, only the latest due occurrence survives - which is what
    Airflow's own `catchup=False` means: do not backfill, run the current one.
    """
    r = _restriction(dag, catchup=True)
    last, out, n = None, [], 0
    while True:
        info = dag.timetable.next_dagrun_info(
            last_automated_data_interval=last, restriction=r)
        if info is None:
            break
        if info.run_after > target:
            break
        if info.run_after > start:
            out.append(info)
        last = info.data_interval
        n += 1
        if n > MAX_ITER:
            raise RuntimeError(
                "timetable for %s did not terminate within %d iterations"
                % (dag.dag_id, MAX_ITER))
    if honour_catchup and not dag.catchup and out:
        out = out[-1:]
    return out


# --------------------------------------------------------------------------
# what this world thinks is due
# --------------------------------------------------------------------------


def world_occurrences(jobs, vtime_start, start, target):
    """(job, at, day_index) for every manifest occurrence in (start, target].

    Mirrors `scheduler._plan_jobs` exactly, minus the dedup against run history
    (which is about replay, not about the schedule). `job_starts` is imported
    from the scheduler rather than reimplemented, because a second copy of the
    schedule rule is the bug this whole comparison exists to detect.
    """
    from .scheduler import job_starts
    out = []
    first = max(0, (start - vtime_start).days - 2)
    last = (target - vtime_start).days + 2
    for day_index in range(first, last + 1):
        day = vtime_start + _dt.timedelta(days=day_index)
        starts = job_starts(jobs, day)
        for name in jobs:
            at = starts[name]
            if start < at <= target:
                out.append((name, at, day_index))
    out.sort(key=lambda t: (t[1], t[0]))
    return out


def root_jobs(jobs):
    """Jobs with no `after` - the ones whose start time IS the schedule."""
    return [n for n, spec in jobs.items() if not spec.get("after")]


# --------------------------------------------------------------------------
# the after-chain: what Airflow's own task ordering would produce
# --------------------------------------------------------------------------


def project_run(dag, info, durations):
    """Airflow's task semantics + our `duration_s`. -> [(task_id, start, end, cls)]

    This is the projection that answers "could a bridge that delegates
    scheduling to Airflow reproduce the world's seeded history?", and the
    answer is on `after_chain_divergence`. The rules are Airflow's, not ours:

      start(root)   = run_after
      start(task)   = max(end(u) for u in upstream)     -- NOT `+ after.minutes`
      end(job)      = start + duration_s
      end(sensor)   = max(start, base + delta)          -- its own poke condition

    `base` is `data_interval.end`, which is what `TimeDeltaSensor._derive_base_
    time` uses in both majors (3.0.2 falls back to `dag_run.run_after` when the
    interval is empty; for `CronTriggerTimetable` they are the same instant).

    The walk is by sorted task_id within each ready layer, so it is ordered and
    reproducible rather than dependent on `dag.tasks` order.
    """
    base = info.data_interval.end if info.data_interval else info.run_after
    ends, out, done = {}, [], set()
    pending = {t.task_id: t for t in dag.tasks}
    while pending:
        progressed = False
        for task_id in sorted(pending):
            task = pending[task_id]
            upstream = sorted(task.upstream_task_ids)
            if any(u not in done for u in upstream):
                continue
            start = max([ends[u] for u in upstream], default=info.run_after)
            cls = type(task).__name__
            if cls.endswith("Sensor") and hasattr(task, "delta"):
                end = max(start, base + task.delta)
            else:
                end = start + _dt.timedelta(seconds=durations.get(task_id, 0))
            ends[task_id] = end
            out.append((task_id, start, end, cls))
            done.add(task_id)
            del pending[task_id]
            progressed = True
        if not progressed:
            raise RuntimeError("cycle in dag %s" % dag.dag_id)
    out.sort(key=lambda r: (r[1], r[0]))
    return out


def after_chain_divergence(dag, jobs, runs, start, target):
    """Per-job start/end delta between the ledger and `project_run`.

    THE RESULT THIS FUNCTION EXISTS TO RECORD (measured, 4 themes, 10 jobs,
    30 seeded nights each, Airflow 3.0.2; identical instants under 2.10.5):

        theme            job               after            start delta
        revenue_daily    daily_load        null                    0 s  30/30
        revenue_daily    dbt_build         daily_load +5        +255 s  30/30
        sftp_vendor      sftp_poll         null                    0 s  30/30
        sftp_vendor      vendor_load       sftp_poll +10        +580 s  30/30
        sftp_vendor      dbt_build         vendor_load +5       +820 s  30/30
        mainframe_batch  nightly_extract   null                    0 s  30/30
        mainframe_batch  batch_load        nightly_extract +45     0 s  30/30
        mainframe_batch  dbt_build         batch_load +10       +480 s  30/30
        excel_close      close_import      null                    0 s  30/30
        excel_close      dbt_build         close_import +10     +540 s  30/30

    Two things to read off it.

    **The deltas are constant, not noisy.** Every one is a single histogram
    bucket across all thirty nights, and each equals the *accumulated idle time*
    the manifest declares: the world's cumulative offset from the root minus
    Airflow's critical-path length. Nothing here is a rounding artifact.

    **`batch_load` is exactly 0, and that is the whole finding.** It is the one
    `after:` job whose DAG spells the wait out - `TimeDeltaSensor(delta=45min)`
    between `nightly_extract` and `batch_load` - and where the DAG spells it
    out, Airflow reproduces the world's instant to the second. Where the theme
    leaves a bare `>>` edge, the gap has nowhere to live and vanishes. So
    `after.minutes` is not un-representable in Airflow; it is *un-written* in
    three of the four DAG files.

    Two ledger phenomena the projection cannot produce at all, at any delta:

      * `ledger_overlapping_pairs_same_night`: 8 in mainframe_batch's seeded
        history, 0 in the projection. `job_overrun` stretches batch_load's
        recorded window over dbt_build's start; under task-dependency ordering
        dbt_build cannot begin until batch_load ends, so an overlap is
        structurally impossible.
      * `extra_attempts_same_job_same_night`: 2 in sftp_vendor (`job_retry`),
        0 in the projection, which emits one row per task per occurrence.

    Hence the resolution this module recommends: **Airflow owns the occurrence
    instant; the world keeps owning the intra-run layout.** `job_starts()` and
    `initial_state()` are unchanged, `var/sched/state.json` keeps its frozen
    shape, and `after.minutes`/`duration_s` stay schedule inputs rather than
    becoming "recorded-window metadata only" as R004 section 10 guessed. The
    price is named in `LEDGER_IS_NOT_THE_DAG` below.
    """
    durations = {n: s["duration_s"] for n, s in jobs.items()}
    projected = {}
    for info in airflow_occurrences(dag, start, target, honour_catchup=False):
        for task_id, s, e, cls in project_run(dag, info, durations):
            projected.setdefault(task_id, []).append((s, e, cls))

    out = {}
    for name in sorted(jobs):
        ledger, seen = [], set()
        for r in sorted((r for r in runs if r["job"] == name),
                        key=lambda r: r["vts_start"]):
            day = r["vts_start"][:10]
            if day in seen:          # a retry is not a second occurrence
                continue
            seen.add(day)
            ledger.append(r)
        starts, ends, n = {}, {}, 0
        for r in ledger:
            day = r["vts_start"][:10]
            same = [(s, e) for s, e, _ in projected.get(name, [])
                    if s.strftime("%Y-%m-%d") == day]
            if not same:
                continue
            s, e = same[0]
            ds = int((_dt.datetime.fromisoformat(
                r["vts_start"].replace("Z", "+00:00")) - s).total_seconds())
            de = int((_dt.datetime.fromisoformat(
                r["vts_end"].replace("Z", "+00:00")) - e).total_seconds())
            starts[ds] = starts.get(ds, 0) + 1
            ends[de] = ends.get(de, 0) + 1
            n += 1
        out[name] = {"after": jobs[name].get("after"),
                     "duration_s": jobs[name]["duration_s"],
                     "compared_nights": n,
                     "start_delta_s": starts, "end_delta_s": ends}
    return out


# LEDGER_IS_NOT_THE_DAG - the price of the recommendation above, stated once.
#
# If Airflow owns only the occurrence instant, then editing a `>>` edge or a
# sensor's `delta` inside a DAG changes what *executes* (proven: dag.test runs
# the real tasks in the real order) but does NOT move the `(job, vts_start,
# vts_end)` rows the ledger records - those still come from `job_starts()`.
# That is a real divergence and it is the one GT007 sits on: its repair is a
# wait-offset edit whose whole effect is on recorded start times. Closing it
# means either (a) teaching the themes' DAGs to spell their waits out, as
# mainframe_batch already does, and reading the offsets back out of the DAG, or
# (b) accepting that the schedule lives in the manifest and the DAG is a
# faithful sibling of it, checked by `workspace/scripts/dag_check.py`.
# (b) is what ships today; (a) is a theme-file change and therefore a proposal.


# --------------------------------------------------------------------------
# the read-only proof
# --------------------------------------------------------------------------


def agreement(dags_dir, jobs, vtime_start, start, target):
    """Does Airflow's timetable agree with `job_starts()` over (start, target]?

    Agreement is defined on the set of *occurrence instants*: the DAG's
    `run_after` values against the start times of the manifest's root jobs.
    Anything else would be comparing two different things, because a theme's
    `jobs` map is finer-grained than its DAG (R004 section 10).
    """
    bag = dagbag(dags_dir)
    result = {
        "dags_dir": str(dags_dir),
        "import_errors": {str(k): str(v) for k, v in bag.import_errors.items()},
        "dag_ids": sorted(bag.dags),
        "span": [start.isoformat(), target.isoformat()],
        "dags": {},
    }
    roots = root_jobs(jobs)
    ours_all = world_occurrences(jobs, vtime_start, start, target)
    ours_root = sorted({at for name, at, _ in ours_all if name in roots})
    result["world_root_jobs"] = sorted(roots)
    result["world_root_occurrences"] = [t.isoformat() for t in ours_root]
    result["unrepresented"] = sorted(
        {name for name, _, _ in ours_all if name not in roots})

    for dag_id, dag in sorted(bag.dags.items()):
        infos = airflow_occurrences(dag, start, target)
        theirs = sorted(i.run_after for i in infos)
        agree = theirs == ours_root
        result["dags"][dag_id] = {
            "schedule": str(getattr(dag, "timetable", None)),
            "timetable_class": type(dag.timetable).__name__,
            "start_date": dag.start_date.isoformat() if dag.start_date else None,
            "catchup": bool(dag.catchup),
            "task_ids": sorted(t.task_id for t in dag.tasks),
            "airflow_occurrences": [t.isoformat() for t in theirs],
            "logical_dates": [i.logical_date.isoformat() for i in infos],
            "data_intervals": [
                [i.data_interval.start.isoformat(), i.data_interval.end.isoformat()]
                for i in infos],
            "agree": agree,
            "only_airflow": [t.isoformat() for t in theirs if t not in ours_root],
            "only_world": [t.isoformat() for t in ours_root if t not in theirs],
        }
    return result


def due(dags_dir, jobs, vtime_start, start, target):
    """Yield (dag, DagRunInfo) for every run due in (start, target].

    This is the shape `advance()` would consume: ask Airflow what is due, then
    run it. `jobs`/`vtime_start` are accepted (and unused for enumeration) so
    that a caller can cross-check against `world_occurrences` at the same call
    site - the disagreement is what you want to see, not hide.
    """
    bag = dagbag(dags_dir)
    if bag.import_errors:
        raise RuntimeError("DAG folder %s has import errors: %s"
                           % (dags_dir, list(bag.import_errors)))
    for dag_id, dag in sorted(bag.dags.items()):
        for info in airflow_occurrences(dag, start, target):
            yield dag, info


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------


def run_one(dag, info, vclock=None):
    """Run one occurrence through Airflow's own task loop, under virtual time.

    `run_after == logical_date` on a scheduled run. That is what makes the
    run_id `scheduled__2026-03-02T01:00:00+00:00` instead of a wall-clock
    string, and it is also exactly this world's `vts_start`.
    """
    if vclock is None:
        from . import vclock as _vc
        vclock = _vc
    with vclock.at(info.run_after):
        return dag.test(run_after=info.run_after, logical_date=info.logical_date)


# --------------------------------------------------------------------------
# normalisation (R004 section 2.4, option 1: normalise, don't patch)
# --------------------------------------------------------------------------

# The three task_instance columns the Airflow 3 Task SDK stamps with
# `datetime.now(tz=timezone.utc)` directly, bypassing `airflow.utils.timezone`.
# They are wall-clock measurements of how long real work took - the same class
# of artifact the world already strips out of dbt output - so they are
# normalised out before anything compares two runs, and they are never
# fingerprinted.
TI_WALLCLOCK_COLUMNS = ("start_date", "end_date", "duration")


def normalise_ti(row, columns=TI_WALLCLOCK_COLUMNS):
    """Drop the Task-SDK wall-clock columns from a task_instance row dict."""
    return {k: v for k, v in row.items() if k not in columns}
