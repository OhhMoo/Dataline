#!/usr/bin/env python3
"""
GT006 surface staging - the half of the world the warehouse tools cannot hold.

WHICH CORPUS. The tracker, the wiki and the channel ARE the context, so they are
what a `[variants]` arm changes: `FIXTURES` below is resolved through
`taskspec.fixtures_dir()` rather than hardcoded, and the two stacked arms add
two corpus tokens of their own. Nothing here knows which arm it is in.

    python stage_surfaces.py --root <world_root> [--vnow ...]

WHY THIS FILE EXISTS. `GatewayConfig`'s defaults for the workflow, observability
and ingestion planes are ABSOLUTE paths outside any world root
(gateway/src/alkera_gateway/config.py:19-23: `/var/ingest/state.json`,
`/var/obs/observability.yml`, `/var/workflow`, all `.resolve()`d). A world
builder that does not SEED these planes and PUBLISH their paths serves
`no_issue_tracker` / `no_observability` / `no_ingest_state` no matter what the
world contains. GT004's first five Opus rollouts were scored and reported with
eight surfaces dead for exactly this reason, and the harness printed that fact
under the number instead of refusing to produce one.

THE ONE RULE. Nothing here is invented. Every ticket, message and page is read
from `fixtures/workflow/*.yaml` and assembled by `world/world/workflow.py` - the
same assembler and the same validator every theme uses. Every observability
sample is read out of `ops.freshness_checks`, `ops.dq_checks` or `ops.job_runs`,
which are the world's own record of what it observed. Every ingestion run is one
real delivery batch out of `raw.payments_landing`. The single declared quantity
that is not derived from world state is the connector's `sync.duration_s`, which
is connector configuration rather than a measurement, and it is named here so a
reader can see that it is declared.

WHAT IS DELIBERATELY NOT SEEDED, and this is the archetype rather than an
omission:

  * No metric and no control compares warehouse.fct_payments against
    raw.payments_landing. A003's premise is that correctness is the axis nobody
    monitors; a panel whose whole job is to notice the defect would delete the
    unit. `fixture_contract.verify()` refuses a world that grows one.
  * No alarm fires and none is planted to fire. Freshness, data quality and the
    job history are green because the world's own tables say they are.
  * No ingestion alert and no schema-drift event. The columns did not change -
    that is the whole trap - so recording a drift event would be a fake, which
    is worse than an absent surface.

NO FIGURE IS QUOTED IN THE CORPUS. The tickets, chat and wiki carry no row
count, amount, rate or percentage, so there is nothing in them that a reseed can
falsify. Dates ARE quoted, as `{{tokens}}` resolved from CONTRACT here; an
unresolved token is refused rather than shipped.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
REPO = TASK.parent.parent

# The CORPUS this stager reads is the thing a context variant changes, so the
# tree is resolved rather than assumed: `default`/`trap-ticket` read the
# package's own fixtures/, the stacked arms read a cached copy with
# fixtures/variants/<arm>/ laid over it.
sys.path.insert(0, str(HERE))
from variant import FIXTURES                                     # noqa: E402

sys.path.insert(0, str(FIXTURES))
from fixture_contract import CONTRACT                            # noqa: E402
import decision_arm                                              # noqa: E402

WAREHOUSE = Path(CONTRACT["layout"]["deployed_world_relpath"])
WORKFLOW_DIR = Path(CONTRACT["layout"]["workflow_relpath"])
OBS_DIR = Path("var") / "obs"
DEFINITIONS = Path(CONTRACT["layout"]["obs_definitions_relpath"])
SAMPLES = Path(CONTRACT["layout"]["obs_samples_relpath"])
INGEST_STATE = Path(CONTRACT["layout"]["ingest_relpath"])

RETENTION_DAYS = 14
JOB = "build_payments"
JOB_ERROR_WINDOW = 10

FRESHNESS_METRICS = {
    "warehouse.fct_payments": ("payments_freshness", "payment_dt"),
    "warehouse.kpi_payments_daily": ("kpi_freshness", "dt"),
}

# The vocabulary the corpus assumes. It is world shape, not corpus content.
WORKFLOW_SPEC = {
    "dir": "workflow",
    "actor": "agent",
    "project": {"key": "DATA", "name": "Data platform"},
    "statuses": ["open", "triage", "in_progress", "blocked", "done", "wontfix"],
    "workspace": "payments-data",
    "space": {"key": "DE", "name": "Data Engineering"},
}

# Every token the corpus may quote, and where its value comes from. A token
# outside this map, or a `{{...}}` left in a staged document, is refused.
TOKENS = {
    "rule_deployed": CONTRACT["rule"]["deployed"],
    "harness_start": CONTRACT["rule"]["harness_traffic_from"],
    "cleanup_from": CONTRACT["rule"]["cleanup_from"],
    "cleanup_to": CONTRACT["rule"]["cleanup_to"],
}

if "stack" in CONTRACT:
    # DEFECT A, in the two stacked arms only: the day the rollup moved into dbt
    # and picked up the commercial list, and the first merchant id that list
    # does not carry. The corpus quotes the id rather than a count, and the
    # contract measures both. Defined only where the contract defines them, so
    # an unstacked corpus that quoted one would be refused rather than rendered.
    TOKENS.update({
        "dim_loaded": CONTRACT["stack"]["dim_loaded"],
        "first_missing_merchant": CONTRACT["stack"]["first_missing_merchant"],
    })

# THE DECISION ARM's dates: the closed reporting period and the pack date the
# runbook section quotes. Defined only where fixtures/decision.yaml is present,
# for the same reason as the stacked tokens above.
TOKENS.update(decision_arm.tokens())


class StagingError(RuntimeError):
    pass


# ---------------------------------------------------------------- utilities
def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)


def _canon(document) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("utf-8")


def _write(path: Path, payload: bytes) -> None:
    """Bytes, never text. `open(..., "w")` on Windows rewrites \\n as \\r\\n and
    the staged world stops being byte-identical to one built anywhere else."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _connect(db: Path):
    """Read-only, aliased ATTACH (F622)."""
    import duckdb

    con = duckdb.connect(":memory:", config={"threads": 1})
    con.execute(f"ATTACH '{Path(db).as_posix()}' AS wh (READ_ONLY)")
    return con


# ------------------------------------------------------------- 1. workflow
def _resolve_tokens(corpus_dir: Path, vnow: str | None = None) -> Path:
    """Render `{{token}}` from CONTRACT into a scratch copy of the corpus.

    The corpus on disk stays tokenised so a date can never be typed twice and
    drift; the copy the assembler reads carries the resolved values.

    ON THE DECISION ARM the overlay's additions (a ticket comment, a chat
    message, a runbook section - fixtures/variants/decision/decision.yaml) are
    spliced into the parsed document FIRST, so they may quote tokens too and
    are validated by the same assembler as everything seeded. The shipped
    yaml files are never edited; the decision arm is one file of additions.
    """
    scratch = Path(tempfile.mkdtemp(prefix="gt006-workflow-")) / "workflow"
    scratch.mkdir(parents=True)
    unknown: set[str] = set()
    for src in sorted(corpus_dir.glob("*.yaml")):
        body = src.read_text(encoding="utf-8")
        if decision_arm.IS_DECISION and src.stem in ("issues", "chat", "kb"):
            doc = decision_arm.splice(src.stem, yaml.safe_load(body),
                                      vnow or str(CONTRACT["virtual_now"]))
            body = yaml.safe_dump(doc, sort_keys=False, allow_unicode=False,
                                  width=1000)
        for name in set(re.findall(r"\{\{\s*([a-z_]+)\s*\}\}", body)):
            if name not in TOKENS:
                unknown.add(f"{src.name}: {{{{{name}}}}}")
        for name, value in TOKENS.items():
            body = re.sub(r"\{\{\s*" + name + r"\s*\}\}", value, body)
        if "{{" in body:
            raise StagingError(f"{src.name} still carries an unresolved token")
        (scratch / src.name).write_text(body, encoding="utf-8", newline="")
    if unknown:
        raise StagingError(
            "the corpus quotes tokens the contract does not define: "
            + ", ".join(sorted(unknown)))
    return scratch


def _strip_author_side(directory: Path) -> None:
    """Remove every `authoring` block from the SEEDED BYTES.

    The gateway already strips them from its own responses
    (gateway/src/alkera_gateway/workflow.py:31, AUTHOR_SIDE), so this is belt as
    well as braces - but the notes say which ticket is the cause and which page
    is the decoy, and leaving that on disk inside the world root depends on no
    tool, present or future, ever handing back a raw document. The notes live in
    fixtures/workflow/*.yaml where a reader wants them; they do not need to
    exist inside the episode at all.
    """
    def strip(node):
        if isinstance(node, dict):
            return {k: strip(v) for k, v in node.items() if k != "authoring"}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    for path in sorted(directory.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        _write(path, json.dumps(strip(doc), sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=True).encode("utf-8") + b"\n")


def stage_workflow(root: Path, vnow: str) -> dict:
    """Seed var/workflow/{issues,chat,kb}.json from the authored corpus.

    Goes through `world/world/workflow.py`'s own `seed`, which resolves every
    `-97d 09:21` offset against the world clock, copies the roster into both
    documents that need one, and runs the full validator - so a corpus this
    accepts is a corpus the workflow tools can read and write back to. Nothing
    in fixtures/workflow/ is edited or reformatted on the way through except the
    CONTRACT token substitution, which is checked above.
    """
    sys.path.insert(0, str(REPO / "world"))
    try:
        from world import workflow as world_workflow
    finally:
        sys.path.pop(0)
    scratch = _resolve_tokens(FIXTURES / "workflow", vnow)
    try:
        directory = world_workflow.seed(root, scratch.parent, WORKFLOW_SPEC, vnow)
    finally:
        shutil.rmtree(scratch.parent, ignore_errors=True)
    documents = sorted(p.name for p in directory.glob("*.json"))
    if documents != ["chat.json", "issues.json", "kb.json"]:
        raise StagingError(f"workflow seeded {documents}, expected all three")
    _strip_author_side(directory)
    return {"dir": str(directory), "documents": documents,
            "project": WORKFLOW_SPEC["project"]["key"],
            "workspace": WORKFLOW_SPEC["workspace"],
            "space": WORKFLOW_SPEC["space"]["key"],
            "actor": WORKFLOW_SPEC["actor"]}


# -------------------------------------------------------- 2. observability
# A literal, not a yaml.dump: the bytes are the contract and a serialiser's key
# order is not something to stake byte-identity on.
DEFINITIONS_YAML = """\
# Observability definitions for GT006 (gateway/SURFACES.md#observability).
# Staged by tests/stage_surfaces.py.
#
# EVERY SURFACE IS GREEN. Jobs ran, freshness passed, data quality passed,
# nothing alerted. That is not a convenience: this world's own tables say so.
# An alarm that fired here would contradict the world it is reading, so none is
# planted to fire and none does.
#
# WHAT IS NOT HERE, ON PURPOSE. There is no metric that compares
# warehouse.fct_payments against raw.payments_landing, and no per-day
# completeness series. The premise of this unit is that correctness is the axis
# nobody monitors; a panel whose whole job is to notice the defect would delete
# it. Volume gauges over each relation are the normal monitoring a team has and
# they are here. A reconciliation monitor is not, and DATA-1712 is the open
# ticket asking for one.
#
# WHICH METRICS CARRY RECORDED HISTORY. Only the ones the world actually
# observed: the two freshness readings (ops.freshness_checks), the data quality
# reading (ops.dq_checks) and the nightly load's duration and error rate
# (ops.job_runs). The row counts are measured live on every call and carry no
# recorded series, because no historical row count exists anywhere in this world
# and reconstructing one from present state is precisely what a sample store
# exists to make impossible.
version: 1

retention:
  days: 14

metrics:
  - id: payments_freshness
    kind: freshness
    unit: seconds
    title: Payments mart freshness
    description: >-
      Seconds between the newest payment_dt in warehouse.fct_payments and the
      reading. payment_dt is a DATE, so this measures to midnight of the newest
      partition - the honest reading for a column with no time of day.
    source: {table: warehouse.fct_payments, timestamp_column: payment_dt}
  - id: kpi_freshness
    kind: freshness
    unit: seconds
    title: Daily settlement KPI freshness
    source: {table: warehouse.kpi_payments_daily, timestamp_column: dt}
  - id: payments_rows
    kind: row_count
    unit: rows
    title: Payments mart rows
    description: Live count of warehouse.fct_payments; no recorded history exists for it.
    source: {table: warehouse.fct_payments}
  - id: landing_rows
    kind: row_count
    unit: rows
    title: Processor feed rows
    source: {table: raw.payments_landing}
  - id: payments_key_null_rate
    kind: null_rate
    unit: ratio
    title: Payments with no payment_id
    description: The not-null control ops.dq_checks runs nightly, measured live.
    source: {table: warehouse.fct_payments, column: payment_id}
  - id: dq_failed_rows
    kind: sql_scalar
    unit: rows
    title: Failing rows in the latest data quality run
    description: >-
      The sum of failed_rows across every control in the most recent
      ops.dq_checks batch. The world's own nightly control, read as a number.
    source:
      sql: >-
        select coalesce(sum(failed_rows), 0) as failed_rows from ops.dq_checks
        where checked_at = (select max(checked_at) from ops.dq_checks)
  - id: nightly_load_duration
    kind: job_duration
    unit: seconds
    title: Nightly payments load duration
    job: build_payments
  - id: nightly_load_error_rate
    kind: job_error_rate
    unit: ratio
    title: Nightly payments load error rate
    job: build_payments
    window_runs: 10

alarms:
  - id: payments_stale
    metric: payments_freshness
    comparison: gt
    threshold: 172800
    severity: critical
    title: The payments mart is more than two days behind
    description: >-
      Freshness against a DATE column peaks at about 118 800 s in the hours
      before each nightly load, so a flat two-day threshold would flicker every
      night on a healthy world. Forty-eight hours means a night was genuinely
      missed.
    runbook: kb:de/runbooks/payments-load
  - id: kpi_stale
    metric: kpi_freshness
    comparison: gt
    threshold: 172800
    severity: warning
    title: The daily settlement KPI is more than two days behind
  - id: dq_failing
    metric: dq_failed_rows
    comparison: gt
    threshold: 0
    severity: critical
    title: The nightly data quality run has failing rows
    description: >-
      AUTHOR NOTE, agent-visible by design: the controls this alarm reads are
      not-null on three columns and row-count-greater-than-zero. None of them
      compares the mart to the feed it is built from.
  - id: payments_key_missing
    metric: payments_key_null_rate
    comparison: gt
    threshold: 0
    severity: warning
    title: Payments are arriving with no payment_id
  - id: nightly_load_failing
    metric: nightly_load_error_rate
    comparison: gte
    threshold: 0.2
    severity: warning
    title: The nightly payments load is failing more than one run in five
    runbook: kb:de/runbooks/payments-load
  - id: nightly_load_slow
    metric: nightly_load_duration
    comparison: gt
    threshold: 900
    severity: warning
    title: The nightly payments load ran long

deploys:
  - id: rebuild_payments_marts
    kind: dbt
    title: Rebuild the payments marts
    description: >-
      Runs the warehouse models and re-measures the canary either side. The
      guards are volume floors: a rebuild that drops rows is the failure mode
      worth catching, and it is reported rather than blocked.
    selector: warehouse
    canary:
      metrics: [payments_rows, payments_freshness, dq_failed_rows]
      alarms: [payments_stale, dq_failing]
      guards:
        - {metric: payments_rows, max_decrease_pct: 5}
"""


def _freshness_samples(con) -> list[dict]:
    rows = con.execute(
        "SELECT checked_at, dataset, max_partition_dt, threshold_hours, status "
        "FROM wh.ops.freshness_checks ORDER BY checked_at, dataset").fetchall()
    out = []
    for checked_at, dataset, watermark, threshold_hours, status in rows:
        entry = FRESHNESS_METRICS.get(dataset)
        if entry is None:
            continue
        mark = datetime(watermark.year, watermark.month, watermark.day)
        out.append({
            "vts": _iso(checked_at),
            "metric": entry[0],
            "value": (checked_at - mark).total_seconds(),
            "source": "advance",
            "run_id": None,
            "detail": {"watermark": _iso(mark), "table": dataset,
                       "column": entry[1], "threshold_hours": int(threshold_hours),
                       "check_status": status},
        })
    return out


def _dq_samples(con) -> list[dict]:
    rows = con.execute(
        "SELECT checked_at, sum(failed_rows) AS failed_rows, count(*) AS checks, "
        "count(*) FILTER (WHERE status <> 'PASS') AS not_pass "
        "FROM wh.ops.dq_checks GROUP BY checked_at ORDER BY checked_at").fetchall()
    return [{
        "vts": _iso(checked_at),
        "metric": "dq_failed_rows",
        "value": float(failed_rows),
        "source": "advance",
        "run_id": None,
        "detail": {"raw": int(failed_rows), "checks": int(checks),
                   "checks_not_pass": int(not_pass)},
    } for checked_at, failed_rows, checks, not_pass in rows]


def _job_samples(con) -> list[dict]:
    rows = con.execute(
        "SELECT run_id, started_at, ended_at, status FROM wh.ops.job_runs "
        f"WHERE job = '{JOB}' ORDER BY ended_at, run_id").fetchall()
    out = []
    for index, (run_id, started_at, ended_at, status) in enumerate(rows):
        window = rows[max(0, index + 1 - JOB_ERROR_WINDOW): index + 1]
        failures = [run for run in window if run[3] != "success"]
        out.append({
            "vts": _iso(ended_at), "metric": "nightly_load_duration",
            "value": (ended_at - started_at).total_seconds(),
            "source": "advance", "run_id": run_id,
            "detail": {"job": JOB, "status": status,
                       "started_at": _iso(started_at), "finished_at": _iso(ended_at)},
        })
        out.append({
            "vts": _iso(ended_at), "metric": "nightly_load_error_rate",
            "value": len(failures) / len(window),
            "source": "advance", "run_id": run_id,
            "detail": {"job": JOB, "runs": len(window),
                       "failed_runs": len(failures),
                       "window_runs": JOB_ERROR_WINDOW},
        })
    return out


def stage_observability(root: Path, vnow: str) -> dict:
    con = _connect(root / WAREHOUSE)
    try:
        samples = _freshness_samples(con) + _dq_samples(con) + _job_samples(con)
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
        raise StagingError(
            f"{stray} exists alongside definitions.yaml; a world declares one")
    window_start = now - timedelta(days=RETENTION_DAYS)
    retained = [row for row in samples if _parse(row["vts"]) >= window_start]
    return {"definitions": str(root / DEFINITIONS), "samples": str(root / SAMPLES),
            "sample_rows": len(samples), "retained_at_vnow": len(retained),
            "metrics_with_record": sorted({row["metric"] for row in samples})}


# --------------------------------------------------------------- 3. ingest
CONNECTOR = {
    "id": "payments_processor_delivery",
    "name": "Card processor settled-payments delivery",
    "source": {"type": "sftp", "location": "processor/settled"},
    "destination": {"schema": "raw", "tables": ["payments_landing"]},
    "status": "active",
    # arrival_ts is payment_dt + 1 day + 02:10 for every row in the stream
    # (make_fixture_world.landing_sql), so the cron is that time.
    "schedule": "10 2 * * *",
    "duration_s": 130,
    "batch_column": "batch_id",
    "time_column": "arrival_ts",
    "table": "raw.payments_landing",
}


def _columns(con, table: str) -> list[str]:
    schema, name = table.split(".")
    return [row[0] for row in con.execute(
        "SELECT column_name FROM duckdb_columns() WHERE database_name = 'wh' "
        "AND schema_name = ? AND table_name = ? ORDER BY column_index",
        [schema, name]).fetchall()]


def stage_ingest(root: Path, vnow: str) -> dict:
    """Write var/ingest/state.json from the deliveries this world actually holds.

    One stream, and it is the world's real ingestion story. Every run is one real
    batch - its id, its timestamp and its row count are read off the rows, not
    assigned. A batch that is not in the data is not a run.

    `alerts` is empty and `column_added`/`column_removed` are the connector's
    declared POLICY rather than an observed event, because this world records no
    ingestion alert and no schema change - the columns did not change, which is
    exactly the trap. Inventing either would be the fake that is worse than an
    absent surface.
    """
    con = _connect(root / WAREHOUSE)
    try:
        table = CONNECTOR["table"]
        full = f"{CONNECTOR['destination']['schema']}.{CONNECTOR['destination']['tables'][0]}"
        if full != table:
            raise StagingError(f"destination {full} does not match {table}")
        batches = con.execute(
            f"SELECT {CONNECTOR['batch_column']} AS batch, "
            f"max({CONNECTOR['time_column']}) AS at, count(*) AS rows "
            f"FROM wh.{table} GROUP BY 1 ORDER BY 2, 1").fetchall()
        if not batches:
            raise StagingError(f"{table} holds no batch to describe")
        connectors = {CONNECTOR["id"]: {
            "name": CONNECTOR["name"],
            "source": CONNECTOR["source"],
            "destination": CONNECTOR["destination"],
            "status": CONNECTOR["status"],
            "schedule": CONNECTOR["schedule"],
            "sync": {"duration_s": CONNECTOR["duration_s"],
                     "tables": {full: {"rows_inserted": int(batches[-1][2]),
                                       "rows_updated": 0}}},
        }}
        schemas = [{"connector_id": CONNECTOR["id"], "table": full,
                    "columns": _columns(con, table),
                    "column_added": "propagate", "column_removed": "ignore"}]
        runs = []
        for batch, at, rows in batches:
            started = at - timedelta(seconds=CONNECTOR["duration_s"])
            runs.append({
                "id": batch, "connector_id": CONNECTOR["id"],
                "started_at": _iso(started), "finished_at": _iso(at),
                "status": "success",
                "tables": {full: {"rows_inserted": int(rows), "rows_updated": 0}},
                "timeline": [{"at": _iso(started), "event": "started"},
                             {"at": _iso(at), "event": "succeeded"}],
            })
    finally:
        con.close()
    runs.sort(key=lambda run: (run["started_at"], run["id"]))
    now = _parse(vnow)
    ahead = [run for run in runs if _parse(run["finished_at"]) > now]
    if ahead:
        raise StagingError(
            f"{len(ahead)} sync run(s) finish after the world clock {vnow}; "
            f"first is {ahead[0]['id']} at {ahead[0]['finished_at']}")
    state = {"connectors": connectors, "runs": runs, "schemas": schemas, "alerts": []}
    _write(root / INGEST_STATE, _canon(state) + b"\n")
    return {"state": str(root / INGEST_STATE), "connectors": sorted(connectors),
            "runs": len(runs), "tables": [entry["table"] for entry in schemas]}


# ----------------------------------------------------------------- the seam
def stage(root: Path, vnow: str) -> dict:
    """Stage every surface task.toml declares and the warehouse cannot serve.

    MUST run after `make_fixture_world.stage_world_root` (it reads the staged
    warehouse) and BEFORE `make_fixture_world.write_grading_zone` (so the
    collateral baseline and pre_repair_world both hold the seeded planes).
    """
    root = Path(root)
    if not (root / WAREHOUSE).is_file():
        raise StagingError(
            f"{root / WAREHOUSE} is not there; stage_world_root must run first")
    return {"workflow": stage_workflow(root, vnow),
            "obs": stage_observability(root, vnow),
            "ingest": stage_ingest(root, vnow)}


def check_staged_before_baseline(world_root: Path, grading: Path) -> list[str]:
    """Did the grading baseline capture the staged planes?

    `collateral.score` penalises a seeded plane that has disappeared. A plane
    present in the world and absent from `grading/world_sections.json` would
    read as "the plane appeared" instead - or worse, a plane seeded AFTER the
    baseline is one the collateral check can never notice being deleted.
    Returns the problems it found; empty means the ordering held.
    """
    problems = []
    snapshot = Path(grading) / "world_sections.json"
    if not snapshot.is_file():
        return [f"{snapshot} does not exist"]
    planes = json.loads(snapshot.read_text(encoding="utf-8")) \
        .get("sections", {}).get("planes", {})
    for name, rel in (("obs", OBS_DIR), ("ingest", INGEST_STATE),
                      ("workflow", WORKFLOW_DIR)):
        in_world = (Path(world_root) / rel).exists()
        in_baseline = planes.get(name) is not None
        if in_world and not in_baseline:
            problems.append(
                f"{name}: staged into the world but missing from the collateral "
                f"baseline - stage() must run before write_grading_zone")
        if in_baseline and not in_world:
            problems.append(f"{name}: in the baseline but not in the world")
    return problems


def _cli(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="stage_surfaces")
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--vnow")
    ap.add_argument("--grading", type=Path)
    a = ap.parse_args(argv)
    vnow = a.vnow
    if vnow is None:
        state = json.loads((a.root / "var" / "sched" / "state.json")
                           .read_text(encoding="utf-8"))
        vnow = state["vnow"]
    report = stage(a.root, vnow)
    if a.grading:
        report["baseline_check"] = check_staged_before_baseline(a.root, a.grading)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
