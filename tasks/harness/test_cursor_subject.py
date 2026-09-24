"""cursor_subject.translate and the blindness rules it relies on.

The fixture lines are the shapes the Cursor CLI 2026.09.18 emitted on
2026-09-20 against a one-tool FastMCP server named `alkera`, trimmed to the
fields the translator reads. See cursor_subject.py's docstring.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import blindness
import cursor_subject

CALL_MCP = "call_A\nfc_1"
CALL_SHELL = "call_B\nfc_2"
CALL_GLOB = "call_C\nfc_3"
CALL_SCHEMA = "call_D\nfc_4"
CALL_TODO = "toolu_todo_1"


def _tool_call(sub, kind, call_id, body):
    return {"type": "tool_call", "subtype": sub, "call_id": call_id,
            "tool_call": {kind: body, "toolCallId": call_id},
            "session_id": "s1"}


def _stream(with_glob: bool = False) -> list[dict]:
    ev = [
        {"type": "system", "subtype": "init", "apiKeySource": "login",
         "cwd": "C:\\ws", "session_id": "s1", "model": "GPT-5.4 Nano None",
         "permissionMode": "default"},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "the ticket"}]}, "session_id": "s1"},
        # a thinking model streams its reasoning as deltas, then closes it
        {"type": "thinking", "subtype": "delta", "text": "Let me ", "session_id": "s1"},
        {"type": "thinking", "subtype": "delta", "text": "look.", "session_id": "s1"},
        {"type": "thinking", "subtype": "completed", "session_id": "s1"},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Looking."}]}, "session_id": "s1"},
        # cursor-agent 2026.09.18: the agent's own todo list (GT009 approved r1)
        _tool_call("started", "updateTodosToolCall", CALL_TODO,
                   {"args": {"todos": [{"id": "1", "content": "fix the loader",
                                        "status": "TODO_STATUS_IN_PROGRESS"}]}}),
        _tool_call("completed", "updateTodosToolCall", CALL_TODO,
                   {"args": {"todos": [{"id": "1", "content": "fix the loader",
                                        "status": "TODO_STATUS_IN_PROGRESS"}]},
                    "result": {"success": {}}}),
        _tool_call("started", "getMcpToolsToolCall", CALL_SCHEMA,
                   {"args": {"server": "alkera", "toolName": "ping"}}),
        _tool_call("completed", "getMcpToolsToolCall", CALL_SCHEMA,
                   {"args": {"server": "alkera", "toolName": "ping"},
                    "result": {"success": {"content": "{\"tool\": \"ping\"}"}}}),
        _tool_call("started", "mcpToolCall", CALL_MCP,
                   {"args": {"name": "alkera-ping", "args": {"word": "hello"},
                             "providerIdentifier": "alkera", "toolName": "ping",
                             "serverIdentifier": "alkera"}}),
        _tool_call("completed", "mcpToolCall", CALL_MCP,
                   {"args": {"name": "alkera-ping", "toolName": "ping",
                             "serverIdentifier": "alkera"},
                    "result": {"success": {"content": [{"text": {"text": "pong:hello"}}],
                                           "isError": False}}}),
        _tool_call("started", "shellToolCall", CALL_SHELL,
                   {"args": {"command": "echo shelltest", "workingDirectory": "",
                             "timeout": 30000}}),
        # the CLI omits `args` on a refused completion
        _tool_call("completed", "shellToolCall", CALL_SHELL,
                   {"result": {"permissionDenied": {
                       "command": "echo shelltest", "workingDirectory": "C:\\ws",
                       "error": "Command blocked by permissions configuration",
                       "isReadonly": False}}}),
    ]
    if with_glob:
        ev += [
            _tool_call("started", "globToolCall", CALL_GLOB,
                       {"args": {"globPattern": "**/*.py"}}),
            _tool_call("completed", "globToolCall", CALL_GLOB,
                       {"args": {"globPattern": "**/*.py"},
                        "result": {"success": {"files": [".\\a.py"], "totalFiles": 1}}}),
        ]
    ev += [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "pong:hello; shell refused"}]}, "session_id": "s1"},
        {"type": "result", "subtype": "success", "duration_ms": 9253,
         "is_error": False, "result": "Looking.pong:hello; shell refused",
         "session_id": "s1", "request_id": "r1",
         "usage": {"inputTokens": 100, "outputTokens": 10,
                   "cacheReadTokens": 0, "cacheWriteTokens": 0}},
    ]
    return ev


def _write(tmp_path: Path, events, bom: bool = True) -> Path:
    raw = tmp_path / "raw.jsonl"
    text = "\n".join(json.dumps(e) for e in events) + "\n"
    raw.write_text(("\ufeff" if bom else "") + text, encoding="utf-8")
    return raw


def _detect(transcript):
    return blindness.detect(
        transcript=transcript, driver="cursor", gateway_tools=["ping"],
        canaries=[], repo_root=Path("."), action_rows=[],
        world_before={}, world_after={}, world_layout={},
        artifact_text="", mcp_server="alkera", topology="host")


def test_translation_shapes(tmp_path):
    raw = _write(tmp_path, _stream())
    out = tmp_path / "transcript.jsonl"
    meta = cursor_subject.translate(raw, out, "alkera")
    tr = blindness.load_transcript(out)

    assert meta["unparsed"] == 0, "the BOM and every known shape must parse"
    assert meta["tool_calls"] == 4 and meta["refused"] == 1
    assert meta["mcp_servers"] == ["alkera"]
    assert meta["session_id"] == "s1"
    assert meta["usage"]["tokens"]["inputTokens"] == 100
    assert meta["final_text"] == "pong:hello; shell refused"

    thinking = [b for ev in tr if ev.get("type") == "assistant"
                for b in blindness._blocks(ev) if b.get("type") == "thinking"]
    assert [b["thinking"] for b in thinking] == ["Let me look."], \
        "deltas are buffered into one block, closed by thinking/completed"

    init = blindness.init_event(tr)
    assert init is not None
    assert init["tools"] == [] and init["tools_reported"] is False
    assert [s["name"] for s in init["mcp_servers"]] == ["alkera"]

    uses = blindness.tool_uses(tr)
    assert [u["name"] for u in uses] == ["TodoWrite", "ToolSearch", "mcp__alkera__ping", "Shell"]
    assert uses[2]["input"] == {"word": "hello"}
    assert uses[2]["id"] == CALL_MCP

    # the refused shell call carries the CLI's own denial payload, marked
    assert blindness.policy_refused_tool_use_ids(tr) == {CALL_SHELL}
    results = [b for ev in tr if ev.get("type") == "user"
               for b in blindness._blocks(ev) if b.get("type") == "tool_result"]
    shell_res = next(b for b in results if b["tool_use_id"] == CALL_SHELL)
    assert shell_res["is_error"] is True
    assert shell_res["content"].startswith(blindness.POLICY_REFUSAL)
    assert "Command blocked by permissions configuration" in shell_res["content"]
    mcp_res = next(b for b in results if b["tool_use_id"] == CALL_MCP)
    assert mcp_res["content"] == "pong:hello" and not mcp_res.get("is_error")


def test_refused_builtin_is_soft_and_ran_builtin_is_hard(tmp_path):
    raw = _write(tmp_path, _stream(with_glob=True))
    out = tmp_path / "transcript.jsonl"
    cursor_subject.translate(raw, out, "alkera")
    kinds = {(v.kind, v.hard) for v in _detect(blindness.load_transcript(out))}

    # the surface is unknown, and the record says so without quarantining
    assert ("tool_surface_unreported", False) in kinds
    # shell asked for, refused by policy, nothing ran: soft
    assert ("refused_by_policy", False) in kinds
    assert not any(k == "host_exec" for k, _ in kinds)
    # glob asked for and RAN (the policy cannot refuse it): hard
    assert ("host_filesystem", True) in kinds
    # the schema fetch and the todo list are bookkeeping, never sight
    assert not any(k == "unknown_tool" for k, _ in kinds)
    assert ("bookkeeping_tool", False) in kinds


def test_unknown_event_type_fails_closed(tmp_path):
    events = _stream()
    events.insert(3, {"type": "agent.turn.started", "session_id": "s1"})
    raw = _write(tmp_path, events, bom=False)
    out = tmp_path / "transcript.jsonl"
    meta = cursor_subject.translate(raw, out, "alkera")
    assert meta["unparsed"] == 1
    kinds = {v.kind for v in _detect(blindness.load_transcript(out))}
    assert "unobserved_episode" in kinds


def test_transport_reconnect_is_neutral_and_counted(tmp_path):
    # cursor-agent 2026.09.18 resumed a Fable session mid-episode (GT004
    # authorized r0): four transport lines, no agent content on any of them
    events = _stream()
    events[3:3] = [
        {"type": "connection", "subtype": "reconnecting", "session_id": "s1",
         "attempt": 1, "endpoint_url": "https://agentn.example"},
        {"type": "retry", "subtype": "starting", "session_id": "s1",
         "attempt": 1, "is_resume": True},
        {"type": "retry", "subtype": "resuming", "session_id": "s1",
         "checkpoint_turn_count": 1, "attempt": 1},
        {"type": "connection", "subtype": "reconnected", "session_id": "s1"},
    ]
    raw = _write(tmp_path, events, bom=False)
    out = tmp_path / "transcript.jsonl"
    meta = cursor_subject.translate(raw, out, "alkera")
    assert meta["unparsed"] == 0 and meta["reconnects"] == 1
    kinds = {v.kind for v in _detect(blindness.load_transcript(out))}
    assert "unobserved_episode" not in kinds


def test_unknown_tool_kind_that_ran_is_hard(tmp_path):
    events = _stream()
    cid = "call_Z\nfc_9"
    events[8:8] = [
        _tool_call("started", "frobnicateToolCall", cid, {"args": {"x": 1}}),
        _tool_call("completed", "frobnicateToolCall", cid,
                   {"args": {"x": 1}, "result": {"success": {"content": "done"}}}),
    ]
    raw = _write(tmp_path, events, bom=False)
    out = tmp_path / "transcript.jsonl"
    cursor_subject.translate(raw, out, "alkera")
    uses = blindness.tool_uses(blindness.load_transcript(out))
    assert "frobnicateToolCall" in [u["name"] for u in uses]
    assert ("unknown_tool", True) in {(v.kind, v.hard)
                                      for v in _detect(blindness.load_transcript(out))}


def test_documented_function_fallback_shape_is_judged_not_unparsed(tmp_path):
    """The docs' shape for 'other tools': tool_call.function {name, arguments}."""
    events = _stream()
    cid = "call_F\nfc_7"
    events[8:8] = [
        {"type": "tool_call", "subtype": "started", "call_id": cid, "session_id": "s1",
         "tool_call": {"function": {"name": "web_search",
                                    "arguments": "{\"query\": \"x\"}"}}},
        {"type": "tool_call", "subtype": "completed", "call_id": cid, "session_id": "s1",
         "tool_call": {"function": {"name": "web_search",
                                    "arguments": "{\"query\": \"x\"}"},
                       "result": {"success": {"content": "hits"}}}},
    ]
    raw = _write(tmp_path, events, bom=False)
    out = tmp_path / "transcript.jsonl"
    meta = cursor_subject.translate(raw, out, "alkera")
    assert meta["unparsed"] == 0
    tr = blindness.load_transcript(out)
    use = next(u for u in blindness.tool_uses(tr) if u["name"] == "web_search")
    assert use["input"] == {"query": "x"}
    kinds = {(v.kind, v.hard) for v in _detect(tr)}
    assert ("unobserved_episode", True) not in kinds
    assert ("unknown_tool", True) in kinds  # it ran, and it is not a gateway tool


def test_prepare_home_writes_only_the_policy(tmp_path):
    home = cursor_subject.prepare_home(tmp_path / "home")
    files = sorted(p.relative_to(home).as_posix() for p in home.rglob("*") if p.is_file())
    assert files == [".cursor/cli-config.json"]
    cfg = json.loads((home / ".cursor" / "cli-config.json").read_text(encoding="utf-8"))
    assert cfg["permissions"]["allow"] == []
    assert set(cfg["permissions"]["deny"]) == set(cursor_subject.DENY_PATTERNS)
    for tok in cfg["permissions"]["deny"]:
        # only the token families the CLI documents as enforceable
        assert tok.split("(")[0] in {"Shell", "Read", "Write", "WebFetch"}


def test_subject_env_isolates_home():
    env = cursor_subject.subject_env({"PATH": "x", "HOME": "/real",
                                      "USERPROFILE": "C:\\real",
                                      "CLAUDE_PROJECT_DIR": "/repo"},
                                     Path("/iso"))
    assert env["HOME"] == str(Path("/iso")) == env["USERPROFILE"]
    assert "CLAUDE_PROJECT_DIR" not in env and env["PATH"] == "x"


def test_cursor_is_a_policy_denied_mcp_surface_driver():
    assert "cursor" in blindness.MCP_SURFACE_DRIVERS
    assert "cursor" not in blindness.MCP_DRIVERS, \
        "cursor cannot prove its granted surface; it must not claim revocation"
    assert blindness.expected_tool_surface("cursor", ["ping"], "alkera") == {
        "mcp__alkera__ping"}
    assert blindness.guarantee(driver="cursor", topology="host",
                               containment=None) == "detection_only"


_LAYOUT = {"world_root": r"C:\r\world", "workspace": r"C:\r\world\workspace",
           "scheduler": r"C:\r\world\var\sched\state.json",
           "profiles_dir": r"C:\r\world\build",
           "warehouse": r"C:\r\world\var\warehouse\warehouse.duckdb"}


def test_human_plane_writes_explain_their_workflow_file():
    """GT009 r1 (2026-09-20): chat_post and issues_comment rows target the
    object, not the file, and the two files they write read as off-channel."""
    before = {"var/workflow/chat.json": [1, "a"], "var/workflow/issues.json": [1, "a"],
              "var/workflow/kb.json": [1, "a"]}
    after = {"var/workflow/chat.json": [1, "b"], "var/workflow/issues.json": [1, "b"],
             "var/workflow/kb.json": [1, "b"]}
    rows = [{"tool": "chat_post", "op_class": "write", "targets": ["chat:payments"]},
            {"tool": "issues_comment", "op_class": "write", "targets": ["issue:PAY-3297"]}]
    # kb.json changed with no kb_* WRITE row: still reported
    assert blindness._unattributed(before, after, rows, _LAYOUT, None) == [
        "var/workflow/kb.json"]
    # a kb READ explains nothing
    rows.append({"tool": "kb_get", "op_class": "read", "targets": ["kb:pay"]})
    assert blindness._unattributed(before, after, rows, _LAYOUT, None) == [
        "var/workflow/kb.json"]
    rows.append({"tool": "kb_edit", "op_class": "write", "targets": ["kb:pay"]})
    assert blindness._unattributed(before, after, rows, _LAYOUT, None) == []
    # ...and a chat write never launders a change to a different file
    assert blindness._unattributed({"var/ledger/receipts.jsonl": [1, "a"]},
                                   {"var/ledger/receipts.jsonl": [1, "b"]},
                                   rows[:1], _LAYOUT, None) == [
        "var/ledger/receipts.jsonl"]


def test_unattributed_served_plane_change_is_soft_for_cursor(tmp_path):
    raw = _write(tmp_path, _stream())
    out = tmp_path / "transcript.jsonl"
    cursor_subject.translate(raw, out, "alkera")
    tr = blindness.load_transcript(out)
    kw = dict(transcript=tr, gateway_tools=["ping"], canaries=[],
              repo_root=Path("."), action_rows=[],
              world_before={"var/world/live.json": [1, "a"]},
              world_after={"var/world/live.json": [1, "b"]},
              world_layout=_LAYOUT, artifact_text="", mcp_server="alkera",
              topology="host")
    for driver, hard in (("cursor", False), ("mcp", False), ("cli", True)):
        vs = [v for v in blindness.detect(driver=driver, **kw)
              if v.kind == "out_of_band_world_change"]
        assert vs and all(v.hard is hard for v in vs), (driver, vs)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
