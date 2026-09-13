import json

from conftest import clone_world, config, world_hash

from alkera_gateway import Gateway
from alkera_gateway.audit import read_rows, validate_trace
from alkera_gateway.records import operation_record
from alkera_gateway.replay import replay
from alkera_gateway.signals import process_signals


def test_transport_records_have_identical_operations(world, tmp_path):
    other = clone_world(world, tmp_path / "other")
    fingerprint = world_hash(world)
    left = Gateway(config(world, tmp_path / "mcp-audit", fingerprint))
    right = Gateway(config(other, tmp_path / "cli-audit", fingerprint))
    try:
        args = {"sql": "select sum(amount) from warehouse.items", "limit": 1000}
        left.call("sql_query", args, "mcp")
        right.call("sql_query", args, "cli")
    finally:
        left.close()
        right.close()
    left_row = read_rows(left.audit.actions)[0]
    right_row = read_rows(right.audit.actions)[0]
    assert operation_record(left_row) == operation_record(right_row)
    assert left_row["transport"] != right_row["transport"]


def test_replay_reconstructs_session(world, tmp_path):
    destination = clone_world(world, tmp_path / "destination")
    fingerprint = world_hash(world)
    source = Gateway(config(world, tmp_path / "source-audit", fingerprint))
    try:
        source.call("fs_write", {"path": "notes.txt", "content": "checked\n"})
        source.call("sql_execute", {"sql": "update warehouse.items set amount=11 where id=1"})
        source.call(
            "sched_trigger",
            {"job_id": "daily_load", "execution_date": "2026-03-05", "variables": {}},
        )
    finally:
        source.close()
    target = Gateway(config(destination, tmp_path / "replay-audit", fingerprint))
    try:
        result = replay(source.audit.directory, target)
    finally:
        target.close()
    assert result["responses_match"]
    assert result["calls"] == 3
    assert world_hash(world) == world_hash(destination)


def test_trace_validation_detects_tampering(gateway):
    gateway.call("sql_query", {"sql": "select 1"})
    row = read_rows(gateway.audit.actions)[0]
    row["result"] = {"changed": True}
    gateway.audit.actions.write_text(json.dumps(row) + "\n", encoding="utf-8")
    try:
        validate_trace(gateway.audit.actions)
    except ValueError as exc:
        assert "Result hash mismatch" in str(exc)
    else:
        raise AssertionError("Tampered trace passed validation")


def test_process_signals_are_advisory(gateway):
    for _ in range(3):
        gateway.call("sql_query", {"sql": "select * from warehouse.items"})
    gateway.call("sql_execute", {"sql": "delete from warehouse.items"})
    result = process_signals(read_rows(gateway.audit.actions))
    assert result["advisory_only"]
    assert result["false_positive_rate"] is None
    assert result["doom_loops"] == 1
    assert result["ungated_destructive"] == 0
