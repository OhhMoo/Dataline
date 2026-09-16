#!/usr/bin/env python3
"""
GT007 fixture contract - every number this task depends on, in one place, plus
the check that refuses a drifted fixture and the falsifier that proves the
check can actually fail.

    python fixture_contract.py                # verify fixtures/build
    python fixture_contract.py --falsify      # plant damage, demand a catch

CONTRACT is the single source of truth. Nothing downstream - not the grader,
not the manifest, not the prompts, not the baselines - may hardcode a row
count, a timestamp, an offset or a digest; each reads it from here. A fixture
regenerated at a different seed, or a generator edited after the contract was
frozen, changes a digest, and `verify` stops the build instead of letting a
task package grade a world it was not written against.

WHAT `verify` IS FOR, stated plainly: it is not a test of the generator, it is
a test of the WORLD. Every clause below is a property some downstream check
depends on. `--falsify` mutates the built fixture twenty-six ways and requires
`verify` to raise on each one; a mutation nothing catches is a property nobody
is checking, which is the only kind of drift that matters.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import make_fixture_world as mfw                                   # noqa: E402
from schedule_model import ScheduleError, derive_jobs, load as load_schedule  # noqa: E402

BUILD = HERE / "build"


# ===========================================================================
# THE CONTRACT
# ===========================================================================
CONTRACT = {
    "seed": mfw.SEED,

    # ------------------------------------------------------------ the clock
    "virtual_now": mfw.VNOW,               # 2026-04-14T09:00:00Z, episode t0
    "vtime_start": mfw.VTIME_START,        # the first night in the history
    "days": mfw.DAYS,                      # 15 nights of history
    # The grader advances exactly one night. 24h from t0 lands at
    # 2026-04-15T09:00Z, which contains the whole of the 2026-04-15 chain and
    # none of the 2026-04-14 one (its last job ended at 04:30 that morning).
    "advance_hours": 24,
    "next_night": mfw.NEXT_NIGHT,          # 2026-04-15

    # ---------------------------------------------------------- the service
    "sla": {
        "job": "export_commercial",
        "deadline": mfw.SLA_DEADLINE,                  # "04:00"
        "deadline_next_night": "2026-04-15T04:00:00Z",
    },

    # --------------------------------------------------------- the schedule
    # Runtimes are MEASURED and live in var/sched/state.json; the repo's
    # schedule.yml declares only start structure. That split is the mechanism:
    # `timeout_s` is the only "ran long" knob the repo offers and it moves
    # nothing at all.
    "durations": dict(mfw.DURATIONS),
    "schedule": {
        "offset_before": mfw.OFFSET_BEFORE,            # 95, nights 0..8
        "offset_after": mfw.OFFSET_AFTER,              # 155, from night 9
        "timeout_before": mfw.TIMEOUT_BEFORE,          # 1800
        "timeout_after": mfw.TIMEOUT_AFTER,            # 2700, the red herring
        "vendor_drop_at": "01:00",
        "earliest_start": "01:05",
        # The repair band, derived, not chosen: the export must start after the
        # 3600s build finishes (offset >= 60) and must end by 04:00
        # (start <= 03:40, offset <= 125). Documentation for a reader - the
        # gate is behavioural and accepts ANY schedule that lands on time
        # without overlapping, including one that drops `after` for a `time`.
        "repair_offset_min": 60,
        "repair_offset_max": 125,
    },

    # ---------------------------------------------------------- the history
    # THE DISCRIMINATION, and it is these three numbers and nothing else.
    "history": {
        "runs_total": 45,                              # 15 nights x 3 jobs
        "export_runtime_s": mfw.DURATIONS["export_commercial"],   # 1200, every night
        "export_runtime_is_constant": True,
        "export_start_before": "03:10",                # nights 0..8
        "export_start_after": "04:10",                 # nights 9..14
        "start_shift_minutes": 60,
        "regression_date": "2026-04-09",               # first night it started late
        "breach_nights": 6,                            # 2026-04-09 .. 2026-04-14
        "breach_finish": "04:30",
        "clean_finish": "03:30",                       # nights 0..7
        # The one-off that provoked the schedule change, and the ONLY run in
        # 45 that failed. The build ran long that night; the export started
        # into a half-built mart and its own guard stopped it.
        "overrun_date": "2026-04-08",
        "overrun_duration_s": mfw.OVERRUN_DURATION,    # 7200
        "build_runtime_s": mfw.DURATIONS["build_commercial"],      # 3600 otherwise
        "failed_runs": 1,
    },

    # ------------------------------------------------------------ the world
    "world": {
        "landing_rows": 579,
        "daily_rows": 60,                              # 15 days x 4 regions
        "extract_rows": 15,
        "regions": list(mfw.REGIONS),
        "tables": ["commercial_daily", "commercial_extract", "commercial_landing"],
        "digests": {
            "commercial_landing":
                "74c5bc932c1ee88ef2f8df9c3d05794f5d839ec2243b575878e7f74a69d45ada",
            "commercial_daily":
                "690626a2b59f73fdd0f8bb166b1d82597341058a0dd385e5d974239c406ca03a",
            "commercial_extract":
                "552b771abfab7d7081b1a3eb2d4a5203674afebe5f5377652a9918264302e1ad",
        },
        "workspace_digest":
            "b290a7ba1eb9ee2b31e2622099a14b7ad8df2c88adbac4d61ec2500d5a70a8a6",
        "drop_digest":
            "10f93b780923451d8e86b39a326e205d94945c6f62f9a8fafeb13e21c13eb88a",
        "plant_digest":
            "f3c63fd69b789163ed920ed60535ee0797ebe821b0db403ae86b99882bbde771",
        "dashboard": mfw.DASHBOARD_ID,
    },

    # ------------------------------------------------- the liveness plant
    # Harness-side only. A chain "fixed" into never running leaves this
    # unincorporated and D1 scores ZERO rather than full marks.
    "plant": {
        "dt": mfw.NEXT_NIGHT,
        "orders": 36,
        "units": 95,
        "amount": "2757.86",
        "extract_rows_after": 16,
    },

    # ------------------------------------------------------------- layout
    "layout": {
        "deployed_world_relpath": "var/warehouse/warehouse.duckdb",
        "scheduler_relpath": "var/sched/state.json",
        "live_relpath": "var/world/live.json",
        "dashboards_relpath": "var/bi/dashboards",
        "schedule_relpath": "workspace/orchestration/schedule.yml",
        "artifact_relpath": "final_report.json",
    },

    # ----------------------------------------------------- the artifact
    # The keys tests/grade.py reads out of /workspace/final_report.json, and
    # the tolerance on each measured one. Tolerances exist so that a right
    # answer stated in a slightly different unit is not marked wrong; they are
    # far too tight to be cleared by guessing.
    "report": {
        "keys": ["sla_job", "sla_deadline", "observed_finish", "verdict",
                 "observed_runtime_s", "runtime_changed",
                 "start_shift_minutes", "regression_date"],
        "verdict_values": ["ran_long", "started_late"],
        "verdict_expected": "started_late",
        "runtime_tolerance_s": 60,
        "shift_tolerance_minutes": 5,
    },
}


class FixtureDrift(Exception):
    """The built fixture is not the world this task package was written for."""


# ===========================================================================
# verify
# ===========================================================================
def _problems_warehouse(fx: Path) -> list[str]:
    db = fx / "world.duckdb"
    if not db.is_file():
        return [f"{db} is missing"]
    out = []
    digests = mfw.content_digest(db)
    want = CONTRACT["world"]
    if sorted(digests) != want["tables"]:
        out.append(f"warehouse holds {sorted(digests)}, expected {want['tables']}")
        return out
    for table, expected in want["digests"].items():
        got = digests[table]["digest"]
        if got != expected:
            out.append(f"{table} content digest is {got[:16]}..., expected "
                       f"{expected[:16]}...")
    for table, key in (("commercial_landing", "landing_rows"),
                       ("commercial_daily", "daily_rows"),
                       ("commercial_extract", "extract_rows")):
        if digests[table]["rows"] != want[key]:
            out.append(f"{table} holds {digests[table]['rows']} row(s), "
                       f"expected {want[key]}")
    # The SYMPTOM must be a timing symptom and not a data symptom: this unit is
    # orthogonal to A009 (GT004) only while the warehouse is CORRECT. A
    # duplicate business key here would make the two units' failures
    # correlated, which is the one thing a composition library cannot afford.
    con = mfw.duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh (READ_ONLY)")
    try:
        dupes = con.execute(
            "SELECT COUNT(*) FROM (SELECT order_id, dt FROM "
            "wh.warehouse.commercial_landing GROUP BY 1, 2 HAVING COUNT(*) > 1)"
        ).fetchone()[0]
        if dupes:
            out.append(f"commercial_landing carries {dupes} duplicated business "
                       "key(s); this unit's mechanism must be TIMING, not "
                       "duplication (orthogonality to A009/GT004)")
        days = con.execute("SELECT COUNT(DISTINCT dt) FROM "
                           "wh.warehouse.commercial_extract").fetchone()[0]
        if days != CONTRACT["days"]:
            out.append(f"commercial_extract covers {days} day(s), expected "
                       f"{CONTRACT['days']}")
    finally:
        con.close()
    return out


def _problems_schedule(fx: Path) -> list[str]:
    path = fx / "workspace" / "orchestration" / "schedule.yml"
    if not path.is_file():
        return [f"{path} is missing"]
    out = []
    try:
        doc = load_schedule(path)
        jobs = derive_jobs(doc, CONTRACT["durations"])
    except ScheduleError as exc:
        return [f"the shipped schedule.yml does not deploy: {exc}"]
    want = CONTRACT["schedule"]
    offset = (jobs["export_commercial"].get("after") or {}).get("minutes")
    if offset != want["offset_after"]:
        out.append(f"export_commercial waits {offset} minute(s) after the "
                   f"build; the shipped defect is {want['offset_after']}")
    sla = doc.get("sla") or {}
    if sla.get("job") != CONTRACT["sla"]["job"]:
        out.append(f"sla.job is {sla.get('job')!r}, expected "
                   f"{CONTRACT['sla']['job']!r}")
    if str(sla.get("deadline")) != CONTRACT["sla"]["deadline"]:
        out.append(f"sla.deadline is {sla.get('deadline')!r}, expected "
                   f"{CONTRACT['sla']['deadline']!r}")
    window = doc.get("vendor_window") or {}
    if str(window.get("earliest_start")) != want["earliest_start"]:
        out.append(f"vendor_window.earliest_start is "
                   f"{window.get('earliest_start')!r}, expected "
                   f"{want['earliest_start']!r}")
    timeout = ((doc.get("jobs") or {}).get("export_commercial") or {}).get("timeout_s")
    if timeout != want["timeout_after"]:
        out.append(f"export_commercial timeout_s is {timeout}, expected the "
                   f"raised-and-useless {want['timeout_after']}")

    # the PRE-change repo, which is what makes the git history evidence
    pre = fx / "workspace_pre" / "orchestration" / "schedule.yml"
    if not pre.is_file():
        out.append(f"{pre} is missing; without it the first commit carries the "
                   "defect and `git log -p` shows no schedule change at all")
    else:
        pre_offset = ((derive_jobs(load_schedule(pre), CONTRACT["durations"])
                       ["export_commercial"].get("after")) or {}).get("minutes")
        if pre_offset != want["offset_before"]:
            out.append(f"the pre-change schedule waits {pre_offset} minute(s), "
                       f"expected {want['offset_before']}")
    return out


def _problems_history(fx: Path) -> list[str]:
    path = fx / "sched_state.json"
    if not path.is_file():
        return [f"{path} is missing"]
    state = json.loads(path.read_text(encoding="utf-8"))
    want = CONTRACT["history"]
    out = []
    if state.get("vnow") != CONTRACT["virtual_now"]:
        out.append(f"vnow is {state.get('vnow')!r}, expected "
                   f"{CONTRACT['virtual_now']!r}")
    runs = state.get("runs") or []
    if len(runs) != want["runs_total"]:
        out.append(f"history holds {len(runs)} run(s), expected "
                   f"{want['runs_total']}")

    def seconds(run) -> int:
        a = mfw.datetime.fromisoformat(run["vts_start"].replace("Z", "+00:00"))
        b = mfw.datetime.fromisoformat(run["vts_end"].replace("Z", "+00:00"))
        return int((b - a).total_seconds())

    exports = [r for r in runs if r["job"] == "export_commercial"]
    if len(exports) != CONTRACT["days"]:
        out.append(f"{len(exports)} export run(s), expected {CONTRACT['days']}")
    ok_exports = [r for r in exports if r["status"] == "success"]
    runtimes = sorted({seconds(r) for r in ok_exports})
    # THE discriminating property of the whole unit. If the export's runtime
    # ever varies, "did it run long?" stops having a clean answer and the gate
    # becomes a coin flip.
    if runtimes != [want["export_runtime_s"]]:
        out.append(f"export_commercial's completed runs took {runtimes}s; the "
                   f"unit requires a CONSTANT {want['export_runtime_s']}s - a "
                   f"varying runtime makes ran-long/started-late unanswerable")
    starts = sorted({r["vts_start"][11:16] for r in exports})
    if starts != sorted({want["export_start_before"], want["export_start_after"]}):
        out.append(f"export_commercial started at {starts}; expected exactly "
                   f"{sorted({want['export_start_before'], want['export_start_after']})}")
    late = [r for r in exports if r["vts_start"][11:16] == want["export_start_after"]]
    if len(late) != want["breach_nights"]:
        out.append(f"{len(late)} night(s) start at {want['export_start_after']}, "
                   f"expected {want['breach_nights']}")
    if late and sorted({r["vts_start"][:10] for r in late})[0] != want["regression_date"]:
        out.append(f"the late starts begin "
                   f"{sorted({r['vts_start'][:10] for r in late})[0]}, expected "
                   f"{want['regression_date']}")
    deadline = CONTRACT["sla"]["deadline"]
    breached = [r for r in exports
                if r["status"] == "success" and r["vts_end"][11:16] > deadline]
    if len(breached) != want["breach_nights"]:
        out.append(f"{len(breached)} night(s) finish after {deadline}, expected "
                   f"{want['breach_nights']}")
    for run in breached:
        if run["vts_end"][11:16] != want["breach_finish"]:
            out.append(f"{run['vts_start'][:10]} finished "
                       f"{run['vts_end'][11:16]}, expected {want['breach_finish']}")
            break
    clean = [r for r in exports
             if r["status"] == "success" and r["vts_end"][11:16] <= deadline]
    if not clean:
        out.append("no night in history ever met the SLA; the world must show "
                   "that it used to, or there is no regression to find")

    failed = [r for r in runs if r["status"] != "success"]
    if len(failed) != want["failed_runs"]:
        out.append(f"{len(failed)} failed run(s), expected {want['failed_runs']}")
    elif failed[0]["vts_start"][:10] != want["overrun_date"]:
        out.append(f"the failed run is on {failed[0]['vts_start'][:10]}, "
                   f"expected {want['overrun_date']}")
    builds = [r for r in runs if r["job"] == "build_commercial"]
    long_builds = [r for r in builds if seconds(r) == want["overrun_duration_s"]]
    if len(long_builds) != 1:
        out.append(f"{len(long_builds)} build(s) ran {want['overrun_duration_s']}s; "
                   "expected exactly the one that provoked the schedule change")
    other = sorted({seconds(r) for r in builds
                    if seconds(r) != want["overrun_duration_s"]})
    if other != [want["build_runtime_s"]]:
        out.append(f"the other builds took {other}s, expected "
                   f"[{want['build_runtime_s']}]")
    return out


def _problems_files(fx: Path) -> list[str]:
    out = []
    ws = fx / "workspace"
    for rel in ("README.md", "pipelines/vendor_sync.py",
                "pipelines/build_commercial.py", "pipelines/export_commercial.py",
                "orchestration/schedule.yml", "docs/data_dictionary.md",
                "docs/runbook_commercial_nightly.md",
                "docs/adr/0001-schedule-in-the-repo.md",
                "docs/adr/0002-export-freshness-guard.md"):
        if not (ws / rel).is_file():
            out.append(f"workspace/{rel} is missing")
    partitions = sorted(p.name for p in (ws / "data" / "vendor_drop").glob("dt=*"))
    if len(partitions) != CONTRACT["days"]:
        out.append(f"the vendor drop holds {len(partitions)} partition(s), "
                   f"expected {CONTRACT['days']}")
    if f"dt={CONTRACT['next_night']}" in partitions:
        out.append(f"dt={CONTRACT['next_night']} is IN the agent-visible drop; "
                   "it is the liveness plant and belongs in the grading zone "
                   "only (F121)")
    digest = mfw.tree_digest(ws)
    if digest != CONTRACT["world"]["workspace_digest"]:
        out.append(f"workspace tree digest is {digest[:16]}..., expected "
                   f"{CONTRACT['world']['workspace_digest'][:16]}...")
    drop = mfw.tree_digest(ws / "data" / "vendor_drop")
    if drop != CONTRACT["world"]["drop_digest"]:
        out.append(f"vendor drop digest is {drop[:16]}..., expected "
                   f"{CONTRACT['world']['drop_digest'][:16]}...")
    plant = fx / "next_night"
    if not plant.is_dir():
        out.append("next_night/ (the liveness plant) is missing")
    else:
        got = mfw.tree_digest(plant)
        if got != CONTRACT["world"]["plant_digest"]:
            out.append(f"liveness plant digest is {got[:16]}..., expected "
                       f"{CONTRACT['world']['plant_digest'][:16]}...")
    dash = fx / "bi" / "dashboards" / f"{CONTRACT['world']['dashboard']}.yml"
    if not dash.is_file():
        out.append(f"{dash} is missing")

    reference = fx / "expected_extract.json"
    if not reference.is_file():
        out.append("expected_extract.json (the harness-side reference) is missing")
    else:
        rows = json.loads(reference.read_text(encoding="utf-8"))
        if len(rows) != CONTRACT["plant"]["extract_rows_after"]:
            out.append(f"the reference holds {len(rows)} day(s), expected "
                       f"{CONTRACT['plant']['extract_rows_after']}")
        last = rows[-1] if rows else {}
        for key in ("dt", "orders", "units", "amount"):
            if str(last.get(key)) != str(CONTRACT["plant"][key]):
                out.append(f"the reference's last day has {key}="
                           f"{last.get(key)!r}, expected "
                           f"{CONTRACT['plant'][key]!r}")
    live = fx / "live.json"
    if not live.is_file():
        out.append("live.json is missing; without it every job reports "
                   "`declares no executable work` and nothing can be advanced")
    else:
        doc = json.loads(live.read_text(encoding="utf-8"))
        if doc.get("vtime_start") != CONTRACT["vtime_start"] or \
                doc.get("days_landed") != CONTRACT["days"]:
            out.append("live.json's vtime_start/days_landed do not match the "
                       "contract; world.scheduler.advance plans the wrong night")
        for name, spec in (doc.get("jobs") or {}).items():
            if not (spec.get("exec") or {}).get("kind"):
                out.append(f"live.json job {name} declares no exec block")
    return out


def _problems_workflow(fx: Path) -> list[str]:
    """The human corpus, assembled the way the world assembles it.

    It does NOT live in `build/` - stage_surfaces.py seeds it straight into the
    world root from fixtures/workflow/ - so without this clause the tracker, the
    channel and the wiki would be the one part of the world nothing verifies.
    They are not decoration here: the FOLK EXPLANATION the run history has to
    overrule lives in them, and a corpus that quietly stopped assembling would
    take the designed trap with it while every other check still passed.
    """
    corpus = fx / "workflow"
    if not corpus.is_dir():
        return [f"{corpus} is missing; the generator copies fixtures/workflow "
                f"into the build so that what is verified is what is staged"]
    sys.path.insert(0, str(HERE.parent.parent.parent / "world"))
    try:
        from world import workflow as world_workflow
    except ImportError as exc:
        return [f"world/workflow.py is not importable: {exc}"]
    finally:
        sys.path.pop(0)
    spec = {"dir": "workflow", "actor": "agent",
            "project": {"key": "DATA", "name": "Data platform"},
            "statuses": ["open", "triage", "in_progress", "blocked", "done",
                         "wontfix"],
            "workspace": "commercial-data",
            "space": {"key": "DE", "name": "Data Engineering"}}
    try:
        docs = world_workflow.documents(fx, spec, CONTRACT["virtual_now"])
        world_workflow.validate(docs, spec["actor"], CONTRACT["virtual_now"])
    except SystemExit as exc:
        return [f"the workflow corpus does not assemble: {exc}"]
    out = []
    issues = {i["id"]: i for i in docs["issues.json"]["issues"]}
    for required in ("DATA-7702", "DATA-7710", "DATA-7714"):
        if required not in issues:
            out.append(f"{required} is missing from the tracker")
    trap = issues.get("DATA-7714")
    if trap and "outgrown" not in trap["title"]:
        out.append("DATA-7714 no longer states the folk explanation the run "
                   "history has to overrule; the designed trap is gone")
    # every chat message must carry `text`, not `body`: the gateway renders a
    # message through record["text"] and a corpus keyed `body` makes
    # chat_history raise KeyError - a surface that probes live on
    # chat_channels and dies the moment the agent reads it.
    for channel in docs["chat.json"]["channels"]:
        for message in channel.get("messages", []):
            if "text" not in message:
                out.append(f"chat message {message.get('id')} has no `text`; "
                           f"chat_history renders record['text']")
                break
    pages = {p["id"] for p in docs["kb.json"]["pages"]}
    if "de/how-to/read-scheduler-history" not in pages:
        out.append("the how-to that states the ran-long/started-late method is "
                   "missing; without it the unit is a trivia task")
    return out


def verify(fx: Path = BUILD, quiet: bool = False) -> None:
    fx = Path(fx)
    if not fx.is_dir():
        raise FixtureDrift(f"no fixture at {fx}; run make_fixture_world.py")
    problems = (_problems_warehouse(fx) + _problems_schedule(fx)
                + _problems_history(fx) + _problems_files(fx)
                + _problems_workflow(fx))
    if problems:
        raise FixtureDrift(
            f"the fixture at {fx} is not the world GT007 was written for:\n  - "
            + "\n  - ".join(problems))
    if not quiet:
        print(f"fixture OK: {fx}")
        print(f"  seed {CONTRACT['seed']}, t0 {CONTRACT['virtual_now']}")
        print(f"  {CONTRACT['history']['runs_total']} runs over "
              f"{CONTRACT['days']} nights, "
              f"{CONTRACT['history']['breach_nights']} of them past the "
              f"{CONTRACT['sla']['deadline']} SLA")
        print(f"  export runtime CONSTANT at "
              f"{CONTRACT['history']['export_runtime_s']}s; start moved "
              f"{CONTRACT['history']['export_start_before']} -> "
              f"{CONTRACT['history']['export_start_after']} on "
              f"{CONTRACT['history']['regression_date']}")


# ===========================================================================
# falsify - a check nothing can falsify is not a check
# ===========================================================================
def _sched(fx: Path) -> Path:
    return fx / "workspace" / "orchestration" / "schedule.yml"


def _edit(path: Path, old: str, new: str):
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"falsifier could not find {old!r} in {path}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="")


def _state_edit(fx: Path, fn):
    path = fx / "sched_state.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    fn(state)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8", newline="")


def _last_export(state: dict) -> dict:
    return [r for r in state["runs"] if r["job"] == "export_commercial"][-1]


def _sql(fx: Path, statement: str):
    con = mfw.duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + str(fx / "world.duckdb").replace("'", "''")
                + "' AS wh")
    con.execute("USE wh.warehouse")
    con.execute(statement)
    con.close()
    Path(str(fx / "world.duckdb") + ".wal").unlink(missing_ok=True)


def _corpus_edit(fx: Path, name: str, old: str, new: str, count: int = 1):
    path = fx / "workflow" / name
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"falsifier could not find {old!r} in {path}")
    path.write_text(text.replace(old, new, count), encoding="utf-8", newline="")


MUTATIONS = [
    ("warehouse: a landing row deleted",
     lambda fx: _sql(fx, "DELETE FROM commercial_landing WHERE rowid = 1")),
    ("warehouse: a landing amount nudged",
     lambda fx: _sql(fx, "UPDATE commercial_landing SET amount = amount + 1 "
                         "WHERE rowid = 2")),
    ("warehouse: a duplicated business key (the A009 mechanism)",
     lambda fx: _sql(fx, "INSERT INTO commercial_landing SELECT * FROM "
                         "commercial_landing LIMIT 1")),
    ("warehouse: the daily mart rebuilt one day short",
     lambda fx: _sql(fx, "DELETE FROM commercial_daily WHERE dt = "
                         "(SELECT MAX(dt) FROM commercial_daily)")),
    ("warehouse: the extract rebuilt one day short",
     lambda fx: _sql(fx, "DELETE FROM commercial_extract WHERE dt = "
                         "(SELECT MAX(dt) FROM commercial_extract)")),
    ("schedule: the defective offset quietly repaired",
     lambda fx: _edit(_sched(fx), f"minutes: {mfw.OFFSET_AFTER}",
                      f"minutes: {mfw.OFFSET_BEFORE}")),
    ("schedule: the SLA deadline moved",
     lambda fx: _edit(_sched(fx), f'deadline: "{mfw.SLA_DEADLINE}"',
                      'deadline: "05:00"')),
    ("schedule: the vendor window removed",
     lambda fx: _edit(_sched(fx), 'earliest_start: "01:05"',
                      'earliest_start: "00:05"')),
    ("schedule: the red-herring timeout put back",
     lambda fx: _edit(_sched(fx), f"timeout_s: {mfw.TIMEOUT_AFTER}",
                      f"timeout_s: {mfw.TIMEOUT_BEFORE}")),
    ("schedule: the pre-change repo already carries the defect",
     lambda fx: _edit(fx / "workspace_pre" / "orchestration" / "schedule.yml",
                      f"minutes: {mfw.OFFSET_BEFORE}",
                      f"minutes: {mfw.OFFSET_AFTER}")),
    ("history: the export's runtime made to vary (ran-long becomes arguable)",
     lambda fx: _state_edit(fx, lambda s: _last_export(s).__setitem__(
         "vts_end", "2026-04-14T04:52:00Z"))),
    ("history: the late start moved back on time",
     lambda fx: _state_edit(fx, lambda s: _last_export(s).update(
         {"vts_start": "2026-04-14T03:10:00Z",
          "vts_end": "2026-04-14T03:30:00Z"}))),
    ("history: a run dropped",
     lambda fx: _state_edit(fx, lambda s: s["runs"].pop())),
    ("history: an extra run appended",
     lambda fx: _state_edit(fx, lambda s: s["runs"].append(
         dict(_last_export(s))))),
    ("history: the failed night made green",
     lambda fx: _state_edit(fx, lambda s: [
         r.update({"status": "success"}) for r in s["runs"]
         if r["status"] == "failed"])),
    ("history: the build's one-off overrun erased",
     lambda fx: _state_edit(fx, lambda s: [
         r.update({"vts_end": "2026-04-08T02:35:00Z"}) for r in s["runs"]
         if r["job"] == "build_commercial"
         and r["vts_start"].startswith("2026-04-08")])),
    ("history: the clock moved",
     lambda fx: _state_edit(fx, lambda s: s.update(
         {"vnow": "2026-04-20T09:00:00Z"}))),
    ("world: the liveness plant leaked into the agent-visible drop",
     lambda fx: shutil.copytree(
         fx / "next_night", fx / "workspace" / "data" / "vendor_drop",
         dirs_exist_ok=True)),
    ("world: live.json's exec block removed",
     lambda fx: (fx / "live.json").write_text(json.dumps(
         {**json.loads((fx / "live.json").read_text(encoding="utf-8")),
          "jobs": {k: {i: j for i, j in v.items() if i != "exec"}
                   for k, v in json.loads((fx / "live.json").read_text(
                       encoding="utf-8"))["jobs"].items()}},
         indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="")),
    ("reference: the expected extract altered",
     lambda fx: (fx / "expected_extract.json").write_text(json.dumps(
         json.loads((fx / "expected_extract.json").read_text(
             encoding="utf-8"))[:-1], indent=2, sort_keys=True) + "\n",
         encoding="utf-8", newline="")),
    ("corpus: the trap ticket deleted",
     lambda fx: _corpus_edit(fx, "issues.yaml", "- id: DATA-7714",
                             "- id: DATA-7799")),
    ("corpus: the folk explanation rewritten out of DATA-7714",
     lambda fx: _corpus_edit(fx, "issues.yaml",
                             "title: export_commercial has outgrown its window",
                             "title: export_commercial is scheduled too late")),
    ("corpus: chat messages keyed `body` instead of `text`",
     lambda fx: _corpus_edit(fx, "chat.yaml", "        text: |-",
                             "        body: |-", count=-1)),
    ("corpus: the how-to that states the method deleted",
     lambda fx: _corpus_edit(fx, "kb.yaml",
                             "  - id: de/how-to/read-scheduler-history",
                             "  - id: de/how-to/unrelated")),
    ("corpus: a ticket filed after the world clock",
     lambda fx: _corpus_edit(fx, "issues.yaml", 'created_at: "-4d 08:12"',
                             'created_at: "+2d 08:12"')),
    ("docs: the runbook deleted",
     lambda fx: (fx / "workspace" / "docs"
                 / "runbook_commercial_nightly.md").unlink()),
]


def falsify(fx: Path = BUILD, quiet: bool = False) -> int:
    """Plant each mutation on a COPY and demand verify() raises. Returns the
    number of mutations that slipped through."""
    fx = Path(fx)
    verify(fx, quiet=True)                     # the clean fixture must pass first
    missed = []
    scratch = Path(tempfile.mkdtemp(prefix="gt007-falsify-"))
    try:
        for label, mutate in MUTATIONS:
            target = scratch / "fx"
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(fx, target)
            Path(str(target / "world.duckdb") + ".wal").unlink(missing_ok=True)
            mutate(target)
            try:
                verify(target, quiet=True)
            except FixtureDrift:
                if not quiet:
                    print(f"  caught   {label}")
                continue
            except Exception as exc:           # a crash is not a catch
                missed.append(f"{label} (verify crashed: {type(exc).__name__}: {exc})")
                if not quiet:
                    print(f"  CRASHED  {label}: {exc}")
                continue
            missed.append(label)
            if not quiet:
                print(f"  MISSED   {label}")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if not quiet:
        print(f"\n{len(MUTATIONS) - len(missed)}/{len(MUTATIONS)} mutations "
              f"caught by verify()")
        for label in missed:
            print(f"  NOT CAUGHT: {label}")
    return len(missed)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", default=str(BUILD))
    ap.add_argument("--falsify", action="store_true",
                    help="plant each known damage and require verify() to catch it")
    ap.add_argument("--contract", action="store_true",
                    help="print CONTRACT as json and exit")
    a = ap.parse_args()
    if a.contract:
        print(json.dumps(CONTRACT, indent=2, sort_keys=True))
        return 0
    if a.falsify:
        return 1 if falsify(Path(a.fixtures)) else 0
    try:
        verify(Path(a.fixtures))
    except FixtureDrift as exc:
        print(f"\nFIXTURE DRIFT\n  {exc}\n", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
