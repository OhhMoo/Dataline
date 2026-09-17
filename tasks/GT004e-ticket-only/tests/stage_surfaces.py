#!/usr/bin/env python3
"""
GT004 surface staging - the half of the world the warehouse tools cannot hold.

WHY THIS FILE EXISTS. The first real K=5 rollout of GT004 reported

    dead surfaces in this world: ['alarms', 'chat', 'dbt', 'deploy', 'ingest',
                                  'issues', 'kb', 'metrics']

and the best rollout's audit log shows the subject calling `issues_list`,
`kb_list`, `chat_channels` and `alarms_list` exactly once each, collecting
`no_issue_tracker` / `no_knowledge_base` / `no_chat` / `no_observability`, and
never looking again. task.toml declares nine surfaces; the episode served four.
`fixtures/workflow/` already held an authored, claim-checked corpus - ten
tickets, three channels, twelve wiki pages - that nothing ever staged into the
world root. This module stages it, and derives the observability and ingestion
planes the same world already implies.

THE ONE RULE. **Nothing here is invented.** Every ticket, message and page is
read from `fixtures/workflow/*.yaml` and assembled by `world/world/workflow.py`
- the same assembler every theme uses, with the same validator. Every
observability sample is read out of `ops.freshness_checks`, `ops.dq_checks` or
`ops.job_runs`, which are the world's own record of what it observed. Every
ingestion run is read out of `raw.orders_landing.arrival_ts` and
`raw.orders_snapshot.snapshot_ts`. The single declared quantity that is *not*
derived from world state is each connector's `sync.duration_s`, which is
connector configuration rather than a measurement - it is the field
`ingest_trigger` builds a new run from - and it is named here so a reader can
see it is declared.

WHAT IS DELIBERATELY NOT SEEDED.
  * No metric compares the fact table against the source extract. Archetype
    A027 is "every surface is green; correctness is unmonitored", so a monitor
    whose whole job is to notice the defect would contradict the world.
  * No alarm fires, and none is planted to fire. Freshness, data quality and
    job health all pass, in the record and live, because in this world they
    genuinely did.
  * No ingestion alert, and no schema-drift event. The world records neither.
  * No recorded sample for a metric the world never observed. `orders_rows`
    has no historical row-count anywhere in `ops.*`, so it carries no recorded
    series and `metrics_series` says so (`source: none`) rather than offering
    a number that was reconstructed from present state. A store that can
    reconstruct the past from the present is decoration (OBS_STORE_CONTRACT.md).

ORDER MATTERS, AND IT IS A SCORING INTERACTION. `probes/collateral.py` compares
the post-episode world against `grading/world_sections.json`, and
`CONTRACT["probes"]["collateral"]["sections"]` marks **obs as `scored`** on the
reasoning that this world has no obs section, so one appearing is an agent
standing up monitoring nobody asked for. That reasoning is now out of date, and
the fix is ordering, not a contract edit: `stage()` must run BEFORE
`make_fixture_world.write_grading_zone`, so the baseline snapshot and
`pre_repair_world/` both contain the seeded planes. Seed them afterwards and
`obs` reads as "the section appeared" - a scored offence, -2 for every agent
including the oracle. `build_world.py` calls this in the right order; a caller
that does not is warned by `check_staged_before_baseline`.

DETERMINISM. Every text file is written with `write_bytes`, so no newline
translation can differ between a Windows host and a Linux container. JSON is
canonical (sorted keys, tight separators). The YAML definitions file is a
literal, not a `yaml.dump`. Every derived value comes from one read-only
aliased ATTACH of a fixture that is itself byte-identical per seed, and every
query carries an explicit ORDER BY. K rollouts from one seed stage identical
bytes.
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

# var/obs/definitions.yaml, not observability.yml: OBS_STORE_CONTRACT.md names
# it, every theme ships it under that name, and the gateway falls back to it
# when `GatewayConfig.observability` points at an observability.yml that does
# not exist (observability.py:518-531). A world carrying BOTH is refused, so
# exactly one is written.
OBS_DIR = Path("var") / "obs"
DEFINITIONS = OBS_DIR / "definitions.yaml"
SAMPLES = OBS_DIR / "samples.jsonl"
INGEST_STATE = Path("var") / "ingest" / "state.json"
WORKFLOW_DIR = Path("var") / "workflow"
WAREHOUSE = Path("var") / "warehouse" / "warehouse.duckdb"

RETENTION_DAYS = 14

# The three datasets ops.freshness_checks actually checks, and the metric each
# one is the recorded history of. Nothing else is declared as a freshness
# metric, because nothing else has a record.
FRESHNESS_METRICS = {
    "warehouse.fct_orders": ("orders_freshness", "order_dt"),
    "warehouse.fct_revenue": ("revenue_freshness", "order_dt"),
    "warehouse.kpi_daily": ("kpi_freshness", "dt"),
}

# The one scheduler job whose name is identical in var/sched/state.json and in
# ops.job_runs. A metric whose live reading comes from the scheduler and whose
# recorded history comes from `ops` must be the same job on both sides or the
# two halves of the surface are describing different things; `build_marts` and
# `refresh_bi_extract` have no same-named ops rows (ops calls them `dbt_build`
# and `load_revenue`), so they get no metric rather than a mapped one.
JOB = "load_orders_delta"
JOB_ERROR_WINDOW = 10


class StagingError(SystemExit):
    """A world these surfaces cannot honestly describe. Fails the build, loudly."""


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
    the staged world stops being byte-identical to the one built in a container."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _connect(db: Path):
    """Read-only, aliased ATTACH. F622: opening var/warehouse/warehouse.duckdb
    as the primary catalog makes the catalog and the `warehouse` schema collide
    and every two-part reference raises Ambiguous reference; READ_ONLY keeps K
    concurrent rollouts off each other's file lock."""
    import duckdb

    con = duckdb.connect()
    con.execute(f"ATTACH '{Path(db).as_posix()}' AS wh (READ_ONLY)")
    return con


# ------------------------------------------------------------- 1. workflow
def workflow_spec() -> dict:
    """The vocabulary, read from the corpus's own claim manifest.

    `fixtures/workflow/claims.yaml` restates the `workflow:` block a theme
    manifest would carry so `check_claims.py` can assemble the documents. It is
    the corpus's own declaration of the project key, the status set, the wiki
    space, the chat workspace and the id the agent writes as, so it is read
    from there rather than restated here - a second copy is a second thing to
    drift.
    """
    claims = yaml.safe_load((FIXTURES / "workflow" / "claims.yaml").read_text(encoding="utf-8"))
    spec = claims.get("manifest_workflow")
    if not isinstance(spec, dict):
        raise StagingError("claims.yaml carries no manifest_workflow block")
    return spec


def stage_workflow(root: Path, vnow: str) -> dict:
    """Seed var/workflow/{issues,chat,kb}.json from the authored corpus.

    Goes through `world/world/workflow.py`'s own `seed`, which resolves every
    `-12d 09:15` offset against the world clock, copies the roster into both
    documents that need one, and runs the full validator - so a corpus this
    accepts is a corpus the fifteen gateway tools can read and write back to.
    Nothing in fixtures/workflow/ is edited or reformatted on the way through.
    """
    sys.path.insert(0, str(REPO / "world"))
    try:
        from world import workflow as world_workflow
    finally:
        sys.path.pop(0)
    spec = workflow_spec()
    directory = world_workflow.seed(root, FIXTURES, spec, vnow)
    documents = sorted(p.name for p in directory.glob("*.json"))
    return {"dir": str(directory), "documents": documents,
            "project": spec["project"]["key"], "workspace": spec["workspace"],
            "space": spec["space"]["key"], "actor": spec.get("actor", "agent"),
            "statuses": list(spec["statuses"])}


# -------------------------------------------------------- 2. observability
# A literal, not a yaml.dump: the bytes are the contract and a serialiser's
# key order is not something to stake byte-identity on. Read by BOTH halves of
# the seam - world-side writers and the gateway's metrics/alarms/deploy tools.
DEFINITIONS_YAML = """\
# Observability definitions for GT004 (OBS_STORE_CONTRACT.md,
# gateway/OBSERVABILITY_CONTRACT.md). Staged by tests/stage_surfaces.py.
#
# ARCHETYPE A027 - EVERY SURFACE IS GREEN. Jobs ran, freshness passed, data
# quality passed, nothing alerted. That is not a convenience: this world's own
# record says so. ops.job_runs holds 544 runs and 0 failures,
# ops.freshness_checks 90 checks and 0 non-PASS, ops.dq_checks 180 checks and
# 0 failed rows. An alarm that fired here would contradict the world it is
# supposed to be reading, so none is planted to fire and none does.
#
# WHAT IS NOT HERE, ON PURPOSE. There is no metric that compares the fact table
# against the source extract, and no per-day bucket series over
# warehouse.fct_orders. A027's premise is that correctness is unmonitored and
# the defect is visible only in the data; a panel whose whole job is to notice
# it would delete the task. Volume gauges over each mart are the normal
# monitoring a team has and they are here; a correctness monitor is not.
#
# WHICH METRICS CARRY RECORDED HISTORY. Only the ones the world actually
# observed: the three freshness readings (ops.freshness_checks), the data
# quality reading (ops.dq_checks) and the nightly load's duration and error
# rate (ops.job_runs). The row counts and the null rate are measured live on
# every call and carry no recorded series, because no historical row count
# exists anywhere in this world and reconstructing one from present state is
# precisely what the sample store exists to make impossible.
version: 1

# Two weeks. The last fortnight of nightly checks is still evidence; the month
# of history before it is reported as aged_out rather than served, which is a
# limit a real record has and an oracle does not.
retention:
  days: 14

metrics:
  - id: orders_freshness
    kind: freshness
    unit: seconds
    title: Orders fact freshness
    description: >-
      Seconds between the newest order_dt in warehouse.fct_orders and the
      reading. order_dt is a DATE, so this measures to midnight of the newest
      partition - the honest reading for a column with no time of day.
    source: {table: warehouse.fct_orders, timestamp_column: order_dt}
  - id: revenue_freshness
    kind: freshness
    unit: seconds
    title: Revenue fact freshness
    source: {table: warehouse.fct_revenue, timestamp_column: order_dt}
  - id: kpi_freshness
    kind: freshness
    unit: seconds
    title: Daily KPI freshness
    source: {table: warehouse.kpi_daily, timestamp_column: dt}
  - id: orders_rows
    kind: row_count
    unit: rows
    title: Orders fact rows
    description: Live count of warehouse.fct_orders; no recorded history exists for it.
    source: {table: warehouse.fct_orders}
  - id: revenue_rows
    kind: row_count
    unit: rows
    title: Revenue fact rows
    source: {table: warehouse.fct_revenue}
  - id: landing_rows
    kind: row_count
    unit: rows
    title: Delta stream rows retained
    description: >-
      raw.orders_landing retains three days. The per-day series is derived from
      the stream on every call and is labelled as derived, not as history.
    source: {table: raw.orders_landing}
    series: {bucket_column: order_dt}
  - id: orders_key_null_rate
    kind: null_rate
    unit: ratio
    title: Orders fact rows with no order_id
    description: The not-null control ops.dq_checks runs nightly, measured live.
    source: {table: warehouse.fct_orders, column: order_id}
  - id: dq_failed_rows
    kind: sql_scalar
    unit: rows
    title: Failing rows in the latest data quality run
    description: >-
      The sum of failed_rows across every check in the most recent
      ops.dq_checks batch. The world's own nightly control, read as a number.
    source:
      sql: >-
        select coalesce(sum(failed_rows), 0) as failed_rows from ops.dq_checks
        where checked_at = (select max(checked_at) from ops.dq_checks)
  - id: nightly_load_duration
    kind: job_duration
    unit: seconds
    title: Nightly delta load duration
    job: load_orders_delta
  - id: nightly_load_error_rate
    kind: job_error_rate
    unit: ratio
    title: Nightly delta load error rate
    job: load_orders_delta
    window_runs: 10

alarms:
  - id: orders_stale
    metric: orders_freshness
    comparison: gt
    threshold: 172800
    severity: critical
    title: The orders fact is more than two days behind
    description: >-
      Freshness against a DATE column peaks at about 118 800 s in the hours
      before each nightly load, so a flat two-day threshold would flicker every
      night on a healthy world. Forty-eight hours means a night was genuinely
      missed.
    runbook: kb:de/runbooks/nightly-load
  - id: revenue_stale
    metric: revenue_freshness
    comparison: gt
    threshold: 172800
    severity: critical
    title: The revenue fact is more than two days behind
    runbook: kb:de/runbooks/nightly-load
  - id: kpi_stale
    metric: kpi_freshness
    comparison: gt
    threshold: 172800
    severity: warning
    title: The daily KPI table is more than two days behind
  - id: dq_failing
    metric: dq_failed_rows
    comparison: gt
    threshold: 0
    severity: critical
    title: The nightly data quality run has failing rows
    description: >-
      AUTHOR NOTE, agent-visible by design: the controls this alarm reads are
      not-null, row-count-greater-than-zero and freshness. None of them is a
      uniqueness or a reconciliation control.
  - id: orders_key_missing
    metric: orders_key_null_rate
    comparison: gt
    threshold: 0
    severity: warning
    title: Orders fact rows are arriving with no order_id
  - id: nightly_load_failing
    metric: nightly_load_error_rate
    comparison: gte
    threshold: 0.2
    severity: warning
    title: The nightly delta load is failing more than one run in five
    runbook: kb:de/runbooks/nightly-load
  - id: nightly_load_slow
    metric: nightly_load_duration
    comparison: gt
    threshold: 600
    severity: warning
    title: The nightly delta load ran long
    description: The load takes about 90 seconds; ten minutes means it processed more than one day.

deploys:
  - id: rebuild_commercial_marts
    kind: dbt
    title: Rebuild the commercial marts
    description: >-
      Runs the warehouse models and re-measures the canary either side. The
      guards are volume floors: a rebuild that drops rows is the failure mode
      worth catching, and it is reported rather than blocked.
    selector: warehouse
    canary:
      metrics: [orders_rows, revenue_rows, orders_freshness, revenue_freshness, dq_failed_rows]
      alarms: [orders_stale, revenue_stale, dq_failing]
      guards:
        - {metric: orders_rows, max_decrease_pct: 5}
        - {metric: revenue_rows, max_decrease_pct: 5}
"""


def _freshness_samples(con) -> list[dict]:
    """One recorded reading per nightly freshness check, as the world took it.

    value = checked_at - midnight(max_partition_dt), in seconds - the same
    arithmetic `_freshness` does live, against the watermark the check itself
    recorded. `detail` carries the watermark and the check's own verdict so a
    reader can re-derive the number from ops.freshness_checks.
    """
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
    """One recorded reading per nightly data quality batch: the failing rows it
    found. The live metric reads the newest batch; this is every batch, at the
    moment it ran."""
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
    """Duration and error rate for every recorded run of the nightly load.

    Taken at `ended_at`, which is when the reading could first have been taken.
    The error rate uses the same trailing window the metric declares, counted
    over the runs that had finished at that moment - so a reader can re-derive
    it from ops.job_runs and get the same number.
    """
    rows = con.execute(
        "SELECT run_id, started_at, ended_at, status FROM wh.ops.job_runs "
        f"WHERE job = '{JOB}' ORDER BY ended_at, run_id").fetchall()
    out = []
    for index, (run_id, started_at, ended_at, status) in enumerate(rows):
        window = rows[max(0, index + 1 - JOB_ERROR_WINDOW): index + 1]
        failures = [run for run in window if run[3] != "success"]
        out.append({
            "vts": _iso(ended_at),
            "metric": "nightly_load_duration",
            "value": (ended_at - started_at).total_seconds(),
            "source": "advance",
            "run_id": run_id,
            "detail": {"job": JOB, "status": status,
                       "started_at": _iso(started_at), "finished_at": _iso(ended_at)},
        })
        out.append({
            "vts": _iso(ended_at),
            "metric": "nightly_load_error_rate",
            "value": len(failures) / len(window),
            "source": "advance",
            "run_id": run_id,
            "detail": {"job": JOB, "runs": len(window),
                       "failed_runs": len(failures), "window_runs": JOB_ERROR_WINDOW},
        })
    return out


def stage_observability(root: Path, vnow: str) -> dict:
    """Write var/obs/definitions.yaml and var/obs/samples.jsonl.

    Both live INSIDE the world tree, which is where the contract puts them:
    `GatewayConfig` refuses only `obs_state_dir` - this episode's own readings,
    alarm transitions and deploy records - inside the world, and that stays at
    `<audit_dir>/obs` where the default leaves it (config.py:68-74).
    """
    con = _connect(root / WAREHOUSE)
    try:
        samples = _freshness_samples(con) + _dq_samples(con) + _job_samples(con)
    finally:
        con.close()
    now = _parse(vnow)
    ahead = [row for row in samples if _parse(row["vts"]) > now]
    if ahead:
        # A row stamped after the world clock is one the reader will refuse to
        # serve. Here it would mean the ops record runs past vnow, which is a
        # world defect, not a staging choice.
        raise StagingError(
            f"{len(ahead)} recorded sample(s) are stamped after the world clock "
            f"{vnow}; first is {ahead[0]['metric']} at {ahead[0]['vts']}")
    samples.sort(key=lambda row: (row["vts"], row["metric"]))
    _write(root / DEFINITIONS, DEFINITIONS_YAML.encode("utf-8"))
    _write(root / SAMPLES, b"".join(_canon(row) + b"\n" for row in samples))
    # observability.yml must NOT also exist: a world carrying two definitions
    # files is refused with invalid_observability.
    stray = root / OBS_DIR / "observability.yml"
    if stray.exists():
        raise StagingError(f"{stray} exists alongside definitions.yaml; a world declares one")
    window_start = now - timedelta(days=RETENTION_DAYS)
    retained = [row for row in samples if _parse(row["vts"]) >= window_start]
    return {
        "definitions": str(root / DEFINITIONS),
        "samples": str(root / SAMPLES),
        "sample_rows": len(samples),
        "retained_at_vnow": len(retained),
        "aged_out_at_vnow": len(samples) - len(retained),
        "metrics_with_record": sorted({row["metric"] for row in samples}),
    }


# --------------------------------------------------------------- 3. ingest
# Connector configuration. `source`, `destination`, `status` and `schedule`
# describe streams this world demonstrably has; `duration_s` is the declared
# sync length `ingest_trigger` builds a new run from and is the one value here
# that is configuration rather than a reading.
CONNECTORS = {
    "orders_delta_landing": {
        "name": "Commercial orders delta (nightly landing)",
        "source": {"type": "sftp", "location": "landing/orders"},
        "destination": {"schema": "raw", "tables": ["orders_landing"]},
        "status": "active",
        # arrival_ts is order_dt + 1 day + 01:40 for every row in the stream
        # (make_fixture_world.LANDING_SQL), so the cron is that time.
        "schedule": "40 1 * * *",
        "duration_s": 120,
        "batch_column": "batch_id",
        "time_column": "arrival_ts",
        "table": "raw.orders_landing",
    },
    "orders_vendor_snapshot": {
        "name": "Commercial orders full snapshot (vendor)",
        "source": {"type": "sftp", "location": "vendor/orders/snapshot"},
        "destination": {"schema": "raw", "tables": ["orders_snapshot"]},
        "status": "active",
        "schedule": "0 2 * * *",
        "duration_s": 900,
        "batch_column": "snapshot_id",
        "time_column": "snapshot_ts",
        "table": "raw.orders_snapshot",
    },
}


def _columns(con, table: str) -> list[str]:
    schema, name = table.split(".")
    return [row[0] for row in con.execute(
        "SELECT column_name FROM duckdb_columns() WHERE database_name = 'wh' "
        "AND schema_name = ? AND table_name = ? ORDER BY column_index",
        [schema, name]).fetchall()]


def stage_ingest(root: Path, vnow: str) -> dict:
    """Write var/ingest/state.json from the deliveries this world actually holds.

    Two streams, and they are the world's real ingestion story: a nightly delta
    that lands three retained days in raw.orders_landing, and one full vendor
    snapshot in raw.orders_snapshot. Every run is one real batch - its id, its
    timestamp and its row count are read off the rows, not assigned. A batch
    that is not in the data is not a run.

    `alerts` is empty and `column_added`/`column_removed` are the connector's
    declared policy rather than an observed drift event, because this world
    records no ingestion alert and no schema change. Inventing either would be
    the fake that is worse than an absent surface.
    """
    con = _connect(root / WAREHOUSE)
    try:
        connectors, schemas, runs = {}, [], []
        for cid in sorted(CONNECTORS):
            spec = CONNECTORS[cid]
            table = spec["table"]
            full = f"{spec['destination']['schema']}.{spec['destination']['tables'][0]}"
            if full != table:
                raise StagingError(f"{cid}: destination {full} does not match {table}")
            batches = con.execute(
                f"SELECT {spec['batch_column']} AS batch, "
                f"max({spec['time_column']}) AS at, count(*) AS rows "
                f"FROM wh.{table} GROUP BY 1 ORDER BY 2, 1").fetchall()
            if not batches:
                raise StagingError(f"{cid}: {table} holds no batch to describe")
            connectors[cid] = {
                "name": spec["name"],
                "source": spec["source"],
                "destination": spec["destination"],
                "status": spec["status"],
                "schedule": spec["schedule"],
                # The template a triggered sync is built from. Row counts are
                # the connector's typical delivery, taken as the most recent
                # real batch rather than chosen.
                "sync": {"duration_s": spec["duration_s"],
                         "tables": {full: {"rows_inserted": int(batches[-1][2]),
                                           "rows_updated": 0}}},
            }
            schemas.append({
                "connector_id": cid, "table": full,
                "columns": _columns(con, table),
                "column_added": "propagate", "column_removed": "ignore",
            })
            for batch, at, rows in batches:
                started = at - timedelta(seconds=spec["duration_s"])
                runs.append({
                    "id": batch,
                    "connector_id": cid,
                    "started_at": _iso(started),
                    "finished_at": _iso(at),
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
        # A run that has not finished at vnow reads back as "running", which
        # would be a sync stuck open in a world whose premise is that everything
        # is green. Refuse rather than ship it.
        raise StagingError(
            f"{len(ahead)} sync run(s) finish after the world clock {vnow}; "
            f"first is {ahead[0]['id']} at {ahead[0]['finished_at']}")
    state = {"connectors": connectors, "runs": runs, "schemas": schemas, "alerts": []}
    _write(root / INGEST_STATE, _canon(state) + b"\n")
    return {"state": str(root / INGEST_STATE),
            "connectors": sorted(connectors),
            "runs": len(runs),
            "tables": sorted(entry["table"] for entry in schemas)}


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
    return {
        "workflow": stage_workflow(root, vnow),
        "obs": stage_observability(root, vnow),
        "ingest": stage_ingest(root, vnow),
    }


def check_staged_before_baseline(world_root: Path, grading: Path) -> list[str]:
    """Did the grading baseline capture the staged planes?

    `probes/collateral.py` scores `obs` (CONTRACT overrides the module's
    advisory default) and `ingest` (the module's own default), so a section
    present in the world and absent from `grading/world_sections.json` reads as
    "the section appeared" and costs -2 per section for every agent, the oracle
    included. Returns the problems it found; empty means the ordering held.
    """
    problems = []
    snapshot = Path(grading) / "world_sections.json"
    if not snapshot.is_file():
        return [f"{snapshot} does not exist"]
    sections = json.loads(snapshot.read_text(encoding="utf-8")).get("sections", {})
    for name, rel in (("obs", OBS_DIR), ("ingest", INGEST_STATE),
                      ("workflow", WORKFLOW_DIR)):
        in_world = (Path(world_root) / rel).exists()
        in_baseline = sections.get(name) is not None
        if in_world and not in_baseline:
            problems.append(
                f"{name}: staged into the world but missing from the collateral "
                f"baseline - stage_surfaces.stage() must run before write_grading_zone")
        if in_baseline and not in_world:
            problems.append(f"{name}: in the baseline but not in the world")
    return problems


def _cli(argv=None) -> int:
    """`python stage_surfaces.py --root <world_root> [--vnow ...]`"""
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
