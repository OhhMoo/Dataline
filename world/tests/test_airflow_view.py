"""Airflow's view of the world's scheduler (world/world/airflow_view.py) and the
scheduler queue it writes through (world/world/scheduler.py enqueue/_drain).

    gateway/.venv/bin/python -m pytest world/tests/test_airflow_view.py -q

The claims:

- DAGs come from the theme's own `dags/*.py`, read with `ast` (never imported):
  dag_id, schedule, tasks, edges; a task is a job exactly when the ledger has
  one by that name (a sensor never is).
- DAG runs mirror ledger runs: one per occurrence of the DAG's root job, every
  task instance the latest attempt of its job in that occurrence.
- `trigger` and `clear` QUEUE (state.json `queue`): `vnow`, `runs` and the
  warehouse do not move. The grader's `advance` drains the queue first, at its
  own start, and the runs then appear with their engine output.
- The queue is invisible until used: no key, no byte, no behaviour change, and
  `advance(1)` x N still lands the world `advance(N)` does.
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
from pathlib import Path

import duckdb
import pytest
import yaml

WORLD_DIR = Path(__file__).resolve().parents[1]
THEMES = WORLD_DIR / "world" / "themes"
sys.path.insert(0, str(WORLD_DIR))

from world.themes import load_theme  # noqa: E402

from world import airflow_view as av  # noqa: E402
from world import scheduler  # noqa: E402

DAYS = 3


def build(root: Path, theme: str = "commerce_analytics", days: int = DAYS,
          live: bool = True) -> Path:
    """A world with the theme's dags/, its seeded ledger history over `days`
    nights and - when `live` - a live block whose jobs run one SQL statement
    each, so `advance` executes them for real and fast."""
    manifest = load_theme(theme).manifest
    seed = yaml.safe_load((THEMES / theme / "seed.example.yaml").read_text("utf-8"))
    seed["days"] = days
    shutil.copytree(THEMES / theme / "workspace" / "dags", root / "workspace" / "dags")
    (root / "var" / "sched").mkdir(parents=True)
    scheduler.save(root, scheduler.initial_state(seed, manifest["jobs"]))
    warehouse = root / "var" / "warehouse" / "warehouse.duckdb"
    warehouse.parent.mkdir(parents=True)
    with duckdb.connect(str(warehouse)) as con:
        con.execute("CREATE TABLE ran (job VARCHAR)")
    if live:
        jobs = {}
        for name, spec in manifest["jobs"].items():
            spec = {k: v for k, v in spec.items() if k != "exec"}
            spec["exec"] = {"kind": "sql",
                            "statements": [f"INSERT INTO ran VALUES ('{name}')"]}
            jobs[name] = spec
        scheduler.save_live(root, {"vtime_start": seed["vtime_start"],
                                   "days_landed": days, "arrival": None, "jobs": jobs})
    return root


def state(root: Path) -> dict:
    return json.loads((root / scheduler.STATE).read_text("utf-8"))


def ran(root: Path) -> list[str]:
    with duckdb.connect(str(root / "var" / "warehouse" / "warehouse.duckdb"),
                        read_only=True) as con:
        return [r[0] for r in con.execute("SELECT job FROM ran").fetchall()]


@pytest.fixture
def world(tmp_path) -> Path:
    return build(tmp_path / "world")


# ------------------------------------------------------------------- DAGs
def test_dags_parsed_from_theme_dags_folder(tmp_path):
    root = build(tmp_path / "w", theme="revenue_daily", live=False)
    found = av.dags(root)
    assert [d.dag_id for d in found] == ["revenue_daily"]
    dag = found[0]
    assert dag.schedule == "0 1 * * *" and dag.file == "revenue_daily.py"
    assert [(t.task_id, t.job, t.operator) for t in dag.tasks] == [
        ("daily_load", "daily_load", "BashOperator"),
        ("dbt_build", "dbt_build", "BashOperator")]
    assert dag.task("dbt_build").upstream == ("daily_load",)
    assert dag.task("daily_load").downstream == ("dbt_build",)
    # default_args reach the task: retries 1, retry_delay 10 minutes
    assert dag.task("daily_load").retries == 1
    assert dag.task("daily_load").retry_delay_s == 600
    assert dag.task("daily_load").execution_timeout_s == 900
    assert dag.task("daily_load").bash_command == "cd /workspace && ./run.sh load"
    assert dag.owners == ("data-platform",) and dag.tags == ("daily", "revenue", "tier-1")
    assert av.dag_source(root, "revenue_daily") == (
        root / "workspace" / "dags" / "revenue_daily.py").read_text("utf-8")


def test_two_dags_in_a_theme_and_module_level_tasks(world):
    found = {d.dag_id: d for d in av.dags(world)}
    assert sorted(found) == ["bi_refresh", "commerce_nightly"]
    # `dag = DAG("commerce_nightly", ...)` (positional id) and tasks with dag=dag
    assert found["commerce_nightly"].jobs == ("daily_load", "dbt_build")
    assert found["bi_refresh"].jobs == ("bi_refresh",)
    assert found["commerce_nightly"].schedule == "30 2 * * *"


def test_a_sensor_is_a_task_and_never_a_job(tmp_path):
    root = build(tmp_path / "w", theme="mainframe_batch", live=False)
    dag = av.dag(root, "claims_nightly")
    sensor = dag.task("settle_window")
    assert sensor.operator == "TimeDeltaSensor" and sensor.job is None
    assert dag.task("batch_load").upstream == ("settle_window",)
    run = av.dag_runs(root, "claims_nightly")[-1]["dag_run_id"]
    tis = {t["task_id"]: t for t in av.task_instances(root, "claims_nightly", run)}
    assert tis["settle_window"]["state"] is None
    assert "no job named 'settle_window'" in tis["settle_window"]["note"]
    assert tis["batch_load"]["state"] == "success"


def test_a_file_that_does_not_parse_is_an_import_error(world):
    (world / "workspace" / "dags" / "broken.py").write_text("with DAG(:\n", "utf-8")
    assert "broken.py" in av.import_errors(world)
    assert sorted(d.dag_id for d in av.dags(world)) == ["bi_refresh", "commerce_nightly"]


def test_the_dag_files_are_read_live(world):
    path = world / "workspace" / "dags" / "commerce_nightly.py"
    path.write_text(path.read_text("utf-8").replace('"30 2 * * *"', '"45 3 * * *"'), "utf-8")
    assert av.dag(world, "commerce_nightly").schedule == "45 3 * * *"


# ------------------------------------------------------------------- runs
def test_dagruns_mirror_ledger_runs(world):
    ledger = state(world)["runs"]
    runs = av.dag_runs(world, "commerce_nightly")
    loads = [r for r in ledger if r["job"] == "daily_load"]
    # one DAG run per occurrence of the root job, keyed by its start
    assert [r["dag_run_id"] for r in runs] == [
        f"scheduled__{av._dt(r['vts_start']).isoformat()}" for r in loads]
    assert all(r["run_type"] == "scheduled" and r["triggered_by"] == "timetable"
               for r in runs)
    first = runs[0]
    assert first["logical_date"] == first["run_after"] == av.stamp(loads[0]["vts_start"])
    builds = [r for r in ledger if r["job"] == "dbt_build"]
    assert first["start_date"] == av.stamp(loads[0]["vts_start"])
    assert first["end_date"] == av.stamp(builds[0]["vts_end"])
    tis = {t["task_id"]: t for t in av.task_instances(world, "commerce_nightly",
                                                      first["dag_run_id"])}
    assert tis["dbt_build"]["start_date"] == av.stamp(builds[0]["vts_start"])
    assert tis["dbt_build"]["state"] == "success" and tis["dbt_build"]["try_number"] == 1
    # the other DAG holds only its own job
    assert len(av.dag_runs(world, "bi_refresh")) == len(
        [r for r in ledger if r["job"] == "bi_refresh"])


def test_a_failed_and_retried_job_is_one_task_instance_with_two_tries(world):
    data = state(world)
    runs = data["runs"]
    index = next(i for i, r in enumerate(runs) if r["job"] == "dbt_build")
    first = dict(runs[index], status="failed", error="boom", try_number=1, max_tries=2)
    second = dict(runs[index], try_number=2, max_tries=2)
    runs[index:index + 1] = [first, second]
    scheduler.save(world, data)
    run = av.dag_runs(world, "commerce_nightly")[0]
    tis = {t["task_id"]: t for t in av.task_instances(world, "commerce_nightly",
                                                      run["dag_run_id"])}
    assert tis["dbt_build"]["try_number"] == 2 and tis["dbt_build"]["state"] == "success"
    assert len(av.dag_runs(world, "commerce_nightly")) == DAYS
    log1 = av.task_log(world, "commerce_nightly", run["dag_run_id"], "dbt_build", 1)
    assert "boom" in log1
    with pytest.raises(av.AirflowError) as missing:
        av.task_log(world, "commerce_nightly", run["dag_run_id"], "dbt_build", 3)
    assert missing.value.status == 404


def test_a_manual_sched_trigger_run_is_a_manual_dag_run_of_its_task(world):
    scheduler.trigger(world, "dbt_build")              # MCP/ops-sched semantics
    runs = av.dag_runs(world, "commerce_nightly")
    manual = [r for r in runs if r["run_type"] == "manual"]
    assert len(manual) == 1 and manual[0]["triggered_by"] == "cli"
    assert manual[0]["dag_run_id"].startswith("manual__")
    tis = {t["task_id"]: t for t in av.task_instances(world, "commerce_nightly",
                                                      manual[0]["dag_run_id"])}
    assert tis["dbt_build"]["state"] == "success" and tis["daily_load"]["state"] is None


# ------------------------------------------------------- the queue: no clock
def test_trigger_queues_and_never_advances(world):
    before = state(world)
    run = av.trigger(world, "commerce_nightly", user="alkera-local-dev")
    after = state(world)
    assert run["state"] == "queued" and run["run_type"] == "manual"
    assert run["start_date"] is None and run["end_date"] is None
    assert run["queued_at"] == run["run_after"] == av.stamp(before["vnow"])
    assert run["triggering_user_name"] == "alkera-local-dev"
    assert after["vnow"] == before["vnow"] and after["runs"] == before["runs"]
    assert [e["job"] for e in after["queue"]] == ["daily_load", "dbt_build"]
    assert all(e["origin"]["dag_run_id"] == run["dag_run_id"] for e in after["queue"])
    assert ran(world) == []                                     # nothing executed
    assert not (world / av.LOGS_DIR).exists()
    tis = av.task_instances(world, "commerce_nightly", run["dag_run_id"])
    assert {t["state"] for t in tis} == {"queued"}
    log = av.task_log(world, "commerce_nightly", run["dag_run_id"], "dbt_build", 1)
    assert "has not run" in log


def test_double_trigger_gives_two_queued_runs(world):
    one = av.trigger(world, "commerce_nightly")
    two = av.trigger(world, "commerce_nightly")
    assert one["dag_run_id"] != two["dag_run_id"]
    assert len(state(world)["queue"]) == 4
    queued = [r for r in av.dag_runs(world, "commerce_nightly") if r["state"] == "queued"]
    assert [r["dag_run_id"] for r in queued] == sorted([one["dag_run_id"],
                                                        two["dag_run_id"]])
    # a logical date makes Airflow's own run id, and a second one is a conflict
    day = state(world)["vnow"][:10] + "T00:00:00Z"            # a past instant
    dated = av.trigger(world, "bi_refresh", logical_date=day)
    assert dated["dag_run_id"] == f"manual__{day[:10]}T00:00:00+00:00"
    with pytest.raises(av.AirflowError) as again:
        av.trigger(world, "bi_refresh", logical_date=day)
    assert again.value.status == 409


def test_a_logical_date_after_the_worlds_clock_is_refused(world):
    """airflowctl fills in the wall clock when no --logical-date is given: a day
    the world has not reached. Refused by name; vnow itself is fine."""
    vnow = state(world)["vnow"]
    with pytest.raises(av.AirflowError) as later:
        av.trigger(world, "commerce_nightly", logical_date="2026-10-07T09:00:00Z")
    assert later.value.status == 400 and "after the world's clock" in str(later.value)
    assert "queue" not in state(world)
    run = av.trigger(world, "commerce_nightly", logical_date=vnow)
    assert run["logical_date"] == av.stamp(vnow)
    assert state(world)["queue"][0]["execution_date"] == vnow[:10]


def test_an_engine_log_the_executor_kept_is_served(world):
    """A run's raw engine output (build/sched/logs/run-NNNNNN.log) is in its log,
    between the start line and the outcome."""
    data = state(world)
    index = max(i for i, r in enumerate(data["runs"], 1) if r["job"] == "dbt_build")
    raw = world / av.LOGS_DIR / f"run-{index:06d}.log"
    raw.parent.mkdir(parents=True)
    raw.write_text("Running with dbt=1.12.4\nCompleted successfully\n", "utf-8")
    run = av.dag_runs(world, "commerce_nightly")[-1]["dag_run_id"]
    events = av.task_log_events(world, "commerce_nightly", run, "dbt_build", 1)
    texts = [e["event"] for e in events]
    assert texts[1:3] == ["Running with dbt=1.12.4", "Completed successfully"]
    assert events[1]["logger"] == "task.stdout" and texts[-1] == "Task instance in success state"
    assert "kept no engine output" not in "\n".join(texts)


def test_the_next_advance_executes_the_queue_first(world):
    before = state(world)
    run = av.trigger(world, "commerce_nightly")
    result = scheduler.advance(world, 1)
    after = state(world)
    assert "queue" not in after
    assert after["vnow"] == scheduler.iso(scheduler.parse_vtime(before["vnow"])
                                          + scheduler.timedelta(hours=1))
    assert [e["name"] for e in result["events"]][:2] == ["daily_load", "dbt_build"]
    assert ran(world)[:2] == ["daily_load", "dbt_build"]
    new = after["runs"][len(before["runs"]):]
    assert [r["job"] for r in new][:2] == ["daily_load", "dbt_build"]
    assert new[0]["vts_start"] == before["vnow"]
    assert new[1]["vts_start"] == new[0]["vts_end"]            # one worker, in order
    assert all(r["trigger"] == "manual" and r["origin"]["dag_run_id"] == run["dag_run_id"]
               for r in new[:2])
    shown = next(r for r in av.dag_runs(world, "commerce_nightly")
                 if r["dag_run_id"] == run["dag_run_id"])
    assert shown["state"] == "success" and shown["end_date"] == av.stamp(new[1]["vts_end"])


def test_clear_marks_and_next_advance_executes(world):
    data = state(world)
    index = max(i for i, r in enumerate(data["runs"]) if r["job"] == "dbt_build")
    data["runs"][index].update(status="failed", error="test failed: rows dropped")
    scheduler.save(world, data)
    run = av.dag_runs(world, "commerce_nightly")[-1]
    assert run["state"] == "failed"
    # dry run (Airflow's default): what would be cleared, and nothing moves
    dry = av.clear(world, "commerce_nightly", dag_run_id=run["dag_run_id"])
    assert [t["task_id"] for t in dry] == ["dbt_build"] and dry[0]["state"] == "failed"
    assert "queue" not in state(world)
    cleared = av.clear(world, "commerce_nightly", dag_run_id=run["dag_run_id"],
                       dry_run=False, user="alkera-local-dev")
    assert [(t["task_id"], t["state"], t["try_number"]) for t in cleared] == [
        ("dbt_build", "queued", 2)]
    assert [e["job"] for e in state(world)["queue"]] == ["dbt_build"]
    assert av.find_run(world, av.dag(world, "commerce_nightly"),
                       run["dag_run_id"])[0].run_id == run["dag_run_id"]
    shown = next(r for r in av.dag_runs(world, "commerce_nightly")
                 if r["dag_run_id"] == run["dag_run_id"])
    assert shown["state"] == "queued" and shown["run_type"] == "scheduled"
    assert ran(world) == []
    scheduler.advance(world, 1)                                   # the grader's
    assert ran(world)[0] == "dbt_build"
    tis = {t["task_id"]: t for t in av.task_instances(world, "commerce_nightly",
                                                      run["dag_run_id"])}
    assert tis["dbt_build"]["state"] == "success" and tis["dbt_build"]["try_number"] == 2
    log2 = av.task_log(world, "commerce_nightly", run["dag_run_id"], "dbt_build", 2)
    assert "Task instance in success state" in log2
    assert "rows dropped" in av.task_log(world, "commerce_nightly", run["dag_run_id"],
                                         "dbt_build", 1)
    # only_failed (the default) now selects nothing
    assert av.clear(world, "commerce_nightly", dag_run_id=run["dag_run_id"]) == []


def test_clear_with_downstream_queues_in_dependency_order(world):
    run = av.dag_runs(world, "commerce_nightly")[-1]
    cleared = av.clear(world, "commerce_nightly", dag_run_id=run["dag_run_id"],
                       task_ids=["daily_load"], include_downstream=True,
                       only_failed=False, dry_run=False)
    assert [t["task_id"] for t in cleared] == ["daily_load", "dbt_build"]
    assert [e["job"] for e in state(world)["queue"]] == ["daily_load", "dbt_build"]
    with pytest.raises(av.AirflowError) as unknown:
        av.clear(world, "commerce_nightly", dag_run_id=run["dag_run_id"],
                 task_ids=["nope"])
    assert unknown.value.status == 400


def test_a_dag_with_no_job_cannot_be_triggered(world):
    (world / "workspace" / "dags" / "only_wait.py").write_text(
        "from airflow import DAG\nfrom airflow.sensors.time_delta import TimeDeltaSensor\n"
        "from datetime import timedelta\n"
        "with DAG('only_wait', schedule=None) as dag:\n"
        "    w = TimeDeltaSensor(task_id='wait', delta=timedelta(minutes=1))\n", "utf-8")
    with pytest.raises(av.AirflowError) as refused:
        av.trigger(world, "only_wait")
    assert refused.value.status == 400 and "no task the world's scheduler runs" in str(
        refused.value)
    assert "queue" not in state(world)


def test_queue_is_invisible_until_used(world, tmp_path):
    """No queue: advance leaves no `queue` key and lands what it always did -
    the same bytes as the world advanced with the drain cut out."""
    twin = tmp_path / "twin"
    shutil.copytree(world, twin)
    scheduler.advance(world, 30)
    original = scheduler._drain
    scheduler._drain = lambda *a, **k: (_ for _ in ()).throw(AssertionError("drained"))
    try:
        scheduler.advance(twin, 30)
    finally:
        scheduler._drain = original
    assert "queue" not in state(world)
    assert (world / scheduler.STATE).read_bytes() == (twin / scheduler.STATE).read_bytes()


def test_queue_drains_path_independently(world, tmp_path):
    av.trigger(world, "commerce_nightly")
    twin = tmp_path / "twin"
    shutil.copytree(world, twin)
    for _ in range(4):
        scheduler.advance(world, 6)
    scheduler.advance(twin, 24)
    assert (world / scheduler.STATE).read_bytes() == (twin / scheduler.STATE).read_bytes()


def test_a_queued_run_in_a_world_with_no_live_block_still_resolves(tmp_path):
    root = build(tmp_path / "w", live=False)
    av.trigger(root, "bi_refresh")
    out = scheduler.advance(root, 1)
    assert out["live"] is False and [e["name"] for e in out["events"]] == ["bi_refresh"]
    last = state(root)["runs"][-1]
    assert last["status"] == "failed" and last["executed"] is False   # nothing to run
    assert "queue" not in state(root)


def test_enqueue_refuses_an_unknown_job(world):
    before = (world / scheduler.STATE).read_bytes()
    with pytest.raises(SystemExit, match="unknown job"):
        scheduler.enqueue(world, "nope")
    assert (world / scheduler.STATE).read_bytes() == before


def test_world_scheduler_enqueue_is_the_one_write(world):
    from world.gateway_scheduler import WorldScheduler

    ws = WorldScheduler(world)
    before = copy.deepcopy(state(world))
    out = ws.enqueue("dbt_build", "2026-03-01", None, {"dag_id": "x", "dag_run_id": "y"})
    assert out["job_id"] == "dbt_build" and out["position"] == 1
    after = state(world)
    assert {k: v for k, v in after.items() if k != "queue"} == before
    with pytest.raises(ValueError, match="Unknown job"):
        ws.enqueue("nope", None)
    with pytest.raises(ValueError, match="ISO"):
        ws.enqueue("dbt_build", "yesterday")


# ------------------------------------------------------------------ shapes
def test_shapes_carry_airflow_33_required_keys(world):
    dag = av.dag(world, "commerce_nightly")
    shape = av.dag_shape(world, dag, "/world/workspace/dags/commerce_nightly.py")
    assert shape["timetable_description"] == "At 02:30" and shape["is_paused"] is False
    assert shape["next_dagrun_run_after"] is not None
    assert {"timetable_partitioned", "timetable_periodic", "is_backfillable",
            "file_token", "allowed_run_types"} <= set(shape)
    details = av.dag_details_shape(world, dag, "/x")
    assert details["catchup"] is False and details["default_args"]["retries"] == 1
    task = av.task_shape(dag, dag.task("daily_load"))
    assert task["downstream_task_ids"] == ["dbt_build"]
    assert task["retry_delay"] == {"__type": "TimeDelta", "days": 0, "seconds": 600,
                                   "microseconds": 0}
    assert av.timetable_description("@daily") == "At 00:00"
    assert av.next_run_after("0 1 * * *", "2026-03-02T00:30:00Z") == "2026-03-02T01:00:00Z"
    assert av.next_run_after("0 1 * * *", "2026-03-02T01:00:00Z") == "2026-03-03T01:00:00Z"
    assert av.next_run_after("0 1 * * MON", "2026-03-02T00:30:00Z") is None


def test_ids_are_deterministic(world, tmp_path):
    twin = tmp_path / "twin"
    shutil.copytree(world, twin)
    a, b = av.trigger(world, "commerce_nightly"), av.trigger(twin, "commerce_nightly")
    assert a == b
    run = a["dag_run_id"]
    assert av.task_instances(world, "commerce_nightly", run) == av.task_instances(
        twin, "commerce_nightly", run)


# ---------------------------------------------------------------- discovery
def test_a_box_that_plugs_airflow_names_one_scheduler(tmp_path):
    """The DISCOVERY row replaces the static scheduler group: one address (8100)
    in .envrc, services.toml and onboarding, and the onboarding row says the
    `airflow` CLI is not there and why."""
    import tomllib

    from world import native_workspace as nw

    slots = ["files", "warehouse", "transform", "orchestrator", "human"]
    plugs = {"orchestrator": ["airflow"], "warehouse": ["duckdb"]}
    env = nw.environment(slots, plugs)
    assert env["AIRFLOW_API_URL"] == "http://airflow.internal:8100/api/v2"
    envrc = nw.envrc_block(slots, plugs)
    assert envrc.count("AIRFLOW_API_URL") == 1 and "airflow.internal:8080" not in envrc
    toml = tomllib.loads(nw.services_toml("/world", slots, plugs))
    assert "scheduler" not in toml
    assert toml["airflow"]["api"] == "http://airflow.internal:8100/api/v2"
    assert toml["airflow"]["port"] == 8100
    page = nw.onboarding_markdown(tmp_path, {"jobs": {"dbt_build": {"command": "x"}}},
                                  slots, plugs)
    row = next(line for line in page.splitlines() if line.startswith("| the scheduler"))
    assert "`airflow.internal:8100`" in row and "no `airflow` CLI" in row
    assert "metadata database" in row and "airflowctl" in row
    assert "## The pipeline" in page                     # the jobs are still listed
    # an ops_sched box keeps the static group, unchanged
    ops = nw.environment(slots, {"orchestrator": ["ops_sched"], "warehouse": ["duckdb"]})
    assert ops["AIRFLOW_API_URL"] == "http://airflow.internal:8080/api/v2"
