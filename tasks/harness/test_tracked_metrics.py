"""tracked_metrics.py: counted, never scored.

    python -m pytest test_tracked_metrics.py -q

A six-line transcript shaped like the claude CLI's stream-json (system init,
user prompt, assistant tool_use, user tool_result, assistant with two
tool_use blocks, result with usage) and a two-row 9.2.2 audit log. The rows
here carry the keys rollout.py / agent_under_test.py actually write.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tracked_metrics as tm  # noqa: E402

T0 = 1_758_240_000.0                     # 2025-09-19T00:00:00Z, epoch seconds


def transcript_rows(with_timestamps: bool = True) -> list[dict]:
    def stamp(offset):
        return {"timestamp": f"2025-09-19T00:00:{offset:02d}Z"} if with_timestamps else {}
    return [
        {"type": "system", "subtype": "init", "tools": ["Bash"], **stamp(0)},
        {"type": "user", "message": {"role": "user", "content": "fix it"}, **stamp(1)},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "looking"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
        ]}, **stamp(2)},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}, **stamp(5)},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "cat a"}},
            {"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "cat b"}},
        ]}, **stamp(6)},
        {"type": "result", "subtype": "success", "duration_ms": 12500, "num_turns": 3,
         "usage": {"input_tokens": 1000, "cache_creation_input_tokens": 200,
                   "cache_read_input_tokens": 300, "output_tokens": 450},
         **stamp(12)},
    ]


def audit_rows() -> list[dict]:
    # `wts` is epoch MILLISECONDS, as native.audit.record writes it
    return [
        {"seq": 1, "tool": "psql.statement", "op_class": "read",
         "targets": ["warehouse.fct_orders"], "args": {"sql": "select 1"},
         "flags": [], "exit": "ok", "wts": int((T0 + 4.5) * 1000),
         "vts": "2026-03-01T09:00:00Z"},
        {"seq": 2, "tool": "workspace.edit", "op_class": "write",
         "targets": ["workspace:models/a.sql"], "args": {"path": "models/a.sql"},
         "flags": ["actor:harness"], "exit": "ok", "wts": int((T0 + 9.0) * 1000),
         "vts": "2026-03-01T09:00:00Z"},
    ]


def test_counts_from_rows():
    m = tm.from_rows(transcript_rows(), audit_rows())
    assert set(m) == set(tm.KEYS)
    assert m["n_toolcalls"] == 3
    assert m["n_turns"] == 4                     # assistant + user rows; init and result are not turns
    assert m["n_tokens_in"] == 1500              # input + both cache fields
    assert m["n_tokens_out"] == 450
    assert m["wall_clock_s"] == 12.5             # result.duration_ms
    assert m["time_to_first_action_s"] == pytest.approx(4.5)   # first audit wts - first transcript ts


def test_empty_audit_gives_no_time_to_first_action():
    m = tm.from_rows(transcript_rows(), [])
    assert m["time_to_first_action_s"] is None
    assert m["n_toolcalls"] == 3                 # the transcript half still counts


def test_started_wts_stands_in_for_unstamped_transcript_rows():
    # the claude CLI stamps no per-event time; the record's agent.started_wts does
    m = tm.from_rows(transcript_rows(with_timestamps=False), audit_rows(),
                     started_wts=int(T0 * 1000))
    assert m["time_to_first_action_s"] == pytest.approx(4.5)
    without = tm.from_rows(transcript_rows(with_timestamps=False), audit_rows())
    assert without["time_to_first_action_s"] is None


def test_nothing_derivable_is_none_never_zero():
    m = tm.from_rows([], [])
    assert m == {k: None for k in tm.KEYS}


def test_parse_ts_accepts_iso_epoch_seconds_and_epoch_ms():
    assert tm.parse_ts("2025-09-19T00:00:00Z") == pytest.approx(T0)
    assert tm.parse_ts("2025-09-19T00:00:00+00:00") == pytest.approx(T0)
    assert tm.parse_ts(T0) == pytest.approx(T0)
    assert tm.parse_ts(int(T0 * 1000)) == pytest.approx(T0)
    assert tm.parse_ts(str(T0)) == pytest.approx(T0)
    assert tm.parse_ts("not a time") is None and tm.parse_ts(None) is None


def _write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_from_paths_and_from_record(tmp_path):
    rd = tmp_path / "r0"
    _write_jsonl(rd / "transcript.jsonl", transcript_rows())
    _write_jsonl(rd / "audit" / "actions.jsonl", audit_rows())
    m = tm.from_paths(rd / "transcript.jsonl", rd / "audit" / "actions.jsonl")
    assert m["n_toolcalls"] == 3 and m["time_to_first_action_s"] == pytest.approx(4.5)
    # a record that names the rollout directory resolves both files from it
    rec = {"rollout_id": "r0", "rollout_dir": str(rd),
           "agent": {"started_wts": int(T0 * 1000)}}
    assert tm.from_record(rec) == m
    # a record that names the files explicitly, as rollout.py's does
    rec2 = {"agent": {"transcript_path": str(rd / "transcript.jsonl")},
            "action_log": {"path": str(rd / "audit" / "actions.jsonl")}}
    assert tm.from_record(rec2) == m
    # rows already on the record win over files
    rec3 = {"transcript_rows": transcript_rows(), "actions": []}
    assert tm.from_record(rec3)["time_to_first_action_s"] is None
    # no source at all is None, not a dict of Nones
    assert tm.from_record({"rollout_id": "r9"}) is None
    assert tm.from_record({"rollout_dir": str(tmp_path / "gone")}) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([str(Path(__file__).resolve()), "-q"]))
