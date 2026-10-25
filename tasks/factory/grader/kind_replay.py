"""Advance an ISOLATED COPY of a world over held-out nights and keep the receipts.

Generalised from `tasks/GT009-settlement-cutoff/tests/grade.py:replay`. The
world root the agent left is evidence for the t0 checks, so it is never
advanced: the copy is (`isolated_copy`), and the t0 checks can run before,
after or concurrently with the replay and read the same bytes.

The replay, in order - each step a spec key, nothing theme-specific in code:

  1. DEPLOY (pluggable). The agent's schedule is turned into the scheduler's
     `jobs` mapping and installed into the copy's `var/sched/state.json` and
     `var/world/live.json`. Themes deploy differently, so this is a strategy:
     a callable passed as `deploy=`, or `spec["replay"]["deploy"]["strategy"]`
     naming one in `DEPLOY_STRATEGIES`. The shipped default,
     `schedule_model`, is GT009's: the task's `fixtures/schedule_model.py`
     (`deployed_jobs`, `state_jobs`) loaded by path, plus the earliest-start
     gate. `none` advances whatever the world already has deployed.
  2. RESTORE the platform's own code from the grading zone over the copy
     (`platform_restore`): it sits in the workspace so the agent can read it;
     what runs at the cutoff is not the agent's to change. The edit is
     recorded (`platform_edited`) rather than honoured.
  3. PLANT night k's held-out inputs (`plant` entries, `{date}` / `{night}`
     substituted from `plant_dates`) and `world.scheduler.advance` the copy
     by `hours_per_night`; record the night's runs, the per-job liveness bit
     and an end-of-night canonical snapshot of every governed target.
  4. READ the copy's ledger (`var/ledger/receipts.jsonl`) and final tables.

SPEC KEYS READ HERE (besides `targets`, `nights`, `hours_per_night`, which
receipt_engine documents):

    plant_dates          list[str], one per night (substituted as {date})
    schedule_relpath     workspace-relative path of the agent's schedule
                         (also accepted under layout.schedule_relpath)
    layout               {"warehouse_relpath": "var/warehouse/warehouse.duckdb",
                          "scheduler_relpath": "var/sched/state.json",
                          "live_relpath": "var/world/live.json"}
    replay               {
      "plant": [{"from": "next_nights/feed/dt={date}",      # under <grading>
                 "to": "workspace/feed/dt={date}"}],        # under the copy
      "platform_restore": [{"from": "platform", "to": "workspace/.platform"}],
      "deploy": {"strategy": "schedule_model" | "none",
                 "module_dir": "<dir holding schedule_model.py>",  # abs or repo-rel
                 "durations": {job: s}, "default_duration_s": int,
                 "earliest_start": "HH:MM", "probe_day": "2026-01-01"},
      "writer_token": "{db}",          # a job whose exec args carry it writes
      "liveness_exclude_jobs": [...],  # platform jobs not judged for liveness
      "final_tables": [...],           # read off the copy after the last night
      "copy_ignore": [".git", "*.wal"]
    }
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import duckdb
import yaml

from . import kind_receipts as engine

REPO = Path(__file__).resolve().parents[3]
DEFAULT_LAYOUT = {"warehouse_relpath": "var/warehouse/warehouse.duckdb",
                  "scheduler_relpath": "var/sched/state.json",
                  "live_relpath": "var/world/live.json"}
DEFAULT_IGNORE = (".git", "*.wal")


class ReplayError(Exception):
    """A named reason the replay cannot proceed; becomes `fatal` (every night
    gated), never a traceback and never a silent zero."""


class PackagingError(ReplayError):
    """The grading zone lacks what the spec says it ships. Not caught by
    `replay`: a missing held-out night is an author defect, not an agent's."""


def _scheduler():
    """`world.scheduler`, from `<repo>/world` - the repo root holds a `world/`
    directory that would otherwise be imported as an empty namespace package."""
    world_pkg = str(REPO / "world")
    if world_pkg not in sys.path:
        sys.path.insert(0, world_pkg)
    loaded = sys.modules.get("world")
    if loaded is not None and getattr(loaded, "__file__", None) is None:
        for name in [m for m in sys.modules if m == "world" or m.startswith("world.")]:
            del sys.modules[name]
    from world import scheduler                                    # noqa: PLC0415
    return scheduler


def _layout(spec: dict) -> dict:
    return {**DEFAULT_LAYOUT, **(spec.get("layout") or {})}


def _replay_spec(spec: dict) -> dict:
    return spec.get("replay") or {}


def isolated_copy(world_root: Path, dest_parent: Path | None = None,
                  ignore=DEFAULT_IGNORE) -> Path:
    """Copy `world_root` to `<dest_parent or a fresh tempdir>/world` and return
    it. `.git` and duckdb WAL files are skipped, as GT009's replay does: a WAL
    beside a closed warehouse is a crash artefact, not state."""
    parent = Path(dest_parent) if dest_parent else Path(
        tempfile.mkdtemp(prefix="replay-isolation-"))
    parent.mkdir(parents=True, exist_ok=True)
    root = parent / "world"
    shutil.copytree(world_root, root, ignore=shutil.ignore_patterns(*ignore))
    return root


def _same_tree(a: Path, b: Path) -> bool:
    if not b.is_dir():
        return False
    left = {p.relative_to(a).as_posix(): p.read_bytes()
            for p in a.rglob("*") if p.is_file()}
    right = {p.relative_to(b).as_posix(): p.read_bytes()
             for p in b.rglob("*") if p.is_file()}
    return left == right


# ===========================================================================
# deploy strategies
# ===========================================================================
# A strategy is `deploy(workspace, grading, spec) -> {"jobs", "planned",
# "install"}`, where `install(root)` writes the deployment into the copy. It
# raises ReplayError for a schedule that does not deploy. Parsing happens
# before the copy is made and installing after, as GT009 orders it, so a
# schedule that cannot deploy costs no copy.
def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PackagingError(f"{path} is not importable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8", newline="")


def deploy_schedule_model(workspace: Path, grading: Path, spec: dict) -> dict:
    """GT009's deployment: `<module_dir>/schedule_model.py` turns the agent's
    schedule into jobs (agent jobs + the platform's), the resolved start times
    are checked against `earliest_start` on a probe day, and `install` writes
    `state_jobs(jobs)` / `jobs` into the copy's state and live files."""
    cfg = _replay_spec(spec).get("deploy") or {}
    module_dir = Path(cfg.get("module_dir") or "")
    if not module_dir.is_absolute():
        module_dir = REPO / module_dir
    model = _load_module(module_dir / "schedule_model.py",
                         f"_schedule_model_{abs(hash(str(module_dir)))}")
    rel = spec.get("schedule_relpath") or _layout(spec).get("schedule_relpath")
    schedule_path = Path(workspace) / rel
    if not schedule_path.is_file():
        raise ReplayError(f"{rel} is gone; nothing can be deployed")
    try:
        doc = yaml.safe_load(schedule_path.read_text(encoding="utf-8"))
        jobs = model.deployed_jobs(doc if isinstance(doc, dict) else {},
                                   cfg.get("durations") or {},
                                   int(cfg.get("default_duration_s", 60)))
    except (model.ScheduleError, yaml.YAMLError, AttributeError) as exc:
        raise ReplayError(f"{Path(rel).name} does not deploy: "
                          f"{engine.first_line(exc)}") from None
    scheduler = _scheduler()
    probe = datetime.fromisoformat(str(cfg.get("probe_day", "2026-01-01"))
                                   ).replace(tzinfo=timezone.utc)
    try:
        starts = scheduler.job_starts(jobs, probe)
    except SystemExit as exc:
        raise ReplayError(f"the deployed schedule does not resolve: {exc}") from None
    earliest = cfg.get("earliest_start")
    if earliest:
        for name, start in starts.items():
            if start.strftime("%H:%M") < earliest and start.date() == probe.date():
                raise ReplayError(f"{name} is scheduled at "
                                  f"{start.strftime('%H:%M')}, before the feed "
                                  f"lands ({earliest})")
    planned = {name: starts[name].strftime("%H:%M") for name in jobs}

    def install(root: Path) -> None:
        layout = _layout(spec)
        state_path = root / layout["scheduler_relpath"]
        live_path = root / layout["live_relpath"]
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["jobs"] = model.state_jobs(jobs)
        _write_json(state_path, state)
        live = json.loads(live_path.read_text(encoding="utf-8"))
        live["jobs"] = jobs
        _write_json(live_path, live)

    return {"jobs": jobs, "planned": planned, "install": install}


def deploy_none(workspace: Path, grading: Path, spec: dict) -> dict:
    """Advance whatever the world already has deployed (themes whose agent
    edits the live control plane directly, or a re-run of a frozen world)."""
    return {"jobs": None, "planned": {}, "install": lambda root: None}


DEPLOY_STRATEGIES: dict[str, Callable] = {"schedule_model": deploy_schedule_model,
                                          "none": deploy_none}


def resolve_deploy(spec: dict, deploy=None) -> Callable:
    if callable(deploy):
        return deploy
    name = deploy if isinstance(deploy, str) else (
        (_replay_spec(spec).get("deploy") or {}).get("strategy", "schedule_model"))
    if name not in DEPLOY_STRATEGIES:
        raise PackagingError(f"unknown deploy strategy {name!r}; known: "
                             f"{sorted(DEPLOY_STRATEGIES)}")
    return DEPLOY_STRATEGIES[name]


# ===========================================================================
# the replay
# ===========================================================================
def _subst(template: str, night: int, date: str | None) -> str:
    return str(template).format(night=night, date=date or "")


def _snapshot(warehouse: Path, targets: list[str]) -> dict:
    con = duckdb.connect(":memory:", config={"threads": 1})
    try:
        con.execute("ATTACH '" + Path(warehouse).as_posix().replace("'", "''")
                    + "' AS wh (READ_ONLY)")
        con.execute("USE wh")
        return {t: engine.canonical_hash(con, t) for t in targets}
    finally:
        con.close()


def _jobs_ok(fresh: list, jobs: dict | None, rs: dict) -> bool:
    """Judged per JOB, not per attempt: a burned first attempt followed by a
    successful retry is alive. Only jobs whose exec args carry the writer
    token (they write the warehouse) and are not platform jobs are judged."""
    token = rs.get("writer_token", "{db}")
    exclude = set(rs.get("liveness_exclude_jobs") or ())
    writers: dict = {}
    for record in fresh:
        job_spec = (jobs or {}).get(record["job"]) or {}
        if record["job"] in exclude:
            continue
        if token not in ((job_spec.get("exec") or {}).get("args") or []):
            continue
        writers.setdefault(record["job"], []).append(record)
    return bool(writers) and all(
        any(r["status"] == "success" and r.get("executed") for r in runs)
        for runs in writers.values())


def replay(world_root: Path, workspace: Path, grading: Path, spec: dict, *,
           deploy=None, keep: bool = False, dest_parent: Path | None = None) -> dict:
    """Copy the world, deploy the agent's schedule, plant the nights, advance.

    Returns GT009 `replay()`'s keys - fatal, runs, receipts, receipts_why,
    final, planned, platform_edited, jobs_ok, snapshots - plus `moved`
    ({target: changed across the replay}), `<role>_moved` for mapping-form
    targets, and `copy` (the advanced copy's path when `keep`, else None).
    """
    world_root, grading = Path(world_root), Path(grading)
    workspace = Path(workspace) if workspace else world_root / "workspace"
    rs = _replay_spec(spec)
    layout = _layout(spec)
    targets = engine.targets_of(spec)
    nights = int(spec["nights"])
    hours = float(spec["hours_per_night"])
    dates = list(spec.get("plant_dates") or [])
    out = {"fatal": None, "runs": [], "receipts": [], "receipts_why": "",
           "final": {}, "planned": {}, "platform_edited": None,
           "jobs_ok": [], "snapshots": {}, "moved": {}, "copy": None}
    strategy = resolve_deploy(spec, deploy)
    try:
        deployment = strategy(workspace, grading, spec)
    except ReplayError as exc:
        if isinstance(exc, PackagingError):
            raise
        out["fatal"] = str(exc)
        return out
    out["planned"] = dict(deployment.get("planned") or {})
    jobs = deployment.get("jobs")

    scratch = Path(dest_parent) if dest_parent else Path(
        tempfile.mkdtemp(prefix="replay-isolation-"))
    try:
        root = isolated_copy(world_root, scratch,
                             tuple(rs.get("copy_ignore") or DEFAULT_IGNORE))
        if keep:
            out["copy"] = str(root)
        restores = rs.get("platform_restore") or []
        edited = []
        for entry in restores:
            shipped = grading / entry["from"]
            live = root / entry["to"]
            if shipped.is_dir():
                edited.append(not _same_tree(shipped, live))
                if live.exists():
                    shutil.rmtree(live)
                shutil.copytree(shipped, live)
        if edited:
            out["platform_edited"] = any(edited)
        state_path = root / layout["scheduler_relpath"]
        live_path = root / layout["live_relpath"]
        if not state_path.is_file() or not live_path.is_file():
            out["fatal"] = "the world root has no scheduler state to re-deploy into"
            return out
        deployment["install"](root)
        state = json.loads(state_path.read_text(encoding="utf-8"))

        plants = rs.get("plant") or []
        for entry in plants:
            base = grading / Path(_subst(entry["from"], 1, dates[0] if dates else None)).parent
            if not base.is_dir():
                raise PackagingError(f"the planted nights are missing from the "
                                     f"grading zone ({base})")
        warehouse = root / layout["warehouse_relpath"]
        before = len(state["runs"])
        before_tables = engine.read_tables(warehouse, targets)
        scheduler = _scheduler()
        for k in range(1, nights + 1):
            day = dates[k - 1] if k - 1 < len(dates) else None
            for entry in plants:
                source = grading / _subst(entry["from"], k, day)
                if source.is_dir():
                    shutil.copytree(source, root / _subst(entry["to"], k, day),
                                    dirs_exist_ok=True)
                elif source.is_file():
                    dest = root / _subst(entry["to"], k, day)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, dest)
            try:
                scheduler.advance(root, hours)
            except SystemExit as exc:
                out["fatal"] = f"advancing night {k} failed: {exc}"
                return out
            except Exception as exc:                               # noqa: BLE001
                out["fatal"] = (f"advancing night {k} raised "
                                f"{type(exc).__name__}: {engine.first_line(exc)}")
                return out
            after = json.loads(state_path.read_text(encoding="utf-8"))
            fresh = after["runs"][before:]
            before = len(after["runs"])
            out["runs"].extend({"night": k, "job": r["job"], "start": r["vts_start"],
                                "status": r["status"], "executed": r.get("executed"),
                                "error": engine.first_line(r.get("error"))}
                               for r in fresh)
            out["jobs_ok"].append(_jobs_ok(fresh, jobs, rs))
            out["snapshots"][k] = _snapshot(warehouse, targets)
        final_tables = list(dict.fromkeys(list(rs.get("final_tables") or []) + targets))
        out["final"] = engine.read_tables(warehouse, final_tables)
        out["moved"] = {t: out["final"].get(t) != before_tables.get(t) for t in targets}
        for table, role in engine.roles_of(spec).items():
            out[f"{role}_moved"] = out["moved"].get(table, False)
        out["receipts"], out["receipts_why"] = engine.load_receipts(root)
        return out
    finally:
        if not keep:
            shutil.rmtree(scratch / "world" if dest_parent else scratch,
                          ignore_errors=True)
