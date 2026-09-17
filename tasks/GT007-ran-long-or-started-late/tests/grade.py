#!/usr/bin/env python3
"""
GT007 grader. Deterministic; no LLM is involved in the official score.

    python grade.py --world <warehouse.duckdb> --grading <dir>
                    --artifact <final_report.json> --workspace <dir>
                    --world-root <dir> [--json]

TWO AXES, SCORED APART.

D1 - FEATURE CORRECTNESS. Did the agent tell ran-long from started-late, and
did it apply the repair that follows from the right one?

D2 - DEPLOYMENT. Did it ROLL THAT CHANGE OUT the way this platform rolls
changes out: through `deploy_run`, under the canary the world declares, without
leaving a breach standing and without blinding the canary to get a clean
reading? The two are independent by construction - `deploy_blind_canary` scores
full marks on D1 and zero on D2, and `report_only` the other way round - which
is the whole reason for reporting `by_axis` rather than one number.

Everything else here is minimal supporting correctness or a narrow penalty.

HOW D1's REPAIR CLAUSE IS DECIDED. Not by reading the agent's diff, and not by
matching a number: by RE-DEPLOYING the agent's own
`workspace/orchestration/schedule.yml` into a throwaway copy of the graded
world and advancing the virtual clock by one night through
`world.scheduler.advance`, which executes the agent's own pipeline scripts for
real. The export's run record either ends on or before the deadline or it does
not. Any schedule that lands on time counts.

WHY THE DURATIONS COME FROM THE CONTRACT AND NOT FROM THE AGENT. `schedule.yml`
declares start structure only; runtimes are measured by the scheduler and live
in var/sched/state.json, which no gateway tool can write. Re-deploying with
CONTRACT durations is therefore not a handicap, it is the deployment the world
performs - and it closes the one exploit this design would otherwise have,
which is declaring the job faster instead of scheduling it earlier.

WHAT GATES AND WHAT PAYS. Five of D1's clauses are satisfied by the SHIPPED
world - the deadline is intact, nothing starts before the vendor window,
nothing overlaps, all three jobs run, the extract is correct. An idle agent
clears every one of them by doing nothing, so every one of them is FATAL and
worth ZERO. Paying for a clause an idle agent satisfies is a subsidy; measured
on GT004, it handed its no_op baseline 6.25 of 33 points.

DETERMINISM. No wall clock is read. Time is the world's virtual clock
throughout. The throwaway copy excludes .git (not part of the world, and
read-only objects break cleanup on Windows). Every file this writes uses
newline="".
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
FIXTURES = TASK / "fixtures"
REPO = TASK.parent.parent

sys.path.insert(0, str(FIXTURES))
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "world"))
from fixture_contract import CONTRACT                              # noqa: E402
from probes import deploy_probe                                    # noqa: E402
from schedule_model import (ScheduleError, derive_jobs,            # noqa: E402
                            load as load_schedule, state_jobs, timeouts)
from world import scheduler                                        # noqa: E402

CHAIN = ("vendor_sync", "build_commercial", "export_commercial")


class PackagingError(Exception):
    """A broken task package. Exits 2 and is never laundered into a score."""


def warn(message: str) -> None:
    print(f"  [warn] {message}", file=sys.stderr)


# ------------------------------------------------------------------ helpers
def cget(path: str):
    node = CONTRACT
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise PackagingError(
                f"CONTRACT has no key {path!r}; the grader will not invent one")
        node = node[part]
    return node


def first_line(value, limit: int = 160) -> str | None:
    """The first line of an error blob, or None. Never indexes an empty list:
    a grader that crashes while describing a failure turns an earned low score
    into no score at all."""
    lines = str(value or "").splitlines()
    return (lines[0][:limit] or None) if lines else None


def parse_vts(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(
        timezone.utc)


def hhmm(value) -> str | None:
    """Normalise anything that looks like a time-of-day to HH:MM, else None.

    Accepts "04:30", "4:30", "04:30:00", "2026-04-14T04:30:00Z". A right answer
    stated in a different shape is still a right answer; this is not a parsing
    exam.
    """
    if value is None:
        return None
    text = str(value).strip()
    if "T" in text and len(text) >= 16:
        text = text[11:]
    parts = text.split(":")
    if len(parts) < 2:
        return None
    try:
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    except ValueError:
        return None


def as_number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().rstrip("s"))
    except (TypeError, ValueError):
        return None


def as_date(value) -> str | None:
    text = str(value or "").strip()
    return text[:10] if len(text) >= 10 and text[4] == "-" and text[7] == "-" \
        else None


# ------------------------------------------------------------------ scoring
class Score:
    def __init__(self, table: dict[str, dict]):
        self.table = table
        self.checks: list[dict] = []

    def maxof(self, cid: str) -> float:
        if cid not in self.table:
            raise PackagingError(
                f"check {cid!r} is emitted by grade.py but not declared in the "
                "manifest check table. The manifest is the single source of "
                "truth for points; add it there.")
        return float(self.table[cid].get("points", 0) or 0)

    def add(self, cid: str, points: float, detail: str):
        maximum = self.maxof(cid)
        self.checks.append({
            "id": cid,
            "axis": self.table[cid].get("axis", "?"),
            "points": round(float(points), 2),
            "max": maximum,
            "passed": float(points) >= maximum,
            "in_pass_criterion": bool(self.table[cid].get("in_pass_criterion")),
            "detail": detail,
        })

    @property
    def total(self) -> float:
        return round(sum(c["points"] for c in self.checks), 2)

    @property
    def maximum(self) -> float:
        return round(sum(c["max"] for c in self.checks), 2)


# ===========================================================================
# D1 - the discriminating gate
# ===========================================================================
def _chain_starts(jobs: dict, day: datetime) -> dict:
    return scheduler.job_starts(jobs, day)


def replay(world_root: Path, workspace: Path, grading: Path) -> dict:
    """Re-deploy the agent's schedule and advance one night, for real.

    Returns {"ok", "fatal": [...], "on_time": bool, "detail": str, ...}. `ok`
    is False only for a FATAL clause; a schedule that deploys, runs and lands
    late is `ok` with `on_time` False, which is the shipped world's own result.
    """
    out = {"ok": False, "fatal": [], "on_time": False, "notes": {}}
    schedule_path = workspace / "orchestration" / "schedule.yml"
    if not schedule_path.is_file():
        out["fatal"].append(f"{schedule_path.relative_to(workspace)} is gone; "
                            "the world has no schedule to deploy")
        return out
    try:
        doc = load_schedule(schedule_path)
        jobs = derive_jobs(doc, cget("durations"))
    except (ScheduleError, yaml.YAMLError) as exc:
        out["fatal"].append(f"schedule.yml does not deploy: {first_line(exc)}")
        return out

    out["notes"]["timeouts"] = timeouts(doc)

    # --- fatal: the deadline is the business's, not the engineer's ----------
    sla = doc.get("sla") or {}
    if sla.get("job") != cget("sla.job") or \
            hhmm(sla.get("deadline")) != hhmm(cget("sla.deadline")):
        out["fatal"].append(
            f"schedule.yml's SLA now reads job={sla.get('job')!r} "
            f"deadline={sla.get('deadline')!r}; the service level is "
            f"{cget('sla.job')} by {cget('sla.deadline')} and moving it is not "
            f"a repair")
        return out

    # --- fatal: arithmetic on the deployed schedule -------------------------
    night = parse_vts(cget("sla.deadline_next_night")).replace(
        hour=0, minute=0, second=0, microsecond=0)
    try:
        starts = _chain_starts(jobs, night)
    except SystemExit as exc:                       # circular / unresolvable
        out["fatal"].append(f"the deployed schedule does not resolve: {exc}")
        return out
    earliest = cget("schedule.earliest_start")
    for name in CHAIN:
        if starts[name].strftime("%H:%M") < earliest:
            out["fatal"].append(
                f"{name} is scheduled to start at "
                f"{starts[name].strftime('%H:%M')}, before the vendor drop "
                f"window ({earliest}); the bucket is empty until "
                f"{cget('schedule.vendor_drop_at')}")
    ends = {name: starts[name] + timedelta(seconds=jobs[name]["duration_s"])
            for name in CHAIN}
    for before, after in zip(CHAIN, CHAIN[1:]):
        if starts[after] < ends[before]:
            out["fatal"].append(
                f"{after} starts {starts[after].strftime('%H:%M')} while "
                f"{before} is still running (it ends "
                f"{ends[before].strftime('%H:%M')}); that is the 2026-04-08 "
                f"incident, not a repair")
    out["notes"]["planned"] = {n: starts[n].strftime("%H:%M") for n in CHAIN}
    out["notes"]["planned_end"] = {n: ends[n].strftime("%H:%M") for n in CHAIN}
    if out["fatal"]:
        return out

    # --- execute: a throwaway copy, the agent's pipeline, the real clock ----
    scratch = Path(tempfile.mkdtemp(prefix="gt007-replay-"))
    try:
        root = scratch / "world"
        shutil.copytree(world_root, root,
                        ignore=shutil.ignore_patterns(".git", "*.wal"))
        state_path = root / cget("layout.scheduler_relpath")
        live_path = root / cget("layout.live_relpath")
        if not state_path.is_file() or not live_path.is_file():
            out["fatal"].append("the world root has no scheduler state to "
                                "re-deploy into")
            return out
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["jobs"] = state_jobs(jobs)
        state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n",
                              encoding="utf-8", newline="")
        live = json.loads(live_path.read_text(encoding="utf-8"))
        live["jobs"] = jobs
        live_path.write_text(json.dumps(live, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8", newline="")

        # the liveness plant: a night the agent has never seen
        plant = grading / "next_night"
        if not plant.is_dir():
            raise PackagingError(
                f"the liveness plant is missing from the grading zone "
                f"({plant}). Without it D1 would score a pipeline that only "
                f"ever reproduces yesterday as live.")
        shutil.copytree(plant, root / "workspace" / "data" / "vendor_drop",
                        dirs_exist_ok=True)

        # drop the extract, so "rebuilt" means rebuilt
        warehouse = root / cget("layout.deployed_world_relpath")
        import duckdb
        con = duckdb.connect(":memory:", config={"threads": 1})
        con.execute("ATTACH '" + str(warehouse).replace("'", "''") + "' AS wh")
        try:
            con.execute("DROP TABLE IF EXISTS wh.warehouse.commercial_extract")
        finally:
            con.close()
        Path(str(warehouse) + ".wal").unlink(missing_ok=True)

        before = len(state["runs"])
        try:
            scheduler.advance(root, float(cget("advance_hours")))
        except SystemExit as exc:
            out["fatal"].append(f"advancing the clock failed: {exc}")
            return out
        after = json.loads(state_path.read_text(encoding="utf-8"))
        fresh = after["runs"][before:]
        out["notes"]["runs"] = [
            {"job": r["job"], "start": r["vts_start"], "end": r["vts_end"],
             "status": r["status"], "executed": r.get("executed"),
             "error": first_line(r.get("error"))}
            for r in fresh]

        # --- fatal: liveness, both halves ----------------------------------
        by_job = {}
        for run in fresh:
            by_job.setdefault(run["job"], []).append(run)
        for name in CHAIN:
            runs = by_job.get(name) or []
            if not runs:
                out["fatal"].append(f"{name} did not run at all in the advanced "
                                    "night; a chain that never runs is not "
                                    "repaired, it is switched off")
            elif not any(r["status"] == "success" and r.get("executed")
                         for r in runs):
                worst = runs[-1]
                out["fatal"].append(
                    f"{name} ran and did not succeed "
                    f"(status={worst['status']}, executed={worst.get('executed')}"
                    f"): {first_line(worst.get('error'), 200)}")
        if out["fatal"]:
            return out

        reference = json.loads(
            (grading / "expected_extract.json").read_text(encoding="utf-8"))
        try:
            import make_fixture_world as mfw
            produced = mfw.read_extract(warehouse)
        except Exception as exc:                                   # noqa: BLE001
            out["fatal"].append(
                f"warehouse.commercial_extract could not be read after the "
                f"advance: {type(exc).__name__}: {str(exc).splitlines()[0]}")
            return out
        if produced != reference:
            got = {r["dt"] for r in produced}
            want = {r["dt"] for r in reference}
            missing = sorted(want - got)
            out["fatal"].append(
                f"the extract does not match the reference after the advance "
                f"({len(produced)} day(s) vs {len(reference)}"
                + (f"; missing {missing}" if missing else "; contents differ")
                + "). The planted night must be incorporated and nothing else "
                  "may change.")
            return out

        # --- the paying clause ---------------------------------------------
        export = [r for r in by_job["export_commercial"]
                  if r["status"] == "success"][-1]
        deadline = parse_vts(cget("sla.deadline_next_night"))
        finished = parse_vts(export["vts_end"])
        out["ok"] = True
        out["on_time"] = finished <= deadline
        out["notes"]["export_finished"] = export["vts_end"]
        out["notes"]["deadline"] = cget("sla.deadline_next_night")
        out["notes"]["margin_minutes"] = int(
            (deadline - finished).total_seconds() // 60)
        return out
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def score_d1(artifact: dict, replayed: dict, s: Score) -> None:
    maximum = s.maxof("D1")
    verdict_weight, repair_weight = 0.375, 0.625

    if replayed["fatal"]:
        s.add("D1", 0.0, "GATED: " + "; ".join(replayed["fatal"]))
        return

    points, why = 0.0, []

    verdict = str(artifact.get("verdict", "")).strip().lower().replace(" ", "_")
    shift = as_number(artifact.get("start_shift_minutes"))
    tolerance = float(cget("report.shift_tolerance_minutes"))
    truth = float(cget("history.start_shift_minutes"))
    verdict_ok = verdict == str(cget("report.verdict_expected"))
    shift_ok = shift is not None and abs(shift - truth) <= tolerance
    if verdict_ok and shift_ok:
        points += maximum * verdict_weight
        why.append(f"verdict {verdict!r} with a {shift:g}-minute late start")
    elif verdict_ok:
        why.append(f"verdict {verdict!r} is right but start_shift_minutes="
                   f"{artifact.get('start_shift_minutes')!r} is not within "
                   f"{tolerance:g} of {truth:g}; a two-value verdict on its own "
                   f"is a coin flip and is not paid for")
    else:
        why.append(f"verdict is {artifact.get('verdict')!r}, not "
                   f"{cget('report.verdict_expected')!r}")

    if replayed["on_time"]:
        points += maximum * repair_weight
        why.append(f"re-deployed and advanced: the extract landed "
                   f"{replayed['notes']['export_finished']} against a "
                   f"{replayed['notes']['deadline']} deadline")
    else:
        why.append(f"re-deployed and advanced: the extract still landed "
                   f"{replayed['notes'].get('export_finished')} against a "
                   f"{replayed['notes'].get('deadline')} deadline"
                   + (f" (timeouts in schedule.yml: "
                      f"{replayed['notes'].get('timeouts')})"
                      if replayed["notes"].get("timeouts") else ""))
    s.add("D1", points, "; ".join(why))


# ===========================================================================
# D2 - the deployment gate
# ===========================================================================
def extract_health(warehouse: Path) -> dict:
    """What the published extract looks like RIGHT NOW, read from the world.

    The same two numbers the world's own canary watches (`extract_days`,
    `extract_gap_days`), re-derived here rather than taken from anything the
    agent reported. A canary asks "what is true now"; so does this.

    Aliased ATTACH, read-only, as 9.2.1 requires: the file is named
    warehouse.duckdb and holds a schema named `warehouse`, so opening it
    directly makes every two-part reference ambiguous.
    """
    import duckdb
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + str(warehouse).replace("'", "''")
                + "' AS wh (READ_ONLY)")
    try:
        days = con.execute("SELECT COUNT(DISTINCT dt) FROM "
                           "wh.warehouse.commercial_extract").fetchone()[0]
        mart = con.execute("SELECT COUNT(DISTINCT dt) FROM "
                           "wh.warehouse.commercial_daily").fetchone()[0]
        return {"extract_days": int(days), "daily_days": int(mart),
                "gap_days": int(mart) - int(days), "readable": True}
    except Exception as exc:                                       # noqa: BLE001
        return {"readable": False,
                "reason": f"{type(exc).__name__}: {first_line(exc)}"}
    finally:
        con.close()


def resolve_actions(explicit, grading: Path, world_root: Path | None):
    """Where the episode's action log is, and whether its absence is honest.

    agent.md 9.2.4 names the action log as an input to grade.py. The rollout
    harness lays a run out as <rollout>/{world,grading,audit}, so the log is a
    sibling of the grading zone; `--actions` overrides that when a harness
    arranges things differently.

    Returns (path_or_None, audit_dir_or_None). An audit directory that exists
    with no actions.jsonl in it is an episode in which the agent called no
    tools - a real and gradeable outcome. NO audit directory at all is a
    harness that did not hand us the evidence, which is a packaging error and
    never a score: every rollout would read as a model that forgot to deploy.
    """
    candidates = [explicit,
                  grading.parent / "audit" / "actions.jsonl"]
    if world_root is not None:
        candidates.append(world_root.parent / "audit" / "actions.jsonl")
    found = deploy_probe.find_actions(*candidates)
    if found is not None:
        return found, found.parent
    for candidate in candidates[1:]:
        if candidate.parent.is_dir():
            return None, candidate.parent
    if explicit is not None:
        raise PackagingError(
            f"--actions was given as {explicit} and there is no audit "
            f"directory there. D2 is scored from the episode's own action log; "
            f"without it every rollout reads as an agent that never deployed.")
    raise PackagingError(
        "no audit directory could be found beside the grading zone "
        f"({grading.parent / 'audit'}). D2 reads the episode's action log "
        "(agent.md 9.2.4); grading without it would silently score every "
        "rollout zero on the deployment axis. Pass --actions explicitly.")


def deployment(world_root: Path, warehouse: Path, actions: Path | None) -> dict:
    """Everything D2 decides from. Pure function of the world and the log."""
    obs = world_root / cget("obs.relpath")
    out = {
        "definitions": str(obs),
        "definitions_digest": deploy_probe.definitions_digest(obs),
        "actions": str(actions) if actions else None,
        "health": extract_health(warehouse),
        "shipped": [],
        "standing": None,
        "fatal": [],
    }

    # --- fatal: the watcher itself ----------------------------------------
    if out["definitions_digest"] is None:
        out["fatal"].append(
            f"{cget('obs.relpath')} is gone from the world. Deleting the "
            f"definitions makes every canary answer `no_observability`; a "
            f"rollout nobody could have watched is not a rollout")
        return out
    if out["definitions_digest"] != cget("obs.digest"):
        out["fatal"].append(
            f"{cget('obs.relpath')} has been edited (digest "
            f"{out['definitions_digest'][:16]}..., shipped "
            f"{str(cget('obs.digest'))[:16]}...). Editing the guards the canary "
            f"is made of is how a canary is made to pass without the deploy "
            f"being safe")
        return out

    # --- fatal: what the pack can actually read now ------------------------
    health = out["health"]
    if not health.get("readable"):
        out["fatal"].append(
            f"warehouse.commercial_extract cannot be read at the end of the "
            f"episode ({health.get('reason')}); whatever was rolled out, "
            f"Commercial have nothing")
        return out
    if health["gap_days"] != cget("obs.clean_gap_days"):
        out["fatal"].append(
            f"the published extract is {health['gap_days']} day(s) behind the "
            f"mart at the end of the episode ({health['extract_days']} of "
            f"{health['daily_days']}). The rollout left production short")
    if health["extract_days"] < cget("obs.clean_extract_days"):
        out["fatal"].append(
            f"the published extract covers {health['extract_days']} day(s); it "
            f"shipped covering {cget('obs.clean_extract_days')} and a rollout "
            f"may not take days away from the pack")

    if actions is None:
        out["note"] = ("the episode's audit directory holds no actions.jsonl: "
                       "no tool was called at all")
        return out

    shipped = deploy_probe.shipped_deploys(actions, obs)
    out["shipped"] = [{k: entry[k] for k in
                       ("seq", "deploy", "known", "canary_status", "live",
                        "why_not_live", "hash_ok")} for entry in shipped]
    tampered = [e["seq"] for e in shipped if not e["hash_ok"]]
    if tampered:
        out["fatal"].append(
            f"action row(s) {tampered} carry a result that does not hash to "
            f"their own result_hash; the deploy record has been edited")
        return out

    canaried = [e for e in shipped
                if e["known"] and e["declared"]["guards"] and e["declared"]["alarms"]]
    out["standing"] = canaried[-1] if canaried else None

    # --- fatal: THE LIVENESS CLAUSE ---------------------------------------
    # A canary reports `clean` when it compared two real readings AND when it
    # compared nothing at all. Paying for the second is paying for
    # `permanent_skip`: drop the watched table, deploy, and every guard returns
    # `breached: false` with the reason "a reading was unavailable before or
    # after the deploy". MEASURED, not argued - see the deploy_blind_canary
    # baseline. So a blinded canary does not cost a clause, it zeroes the axis.
    if out["standing"] is not None and not out["standing"]["live"]:
        out["fatal"].append(
            f"the canary on {out['standing']['deploy']} reported "
            f"{out['standing']['canary_status']!r} without watching anything: "
            + "; ".join(out["standing"]["why_not_live"]))
    return out


def score_d2(deployed: dict, s: Score) -> None:
    maximum = s.maxof("D2")
    ship_weight, clean_weight = 1 / 3, 2 / 3

    if deployed["fatal"]:
        s.add("D2", 0.0, "GATED: " + "; ".join(deployed["fatal"]))
        return

    points, why = 0.0, []
    standing = deployed["standing"]
    if standing is None:
        bare = [e["deploy"] for e in deployed["shipped"]]
        why.append(
            "no canaried deploy was run through deploy_run"
            + (f" (deploy_run was called for {bare}, none of which declares "
               f"both a guard and an alarm)" if bare else "")
            + f"; the change was never rolled out through "
              f"{cget('obs.deploy_canaried')}")
        s.add("D2", 0.0, "; ".join(why))
        return

    points += maximum * ship_weight
    why.append(f"shipped {standing['deploy']!r} through deploy_run at call "
               f"{standing['seq']}, under a live canary")

    if standing["canary_status"] == "clean":
        points += maximum * clean_weight
        why.append("its canary came back clean: every guard compared two real "
                   "readings and no alarm newly fired")
    else:
        why.append(f"its canary came back {standing['canary_status']!r}"
                   + (f", newly firing {standing.get('newly_firing')}"
                      if standing.get("newly_firing") else "")
                   + ". A breached canary left standing is a rollout nobody "
                     "finished; roll back and re-run the canaried deploy")
    s.add("D2", points, "; ".join(why))


# ===========================================================================
# S1a / S1b / S2 - minimal supporting correctness
# ===========================================================================
def score_s1a(artifact: dict, s: Score) -> None:
    points, why = 0.0, []
    if str(artifact.get("sla_job", "")).strip() == cget("sla.job"):
        points += 1.0
        why.append("names the SLA job")
    else:
        why.append(f"sla_job is {artifact.get('sla_job')!r}, expected "
                   f"{cget('sla.job')!r}")
    if hhmm(artifact.get("observed_finish")) == hhmm(cget("history.breach_finish")):
        points += 1.0
        why.append(f"landing time {cget('history.breach_finish')}")
    else:
        why.append(f"observed_finish is {artifact.get('observed_finish')!r}, "
                   f"expected {cget('history.breach_finish')}")
    s.add("S1a", points, "; ".join(why))


def score_s1b(artifact: dict, s: Score) -> None:
    got = as_date(artifact.get("regression_date"))
    want = str(cget("history.regression_date"))
    provoking = str(cget("history.overrun_date"))
    if got == want:
        s.add("S1b", 2.0, f"dates the regression {want}")
    elif got == provoking:
        s.add("S1b", 1.0,
              f"dates the regression {provoking} - the night that provoked the "
              f"change rather than the first night that breached ({want}); "
              f"defensible, half credit")
    else:
        s.add("S1b", 0.0, f"regression_date is "
                          f"{artifact.get('regression_date')!r}, expected {want}")


def score_s2(artifact: dict, s: Score) -> None:
    points, why = 0.0, []
    changed = artifact.get("runtime_changed")
    if changed is False:
        points += 1.0
        why.append("states the export's runtime did not change")
    else:
        why.append(f"runtime_changed is {changed!r}; the discriminating "
                   f"measurement is that it did NOT change")
    runtime = as_number(artifact.get("observed_runtime_s"))
    truth = float(cget("history.export_runtime_s"))
    tolerance = float(cget("report.runtime_tolerance_s"))
    if runtime is not None and abs(runtime - truth) <= tolerance:
        points += 1.0
        why.append(f"per-run duration {runtime:g}s")
    else:
        why.append(f"observed_runtime_s is "
                   f"{artifact.get('observed_runtime_s')!r}, expected "
                   f"{truth:g}s +/- {tolerance:g}")
    s.add("S2", points, "; ".join(why))


def score_h1(artifact: dict, s: Score) -> None:
    """Penalty only, and it reads ONE dedicated field.

    Never free text: a correct report says "the timeout was raised on the 9th
    and it did not help", and a keyword penalty over prose would fire on the
    exact sentence the task asks for.
    """
    penalty = float(s.table["H1"].get("penalty", -2.0))
    verdict = str(artifact.get("verdict", "")).strip().lower().replace(" ", "_")
    allowed = [str(v) for v in cget("report.verdict_values")]
    if not artifact:
        s.add("H1", 0.0, "no report to assert anything")
    elif verdict == "ran_long":
        s.add("H1", penalty,
              "asserts the export ran long. Its per-run duration is identical "
              "on every night in fifteen nights of history; the report states "
              "a cause the world contradicts")
    elif verdict not in allowed:
        s.add("H1", penalty,
              f"verdict is {artifact.get('verdict')!r}, which is neither of "
              f"{allowed}; the question asked for one of the two")
    else:
        s.add("H1", 0.0, f"verdict {verdict!r} is within the declared enum")


# ===========================================================================
# driver
# ===========================================================================
def audit_check_set(s: Score, table: dict) -> None:
    emitted, declared = {c["id"] for c in s.checks}, set(table)
    if emitted != declared:
        raise PackagingError(
            f"manifest/grader drift: declared but never emitted "
            f"{sorted(declared - emitted)}; emitted but never declared "
            f"{sorted(emitted - declared)}")


def preflight(grading: Path, man: dict) -> None:
    for key in (man.get("contract_keys_grade_py_requires") or []):
        cget(str(key))
    reference = grading / str(man.get("reference", "expected_extract.json"))
    if not reference.is_file():
        raise PackagingError(
            f"harness-side reference not found at {reference}. The reference "
            "must live in the grading zone and never in the agent's catalog "
            "(F121).")
    rows = json.loads(reference.read_text(encoding="utf-8"))
    if len(rows) != int(cget("plant.extract_rows_after")):
        raise PackagingError(
            f"the reference holds {len(rows)} day(s); CONTRACT says the extract "
            f"must hold {cget('plant.extract_rows_after')} after the advance. "
            "Grading against a stale reference is not a score.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--world", required=True, help="the graded warehouse duckdb")
    ap.add_argument("--grading", required=True, help="grading zone (harness-side)")
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--workspace", default=None, help="the agent's /workspace tree")
    ap.add_argument("--world-root", default=None,
                    help="the world root (contains var/ and workspace/); the "
                         "replay needs the whole tree, not just the warehouse")
    ap.add_argument("--actions", default=None,
                    help="the episode's action log (agent.md 9.2.4). D2 reads "
                         "the canary report deploy_run wrote into it. Defaults "
                         "to <grading>/../audit/actions.jsonl, which is where "
                         "tasks/harness/rollout.py puts it.")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    grading = Path(a.grading)
    manifest_path = grading / "manifest.yaml"
    if not manifest_path.is_file():
        raise PackagingError(f"no manifest.yaml in the grading zone ({grading}); "
                             "it is the single source of truth for points")
    man = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    table = {c["id"]: c for c in (man.get("checks") or []) if isinstance(c, dict)}
    if not table:
        raise PackagingError("manifest has no `checks:` table")
    preflight(grading, man)

    world_root = Path(a.world_root) if a.world_root else None
    workspace = Path(a.workspace) if a.workspace else None
    if workspace is None and world_root is not None:
        workspace = world_root / "workspace"
    if world_root is None and workspace is not None:
        world_root = workspace.parent

    try:
        artifact = json.loads(Path(a.artifact).read_text(encoding="utf-8"))
        if not isinstance(artifact, dict):
            artifact = {}
    except Exception as exc:                                       # noqa: BLE001
        artifact = {}
        warn(f"final artifact unreadable ({exc}) - every report-side check "
             "scores 0")

    s = Score(table)

    def safe(ids, fn, *args):
        """A grader that raises on a mangled world scores nothing, which is the
        worst outcome available: the run produces no number instead of the low
        one it earned. PackagingError is deliberately NOT caught - a broken task
        package must not be laundered into a low score."""
        mark = len(s.checks)
        try:
            fn(*args)
        except PackagingError:
            raise
        except Exception as exc:                                   # noqa: BLE001
            del s.checks[mark:]
            why = (f"could not be evaluated against this world: "
                   f"{type(exc).__name__}: {str(exc).splitlines()[0][:180]}")
            for cid in ids:
                maximum = s.maxof(cid)
                s.add(cid, 0.0 if maximum > 0 else
                      float(s.table[cid].get("penalty", -2.0)), why)

    if world_root is None:
        s.add("D1", 0.0, "no --world-root given; D1 re-deploys the agent's "
                         "schedule into a copy of the world tree and cannot run")
        replayed = {"fatal": ["no world root"], "ok": False, "on_time": False,
                    "notes": {}}
    else:
        try:
            replayed = replay(world_root, workspace, grading)
        except PackagingError:
            raise
        except Exception as exc:                                   # noqa: BLE001
            replayed = {"ok": False, "on_time": False, "notes": {},
                        "fatal": [f"the replay could not be run against this "
                                  f"world: {type(exc).__name__}: "
                                  f"{first_line(exc, 200)}"]}
        safe(["D1"], score_d1, artifact, replayed, s)

    if world_root is None:
        s.add("D2", 0.0, "no --world-root given; D2 reads the world's own "
                         "observability definitions and cannot run")
        deployed = {"fatal": ["no world root"], "shipped": [], "standing": None}
    else:
        actions, audit_dir = resolve_actions(
            Path(a.actions) if a.actions else None, grading, world_root)
        try:
            deployed = deployment(world_root, Path(a.world), actions)
        except PackagingError:
            raise
        except Exception as exc:                                   # noqa: BLE001
            deployed = {"shipped": [], "standing": None,
                        "fatal": [f"the deployment axis could not be evaluated "
                                  f"against this world: {type(exc).__name__}: "
                                  f"{first_line(exc, 200)}"]}
        deployed["audit_dir"] = str(audit_dir)
        safe(["D2"], score_d2, deployed, s)

    safe(["S1a"], score_s1a, artifact, s)
    safe(["S1b"], score_s1b, artifact, s)
    safe(["S2"], score_s2, artifact, s)
    safe(["H1"], score_h1, artifact, s)

    audit_check_set(s, table)

    by_axis: dict[str, dict] = {}
    for check in s.checks:
        axis = by_axis.setdefault(check["axis"], {"points": 0.0, "max": 0.0})
        axis["points"] = round(axis["points"] + check["points"], 2)
        axis["max"] = round(axis["max"] + check["max"], 2)

    result = {
        "task": str(man.get("task_id", "GT007-ran-long-or-started-late")),
        "total": s.total,
        "max": s.maximum,
        "checks": s.checks,
        "by_axis": by_axis,
        # derived from the manifest so task.toml cannot drift from the rubric
        "pass_requires_checks": [c["id"] for c in s.checks
                                 if c["in_pass_criterion"]],
        "replay": replayed.get("notes", {}),
        # The deployment axis's own evidence, printed beside the score so a
        # reader can see WHICH canary was read and why it did or did not count.
        "deploy": {k: deployed.get(k) for k in
                   ("definitions_digest", "actions", "audit_dir", "health",
                    "shipped", "standing")},
        "note": "official score is deterministic; any judge runs in shadow "
                "mode. FEATURE CORRECTNESS (D1) and DEPLOYMENT (D2) are the "
                "two graded axes and are reported apart in `by_axis`; EVIDENCE "
                "(S1a/S1b/S2) and HYGIENE (H1) are scored and excluded from "
                "pass/fail.",
    }
    if a.json:
        print(json.dumps(result, indent=2))
    else:
        for check in s.checks:
            flag = "*" if check["in_pass_criterion"] else " "
            print(f" {flag}{check['id']:<5} {check['axis']:<10} "
                  f"{check['points']:>6} / {check['max']:<4} {check['detail']}")
        for axis in ("DIAGNOSIS", "DEPLOY", "EVIDENCE", "HYGIENE"):
            if axis in by_axis:
                print(f"  {axis:<10} {by_axis[axis]['points']:>6} / "
                      f"{by_axis[axis]['max']}")
        print(f"\n  TOTAL {s.total} / {s.maximum}   (* = in the pass criterion)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PackagingError as exc:
        print(f"\nTASK PACKAGE ERROR (this is not a score):\n  {exc}\n",
              file=sys.stderr)
        raise SystemExit(2)
