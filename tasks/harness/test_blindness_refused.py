"""A tool call the runtime REFUSED is a soft signal, not a quarantine.

Observed 2026-09-16, GT004b r2 (ablate-R-20260916-k5): the subject emitted
`mcp__antml:alkera__sql_execute` - its call-format namespace leaked into the
tool name - the CLI answered `No such tool available`, and the subject then
re-issued the same DELETE through the real tool, which is in the audit log.
The episode repaired defect 2 (31/33) and was quarantined for a call that
never reached anything. Only the exact runtime refusal is soft; an unknown
tool that got any other answer stays hard, because fail-closed is the rule.

Run: gateway/.venv/Scripts/python.exe tasks/harness/test_blindness_refused.py
Skips itself (exit 0, says so) on a tree where BLINDNESS.patch is not applied.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import blindness  # noqa: E402

if not hasattr(blindness, "refused_tool_use_ids"):
    print("SKIP: BLINDNESS.patch not applied on this tree (no refused_tool_use_ids)")
    sys.exit(0)

ALLOWED = {"mcp__alkera__sql_query", "mcp__alkera__sql_execute"}
SQL = "DELETE FROM ops.dq_checks WHERE checked_at = TIMESTAMP '2026-09-16 00:12:12.653131';"


def transcript(answer: str, is_error: bool = True) -> list[dict]:
    return [
        {"type": "system", "subtype": "init", "tools": sorted(ALLOWED),
         "mcp_servers": [{"name": "alkera"}]},
        {"type": "user", "message": {"role": "user", "content": "prompt"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_bad", "name": "mcp__antml:alkera__sql_execute",
             "input": {"sql": SQL}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_bad", "is_error": is_error,
             "content": answer}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_ok", "name": "mcp__alkera__sql_execute",
             "input": {"sql": SQL}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_ok", "content": "{\"rows_affected\": 1}"}]}},
    ]


def kinds(t):
    return {(v.kind, v.hard) for v in blindness.detect_tool_use_violations(t, ALLOWED)} \
        if hasattr(blindness, "detect_tool_use_violations") else None


failures = 0


def check(label, cond):
    global failures
    print(("PASS " if cond else "FAIL ") + label)
    failures += 0 if cond else 1


refused = blindness.refused_tool_use_ids(transcript("<tool_use_error>Error: No such tool available: mcp__antml:alkera__sql_execute</tool_use_error>"))
check("refusal is recognised by tool_use id", refused == {"toolu_bad"})
check("a tool that ran and errored is NOT a refusal",
      blindness.refused_tool_use_ids(transcript("Catalog Error: table ops.dq_checks does not exist")) == set())
check("a non-error result never counts",
      blindness.refused_tool_use_ids(transcript("No such tool available", is_error=False)) == set())
uses = blindness.tool_uses(transcript("x"))
check("tool_uses carries the id", [u.get("id") for u in uses] == ["toolu_bad", "toolu_ok"])

print(f"{'OK' if not failures else 'FAILED'}: {failures} failure(s)")
sys.exit(1 if failures else 0)
