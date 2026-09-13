from alkera_gateway.audit import read_rows


def assert_ok(result):
    assert result["ok"], result["error"]
    return result["result"]


def test_all_tools(gateway):
    query = assert_ok(
        gateway.call("sql_query", {"sql": "select * from warehouse.items order by id"})
    )
    assert query["rows"] == [
        [1, {"type": "decimal", "value": "10.50"}],
        [2, {"type": "decimal", "value": "20.25"}],
    ]

    assert_ok(gateway.call("sql_execute", {"sql": "insert into warehouse.items values (3, 2.00)"}))
    assert_ok(gateway.call("fs_read", {"path": "README.md"}))
    assert_ok(gateway.call("fs_write", {"path": "README.md", "content": "# Changed\n"}))
    assert_ok(gateway.call("fs_list", {"path": ".", "recursive": False}))
    assert (
        "idempotent"
        in assert_ok(gateway.call("docs_search", {"query": "idempotent"}))["matches"][0]["text"]
    )

    assert_ok(gateway.call("git_log", {"limit": 5}))
    assert "README.md" in assert_ok(gateway.call("git_diff", {}))["output"]
    assert_ok(gateway.call("git_commit", {"message": "update fixture", "paths": ["README.md"]}))

    dashboard = assert_ok(gateway.call("bi_render", {"dashboard": "item_totals"}))
    assert dashboard["panels"]["summary"]["rows"][0][0] == 3

    jobs = assert_ok(gateway.call("sched_list", {}))
    assert jobs["jobs"][0]["id"] == "daily_load"
    assert assert_ok(gateway.call("sched_state", {}))["vnow"] == "2026-03-05T09:00:00Z"
    assert assert_ok(gateway.call("sched_logs", {}))["runs"] == []
    run = assert_ok(
        gateway.call(
            "sched_trigger",
            {"job_id": "daily_load", "execution_date": "2026-03-05", "variables": {}},
        )
    )
    assert assert_ok(gateway.call("sched_state", {"run_id": run["id"]}))["status"] == "success"
    assert "intent" in assert_ok(gateway.call("sched_logs", {"run_id": run["id"]}))["result"]

    assert_ok(gateway.call("dbt_compile", {"selector": "sample", "variables": {}}))
    assert_ok(
        gateway.call("dbt_run", {"selector": "sample", "variables": {}, "full_refresh": False})
    )
    assert_ok(gateway.call("dbt_test", {"selector": "sample", "variables": {}}))


def test_sql_flags_destructive_operations(gateway):
    assert_ok(gateway.call("sql_execute", {"sql": "delete from warehouse.items"}))
    row = read_rows(gateway.audit.actions)[0]
    assert row["op_class"] == "destructive"
    assert "unbounded_delete" in row["flags"]
    assert row["rows_affected"] == 2


def test_sql_execute_runs_multiple_statements_atomically(gateway):
    result = assert_ok(
        gateway.call(
            "sql_execute",
            {
                "sql": "delete from warehouse.items where id=1; "
                "insert into warehouse.items values (3, 5.00)"
            },
        )
    )
    assert len(result["statements"]) == 2
    rows = assert_ok(
        gateway.call("sql_query", {"sql": "select id from warehouse.items order by id"})
    )["rows"]
    assert rows == [[2], [3]]

    failed = gateway.call(
        "sql_execute",
        {
            "sql": "update warehouse.items set amount=99 where id=2; "
            "insert into warehouse.missing values (1)"
        },
    )
    assert not failed["ok"]
    amount = assert_ok(
        gateway.call("sql_query", {"sql": "select amount from warehouse.items where id=2"})
    )["rows"][0][0]
    assert amount == {"type": "decimal", "value": "20.25"}


def test_paths_and_external_database_access_are_denied(gateway, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("hidden", encoding="utf-8")
    result = gateway.call("fs_read", {"path": "../secret.txt"})
    assert not result["ok"]
    assert result["error"]["code"] == "path_denied"

    result = gateway.call("sql_query", {"sql": f"select * from read_text('{secret.as_posix()}')"})
    assert not result["ok"]
    assert "external" in result["error"]["message"].lower()

    result = gateway.call("sql_execute", {"sql": "attach '/grading/expected.duckdb' as expected"})
    assert not result["ok"]
    assert result["error"]["code"] == "unsupported_sql"


def test_invalid_arguments_are_logged(gateway):
    result = gateway.call("sql_query", {"sql": "select 1", "extra": True})
    assert not result["ok"]
    rows = read_rows(gateway.audit.actions)
    assert rows[0]["exit"] == "error"
    assert rows[0]["error"]["code"] == "invalid_arguments"
