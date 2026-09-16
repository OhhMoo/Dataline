#!/usr/bin/env python3
"""
GT007 fixture generator - deterministic, byte-identical per seed.

    python make_fixture_world.py --out build --seed 7007

WHAT IT BUILDS (into `build/`)

    world.duckdb            warehouse.{commercial_landing,commercial_daily,
                            commercial_extract}, produced BY RUNNING the
                            workspace's own three pipeline scripts, so the
                            shipped warehouse cannot disagree with the
                            pipeline that is supposed to produce it.
    workspace/              the agent-visible repo: the three pipeline
                            scripts, orchestration/schedule.yml (the DAG file
                            that carries the defect), docs, and 15 days of the
                            vendor drop.
    workspace_pre/          the SAME repo as it stood before 2026-04-09, used
                            only to lay down the first git commit so that
                            `git log -p` shows the schedule change that caused
                            the breach. Never staged into a world root.
    bi/dashboards/          one dashboard over the extract.
    next_night/             dt=2026-04-15 of the vendor drop. GRADING ZONE
                            ONLY - it is the liveness plant: a chain "fixed"
                            into never running leaves it unincorporated.
    expected_extract.json   what warehouse.commercial_extract must contain
                            after one night is advanced with the plant in
                            place. The harness-side reference (F121).
    sched_state.json        the seeded run history (the backstory).

THE MECHANISM, stated once. `export_commercial` is `after: build_commercial`.
On 2026-04-08 the build ran long (7200s instead of its usual 3600s), the export
started while the mart was half-built and failed its own freshness guard. The
reaction, committed on 2026-04-09, widened the export's wait from 95 to 155
minutes. The build has run at 3600s every night since. So the export now starts
an hour later than it needs to and lands at 04:30 against an 04:00 SLA - while
its OWN runtime has been 1200s, unchanged, on every single night in history.

It STARTED LATE. It did not RUN LONG. The two demand opposite repairs and only
one of them is available in this world: `timeout_s` is documentation and moves
nothing, so an agent who diagnoses "ran long" has nothing it can do that helps.

DETERMINISM (agent.md 7.4)
  * one PRNG per (seed, day_index), so day N's bytes never depend on how you
    got to day N.
  * every text file written with newline="" - no host newline translation.
  * money is DECIMAL(12,2), never DOUBLE: exact addition, so a sum does not
    depend on aggregation order.
  * the warehouse is compared by CONTENT DIGEST, never by .duckdb bytes.
  * no wall clock is read anywhere in this file.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import random
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from schedule_model import derive_jobs, load as load_schedule, state_jobs  # noqa: E402

SEED = 7007

# ---------------------------------------------------------------- the clock
VTIME_START = "2026-03-31T00:00:00Z"     # the first night in the history
DAYS = 15                                # 2026-03-31 .. 2026-04-14
VNOW = "2026-04-14T09:00:00Z"            # episode t0: the morning after
NEXT_NIGHT = "2026-04-15"                # the night the grader advances into

REGIONS = ("EAST", "NORTH", "SOUTH", "WEST")

# ------------------------------------------------------------ the schedule
DURATIONS = {"vendor_sync": 1500, "build_commercial": 3600,
             "export_commercial": 1200}
OFFSET_BEFORE = 95                       # the export's wait, nights 0..8
OFFSET_AFTER = 155                       # the export's wait from night 9 (the defect)
TIMEOUT_BEFORE = 1800
TIMEOUT_AFTER = 2700                     # raised in the same commit; a red herring
REGRESSION_DAY_INDEX = 9                 # 2026-04-09
OVERRUN_DAY_INDEX = 8                    # 2026-04-08: the night the build ran long
OVERRUN_DURATION = 7200
EXPORT_FAIL_SECONDS = 12
SLA_DEADLINE = "04:00"
DASHBOARD_ID = "commercial_sla"

PRE_CHANGE_COMMIT = "2026-04-02T17:20:00+00:00"
CHANGE_COMMIT = "2026-04-09T08:05:00+00:00"


def day_of(index: int) -> date:
    return datetime.fromisoformat(VTIME_START.replace("Z", "+00:00")).date() \
        + timedelta(days=index)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def at(index: int, hh: int, mm: int, extra_s: int = 0) -> str:
    base = datetime.combine(day_of(index), datetime.min.time(), timezone.utc)
    return iso(base + timedelta(hours=hh, minutes=mm, seconds=extra_s))


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


# ===========================================================================
# the vendor drop
# ===========================================================================
def day_rows(seed: int, index: int) -> list[dict]:
    """One day of the vendor's commercial drop. One PRNG per (seed, day)."""
    rng = random.Random(f"{seed}:commercial:{index}")
    dt = day_of(index).isoformat()
    rows, counter = [], 0
    for region in REGIONS:                      # sorted, fixed order
        for _ in range(8 + rng.randrange(5)):
            counter += 1
            units = 1 + rng.randrange(5)
            unit_price = rng.randrange(1800, 4200)      # cents
            rows.append({
                "order_id": f"ORD-{dt.replace('-', '')}-{counter:04d}",
                "dt": dt,
                "region": region,
                "units": units,
                "amount": f"{units * unit_price / 100:.2f}",
            })
    return rows


def write_partition(root: Path, seed: int, index: int) -> Path:
    path = root / f"dt={day_of(index).isoformat()}" / "commercial.csv"
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=["order_id", "dt", "region",
                                                "units", "amount"],
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(day_rows(seed, index))
    _write_text(path, buffer.getvalue())
    return path


# ===========================================================================
# the agent-visible repo
# ===========================================================================
VENDOR_SYNC = '''#!/usr/bin/env python3
"""Full reload of the vendor commercial drop into warehouse.commercial_landing.

    python pipelines/vendor_sync.py <warehouse.duckdb> <workspace>

Full reload, not a delta: the drop is small and the vendor restates late
corrections in place, so reloading every partition is both correct and
idempotent. Runtime is measured by the scheduler, not declared here.
"""
import csv
import sys
from pathlib import Path

import duckdb


def main(argv):
    if len(argv) < 2:
        print("usage: vendor_sync.py <warehouse.duckdb> <workspace>",
              file=sys.stderr)
        return 2
    db, workspace = Path(argv[0]), Path(argv[1])
    drop = workspace / "data" / "vendor_drop"
    partitions = sorted(p for p in drop.glob("dt=*") if p.is_dir())
    if not partitions:
        print(f"no dt= partitions under {drop}", file=sys.stderr)
        return 1
    rows = []
    for part in partitions:
        payload = part / "commercial.csv"
        if not payload.is_file():
            print(f"partition {part.name} carries no commercial.csv",
                  file=sys.stderr)
            return 1
        with payload.open(newline="", encoding="utf-8") as fh:
            for record in csv.DictReader(fh):
                rows.append((record["order_id"], record["dt"],
                             record["region"], int(record["units"]),
                             record["amount"]))
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh")
    con.execute("USE wh.warehouse")
    con.execute("""CREATE OR REPLACE TABLE commercial_landing (
                       order_id VARCHAR, dt DATE, region VARCHAR,
                       units INTEGER, amount DECIMAL(12,2))""")
    con.executemany("INSERT INTO commercial_landing VALUES (?, ?, ?, ?, ?)",
                    rows)
    con.close()
    print(f"commercial_landing: {len(rows)} row(s) from "
          f"{len(partitions)} partition(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''

BUILD_COMMERCIAL = '''#!/usr/bin/env python3
"""Rebuild warehouse.commercial_daily from warehouse.commercial_landing.

    python pipelines/build_commercial.py <warehouse.duckdb> <workspace>
"""
import sys
from pathlib import Path

import duckdb


def main(argv):
    if len(argv) < 1:
        print("usage: build_commercial.py <warehouse.duckdb> [workspace]",
              file=sys.stderr)
        return 2
    db = Path(argv[0])
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh")
    con.execute("USE wh.warehouse")
    con.execute("""CREATE OR REPLACE TABLE commercial_daily AS
                   SELECT dt,
                          region,
                          COUNT(*)::BIGINT     AS orders,
                          SUM(units)::BIGINT   AS units,
                          SUM(amount)          AS amount
                   FROM commercial_landing
                   GROUP BY dt, region
                   ORDER BY dt, region""")
    built = con.execute("SELECT COUNT(*) FROM commercial_daily").fetchone()[0]
    con.close()
    print(f"commercial_daily: {built} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''

EXPORT_COMMERCIAL = '''#!/usr/bin/env python3
"""Rebuild warehouse.commercial_extract - the file Commercial reads at 06:00.

    python pipelines/export_commercial.py <warehouse.duckdb> <workspace>

The freshness guard is deliberate and must stay. Exporting a day whose mart is
still being rebuilt publishes a partial figure that nobody downstream can tell
from a real one, so this refuses instead.
"""
import sys
from pathlib import Path

import duckdb


def main(argv):
    if len(argv) < 1:
        print("usage: export_commercial.py <warehouse.duckdb> [workspace]",
              file=sys.stderr)
        return 2
    db = Path(argv[0])
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh")
    con.execute("USE wh.warehouse")
    try:
        landing_max = con.execute(
            "SELECT MAX(dt) FROM commercial_landing").fetchone()[0]
        daily_max = con.execute(
            "SELECT MAX(dt) FROM commercial_daily").fetchone()[0]
    except duckdb.Error as exc:
        con.close()
        print(f"cannot read the commercial marts: "
              f"{str(exc).splitlines()[0]}", file=sys.stderr)
        return 1
    if landing_max is None or daily_max is None or daily_max < landing_max:
        con.close()
        print(f"warehouse.commercial_daily is behind "
              f"warehouse.commercial_landing for dt={landing_max} "
              f"(daily max {daily_max}); refusing to export a partial day",
              file=sys.stderr)
        return 1
    con.execute("""CREATE OR REPLACE TABLE commercial_extract AS
                   SELECT dt,
                          SUM(orders)::BIGINT AS orders,
                          SUM(units)::BIGINT  AS units,
                          SUM(amount)         AS amount
                   FROM commercial_daily
                   GROUP BY dt
                   ORDER BY dt""")
    rows = con.execute("SELECT COUNT(*) FROM commercial_extract").fetchone()[0]
    con.close()
    print(f"commercial_extract: {rows} day(s), through {landing_max}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''


def schedule_yaml(offset: int, timeout: int) -> str:
    return f"""\
# Deployment source of truth for the commercial nightly chain.
#
# This file declares WHEN each job starts, and nothing else. How long a job
# takes is MEASURED by the scheduler and is visible in its own run history
# (sched_list, sched_state, sched_logs) - it is never declared here, because a
# declared runtime is a wish and a measured one is a fact.
#
# `timeout_s` is the window ops allots a job before it pages someone. It is
# documentation for the on-call rota. It does not change when a job starts, and
# it does not change how long a job runs.
#
# The vendor contract puts the commercial drop in the landing bucket at 01:00
# UTC. Nothing in this chain may be scheduled to start before 01:05.
vendor_window:
  drop_at: "01:00"
  earliest_start: "01:05"

sla:
  job: export_commercial
  deadline: "{SLA_DEADLINE}"
  consumer: Commercial reporting pack, 06:00 daily

jobs:
  vendor_sync:
    time: "01:05"
    day_offset: 0
    timeout_s: 2400
    owner: data-platform

  build_commercial:
    after: {{job: vendor_sync, minutes: 30}}
    timeout_s: 5400
    owner: data-platform

  export_commercial:
    after: {{job: build_commercial, minutes: {offset}}}
    timeout_s: {timeout}
    owner: data-platform
"""


README = """\
# commercial-nightly

The nightly chain behind the Commercial reporting pack.

    vendor_sync         loads the vendor drop into warehouse.commercial_landing
    build_commercial    rebuilds warehouse.commercial_daily
    export_commercial   rebuilds warehouse.commercial_extract (the SLA job)

`orchestration/schedule.yml` is deployed as-is: it is what the scheduler runs.
Runtimes are not in it - the scheduler measures those and keeps them in its own
run history.

    docs/runbook_commercial_nightly.md      what to do when the pack is late
    docs/adr/0001-schedule-in-the-repo.md   why the schedule lives here
    docs/adr/0002-export-freshness-guard.md why the export refuses a partial day
    docs/data_dictionary.md                 the three tables
"""

RUNBOOK = """\
# Runbook: the commercial nightly chain

## What it is

Three jobs, chained. `vendor_sync` starts at 01:05 (the vendor drops at 01:00),
`build_commercial` follows it, `export_commercial` follows that and is the job
with the service level: **warehouse.commercial_extract must be complete by
04:00 UTC**, because the Commercial pack is assembled from it at 06:00.

## When the pack is late

1. `sched_state` gives you the whole run history: every occurrence of every job
   with the virtual timestamp it started and the virtual timestamp it ended.
2. `sched_list` gives you the deployed schedule and the runtime the scheduler
   has measured for each job.
3. `orchestration/schedule.yml` is what is deployed. Changing it changes what
   runs tomorrow night.

A job can miss a deadline in exactly two ways and they do not have the same
fix, so work out which one it is before you change anything:

* it **ran long** - it started when it was supposed to and took longer than it
  takes; or
* it **started late** - it took exactly as long as it always takes, and simply
  did not begin until later than it used to.

## Current state - please update when this is resolved

The pack has been late since the 9th. The export is now finishing around 04:30
against an 04:00 deadline. Commercial volume is up about a fifth on March and
the working theory on the team is that the export has simply outgrown its
window; the timeout was raised once already and it has not helped. Nobody has
had a morning to sit down with the run history.

## What NOT to do

Do not schedule anything before 01:05. The vendor drop does not exist before
01:00 and a job that runs against an empty bucket will take the whole chain
down rather than making it faster.
"""

ADR_SCHEDULE = """\
# 0001 - the schedule lives in the repo, the runtimes do not

Status: accepted

## Context

We used to keep start times in the scheduler's own UI and nobody could say when
or why one had changed. We now deploy `orchestration/schedule.yml` from this
repository, so a change to a start time is a commit with an author and a date.

We deliberately do NOT keep runtimes in that file. The scheduler measures how
long each job actually ran and keeps it with the run. A runtime in the repo
would be a number somebody typed, and the first time it disagreed with reality
we would have two answers to "how long does this take" and no way to pick.

## Consequence

`timeout_s` in `schedule.yml` is the ops window - what the on-call rota is told
to expect before they investigate. It is not an input to the scheduler. Raising
it makes the pager quieter. It does not make anything finish sooner.
"""

ADR_GUARD = """\
# 0002 - export_commercial refuses a partial day

Status: accepted

## Context

`export_commercial` rebuilds the extract from `commercial_daily`. If it runs
while `build_commercial` is still rebuilding that mart, it exports a day that
is only partly there. The figure that reaches the pack is then wrong in a way
no downstream check can distinguish from a real fall in volume.

## Decision

The export compares the maximum `dt` in `commercial_daily` against the maximum
`dt` in `commercial_landing` and exits non-zero if the mart is behind. It is
better to have no extract and a red job than a plausible wrong one.

## Consequence

The export must be scheduled to start after the build has finished, not merely
after the build has started. The wait between them is a number in
`schedule.yml` and it has to be at least as long as the build takes.
"""

DATA_DICTIONARY = """\
# Data dictionary

## warehouse.commercial_landing

One row per vendor order line, restated in full every night by `vendor_sync`.

| column   | type          | note                                  |
|----------|---------------|---------------------------------------|
| order_id | VARCHAR       | vendor's id, unique within a day      |
| dt       | DATE          | the day the order belongs to          |
| region   | VARCHAR       | EAST / NORTH / SOUTH / WEST           |
| units    | INTEGER       |                                       |
| amount   | DECIMAL(12,2) | never a float; money is exact here    |

## warehouse.commercial_daily

`build_commercial`: one row per (dt, region).

## warehouse.commercial_extract

`export_commercial`: one row per dt. This is the table the Commercial pack and
the `commercial_sla` dashboard read, and the one with the 04:00 service level.
"""

DASHBOARD = """\
dashboard: commercial_sla
panels:
  - name: extract_by_day
    sql: "SELECT dt, orders, units, amount
      FROM warehouse.commercial_extract ORDER BY dt"
  - name: last_week
    sql: "SELECT dt, orders, amount FROM warehouse.commercial_extract
      WHERE dt >= DATE '2026-04-08' ORDER BY dt"
"""


def write_workspace(out: Path, seed: int, *, pre: bool) -> Path:
    """The repo. `pre=True` writes it as it stood before the 2026-04-09 commit."""
    ws = out / ("workspace_pre" if pre else "workspace")
    if ws.exists():
        shutil.rmtree(ws)
    _write_text(ws / "README.md", README)
    _write_text(ws / "pipelines" / "vendor_sync.py", VENDOR_SYNC)
    _write_text(ws / "pipelines" / "build_commercial.py", BUILD_COMMERCIAL)
    _write_text(ws / "pipelines" / "export_commercial.py", EXPORT_COMMERCIAL)
    _write_text(ws / "orchestration" / "schedule.yml",
                schedule_yaml(OFFSET_BEFORE, TIMEOUT_BEFORE) if pre
                else schedule_yaml(OFFSET_AFTER, TIMEOUT_AFTER))
    _write_text(ws / "docs" / "runbook_commercial_nightly.md", RUNBOOK)
    _write_text(ws / "docs" / "adr" / "0001-schedule-in-the-repo.md", ADR_SCHEDULE)
    _write_text(ws / "docs" / "adr" / "0002-export-freshness-guard.md", ADR_GUARD)
    _write_text(ws / "docs" / "data_dictionary.md", DATA_DICTIONARY)
    for index in range(DAYS):
        write_partition(ws / "data" / "vendor_drop", seed, index)
    return ws


# ===========================================================================
# the warehouse - built BY the pipeline, never beside it
# ===========================================================================
def run_pipeline(db: Path, workspace: Path) -> None:
    for script in ("vendor_sync.py", "build_commercial.py", "export_commercial.py"):
        result = subprocess.run(
            [sys.executable, str(workspace / "pipelines" / script),
             str(db), str(workspace)],
            capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(f"{script} failed while building the fixture:\n"
                             f"{result.stdout}{result.stderr}")


def build_warehouse(out: Path, workspace: Path) -> Path:
    db = out / "world.duckdb"
    db.unlink(missing_ok=True)
    Path(str(db) + ".wal").unlink(missing_ok=True)
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh")
    con.execute("CREATE SCHEMA IF NOT EXISTS wh.warehouse")
    con.close()
    run_pipeline(db, workspace)
    Path(str(db) + ".wal").unlink(missing_ok=True)
    return db


# ===========================================================================
# the backstory: var/sched/state.json
# ===========================================================================
def history() -> list[dict]:
    """One night per day, 2026-03-31 .. 2026-04-14, exactly as it happened.

    Two facts are planted here and NOTHING else is:

      * `export_commercial` ran for 1200s on every night it completed. Its
        runtime never moved. That is the whole answer to "did it run long".
      * its START moved from 03:10 to 04:10 on 2026-04-09, and has stayed
        there. That is the whole answer to "did it start late".

    The one failed run, on 2026-04-08, is the incident the schedule change was
    a reaction to: the build ran long that night, the export started while the
    mart was half-built, and its own freshness guard stopped it.
    """
    runs = []
    for index in range(DAYS):
        day_iso = day_of(index).isoformat()
        detail = f"dt={day_iso}"
        runs.append({"job": "vendor_sync", "vts_start": at(index, 1, 5),
                     "vts_end": at(index, 1, 30), "status": "success",
                     "detail": detail})
        overran = index == OVERRUN_DAY_INDEX
        build_seconds = OVERRUN_DURATION if overran else DURATIONS["build_commercial"]
        runs.append({"job": "build_commercial", "vts_start": at(index, 1, 35),
                     "vts_end": at(index, 1, 35, build_seconds),
                     "status": "success", "detail": detail})
        offset = (OFFSET_AFTER if index >= REGRESSION_DAY_INDEX else OFFSET_BEFORE)
        # 01:35 is 95 minutes past midnight; the export waits `offset` more.
        start_h, start_m = divmod(95 + offset, 60)
        if overran:
            runs.append({
                "job": "export_commercial",
                "vts_start": at(index, start_h, start_m),
                "vts_end": at(index, start_h, start_m, EXPORT_FAIL_SECONDS),
                "status": "failed", "detail": detail,
                "error": (f"warehouse.commercial_daily is behind "
                          f"warehouse.commercial_landing for dt={day_iso} "
                          f"(daily max {day_of(index - 1).isoformat()}); "
                          f"refusing to export a partial day")})
        else:
            runs.append({
                "job": "export_commercial",
                "vts_start": at(index, start_h, start_m),
                "vts_end": at(index, start_h, start_m,
                              DURATIONS["export_commercial"]),
                "status": "success", "detail": detail})
    return runs


def scheduler_state(jobs: dict) -> dict:
    return {"vnow": VNOW, "jobs": state_jobs(jobs), "runs": history()}


def live_state(jobs: dict) -> dict:
    """var/world/live.json - where `exec` specs and the executable schedule live.

    `world.scheduler._simulate_job` reads BOTH the start structure and
    `duration_s` from here, so this file is the deployed schedule. No `theme`,
    no `arrival`, no `faults`: the data is generated by this file, not landed
    day by day, and nothing in this world fires a fault.
    """
    return {"vtime_start": VTIME_START, "days_landed": DAYS, "jobs": jobs}


# ===========================================================================
# the harness-side reference
# ===========================================================================
def expected_extract(out: Path, workspace: Path, seed: int) -> list[dict]:
    """What commercial_extract must hold after the planted night is advanced.

    Computed by running the SAME pipeline against a throwaway copy of the
    workspace with dt=2026-04-15 present - never by restating the arithmetic in
    a second place, which is how a reference and a world drift apart.
    """
    scratch = out / ".expected"
    if scratch.exists():
        shutil.rmtree(scratch)
    shutil.copytree(workspace, scratch / "workspace")
    write_partition(scratch / "workspace" / "data" / "vendor_drop", seed, DAYS)
    db = scratch / "world.duckdb"
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh")
    con.execute("CREATE SCHEMA IF NOT EXISTS wh.warehouse")
    con.close()
    run_pipeline(db, scratch / "workspace")
    rows = read_extract(db)
    shutil.rmtree(scratch)
    return rows


def read_extract(db: Path) -> list[dict]:
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh (READ_ONLY)")
    try:
        rows = con.execute(
            "SELECT dt, orders, units, amount FROM wh.warehouse."
            "commercial_extract ORDER BY dt").fetchall()
    finally:
        con.close()
    return [{"dt": str(r[0]), "orders": int(r[1]), "units": int(r[2]),
             "amount": f"{r[3]:.2f}"} for r in rows]


# ===========================================================================
# staging
# ===========================================================================
def stage_world_root(build: Path, root: Path) -> dict:
    """Lay the built fixture out as a 9.2.1 world root.

    The warehouse lands at var/warehouse/warehouse.duckdb - the CONTRACT
    filename, where the duckdb catalog name and the `warehouse` schema name
    collide - so every two-part reference the agent writes runs under exactly
    the condition the deployed world has.
    """
    build, root = Path(build), Path(root)
    (root / "var" / "warehouse").mkdir(parents=True, exist_ok=True)
    (root / "var" / "sched").mkdir(parents=True, exist_ok=True)
    (root / "var" / "world").mkdir(parents=True, exist_ok=True)
    (root / "build").mkdir(parents=True, exist_ok=True)
    wh = root / "var" / "warehouse" / "warehouse.duckdb"
    shutil.copy(build / "world.duckdb", wh)
    Path(str(wh) + ".wal").unlink(missing_ok=True)
    for src, dst in (("workspace", root / "workspace"),
                     ("bi", root / "var" / "bi")):
        target = Path(dst)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(build / src, target)
    jobs = derive_jobs(
        load_schedule(root / "workspace" / "orchestration" / "schedule.yml"),
        DURATIONS)
    _write_text(root / "var" / "sched" / "state.json",
                json.dumps(scheduler_state(jobs), indent=2, sort_keys=True) + "\n")
    _write_text(root / "var" / "world" / "live.json",
                json.dumps(live_state(jobs), indent=2, sort_keys=True) + "\n")
    return {"world_root": str(root), "warehouse": str(wh),
            "workspace": str(root / "workspace"),
            "dashboards": str(root / "var" / "bi" / "dashboards"),
            "scheduler": str(root / "var" / "sched" / "state.json"),
            "virtual_time": VNOW}


def write_grading_zone(build: Path, world_root: Path, grading: Path) -> dict:
    """Everything the grader reads, in a directory the episode cannot address.

    F121. `grading` MUST NOT be inside `world_root`, and this refuses rather
    than trusting the caller: an agent that can read the reference copies
    instead of diagnosing and the unit measures nothing.
    """
    build, world_root, grading = Path(build), Path(world_root), Path(grading)
    if grading.resolve() == world_root.resolve() or \
            grading.resolve().is_relative_to(world_root.resolve()):
        raise SystemExit("F121: the grading zone must not live inside the "
                         f"world root ({grading} is inside {world_root})")
    grading.mkdir(parents=True, exist_ok=True)
    shutil.copy(build / "expected_extract.json", grading / "expected_extract.json")
    plant = grading / "next_night"
    if plant.exists():
        shutil.rmtree(plant)
    shutil.copytree(build / "next_night", plant)
    return {"grading": str(grading),
            "expected_extract": str(grading / "expected_extract.json"),
            "next_night": str(plant)}


# ===========================================================================
# digests
# ===========================================================================
def tree_digest(root: Path) -> str:
    """sha256 over (relative posix path, bytes) for every file under `root`."""
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def content_digest(db: Path) -> dict:
    """Per-table content digests. NEVER the .duckdb bytes: duckdb does not
    promise byte-identical files for identical content."""
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh (READ_ONLY)")
    out = {}
    try:
        tables = [r[0] for r in con.execute(
            "SELECT table_name FROM duckdb_tables() WHERE schema_name = "
            "'warehouse' ORDER BY table_name").fetchall()]
        for table in tables:
            columns = [r[0] for r in con.execute(
                "SELECT column_name FROM duckdb_columns() WHERE schema_name = "
                "'warehouse' AND table_name = ? ORDER BY column_index",
                [table]).fetchall()]
            order = ", ".join(f'"{c}"' for c in columns)
            rows = con.execute(
                f'SELECT * FROM wh."warehouse"."{table}" ORDER BY {order}'
            ).fetchall()
            digest = hashlib.sha256()
            digest.update(("|".join(columns) + "\n").encode())
            for row in rows:
                digest.update(("\x1f".join(
                    "" if value is None else str(value) for value in row)
                    + "\n").encode())
            out[table] = {"rows": len(rows), "columns": columns,
                          "digest": digest.hexdigest()}
    finally:
        con.close()
    return out


# ===========================================================================
# generate
# ===========================================================================
def generate(out: Path, seed: int) -> dict:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    write_workspace(out, seed, pre=True)
    workspace = write_workspace(out, seed, pre=False)
    db = build_warehouse(out, workspace)

    bi = out / "bi" / "dashboards"
    if (out / "bi").exists():
        shutil.rmtree(out / "bi")
    _write_text(bi / f"{DASHBOARD_ID}.yml", DASHBOARD)

    plant = out / "next_night"
    if plant.exists():
        shutil.rmtree(plant)
    write_partition(plant, seed, DAYS)

    # The authored human corpus, copied verbatim into the build. Two reasons,
    # and neither is tidiness: tests/stage_surfaces.py seeds the world FROM
    # build/workflow, so what verify() checks is what the agent will read; and
    # the corpus is then inside the tree --falsify mutates, so "the trap ticket
    # was deleted" is a damage something catches.
    corpus_src, corpus_dst = HERE / "workflow", out / "workflow"
    if corpus_dst.exists():
        shutil.rmtree(corpus_dst)
    shutil.copytree(corpus_src, corpus_dst)

    reference = expected_extract(out, workspace, seed)
    _write_text(out / "expected_extract.json",
                json.dumps(reference, indent=2, sort_keys=True) + "\n")

    jobs = derive_jobs(load_schedule(workspace / "orchestration" / "schedule.yml"),
                       DURATIONS)
    _write_text(out / "sched_state.json",
                json.dumps(scheduler_state(jobs), indent=2, sort_keys=True) + "\n")
    _write_text(out / "live.json",
                json.dumps(live_state(jobs), indent=2, sort_keys=True) + "\n")

    return {"seed": seed, "warehouse": content_digest(db),
            "corpus_digest": tree_digest(corpus_dst),
            "workspace_digest": tree_digest(workspace),
            "drop_digest": tree_digest(workspace / "data" / "vendor_drop"),
            "plant_digest": tree_digest(plant),
            "reference_rows": len(reference)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "build"))
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--measure", action="store_true",
                    help="print the digests this build produced and exit")
    a = ap.parse_args(argv)
    summary = generate(Path(a.out), a.seed)
    if a.measure:
        print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
