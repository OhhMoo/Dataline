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


def test_non_utf8_files_are_readable_and_diagnosable(gateway, tmp_path):
    """Real data work hits files that are not UTF-8.

    T3 (R002) found the toolbelt could not represent this at all: fs_read ended in
    a bare content.decode("utf-8"), so an EBCDIC mainframe extract or a cp1252
    export raised an unhandled UnicodeDecodeError. Four planned environment themes
    exist specifically to plant encoding defects, and the tool surface was
    sanitizing away the very defect they plant.
    """
    workspace = gateway.config.workspace

    # A cp037 (EBCDIC) extract, as a mainframe would hand it over.
    (workspace / "extract.dat").write_bytes("CUSTOMER 00417".encode("cp037"))
    # A cp1252 export with a character that is NOT valid UTF-8 on its own.
    (workspace / "export.csv").write_bytes("name,city\nJos\xe9,M\xfcnchen\n".encode("cp1252"))

    # 1. Strict UTF-8 read fails LOUDLY and says where, rather than silently
    #    substituting U+FFFD - a replacement char would hand the agent
    #    plausible-looking wrong data and turn a findable defect into an
    #    unfindable one.
    bad = gateway.call("fs_read", {"path": "export.csv"})
    assert bad["ok"] is False
    assert bad["error"]["code"] == "decode_error"
    assert "offset" in bad["error"]["message"]

    # 2. Naming the right codec recovers the data.
    good = assert_ok(gateway.call("fs_read", {"path": "export.csv", "encoding": "cp1252"}))
    assert "José" in good["content"] and "München" in good["content"]
    assert good["encoding"] == "cp1252"

    ebcdic = assert_ok(gateway.call("fs_read", {"path": "extract.dat", "encoding": "cp037"}))
    assert ebcdic["content"] == "CUSTOMER 00417"

    # 3. Round-trip: an agent that diagnoses the encoding can write it back.
    assert_ok(gateway.call("fs_write", {"path": "out.csv", "content": "Jos\xe9\n",
                                        "encoding": "cp1252"}))
    assert (workspace / "out.csv").read_bytes() == "Jos\xe9\n".encode("cp1252")

    # 4. A nonsense codec is a clean error, not a traceback.
    assert gateway.call("fs_read", {"path": "extract.dat", "encoding": "cp9999"})["error"][
        "code"
    ] == "unknown_encoding"
