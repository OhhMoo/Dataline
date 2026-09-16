#!/usr/bin/env python3
"""
GT005 surface staging - the half of the world the warehouse cannot hold.

WHY. GT004's first real K=5 rollout was scored and reported with eight surfaces
dead, because task.toml declared them and nothing ever staged them. A number
measured in a half-absent world is indistinguishable from a real one unless
something refuses to publish it, so this module stages what GT005 declares, and
`task.toml` declares `dead_by_design` (with reasons) for what it does not.

WHAT IS STAGED
    var/workflow/{issues,chat,kb}.json  from fixtures/workflow/*.yaml, through
                                        world/world/workflow.py's own seed and
                                        validator - not hand-assembled here.
    var/obs/definitions.yaml            metrics and alarms, measured live.
    var/obs/samples.jsonl               the recorded nightly build history,
                                        read out of ops.job_runs.

WHAT IS DELIBERATELY NOT STAGED, AND WHY IT IS NOT AN OVERSIGHT
    No metric compares the mart against the extract. The premise of this unit is
    that a silent undercount is unmonitored - the suite is green, the scheduler
    is green, the alarms are quiet - so a panel whose whole job is to notice the
    shortfall would delete the task. Volume and freshness gauges are the normal
    monitoring a team has and they are here; a reconciliation monitor is not.

    No alarm fires and none is planted to fire. In this world's own record
    nothing failed, and an alarm firing against a record that says otherwise is
    a fixture bug wearing a costume.

ORDER MATTERS, AND IT IS A SCORING INTERACTION. `stage()` must run BEFORE
`make_fixture_world.write_grading_zone`, so the collateral baseline captures the
staged planes. Seed them afterwards and every agent - the oracle included - is
billed for "the obs section appeared". `check_staged_before_baseline` is the
operational check on that ordering.

DETERMINISM. Every file is written as BYTES, so no newline translation can
differ between a Windows host and a Linux container. JSON is canonical.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
FIXTURES = TASK / "fixtures"
REPO = TASK.parent.parent

OBS_DIR = Path("var") / "obs"
DEFINITIONS = OBS_DIR / "definitions.yaml"
SAMPLES = OBS_DIR / "samples.jsonl"
WORKFLOW_DIR = Path("var") / "workflow"
WAREHOUSE = Path("var") / "warehouse" / "warehouse.duckdb"

RETENTION_DAYS = 14
JOB = "dbt_build"
JOB_ERROR_WINDOW = 10

WORKFLOW_SPEC = {
    "dir": "workflow",
    "actor": "agent",
    "project": {"key": "DATA", "name": "Data Platform"},
    "statuses": ["open", "in_progress", "blocked", "done"],
    "workspace": {"id": "alkera", "name": "Alkera"},
    "space": {"key": "de", "name": "Data Engineering"},
}


class StagingError(SystemExit):
    """A world these surfaces cannot honestly describe. Fails the build, loudly."""


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def _canon(document) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _connect(db: Path):
    """Read-only, ALIASED attach (F622). Opening var/warehouse/warehouse.duckdb
    directly makes the catalog and the `warehouse` schema collide."""
    import duckdb

    con = duckdb.connect()
    con.execute(f"ATTACH '{Path(db).as_posix()}' AS wh (READ_ONLY)")
    return con


# ------------------------------------------------------------- 1. workflow
def stage_workflow(root: Path, vnow: str) -> dict:
    sys.path.insert(0, str(REPO / "world"))
    try:
        from world import workflow as world_workflow
    finally:
        sys.path.pop(0)
    directory = world_workflow.seed(root, FIXTURES, WORKFLOW_SPEC, vnow)
    return {"dir": str(directory),
            "documents": sorted(p.name for p in directory.glob("*.json")),
            "project": WORKFLOW_SPEC["project"]["key"],
            "space": WORKFLOW_SPEC["space"]["key"]}


# -------------------------------------------------------- 2. observability
DEFINITIONS_YAML = """\
# Observability definitions for GT005. Staged by tests/stage_surfaces.py.
#
# EVERY SURFACE IS GREEN, AND THE WORLD'S OWN RECORD SAYS SO. ops.job_runs holds
# 42 nightly builds and zero failures; ops.dq_checks holds 126 checks and zero
# failing rows. Nothing here is planted to fire, because nothing in this world
# failed.
#
# WHAT IS NOT HERE, ON PURPOSE. There is no metric that reconciles
# warehouse.fct_revenue_daily against raw.billing_invoice_line. The unit's
# premise is a silent undercount that no surface notices; a monitor whose whole
# job is to notice it would delete the unit. Volume and freshness gauges are the
# monitoring a real team has, and they are here.
version: 1

retention:
  days: 14

metrics:
  - id: revenue_freshness
    kind: freshness
    unit: seconds
    title: Revenue fact freshness
    description: >-
      Seconds between the newest invoice_date in warehouse.fct_revenue_daily and
      the reading. invoice_date is a DATE, so this measures to midnight of the
      newest partition.
    source: {table: warehouse.fct_revenue_daily, timestamp_column: invoice_date}
  - id: invoice_line_rows
    kind: row_count
    unit: rows
    title: Invoice line fact rows
    description: Live count of warehouse.fct_invoice_line.
    source: {table: warehouse.fct_invoice_line}
  - id: revenue_rows
    kind: row_count
    unit: rows
    title: Daily revenue fact rows
    source: {table: warehouse.fct_revenue_daily}
  - id: raw_invoice_rows
    kind: row_count
    unit: rows
    title: Landed invoice lines
    source: {table: raw.billing_invoice_line}
  - id: invoice_key_null_rate
    kind: null_rate
    unit: ratio
    title: Invoice line rows with no key
    source: {table: warehouse.fct_invoice_line, column: invoice_line_key}
  - id: dq_failed_rows
    kind: sql_scalar
    unit: rows
    title: Failing rows in the latest data quality run
    source:
      sql: >-
        select coalesce(sum(failed_rows), 0) as failed_rows from ops.dq_checks
        where checked_at = (select max(checked_at) from ops.dq_checks)
  - id: nightly_build_duration
    kind: job_duration
    unit: seconds
    title: Nightly build duration
    job: dbt_build
  - id: nightly_build_error_rate
    kind: job_error_rate
    unit: ratio
    title: Nightly build error rate
    job: dbt_build
    window_runs: 10

alarms:
  - id: revenue_stale
    metric: revenue_freshness
    comparison: gt
    threshold: 1209600
    severity: critical
    title: The revenue fact is more than two weeks behind
    description: >-
      The extract lands several days behind the invoice date by design, so a
      two-day threshold would flicker nightly on a healthy world.
    runbook: kb:de/runbooks/nightly-build
  - id: dq_failing
    metric: dq_failed_rows
    comparison: gt
    threshold: 0
    severity: critical
    title: The nightly data quality run has failing rows
    description: >-
      AUTHOR NOTE, agent-visible by design: the controls this alarm reads are
      not-null, row-count-greater-than-zero and freshness. None of them is a
      reconciliation control against the extract.
  - id: invoice_key_missing
    metric: invoice_key_null_rate
    comparison: gt
    threshold: 0
    severity: warning
    title: Invoice line rows are arriving with no key
  - id: nightly_build_failing
    metric: nightly_build_error_rate
    comparison: gte
    threshold: 0.2
    severity: warning
    title: The nightly build is failing more than one run in five
    runbook: kb:de/runbooks/nightly-build
  - id: nightly_build_slow
    metric: nightly_build_duration
    comparison: gt
    threshold: 600
    severity: warning
    title: The nightly build ran long

deploys:
  - id: rebuild_commercial_marts
    kind: dbt
    title: Rebuild the commercial marts
    description: >-
      Runs the mart models and re-measures the canary either side. The guards are
      volume floors: a rebuild that drops rows is the failure mode worth
      catching, and it is reported rather than blocked.
    selector: marts
    canary:
      metrics: [invoice_line_rows, revenue_rows, revenue_freshness, dq_failed_rows]
      alarms: [revenue_stale, dq_failing]
      guards:
        - {metric: invoice_line_rows, max_decrease_pct: 5}
        - {metric: revenue_rows, max_decrease_pct: 5}
"""


def _job_samples(con) -> list[dict]:
    rows = con.execute(
        "SELECT run_id, started_at, ended_at, status FROM wh.ops.job_runs "
        f"WHERE job = '{JOB}' ORDER BY ended_at, run_id").fetchall()
    out = []
    for index, (run_id, started_at, ended_at, status) in enumerate(rows):
        window = rows[max(0, index + 1 - JOB_ERROR_WINDOW): index + 1]
        failures = [run for run in window if run[3] != "success"]
        out.append({"vts": _iso(ended_at), "metric": "nightly_build_duration",
                    "value": (ended_at - started_at).total_seconds(),
                    "source": "advance", "run_id": run_id,
                    "detail": {"job": JOB, "status": status,
                               "started_at": _iso(started_at),
                               "finished_at": _iso(ended_at)}})
        out.append({"vts": _iso(ended_at), "metric": "nightly_build_error_rate",
                    "value": len(failures) / len(window),
                    "source": "advance", "run_id": run_id,
                    "detail": {"job": JOB, "runs": len(window),
                               "failed_runs": len(failures),
                               "window_runs": JOB_ERROR_WINDOW}})
    return out


def _dq_samples(con) -> list[dict]:
    rows = con.execute(
        "SELECT checked_at, sum(failed_rows) AS failed_rows, count(*) AS checks "
        "FROM wh.ops.dq_checks GROUP BY checked_at ORDER BY checked_at").fetchall()
    return [{"vts": _iso(checked_at), "metric": "dq_failed_rows",
             "value": float(failed_rows), "source": "advance", "run_id": None,
             "detail": {"raw": int(failed_rows), "checks": int(checks)}}
            for checked_at, failed_rows, checks in rows]


def stage_observability(root: Path, vnow: str) -> dict:
    con = _connect(root / WAREHOUSE)
    try:
        samples = _job_samples(con) + _dq_samples(con)
    finally:
        con.close()
    now = _parse(vnow)
    ahead = [row for row in samples if _parse(row["vts"]) > now]
    if ahead:
        raise StagingError(
            f"{len(ahead)} recorded sample(s) are stamped after the world clock "
            f"{vnow}; first is {ahead[0]['metric']} at {ahead[0]['vts']}")
    samples.sort(key=lambda row: (row["vts"], row["metric"]))
    _write(root / DEFINITIONS, DEFINITIONS_YAML.encode("utf-8"))
    _write(root / SAMPLES, b"".join(_canon(row) + b"\n" for row in samples))
    stray = root / OBS_DIR / "observability.yml"
    if stray.exists():
        raise StagingError(f"{stray} exists alongside definitions.yaml; a world declares one")
    window_start = now - timedelta(days=RETENTION_DAYS)
    retained = [row for row in samples if _parse(row["vts"]) >= window_start]
    return {"definitions": str(root / DEFINITIONS), "samples": str(root / SAMPLES),
            "sample_rows": len(samples), "retained_at_vnow": len(retained),
            "metrics_with_record": sorted({row["metric"] for row in samples})}


# ----------------------------------------------------------------- the seam
def stage(root: Path, vnow: str) -> dict:
    root = Path(root)
    if not (root / WAREHOUSE).is_file():
        raise StagingError(f"{root / WAREHOUSE} is not there; stage_world_root must run first")
    return {"workflow": stage_workflow(root, vnow),
            "obs": stage_observability(root, vnow)}


def check_staged_before_baseline(world_root: Path, grading: Path) -> list[str]:
    """Did the collateral baseline capture the staged planes?

    A section present in the world and absent from the baseline reads as "the
    section appeared", which is an offence every agent commits by standing still.
    """
    problems = []
    snapshot = Path(grading) / "world_sections.json"
    if not snapshot.is_file():
        return [f"{snapshot} does not exist"]
    sections = json.loads(snapshot.read_text(encoding="utf-8")).get("sections", {})
    for name, rel in (("obs", OBS_DIR), ("workflow", WORKFLOW_DIR)):
        in_world = (Path(world_root) / rel).exists()
        in_baseline = sections.get(name) is not None
        if in_world and not in_baseline:
            problems.append(f"{name}: staged into the world but missing from the "
                            "collateral baseline - stage() must run before "
                            "write_grading_zone")
        if in_baseline and not in_world:
            problems.append(f"{name}: in the baseline but not in the world")
    return problems


def _cli(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="stage_surfaces")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--vnow")
    parser.add_argument("--grading", type=Path)
    args = parser.parse_args(argv)
    vnow = args.vnow
    if vnow is None:
        state = json.loads((args.root / "var" / "sched" / "state.json")
                           .read_text(encoding="utf-8"))
        vnow = state["vnow"]
    report = stage(args.root, vnow)
    if args.grading:
        report["baseline_check"] = check_staged_before_baseline(args.root, args.grading)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
