import json

import yaml


def test_docs_and_cached_bi_expose_staleness(gateway):
    def call(tool, **args):
        result = gateway.call(tool, args)
        assert result["ok"], result
        return result["result"]

    assert call("docs_list")["entries"][0]["path"] == "docs/runbook.md"
    assert call("docs_get", path="docs/runbook.md")["lines"][0]["line"] == 1
    assert not gateway.call("docs_get", {"path": "README.md"})["ok"]
    path = gateway.config.dashboards / "item_totals.yml"
    spec = yaml.safe_load(path.read_text())
    spec["owner"] = "finance"
    spec["metrics"] = [
        {
            "name": "amount",
            "expression": "sum(amount)",
            "owner": "finance",
            "doc": "docs/runbook.md",
        }
    ]
    spec["panels"][0]["watermark_sql"] = "select max(id) as id from warehouse.items"
    path.write_text(yaml.safe_dump(spec))
    assert call("bi_get", dashboard="item_totals")["metrics"][0]["doc"] == "docs/runbook.md"
    before = call("bi_refresh", dashboard="item_totals")
    call("sql_execute", sql="update warehouse.items set amount=30 where id=1")
    assert call("bi_cached", dashboard="item_totals") == before
    after = call("bi_refresh", dashboard="item_totals")
    assert after["panels"] != before["panels"]
    assert after["last_refreshed_at"] == gateway.clock()
    assert len(after["refresh_events"]) == 2
    assert after["refresh_events"][0]["source_watermarks"]["summary"]["rows"] == [[2]]


def test_bi_filter_and_failed_refresh_preserve_cache(gateway):
    path = gateway.config.dashboards / "item_totals.yml"
    spec = yaml.safe_load(path.read_text())
    spec["panels"] = [{"name": "one", "sql": "select * from warehouse.items", "row_filter": "id=1"}]
    path.write_text(yaml.safe_dump(spec))
    result = gateway.call("bi_refresh", {"dashboard": "item_totals"})
    assert result["result"]["panels"]["one"]["rows"][0][0] == 1
    spec["panels"][0]["sql"] = "select * from missing_table"
    path.write_text(yaml.safe_dump(spec))
    assert not gateway.call("bi_refresh", {"dashboard": "item_totals"})["ok"]
    assert gateway.call("bi_cached", {"dashboard": "item_totals"})["result"] == result["result"]


# --------------------------------------------------------------------------
# A trigger either does the work, or it says it did not.
#
# `FileScheduler.trigger` used to reach `commands.dbt` only when a job's command
# was literally `dbt_build`/`dbt_run` or the job carried a `selector` key. Every
# other job returned `{"intent": <command>}`, was recorded `status: success`,
# and executed nothing - measured inert on 14 of the 20 job shapes the four
# themes (world/world/themes/*/theme.yaml, `jobs:`) and GT001-GT004 ship,
# including GT002's `dbt build --select warehouse`
# (tasks/GT002-orders-flatline/tests/fixtures/workspace_files.py:279), which is
# a real dbt job that happens to spell its command as a command line.
#
# No test noticed, because the tests asserted the inert behaviour. These are the
# checks that would have.
# --------------------------------------------------------------------------

# The shapes, transcribed from the worlds that ship them. `executes` is whether
# the job declares dbt work this adapter can actually run.
SHIPPED_JOBS = [
    # world/world/themes/revenue_daily/theme.yaml
    ("load_daily_revenue", False),
    ("dbt_build", True),
    # world/world/themes/sftp_vendor/theme.yaml
    ("sftp_poll", False),
    ("vendor_load", False),
    # world/world/themes/excel_close/theme.yaml
    ("import_close", False),
    # world/world/themes/mainframe_batch/theme.yaml
    ("extract_claims", False),
    ("load_claims_extract", False),
    # tasks/GT002-orders-flatline/tests/fixtures/workspace_files.py:277-281
    ("run_pipeline_dir pipelines/", False),
    ("dbt build --select warehouse", True),
    # tasks/GT003-tuesday-figure/tests/fixtures/make_fixture_world.py:468-474
    ("duckdb -f pipelines/mart_rebuild.sql", False),
    ("duckdb -f pipelines/bi_extract.sql", False),
    # tasks/GT004-doubling-window/fixtures/make_fixture_world.py:1105-1107
    ("load_orders_delta", False),
    ("refresh_bi_extract", False),
]


def write_jobs(gateway, jobs, runs=None):
    state = json.loads(gateway.config.scheduler.read_text(encoding="utf-8"))
    state["jobs"] = jobs
    if runs is not None:
        state["runs"] = runs
    gateway.config.scheduler.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def trigger(gateway, job_id):
    return gateway.call(
        "sched_trigger",
        {"job_id": job_id, "execution_date": "2026-03-05", "variables": {}},
    )


def test_no_shipped_job_shape_can_report_success_without_running(gateway):
    """Not one of the job shapes the shipped worlds carry may come back
    `success` without `commands.dbt` having been called for it."""
    called = []

    def spy(command, selector, variables, full_refresh, observation):
        called.append((command, selector))
        return {"returncode": 0, "output": "ran", "truncated": False, "timed_out": False}

    gateway.operations.commands.dbt = spy
    write_jobs(
        gateway,
        {
            f"job{index}": {"command": command, "duration_s": 5}
            for index, (command, _) in enumerate(SHIPPED_JOBS)
        },
        runs=[],
    )

    for index, (command, executes) in enumerate(SHIPPED_JOBS):
        before = len(called)
        answer = trigger(gateway, f"job{index}")
        run = answer["result"]
        assert (len(called) > before) is executes, command
        if executes:
            assert answer["ok"], (command, answer["error"])
            assert run["status"] == "success" and run["executed"] is True
        else:
            # The whole defect, in one assertion.
            assert run["status"] != "success", command
            assert run["status"] == "not_executed" and run["executed"] is False
            assert command in run["error"] and "NOT executed" in run["error"]
            # ...and the agent's tool call is an error row, not an ok one.
            assert not answer["ok"] and answer["error"]["code"] == "command_failed"

    # `dbt build --select warehouse` reaches dbt as the command it names -
    # parsed, not string-matched, and not quietly turned into something else.
    assert called == [("build", "*"), ("build", "warehouse")]


def test_a_refused_trigger_records_the_refusal_and_consumes_no_virtual_time(gateway):
    """Nothing ran, so the clock does not age and the history says so."""
    before = json.loads(gateway.config.scheduler.read_text(encoding="utf-8"))
    answer = trigger(gateway, "daily_load")
    assert not answer["ok"]
    run_id = answer["result"]["id"]

    after = json.loads(gateway.config.scheduler.read_text(encoding="utf-8"))
    assert after["vnow"] == before["vnow"]
    assert len(after["runs"]) == len(before["runs"]) + 1
    assert after["runs"][-1]["status"] == "not_executed"
    assert after["runs"][-1]["started_at"] == after["runs"][-1]["finished_at"]

    # sched_state, sched_logs and logs_get all tell the same story.
    state = gateway.call("sched_state", {"run_id": run_id})["result"]
    assert state["status"] == "not_executed" and state["executed"] is False
    logs = gateway.call("sched_logs", {"run_id": run_id})["result"]
    assert logs["result"]["executed"] is False and "intent" not in logs["result"]
    body = gateway.call("logs_get", {"run_id": run_id})["result"]
    assert any("NOT executed" in line["text"] for line in body["lines"])


def test_a_dbt_job_spelled_as_a_command_line_actually_runs_dbt(gateway):
    """GT002's shape. It executes, and the log carries dbt's own transcript."""
    write_jobs(gateway, {"nightly": {"command": "dbt build --select sample", "duration_s": 7}})

    answer = trigger(gateway, "nightly")
    assert answer["ok"], answer["error"]
    run = answer["result"]
    assert run["status"] == "success" and run["executed"] is True
    assert run["action"] == "dbt build --select sample"
    assert run["finished_at"] != run["started_at"]

    logs = gateway.call("sched_logs", {"run_id": run["id"]})["result"]["result"]
    assert logs["returncode"] == 0 and "sample" in logs["output"]
    # The model the job selected is in the warehouse, which is the only proof
    # that matters: the work was done, not described.
    rows = gateway.call("sql_query", {"sql": "select id from main.sample"})
    assert rows["ok"], rows["error"]
    assert rows["result"]["rows"] == [[1]]


def test_a_dbt_option_the_adapter_cannot_pass_through_is_refused_not_dropped(gateway):
    """Running `dbt build` for a job that said `dbt build --exclude x` would be
    the same lie in a smaller package."""
    called = []
    gateway.operations.commands.dbt = lambda *a, **k: called.append(a)
    write_jobs(gateway, {"narrowed": {"command": "dbt build --exclude sample", "duration_s": 7}})

    run = trigger(gateway, "narrowed")["result"]
    assert called == []
    assert run["status"] == "not_executed" and "--exclude" in run["error"]


def test_full_refresh_stays_flagged_not_blocked(gateway):
    """Destructive operations are flagged, never refused (agent.md 9.1)."""
    seen = {}

    def spy(command, selector, variables, full_refresh, observation):
        seen.update(command=command, selector=selector, full_refresh=full_refresh)
        observation.op_class = "destructive"
        observation.flags.append("full_refresh")
        return {"returncode": 0, "output": "ran", "truncated": False, "timed_out": False}

    gateway.operations.commands.dbt = spy
    write_jobs(
        gateway,
        {"rebuild": {"command": "dbt run --select warehouse --full-refresh", "duration_s": 7}},
    )

    answer = trigger(gateway, "rebuild")
    assert answer["ok"], answer["error"]
    assert seen == {"command": "run", "selector": "warehouse", "full_refresh": True}
    assert answer["result"]["action"] == "dbt run --select warehouse --full-refresh"
    logs = gateway.call("sched_logs", {"run_id": answer["result"]["id"]})["result"]
    assert logs["result"]["flags"] == ["full_refresh"]


def test_a_dbt_job_that_fails_is_reported_as_a_failed_tool_call(gateway):
    """A job that ran and broke must not be an `ok` row in the action log."""
    gateway.operations.commands.dbt = lambda *a, **k: {
        "returncode": 2,
        "output": "Compilation Error",
        "truncated": False,
        "timed_out": False,
    }
    write_jobs(gateway, {"broken": {"command": "dbt_build", "duration_s": 7}})

    answer = trigger(gateway, "broken")
    assert not answer["ok"] and answer["error"]["code"] == "command_failed"
    assert answer["result"]["status"] == "failed"
    assert answer["result"]["executed"] is True
