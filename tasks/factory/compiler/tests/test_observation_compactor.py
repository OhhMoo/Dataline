"""Synthetic and live checks for loss-mask-preserving observation compaction."""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path

import pytest

from tasks.factory.export_rl_dataset import pair_tool_results, token_loss_mask_rle
from tasks.factory.observation_compactor import compact_jsonl_file, compact_trajectory_messages


LIVE_SFT = Path("/home/ubuntu/alkera_factory_1017/rl_dataset_v1/train_sft.jsonl")
MAX_TOKENS = 24_576


def _tool_call(call_id: str, name: str, arguments: dict | None = None) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments or {})},
    }


def _assistant_call(call_id: str, name: str, arguments: dict | None = None, **extra) -> dict:
    call = _tool_call(call_id, name, arguments)
    reasoning = "Keep this reasoning byte-for-byte.\n"
    return {
        "role": "assistant",
        "loss_mask": 1,
        "causal_phase": "discovery",
        "content": "",
        "reasoning_content": reasoning,
        "tool_calls": [call],
        "approx_tokens": max(1, math.ceil((len(reasoning) + len(call["function"]["arguments"])) / 4)),
        **extra,
    }


def _tool_result(call_id: str, content: str, **extra) -> dict:
    return {
        "role": "tool",
        "loss_mask": 0,
        "causal_phase": "discovery",
        "tool_call_id": call_id,
        "content": content,
        "approx_tokens": math.ceil(len(content) / 4),
        **extra,
    }


def _tool_call_ids(messages: list[dict]) -> set[str]:
    return {
        call["id"]
        for message in messages
        if message.get("role") == "assistant"
        for call in message.get("tool_calls", [])
        if call.get("id")
    }


def _full_schema_names(value) -> set[str]:
    """Find named tool entries that still carry their input schema after compaction."""
    found: set[str] = set()
    if isinstance(value, dict):
        schema_name = value.get("tool", value.get("name"))
        if schema_name and any(key in value for key in ("inputSchema", "input_schema", "parameters")):
            found.add(str(schema_name))
        for child in value.values():
            found |= _full_schema_names(child)
    elif isinstance(value, list):
        for child in value:
            found |= _full_schema_names(child)
    return found


def _contains_field(value, key: str, expected) -> bool:
    if isinstance(value, dict):
        return value.get(key) == expected or any(_contains_field(child, key, expected) for child in value.values())
    if isinstance(value, list):
        return any(_contains_field(child, key, expected) for child in value)
    return False


def _assert_no_orphan_tool_results(messages: list[dict]) -> None:
    _, orphan_ids = pair_tool_results(messages)
    assert orphan_ids == []
    call_ids = _tool_call_ids(messages)
    assert all(
        message.get("tool_call_id") in call_ids
        for message in messages
        if message.get("role") == "tool"
    )


def _find_final_report_assistant(messages: list[dict]) -> dict | None:
    for message in messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls", []):
            function = call.get("function", {})
            if function.get("name") != "fs_write":
                continue
            try:
                arguments = json.loads(function.get("arguments", "{}"))
            except (TypeError, json.JSONDecodeError):
                continue
            if Path(arguments.get("path", "")).name == "final_report.json":
                return message
    return None


def test_compact_trajectory_preserves_assistant_and_pairs_tool_messages() -> None:
    schema_dump = {
        "mode": "search",
        "namespace": "alkera",
        "namespaceStatus": "ready",
        "tools": [
            {"tool": "sql_query", "description": "Inspect warehouse. " + ("schema detail " * 1500), "inputSchema": {"type": "object", "properties": {"sql": {"type": "string"}}}},
            {"tool": "fs_write", "description": "Write a file", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
            {"tool": "unused_admin_tool", "description": "Uninvoked tool schema", "inputSchema": {"type": "object", "properties": {"secret": {"type": "string"}}}},
        ],
    }
    schema_text = json.dumps(schema_dump, indent=2)
    report = {
        "decision": {"ticket": "OPS-42", "approved_by": "fkim"},
        "audit": {
            "passed": True, "row_count": 7, "business_day": "2026-10-24",
            "details": ["long audit detail " * 30 for _ in range(30)],
        },
    }
    messages = [
        {"role": "user", "loss_mask": 0, "content": "Inspect and report.", "approx_tokens": 5},
        _assistant_call("tool-1", "ToolSearch", {"server": "alkera"}),
        _tool_result("tool-1", schema_text),
        _assistant_call("tool-2", "sql_query", {"sql": "select 1"}),
        _tool_result("tool-2", json.dumps(report, indent=2), causal_phase="verification_and_execution"),
        {
            "role": "assistant", "loss_mask": 1, "causal_phase": "governance_and_reporting",
            "content": "", "reasoning_content": "Final report reasoning.",
            "tool_calls": [_tool_call("report-call", "fs_write", {"path": "/workspace/final_report.json", "content": json.dumps(report)})],
            "approx_tokens": max(1, math.ceil((len("Final report reasoning.") + len(json.dumps({"path": "/workspace/final_report.json", "content": json.dumps(report)}))) / 4)),
        },
        _tool_result("report-call", "written", causal_phase="governance_and_reporting"),
    ]
    assistant_before = [copy.deepcopy(m) for m in messages if m.get("role") == "assistant"]

    result = compact_trajectory_messages(messages, max_seq_tokens=200, target_obs_chars=180)

    assert [m for m in result["messages"] if m.get("role") == "assistant"] == assistant_before
    assert result["assistant_tokens_before"] == result["assistant_tokens_after"]
    assert result["compacted_tool_turns"] >= 1
    _assert_no_orphan_tool_results(result["messages"])
    # ToolSearch/schema compaction and general observation compaction must keep JSON valid when
    # the source observation is JSON. Summary fields are retained to support audit review.
    compacted_schema = next(m["content"] for m in result["messages"] if m.get("tool_call_id") == "tool-1" and m.get("causal_phase") == "discovery")
    parsed_schema = json.loads(compacted_schema)
    assert parsed_schema["namespace"] == "alkera"
    assert _full_schema_names(parsed_schema) <= {"sql_query", "fs_write"}
    retained_schemas = parsed_schema.get("tools", [])
    if retained_schemas:
        assert any(tool.get("tool", tool.get("name")) == "sql_query" for tool in retained_schemas)
        assert any(tool.get("tool", tool.get("name")) == "fs_write" for tool in retained_schemas)
        assert not any(tool.get("tool", tool.get("name")) == "unused_admin_tool" for tool in retained_schemas)
    compacted_audit = next(m["content"] for m in result["messages"] if m.get("causal_phase") == "verification_and_execution")
    parsed_audit = json.loads(compacted_audit)
    assert len(compacted_audit) < len(json.dumps(report, indent=2))
    assert _contains_field(parsed_audit, "ticket", "OPS-42")
    assert _contains_field(parsed_audit, "approved_by", "fkim")
    assert _contains_field(parsed_audit, "passed", True)
    assert _contains_field(parsed_audit, "row_count", 7)
    assert _contains_field(parsed_audit, "business_day", "2026-10-24")


def test_compact_jsonl_updates_masks_and_preserves_assistant_turns(tmp_path: Path) -> None:
    report_args = {"path": "/workspace/final_report.json", "content": "{}"}
    messages = [
        {"role": "user", "loss_mask": 0, "content": "prompt", "approx_tokens": 2},
        _assistant_call("big", "sql_query", {"sql": "select *"}),
        _tool_result("big", "row_count=999\n" + ("very long observation line\n" * 700)),
        {
            "role": "assistant", "loss_mask": 1, "causal_phase": "governance_and_reporting",
            "content": "", "reasoning_content": "Done.",
            "tool_calls": [_tool_call("report", "fs_write", report_args)],
            "approx_tokens": max(1, math.ceil((len("Done.") + len(json.dumps(report_args))) / 4)),
        },
        _tool_result("report", "ok", causal_phase="governance_and_reporting"),
    ]
    source = tmp_path / "input.jsonl"
    destination = tmp_path / "output.jsonl"
    row = {
        "id": "synthetic-1", "task_id": "FT-test",
        "loss_mask": [message["loss_mask"] for message in messages],
        "messages": messages, "token_loss_mask_rle": [[0, 2], [1, 12], [0, 100]],
    }
    source.write_text(json.dumps(row) + "\n", encoding="utf-8")
    expected_assistant = [copy.deepcopy(m) for m in messages if m.get("role") == "assistant"]

    stats = compact_jsonl_file(source, destination, max_seq_tokens=2_000)

    compacted_row = json.loads(destination.read_text(encoding="utf-8").splitlines()[0])
    assert [m for m in compacted_row["messages"] if m.get("role") == "assistant"] == expected_assistant
    assert compacted_row["token_loss_mask_rle"] == token_loss_mask_rle(compacted_row["messages"])
    assert sum(length for _, length in compacted_row["token_loss_mask_rle"]) == sum(m["approx_tokens"] for m in compacted_row["messages"])
    assert isinstance(stats, dict)


@pytest.mark.skipif(not LIVE_SFT.is_file(), reason="Live rl_dataset_v1 dataset is not available")
def test_live_130_row_sft_compaction_retains_supervision_and_fits_24k(tmp_path: Path) -> None:
    original_rows = [json.loads(line) for line in LIVE_SFT.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(original_rows) == 130
    expected_assistants = {
        row["id"]: [copy.deepcopy(message) for message in row["messages"] if message.get("role") == "assistant"]
        for row in original_rows
    }
    expected_error_flags = {
        row["id"]: [message.get("is_error") for message in row["messages"] if message.get("role") == "tool"]
        for row in original_rows
    }
    expected_tool_metadata = {
        row["id"]: [
            (message.get("tool_call_id"), message.get("is_error"), message.get("causal_phase"), message.get("loss_mask"))
            for message in row["messages"] if message.get("role") == "tool"
        ]
        for row in original_rows
    }
    expected_report_calls = {
        row["id"]: _find_final_report_assistant(row["messages"])
        for row in original_rows
    }
    assert sum(call is not None for call in expected_report_calls.values()) == 130
    final_assistant_phases = [
        next(message for message in reversed(row["messages"]) if message.get("role") == "assistant").get("causal_phase")
        for row in original_rows
    ]
    assert final_assistant_phases.count("governance_and_reporting") == 30
    output = tmp_path / "train_sft_compacted.jsonl"

    stats = compact_jsonl_file(LIVE_SFT, output, max_seq_tokens=MAX_TOKENS)

    compacted_rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(compacted_rows) == 130
    for row in compacted_rows:
        assert [m for m in row["messages"] if m.get("role") == "assistant"] == expected_assistants[row["id"]]
        assert [m.get("is_error") for m in row["messages"] if m.get("role") == "tool"] == expected_error_flags[row["id"]]
        assert [
            (m.get("tool_call_id"), m.get("is_error"), m.get("causal_phase"), m.get("loss_mask"))
            for m in row["messages"] if m.get("role") == "tool"
        ] == expected_tool_metadata[row["id"]]
        assert row["loss_mask"] == [m["loss_mask"] for m in row["messages"]]
        assert row["token_loss_mask_rle"] == token_loss_mask_rle(row["messages"])
        assert sum(length for _, length in row["token_loss_mask_rle"]) == sum(m["approx_tokens"] for m in row["messages"])
        _assert_no_orphan_tool_results(row["messages"])
        original_report_call = expected_report_calls[row["id"]]
        if original_report_call is not None:
            report_call = _find_final_report_assistant(row["messages"])
            assert report_call == original_report_call
            assert report_call["causal_phase"] == "governance_and_reporting"
        assert sum(message["approx_tokens"] for message in row["messages"]) <= MAX_TOKENS
    assert isinstance(stats, dict)
    assert stats["rows"] == 130
    assert stats["rows_fitting"] == 130
    assert stats["fits_max_seq_tokens"] is True
