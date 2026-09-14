import json

from alkera_gateway.audit import read_rows
from alkera_gateway.records import operation_record


def assert_ok(result):
    assert result["ok"], result["error"]
    return result["result"]


def test_list_and_get_connectors(gateway):
    connectors = assert_ok(gateway.call("ingest_list", {}))["connectors"]
    assert [c["id"] for c in connectors] == ["acme_orders", "globex_returns"]
    acme = connectors[0]
    assert acme["status"] == "active"
    assert acme["last_run"] == {
        "id": "sync-000003",
        "status": "success",
        "finished_at": "2026-03-05T05:31:45Z",
    }
    # The seeded sync template is mock plumbing, never agent-visible.
    assert "sync" not in acme

    globex = assert_ok(gateway.call("ingest_get", {"connector_id": "globex_returns"}))
    assert globex["source"] == {"type": "sftp", "location": "data/drop/globex"}
    assert globex["status"] == "paused"
    assert globex["last_run"]["status"] == "failed"

    result = gateway.call("ingest_get", {"connector_id": "initech"})
    assert not result["ok"]
    assert result["error"]["code"] == "connector_not_found"


def test_runs_are_paged_newest_first(gateway):
    page = assert_ok(gateway.call("ingest_runs", {"limit": 2}))
    assert [run["id"] for run in page["runs"]] == ["sync-000003", "sync-000002"]
    assert page["next_token"] == "2"

    rest = assert_ok(
        gateway.call("ingest_runs", {"limit": 2, "continuation_token": page["next_token"]})
    )
    assert [run["id"] for run in rest["runs"]] == ["sync-000001"]
    assert rest["next_token"] is None

    acme = assert_ok(gateway.call("ingest_runs", {"connector_id": "acme_orders"}))
    assert [run["id"] for run in acme["runs"]] == ["sync-000003", "sync-000001"]

    bad = gateway.call("ingest_runs", {"continuation_token": "abc"})
    assert not bad["ok"]
    assert bad["error"]["code"] == "invalid_token"


def test_run_detail_has_timeline_and_table_counts(gateway):
    run = assert_ok(gateway.call("ingest_run", {"run_id": "sync-000002"}))
    assert run["status"] == "failed"
    assert run["tables"] == {"raw.vendor_returns": {"rows_inserted": 0, "rows_updated": 0}}
    assert [event["event"] for event in run["timeline"]] == ["started", "failed"]

    result = gateway.call("ingest_run", {"run_id": "sync-999999"})
    assert not result["ok"]
    assert result["error"]["code"] == "run_not_found"


def test_trigger_advances_on_the_virtual_clock(gateway, world):
    # vnow is 2026-03-05T09:00:00Z; the seeded template runs for 45s.
    run = assert_ok(gateway.call("ingest_trigger", {"connector_id": "acme_orders"}))
    assert run["id"] == "sync-000004"
    assert run["started_at"] == "2026-03-05T09:00:00Z"
    assert run["finished_at"] == "2026-03-05T09:00:45Z"
    assert run["status"] == "running"
    assert [event["event"] for event in run["timeline"]] == ["started"]

    # Advance the world's virtual clock; the run completes without any new call.
    state_path = world / "var" / "sched" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["vnow"] = "2026-03-05T09:01:00Z"
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

    finished = assert_ok(gateway.call("ingest_run", {"run_id": "sync-000004"}))
    assert finished["status"] == "success"
    assert [event["event"] for event in finished["timeline"]] == ["started", "succeeded"]
    assert finished["tables"]["raw.vendor_orders"]["rows_inserted"] == 50


def test_trigger_refuses_paused_connector(gateway):
    result = gateway.call("ingest_trigger", {"connector_id": "globex_returns"})
    assert not result["ok"]
    assert result["error"]["code"] == "connector_paused"


def test_schema_config_read_and_update(gateway):
    schema = assert_ok(gateway.call("ingest_schema", {"connector_id": "acme_orders"}))
    entry = schema["tables"][0]
    assert entry["columns"] == ["order_id", "order_date", "customer_id", "amount"]
    assert entry["column_added"] == "propagate"

    updated = assert_ok(
        gateway.call(
            "ingest_schema_update",
            {"connector_id": "acme_orders", "table": "raw.vendor_orders",
             "column_added": "alert", "column_removed": None},
        )
    )
    assert updated["column_added"] == "alert"
    assert updated["column_removed"] == "ignore"

    reread = assert_ok(gateway.call("ingest_schema", {"connector_id": "acme_orders"}))
    assert reread["tables"][0]["column_added"] == "alert"

    result = gateway.call(
        "ingest_schema_update",
        {"connector_id": "acme_orders", "table": "raw.vendor_orders"},
    )
    assert not result["ok"]
    assert result["error"]["code"] == "invalid_policy"

    result = gateway.call(
        "ingest_schema_update",
        {"connector_id": "acme_orders", "table": "raw.missing", "column_added": "ignore"},
    )
    assert not result["ok"]
    assert result["error"]["code"] == "table_not_found"

    result = gateway.call(
        "ingest_schema_update",
        {"connector_id": "acme_orders", "table": "raw.vendor_orders",
         "column_added": "explode"},
    )
    assert not result["ok"]
    assert result["error"]["code"] == "invalid_arguments"


def test_alerts(gateway):
    alerts = assert_ok(gateway.call("ingest_alerts", {}))["alerts"]
    assert [a["id"] for a in alerts] == ["alert-000001"]
    assert alerts[0]["kind"] == "sync_failed"

    unacked = assert_ok(gateway.call("ingest_alerts", {"acknowledged": False}))["alerts"]
    assert len(unacked) == 1
    assert assert_ok(gateway.call("ingest_alerts", {"acknowledged": True}))["alerts"] == []
    assert (
        assert_ok(gateway.call("ingest_alerts", {"connector_id": "acme_orders"}))["alerts"] == []
    )


def test_action_log_rows_match_existing_shape(gateway):
    gateway.call("ingest_list", {})
    gateway.call("ingest_trigger", {"connector_id": "acme_orders"})
    gateway.call(
        "ingest_schema_update",
        {"connector_id": "acme_orders", "table": "raw.vendor_orders",
         "column_added": "ignore"},
    )
    rows = read_rows(gateway.audit.actions)
    keys = set(operation_record(rows[0]))
    for row in rows:
        assert set(operation_record(row)) == keys
    assert rows[0]["op_class"] == "read"
    assert rows[0]["targets"] == ["ingest"]
    assert rows[1]["op_class"] == "exec"
    assert rows[1]["targets"] == ["ingest:acme_orders"]
    assert rows[2]["op_class"] == "write"


def test_green_status_while_rows_missing_is_representable(gateway, world):
    """The whole point of the build: a green sync whose counts disagree with
    the warehouse must be expressible and observable."""
    state_path = world / "var" / "ingest" / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["runs"][-1]["tables"]["raw.vendor_orders"]["rows_inserted"] = 9999
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

    run = assert_ok(gateway.call("ingest_run", {"run_id": "sync-000003"}))
    assert run["status"] == "success"
    assert run["tables"]["raw.vendor_orders"]["rows_inserted"] == 9999
