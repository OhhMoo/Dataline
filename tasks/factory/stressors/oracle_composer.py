"""Compose a real t0 defect, future arrivals, and executable repair scripts.

Public API
----------
``compose_task(theme, stressors, hazards, governance_mode, seed, out_dir)``
builds a minimal scheduler world for a theme that loads, then writes:

1. broken ``workspace/pipelines/ingest_feed.py``, ``fold_ledger.py``, and
   ``publish_handoff.py`` carrying the active stressors and the vulnerable
   readers for the active hazards;
2. ``solution/oracle/`` with the three repaired modules, plus ``solve.py``
   and ``solution.sh``;
3. ``solution/partial_fix.py`` and the rival baselines
   ``wrong_precedence``, ``duplicate_counted``, and ``boundary_violated``.

Accepts 1 to 3 distinct stressors and 1 to 3 distinct hazards. Long ids such
as ``S01_checkpoint_calendar_advance_on_gap`` canonicalize to ``S01``.

``compose_task(task_dir, blueprint, theme_world_dir)`` injects an already
built world. ``stage_world`` repeats that injection. Generated worlds must
sit outside the git repository.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Mapping

import duckdb

from tasks.factory.hazards.arrival_file_hazards import apply_arrival_hazards
from tasks.factory.hazards.t0_breadcrumbs import stage_breadcrumbs
from tasks.factory.hazards.temporal_contract_hazards import apply_temporal_hazards

from .domain_schema_adapter import DECLARED_FORBIDDEN, load_theme_binding
from .runtime_template import (MODULES, PhysicalTables, oracle_behavior,
                               partial_behavior, render_modules, rival_behavior,
                               shipped_behavior)
from .t0_pipeline_stressors import STRESSORS, _write_csv, apply_stressors


# Names kept for callers that still import the historical sidecar list.
# Composed worlds report the tables they actually wrote on the result.
TABLES = ["raw.factory_events", "warehouse.fct_factory_events",
          "warehouse.factory_handoff", "warehouse.factory_checkpoint"]
FUTURE_TABLES = TABLES[:3]
JOB_NAMES = ["factory_arrive", "factory_load", "factory_publish", "factory_close"]
RIVAL_NAMES = ("wrong_precedence", "duplicate_counted", "boundary_violated")
def _list_ids(blueprint: Mapping[str, Any], *names: str) -> list:
    for name in names:
        value = blueprint.get(name)
        if not value:
            continue
        if isinstance(value, (str, Mapping)):
            return [value]
        return list(value)
    return []


def _token(value: Any) -> str:
    if isinstance(value, Mapping):
        for key in ("hazard_id", "stressor_id", "id", "code"):
            if value.get(key):
                return str(value[key])
        raise ValueError(f"id mapping has no hazard_id/stressor_id/id: {value}")
    return str(value).strip()


def _code(value: Any, valid: Mapping[str, str]) -> str:
    text = _token(value)
    if text in valid:
        return text
    prefix = text.split("_", 1)[0]
    if prefix in valid:
        return prefix
    raise ValueError(f"unknown id {text!r}; expected one of {sorted(valid)}")


def _selection(blueprint: Mapping[str, Any]) -> tuple[list[str], list[str], int, str]:
    from tasks.factory.hazards import ALL_HAZARDS
    stressors = [_code(item, STRESSORS) for item in _list_ids(
        blueprint, "t0_stressors", "t0_stressor", "stressors", "stressor")]
    hazards = [_code(item, ALL_HAZARDS) for item in _list_ids(
        blueprint, "hazards", "r_hazards", "hazard_pair")]
    if not 1 <= len(stressors) <= 3 or len(set(stressors)) != len(stressors):
        raise ValueError("expected 1 to 3 distinct stressors")
    if not 1 <= len(hazards) <= 3 or len(set(hazards)) != len(hazards):
        raise ValueError("expected 1 to 3 distinct future hazards")
    mode = str(blueprint.get("governance_mode") or blueprint.get("governance")
               or "act-authorized")
    if mode not in {"act-authorized", "withhold-blocked", "escalate-ambiguous"}:
        raise ValueError(f"unknown governance mode: {mode}")
    return stressors, hazards, int(blueprint.get("seed", 1017)), mode


_REPO = Path("/home/ubuntu/env_project").resolve()
_VNOW = "2026-06-12T00:00:00Z"


def _outside_repo(path: Path) -> Path:
    resolved = Path(path).resolve()
    if resolved == _REPO or _REPO in resolved.parents:
        raise ValueError(f"refusing to write a composed world inside {_REPO}: {resolved}")
    return resolved


def _load_theme(name: str):
    world_track = str(_REPO / "world")
    if world_track not in sys.path:
        sys.path.insert(0, world_track)
    existing_world = sys.modules.get("world")
    if existing_world is not None and getattr(existing_world, "__file__", None) is None:
        for mod_name in [k for k in sys.modules if k == "world" or k.startswith("world.")]:
            sys.modules.pop(mod_name, None)
    try:
        from world.themes import load_theme
    except ImportError:
        from world.world.themes import load_theme
    try:
        theme = load_theme(name)
    except SystemExit as exc:
        raise ValueError(str(exc)) from exc
    if theme.name != name:
        raise ValueError(f"theme {name!r} loaded as {theme.name!r}")
    return theme


def _prepare_minimal_world(world: Path) -> None:
    """Scheduler shell the stressor and hazard functions accept without dbt."""
    if world.exists():
        shutil.rmtree(world)
    (world / "var/warehouse").mkdir(parents=True)
    (world / "var/sched").mkdir(parents=True)
    (world / "var/world").mkdir(parents=True)
    (world / "workspace/pipelines").mkdir(parents=True)
    (world / "var/sched/state.json").write_text(json.dumps({
        "vnow": _VNOW, "jobs": {}, "runs": [],
    }, sort_keys=True) + "\n", encoding="utf-8")
    (world / "var/world/live.json").write_text(json.dumps({
        "vtime_start": _VNOW, "days_landed": 0, "jobs": {},
    }, sort_keys=True) + "\n", encoding="utf-8")


def _future_rows(root: Path, *, seed: int, t0_day: str) -> None:
    start = date.fromisoformat(t0_day)
    for night in range(1, 5):
        day = start + timedelta(days=night - 1)
        rows = []
        for index in range(3):
            rows.append({"event_id": f"EV-FUT-{seed}-{night}-{index}",
                         "delivered_at": f"{day}T04:00:0{index}+00:00",
                         "business_day": str(day),
                         "business_id": f"B-FUT-{seed}-{night}-{index}",
                         "version": 1, "event_type": "settle",
                         "entity_id": ("ORG-A", "ORG-B", "ORG-C")[index],
                         "amount_cents": 1000 + seed % 37 + night * 100 + index * 70})
        _write_csv(root / f"R{night}" / f"dt={day}" / "events.csv", rows)


def apply_hazards(future_root: Path, hazard_ids: list[str] | tuple[str, ...],
                  *, seed: int, historical_day: str,
                  ticket: str | None = None,
                  controller: str | None = None) -> list[dict]:
    """Mutate pre-created R1..R4 payloads and return precise file evidence.

    ``controller`` is forwarded to H13 so ``R3/_approvals.csv`` uses the same
    ``approved_by`` value as t0 ``warehouse.factory_corrections``.
    """
    file_ids = [hid for hid in hazard_ids if int(hid[1:]) <= 7 and hid != "H01"]
    temporal_ids = [hid for hid in hazard_ids if int(hid[1:]) >= 8]
    effects = apply_arrival_hazards(future_root, file_ids, seed=seed,
                                    historical_day=historical_day)
    effects += apply_temporal_hazards(future_root, temporal_ids, seed=seed,
                                      historical_day=historical_day,
                                      ticket=ticket, controller=controller)
    if "H01" in hazard_ids:
        # Checksums describe the final delivered event bytes, including any
        # simultaneous column-order or temporal event mutation.
        effects += apply_arrival_hazards(future_root, ["H01"], seed=seed,
                                         historical_day=historical_day)
    return sorted(effects, key=lambda effect: effect["id"])


def _job(script: str, action: str, *, time: str | None = None,
         after: dict | None = None, publishes: list[str] = ()) -> dict:
    spec = {"command": f"factory_{action}", "duration_s": 15,
            "day_offset": 0, "detail": None, "after": after,
            "exec": {"kind": "script", "path": script,
                     "args": [action, "{root}"],
                     "tables": list(publishes)}}
    if time is not None:
        spec["time"] = time
    if publishes:
        spec["publishes"] = [{"target": table, "kind": "table",
                              "consumer": "factory_contract"} for table in publishes]
    return spec


def _feed_relative(world: Path, landing_dir: str) -> str:
    """Use the theme landing on a minimal world.

    A built theme already stores its own files under that landing directory.
    Those worlds keep the feed in ``data/factory_feed`` so the theme drop is
    not removed.
    """
    live_path = world / "var/world/live.json"
    if live_path.is_file():
        live = json.loads(live_path.read_text(encoding="utf-8"))
        foreign = [name for name in (live.get("jobs") or {})
                   if not str(name).startswith("factory_")]
        if foreign:
            return "data/factory_feed"
    return landing_dir


def _write_workspace_schedule(world: Path, *, hazards: list[str],
                              stressors: list[str]) -> None:
    """Write the agent-visible schedule that ``scheduler.advance`` overlays.

    When S09 is active, ``factory_publish`` is pinned at 21:50 and
    ``factory_load`` at 22:00, so publish is earlier than load. Otherwise
    publish waits on load (or on ``factory_load_retry`` when H12 is active).
    Clock times are quoted so YAML keeps them as ``HH:MM`` strings.
    """
    lines = [
        "# Nightly factory orchestration.",
        "jobs:",
        "  factory_arrive:",
        '    time: "08:00"',
        "  factory_load:",
        '    time: "22:00"',
    ]
    if "H12" in hazards:
        lines.extend([
            "  factory_load_retry:",
            "    after:",
            "      job: factory_load",
            "      minutes: 5",
        ])
    lines.append("  factory_publish:")
    if "S09" in stressors:
        lines.append('    time: "21:50"')
    elif "H12" in hazards:
        lines.extend([
            "    after:",
            "      job: factory_load_retry",
            "      minutes: 15",
        ])
    else:
        lines.extend([
            "    after:",
            "      job: factory_load",
            "      minutes: 20",
        ])
    lines.extend([
        "  factory_close:",
        '    time: "23:30"',
        "",
    ])
    path = world / "workspace/orchestration/schedule.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _install_jobs(world: Path, *, hazards: list[str], stressors: list[str],
                  tables: dict[str, str]) -> None:
    live_path = world / "var/world/live.json"
    state_path = world / "var/sched/state.json"
    if not live_path.is_file():
        raise ValueError("theme world has no live scheduler: " + str(world))
    live = json.loads(live_path.read_text(encoding="utf-8"))
    state = json.loads(state_path.read_text(encoding="utf-8"))
    jobs = live.setdefault("jobs", {})
    raw_fact = [tables["raw"], tables["fact"]]
    handoff = [tables["handoff"]]
    jobs["factory_arrive"] = _job("pipelines/ingest_feed.py", "arrive", time="08:00")
    jobs["factory_load"] = _job("pipelines/ingest_feed.py", "load", time="22:00",
                                publishes=raw_fact)
    if "H12" in hazards:
        jobs["factory_load_retry"] = _job(
            "pipelines/ingest_feed.py", "retry",
            after={"job": "factory_load", "minutes": 5}, publishes=raw_fact)
    else:
        jobs.pop("factory_load_retry", None)
        state["jobs"].pop("factory_load_retry", None)
    publish_after = ({"job": "factory_load_retry", "minutes": 15} if "H12" in hazards
                     else {"job": "factory_load", "minutes": 20})
    if "S09" in stressors:
        jobs["factory_publish"] = _job("pipelines/publish_handoff.py", "publish",
                                        time="21:50", publishes=handoff)
    else:
        jobs["factory_publish"] = _job("pipelines/publish_handoff.py", "publish",
                                        after=publish_after, publishes=handoff)
    jobs["factory_close"] = _job("pipelines/publish_handoff.py", "close", time="23:30")
    for name in jobs:
        if name.startswith("factory_"):
            state["jobs"][name] = {"command": jobs[name]["command"],
                                   "duration_s": jobs[name]["duration_s"]}
    live_path.write_text(json.dumps(live, sort_keys=True, indent=2) + "\n",
                         encoding="utf-8")
    state_path.write_text(json.dumps(state, sort_keys=True, indent=2) + "\n",
                          encoding="utf-8")
    _write_workspace_schedule(world, hazards=hazards, stressors=stressors)


def _run_script(script: Path, action: str, world: Path) -> None:
    result = subprocess.run([sys.executable, str(script), action, str(world)],
                            capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if result.returncode:
        raise RuntimeError(f"{script.name} {action} failed: {result.stderr.strip()}")


def _clear_owned_clues(world: Path) -> None:
    for relative in ("workspace/docs/partner_feed_contract.md",
                     "workspace/docs/ops_runbook.md",
                     "var/logs/ingest_daemon.log",
                     "var/logs/sftp_sync.log"):
        path = world / relative
        if path.is_file():
            path.unlink()
    archive = world / "workspace/docs/archive"
    if archive.exists():
        shutil.rmtree(archive)


def _seed_governance_warehouse_fact(task_dir: Path, world: Path,
                                    blueprint: Mapping[str, Any],
                                    closed_day: str) -> dict | None:
    """Create and seed the 4th-hop governance prerequisite table in warehouse.duckdb."""
    fact = blueprint.get("warehouse_fact")
    if not isinstance(fact, Mapping):
        gov_path = task_dir / "tests/governance.json"
        if gov_path.is_file():
            try:
                gov = json.loads(gov_path.read_text(encoding="utf-8"))
                if isinstance(gov, Mapping) and isinstance(gov.get("warehouse_fact"), Mapping):
                    fact = gov["warehouse_fact"]
            except (OSError, ValueError):
                fact = None
    if not isinstance(fact, Mapping):
        return None
    relation = str(fact.get("relation") or "").strip()
    if not relation:
        return None
    db = world / "var/warehouse/warehouse.duckdb"
    if not db.is_file():
        return None
    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute("ATTACH '" + str(db).replace("'", "''") + "' AS wh")
    con.execute("USE wh")
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS warehouse")
        if relation == "warehouse.partner_recon":
            status = str(fact.get("actual") or "matched")
            con.execute(
                "CREATE TABLE IF NOT EXISTS warehouse.partner_recon "
                "(business_day DATE, status VARCHAR)"
            )
            con.execute(
                "DELETE FROM warehouse.partner_recon WHERE business_day = CAST(? AS DATE)",
                [closed_day],
            )
            con.execute(
                "INSERT INTO warehouse.partner_recon (business_day, status) "
                "VALUES (CAST(? AS DATE), ?)",
                [closed_day, status],
            )
        elif relation == "warehouse.restatement_delta":
            cents = int(fact.get("actual_cents") or 0)
            con.execute(
                "CREATE TABLE IF NOT EXISTS warehouse.restatement_delta "
                "(business_day DATE, amount_cents BIGINT)"
            )
            con.execute(
                "DELETE FROM warehouse.restatement_delta WHERE business_day = CAST(? AS DATE)",
                [closed_day],
            )
            con.execute(
                "INSERT INTO warehouse.restatement_delta (business_day, amount_cents) "
                "VALUES (CAST(? AS DATE), ?)",
                [closed_day, cents],
            )
        elif relation == "warehouse.rate_cutover":
            cutover = str(fact.get("actual") or closed_day)
            con.execute(
                "CREATE TABLE IF NOT EXISTS warehouse.rate_cutover (cutover_day DATE)"
            )
            con.execute("DELETE FROM warehouse.rate_cutover")
            con.execute(
                "INSERT INTO warehouse.rate_cutover (cutover_day) VALUES (CAST(? AS DATE))",
                [cutover],
            )
    finally:
        con.close()
    return dict(fact)


def stage_world(task_dir: Path, world_root: Path,
                blueprint: Mapping[str, Any]) -> dict:
    """Install the three shipped modules and the active defects on ``world_root``."""
    task_dir, world = Path(task_dir), Path(world_root)
    stressors, hazards, seed, mode = _selection(blueprint)
    theme = str(blueprint.get("theme") or "")
    if not theme:
        raise ValueError("blueprint requires theme")
    binding = load_theme_binding(theme)
    feed_rel = _feed_relative(world, binding.landing_dir)
    feed = world / "workspace" / feed_rel
    future = world / "build/factory_future"
    if feed.exists():
        shutil.rmtree(feed)
    if future.exists():
        shutil.rmtree(future)
    if (world / "var/factory").exists():
        shutil.rmtree(world / "var/factory")
    _clear_owned_clues(world)
    t0 = apply_stressors(world, stressors, seed=seed, governance_mode=mode,
                         closed_day=blueprint.get("closed_day"),
                         ticket=blueprint.get("ticket"),
                         controller=str(blueprint.get("controller") or "controller"),
                         landing_dir=feed_rel,
                         tables=binding.preferred_tables())
    _seed_governance_warehouse_fact(task_dir, world, blueprint,
                                    t0["dates"]["historical"])
    physical = t0["physical_tables"]
    _future_rows(future, seed=seed, t0_day=t0["dates"]["t0"])
    effects = apply_hazards(future, hazards, seed=seed,
                            historical_day=t0["dates"]["historical"],
                            ticket=t0["authority"]["ticket"],
                            controller=t0["authority"]["controller"])
    names = PhysicalTables(
        raw=physical["raw"], fact=physical["fact"], handoff=physical["handoff"],
        close=physical["close"], corrections=physical["corrections"],
        checkpoint=physical["checkpoint"], feed_rel=feed_rel)
    pipeline = world / "workspace/pipelines"
    pipeline.mkdir(parents=True, exist_ok=True)
    legacy = pipeline / "factory_events.py"
    if legacy.exists():
        legacy.unlink()
    for name, source in render_modules(
            shipped_behavior(stressors, hazards), names,
            strict_approval_authority="H13" not in hazards).items():
        (pipeline / name).write_text(source, encoding="utf-8")
    config = world / "var/factory/config.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({
        "t0_day": t0["dates"]["t0"], "factory_sidecar": True,
        "theme": theme, "seed": seed,
    }, sort_keys=True) + "\n", encoding="utf-8")
    _install_jobs(world, hazards=hazards, stressors=stressors, tables=physical)
    ingest = pipeline / "ingest_feed.py"
    publish = pipeline / "publish_handoff.py"
    if "S09" in stressors:
        _run_script(publish, "publish", world)
        _run_script(ingest, "load", world)
    else:
        _run_script(ingest, "load", world)
        _run_script(publish, "publish", world)
    breadcrumbs = stage_breadcrumbs(world, hazards, historical_day=t0["dates"]["historical"],
                                    seed=seed, stressor_ids=stressors)
    execution = [physical["raw"], physical["fact"], physical["handoff"], physical["checkpoint"]]
    future_tables = execution[:3]
    return {"factory_sidecar": True, "theme_world_dir": str(world),
            "stressor_ids": stressors, "hazard_ids": hazards,
            "seed": seed, "governance_mode": mode,
            "dates": t0["dates"], "hazard_effects": effects,
            "authority": t0["authority"],
            "t0_tables": execution, "future_tables": future_tables,
            "publication_table": physical["handoff"],
            "physical_tables": physical, "feed_rel": feed_rel,
            "landing": str(feed), "binding": binding.as_dict(),
            "breadcrumbs": breadcrumbs,
            "modules": [f"pipelines/{name}" for name in MODULES],
            "jobs": JOB_NAMES + (["factory_load_retry"] if "H12" in hazards else [])}


def _install_script(corrections: str, after: str) -> str:
    """Copy the oracle modules, repair t0, then optionally leave a rival in place."""
    after_block = ""
    if after:
        after_block = f'    _install(world, "{after}")\n'
    return f'''#!/usr/bin/env python3
"""Install the selected repair and reconcile the current feed."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb

MODULES = ("ingest_feed.py", "fold_ledger.py", "publish_handoff.py")

def _install(world, folder):
    dest = world / "workspace/pipelines"
    dest.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parent / folder
    for name in MODULES:
        shutil.copyfile(source / name, dest / name)
    legacy = dest / "factory_events.py"
    if legacy.exists():
        legacy.unlink()

def main():
    world = Path(sys.argv[1]).resolve()
    here = Path(__file__).resolve().parent
    _install(world, "oracle")
    live_path = world / "var/world/live.json"
    live = json.loads(live_path.read_text(encoding="utf-8"))
    publisher = live["jobs"]["factory_publish"]
    publisher["after"] = {{"job": "factory_load_retry" if "factory_load_retry" in live["jobs"] else "factory_load", "minutes": 20}}
    publisher.pop("time", None)
    live_path.write_text(json.dumps(live, sort_keys=True, indent=2) + "\\n", encoding="utf-8")
    sched = world / "workspace/orchestration/schedule.yml"
    sched.parent.mkdir(parents=True, exist_ok=True)
    after_job = publisher["after"]["job"]
    lines = [
        "# Nightly factory orchestration.",
        "jobs:",
        "  factory_arrive:",
        '    time: "08:00"',
        "  factory_load:",
        '    time: "22:00"',
    ]
    if "factory_load_retry" in live["jobs"]:
        lines.extend([
            "  factory_load_retry:",
            "    after:",
            "      job: factory_load",
            "      minutes: 5",
        ])
    lines.extend([
        "  factory_publish:",
        "    after:",
        "      job: " + after_job,
        "      minutes: 20",
        "  factory_close:",
        '    time: "23:30"',
        "",
    ])
    sched.write_text("\\n".join(lines), encoding="utf-8")
    pipelines = world / "workspace/pipelines"
    subprocess.run([sys.executable, str(pipelines / "ingest_feed.py"), "load", str(world)],
                   check=True, stdin=subprocess.DEVNULL)
    subprocess.run([sys.executable, str(pipelines / "publish_handoff.py"), "publish", str(world)],
                   check=True, stdin=subprocess.DEVNULL)
    report = json.loads((here / "report_template.json").read_text(encoding="utf-8"))
    con = duckdb.connect(":memory:", config={{"threads": 1}})
    path = str(world / "var/warehouse/warehouse.duckdb").replace("'", "''")
    con.execute("ATTACH '" + path + "' AS wh (READ_ONLY)")
    con.execute("USE wh")
    try:
        approval = con.execute(
            "SELECT COUNT(*) FROM {corrections} "
            "WHERE business_day = ? AND ticket = ? AND approved_by = ?",
            [report["decision"]["closed_day"], report["decision"]["ticket"],
             report["decision"]["controller"]]).fetchone()[0]
    finally:
        con.close()
    mode = report["decision"]["mode"]
    report["decision"]["action"] = (
        "publish_approved_correction" if approval else
        "escalate_and_withhold" if mode == "escalate-ambiguous" else
        "withhold_closed_restatement")
    report["decision"]["blocker"] = (None if approval else
        "No matching controller approval for the closed day")
    destination = world / "workspace/final_report.json"
    destination.write_text(json.dumps(report, sort_keys=True, indent=2) + "\\n",
                           encoding="utf-8")
{after_block}
if __name__ == "__main__":
    main()
'''


def _write_tree(directory: Path, behavior, tables: PhysicalTables) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, source in render_modules(behavior, tables).items():
        (directory / name).write_text(source, encoding="utf-8")


def _write_solution(task_dir: Path, blueprint: Mapping[str, Any],
                    staged: Mapping[str, Any]) -> None:
    solution = task_dir / "solution"
    solution.mkdir(parents=True, exist_ok=True)
    physical = staged["physical_tables"]
    tables = PhysicalTables(
        raw=physical["raw"], fact=physical["fact"], handoff=physical["handoff"],
        close=physical["close"], corrections=physical["corrections"],
        checkpoint=physical["checkpoint"], feed_rel=staged["feed_rel"])
    _write_tree(solution / "oracle", oracle_behavior(), tables)
    _write_tree(solution / "partial", partial_behavior(), tables)
    for name in RIVAL_NAMES:
        _write_tree(solution / name, rival_behavior(name), tables)
    # Pointers so an older file-layout check still finds a non-empty module.
    (solution / "factory_events_oracle.py").write_text(
        '"""Repaired modules are in the oracle directory beside this file."""\n',
        encoding="utf-8")
    (solution / "factory_events_partial.py").write_text(
        '"""Partial modules are in the partial directory beside this file."""\n',
        encoding="utf-8")
    stressors, _hazards, seed, mode = _selection(blueprint)
    report_template = {"decision": {
        "mode": mode, "closed_day": staged["authority"]["closed_day"],
        "ticket": staged["authority"]["ticket"],
        "controller": staged["authority"]["controller"],
        "action": None, "blocker": None},
        "evidence": {"sources": [physical["corrections"], physical["close"],
                                  physical["handoff"]]}}
    (solution / "report_template.json").write_text(
        json.dumps(report_template, sort_keys=True, indent=2) + "\n",
        encoding="utf-8")
    (solution / "solve.py").write_text(
        _install_script(physical["corrections"], ""), encoding="utf-8")
    (solution / "partial_fix.py").write_text(
        _install_script(physical["corrections"], "partial"), encoding="utf-8")
    for name in RIVAL_NAMES:
        (solution / f"{name}.py").write_text(
            _install_script(physical["corrections"], name), encoding="utf-8")
    (solution / "solution.sh").write_text(
        '#!/bin/sh\nset -eu\nexec python3 "$(dirname "$0")/solve.py" "$1"\n',
        encoding="utf-8")
    del stressors, seed


def _compose_on_world(task_dir: Path, blueprint: Mapping[str, Any],
                      theme_world_dir: Path) -> dict:
    """Inject ``theme_world_dir`` and write the oracle plus rival modules."""
    task_dir = Path(task_dir)
    theme_world_dir = Path(theme_world_dir)
    result = stage_world(task_dir, theme_world_dir, blueprint)
    _write_solution(task_dir, blueprint, result)
    fixture = task_dir / "fixtures/next_nights"
    if fixture.exists():
        shutil.rmtree(fixture)
    shutil.copytree(theme_world_dir / "build/factory_future", fixture)
    tests = task_dir / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    physical = result["physical_tables"]
    declared = []
    for name in (result["binding"]["raw_table"], result["binding"]["fact_table"],
                 result["binding"]["handoff_table"], result["binding"]["close_table"],
                 result["binding"]["corrections_table"]):
        if name and name not in DECLARED_FORBIDDEN and name not in declared:
            declared.append(name)
    spec = {"adapter": "factory_sidecar",
            "modules": result["modules"],
            "tables": declared,
            "execution_tables": result["t0_tables"],
            "future_tables": result["future_tables"],
            "grain_keys": {
                physical["raw"]: ["event_id"],
                physical["fact"]: ["business_id"],
                physical["handoff"]: ["business_day", "entity_id"],
                physical["checkpoint"]: ["through_day"],
            },
            "domain_grain_keys": result["binding"]["grain_keys"],
            "domain": result["binding"],
            "publication_table": result["publication_table"],
            "landing_dir": result["feed_rel"],
            "night_count": 4, "checks": ["C0", "R1", "R2", "R3", "R4"],
            "hazards": result["hazard_ids"], "stressors": result["stressor_ids"]}
    (tests / "factory_sidecar_spec.json").write_text(
        json.dumps(spec, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    result["solution"] = str(task_dir / "solution/solve.py")
    result["spec"] = str(tests / "factory_sidecar_spec.json")
    result["future_payloads"] = str(fixture)
    result["world"] = str(theme_world_dir)
    result["partial_fix"] = str(task_dir / "solution/partial_fix.py")
    result["rivals"] = [str(task_dir / "solution" / f"{name}.py")
                        for name in ("wrong_precedence", "duplicate_counted",
                                     "boundary_violated")]
    return result


def _compose_for_theme(theme: str, stressors, hazards, governance_mode: str,
                       seed: int, out_dir: Path) -> dict:
    out_dir = _outside_repo(out_dir)
    loaded = _load_theme(str(theme))
    out_dir.mkdir(parents=True, exist_ok=True)
    world = out_dir / "world"
    _prepare_minimal_world(world)
    blueprint = {
        "theme": loaded.name,
        "stressors": [stressors] if isinstance(stressors, str) else list(stressors),
        "hazards": [hazards] if isinstance(hazards, str) else list(hazards),
        "governance_mode": governance_mode,
        "seed": int(seed),
    }
    result = _compose_on_world(out_dir, blueprint, world)
    result["theme"] = loaded.name
    result["out_dir"] = str(out_dir)
    return result


def _blueprint_call(args: tuple, kwargs: dict) -> bool:
    if {"task_dir", "blueprint", "theme_world_dir"} & set(kwargs):
        return True
    return len(args) >= 2 and isinstance(args[1], Mapping)


def compose_task(*args, **kwargs):
    """Synthesize a shipped defect, an oracle repair, and three rival baselines.

    ``compose_task(theme, stressors, hazards, governance_mode, seed, out_dir)``
    loads ``theme``, builds a minimal DuckDB world under ``out_dir/world``, and
    returns the staged paths. ``theme`` must be a theme directory
    ``world.themes.load_theme`` can import (``settlement_clearing`` and
    ``commerce_analytics`` both load; ``settlement_platform`` does not).

    ``compose_task(task_dir, blueprint, theme_world_dir)`` injects an already
    built world in place.
    """
    if _blueprint_call(args, kwargs):
        task_dir = kwargs.get("task_dir", args[0] if args else None)
        blueprint = kwargs.get("blueprint", args[1] if len(args) > 1 else None)
        world = kwargs.get("theme_world_dir", args[2] if len(args) > 2 else None)
        if task_dir is None or not isinstance(blueprint, Mapping) or world is None:
            raise TypeError("compose_task(task_dir, blueprint, theme_world_dir)")
        return _compose_on_world(Path(task_dir), blueprint, Path(world))
    theme = kwargs.get("theme", args[0] if args else None)
    stressors = kwargs.get("stressors", args[1] if len(args) > 1 else None)
    hazards = kwargs.get("hazards", args[2] if len(args) > 2 else None)
    mode = kwargs.get("governance_mode", args[3] if len(args) > 3 else "act-authorized")
    seed = kwargs.get("seed", args[4] if len(args) > 4 else 1017)
    out_dir = kwargs.get("out_dir", args[5] if len(args) > 5 else None)
    if theme is None or stressors is None or hazards is None or out_dir is None:
        raise TypeError(
            "compose_task(theme, stressors, hazards, governance_mode, seed, out_dir)")
    return _compose_for_theme(str(theme), stressors, hazards, str(mode),
                              int(seed), Path(out_dir))


def oracle_trajectory(task_dir: Path, staged_world: Path,
                      output_root: Path) -> dict:
    """Run the oracle on an isolated copy and return C0/R1..R4 evidence."""
    try:
        from world import ledger, scheduler
    except ImportError:
        from world.world import ledger, scheduler
    task_dir, staged_world, output_root = map(Path,
                                               (task_dir, staged_world, output_root))
    if output_root.exists():
        raise FileExistsError(output_root)
    shutil.copytree(staged_world, output_root)
    subprocess.run([sys.executable, str(task_dir / "solution/solve.py"),
                    str(output_root)], check=True, stdin=subprocess.DEVNULL)
    spec_path = task_dir / "tests/factory_sidecar_spec.json"
    tables, future = TABLES, FUTURE_TABLES
    if spec_path.is_file():
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        tables = spec.get("execution_tables") or spec.get("tables") or TABLES
        future = spec.get("future_tables") or FUTURE_TABLES
    t0 = {table: ledger.table_contents(output_root, table)["canonical_hash"]
          for table in tables}
    nights = []
    before = len(ledger.load(output_root))
    for number in range(1, 5):
        scheduler.advance(output_root, 24)
        receipts = [r for r in ledger.load(output_root)[before:]
                    if r["job"].startswith("factory_")]
        before = len(ledger.load(output_root))
        nights.append({"night": number,
                       "tables": {table: ledger.table_contents(output_root, table)["canonical_hash"]
                                  for table in future},
                       "receipts": [{"job": r["job"], "target": r["target"],
                                     "canonical_hash": r["canonical_hash"]}
                                    for r in receipts]})
    return {"t0": t0, "nights": nights, "world": str(output_root)}
