"""Per-episode TRACKED metrics: counted, reported, never scored.

docs/Scoring_Redesign.md §3 refuses a brevity or step-count reward, and
§4.2 keeps efficiency as a raw count beside the number. This module is that
count. Nothing here feeds reward.py; report.py attaches the dict to each
per-rollout row so a reader can see what an episode cost, and a trainer's
filter can read it without the harness having priced it.

The two sources, and the row keys they actually carry:

  transcript.jsonl   the subject CLI's stream-json events, written verbatim
                     by agent_under_test.run's pump(): one JSON object per
                     line with `type` in {system, assistant, user, result}.
                     Tool calls are `tool_use` blocks in an assistant event's
                     `message.content`; token counts live on the `result`
                     event's `usage` ({input_tokens, output_tokens,
                     cache_creation_input_tokens, cache_read_input_tokens});
                     `duration_ms` and `num_turns` sit beside it. The CLI
                     stamps NO per-event wall-clock time, so a row-level
                     timestamp is read only when a row happens to carry one
                     (`timestamp` / `ts` / `wts` / `time` / `created_at`),
                     and the episode start otherwise comes from the record's
                     `agent.started_wts` (epoch ms), passed in as
                     `started_wts`.
  audit/actions.jsonl
                     contract-9.2.2 rows (gateway `Gateway._call`,
                     native `alkera_gateway.native.audit.record`): `seq`,
                     `tool`, `op_class`, `targets`, `args`, `flags`, `wts`
                     (wall clock, epoch MILLISECONDS) and `vts` (the world's
                     virtual clock, ISO). Only `wts` is a real time.

Definitions (every value is None when not derivable, never 0):

  n_turns                 message events in the transcript: `assistant` +
                          `user` rows. The system init event and the final
                          `result` event are not turns. (The CLI's own
                          `num_turns` on the result event counts differently
                          across versions, so it is not used.)
  n_toolcalls             `tool_use` blocks across assistant events.
  n_tokens_in             result.usage.input_tokens plus the two cache
                          fields when present - every token the model READ.
  n_tokens_out            result.usage.output_tokens.
  wall_clock_s            result.duration_ms / 1000; else last minus first
                          row timestamp when rows carry one.
  time_to_first_action_s  first audit row's wall time minus the episode's
                          start (first transcript row timestamp, else
                          `started_wts`). None without both ends.

Timestamps are accepted as ISO-8601 strings (a trailing Z is UTC), epoch
seconds (float or int) or epoch milliseconds (an int or float >= 1e11 - no
episode ran before 1973 or after the year 5138).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

KEYS = ("n_turns", "n_toolcalls", "n_tokens_in", "n_tokens_out",
        "wall_clock_s", "time_to_first_action_s")
TS_KEYS = ("timestamp", "ts", "wts", "time", "created_at")
_EPOCH_MS_FLOOR = 1e11


def _empty() -> dict:
    return {k: None for k in KEYS}


def parse_ts(value) -> float | None:
    """A timestamp as epoch SECONDS, or None for anything unreadable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return v / 1000.0 if v >= _EPOCH_MS_FLOOR else v
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return parse_ts(float(s))
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith("Z") else s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def _row_ts(row) -> float | None:
    if not isinstance(row, dict):
        return None
    for key in TS_KEYS:
        if key in row:
            ts = parse_ts(row[key])
            if ts is not None:
                return ts
    return None


def _blocks(ev: dict) -> list[dict]:
    msg = ev.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in (content or []) if isinstance(b, dict)]


def _int_or_none(v) -> int | None:
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def from_rows(transcript_rows, audit_rows, started_wts=None) -> dict:
    """The six tracked metrics from parsed rows. See the module docstring."""
    out = _empty()
    rows = [r for r in (transcript_rows or []) if isinstance(r, dict)]
    actions = [r for r in (audit_rows or []) if isinstance(r, dict)]

    if rows:
        out["n_turns"] = sum(1 for r in rows if r.get("type") in ("assistant", "user"))
        out["n_toolcalls"] = sum(1 for r in rows if r.get("type") == "assistant"
                                 for b in _blocks(r) if b.get("type") == "tool_use")
        result = next((r for r in rows if r.get("type") == "result"), None)
        if result is not None:
            usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
            tokens_in = _int_or_none(usage.get("input_tokens"))
            if tokens_in is not None:
                for extra in ("cache_creation_input_tokens", "cache_read_input_tokens"):
                    tokens_in += _int_or_none(usage.get(extra)) or 0
            out["n_tokens_in"] = tokens_in
            out["n_tokens_out"] = _int_or_none(usage.get("output_tokens"))
            duration = result.get("duration_ms")
            if isinstance(duration, (int, float)) and not isinstance(duration, bool):
                out["wall_clock_s"] = round(float(duration) / 1000.0, 3)

    stamps = [t for t in (_row_ts(r) for r in rows) if t is not None]
    if out["wall_clock_s"] is None and len(stamps) >= 2:
        out["wall_clock_s"] = round(stamps[-1] - stamps[0], 3)

    start = stamps[0] if stamps else parse_ts(started_wts)
    first_action = next((t for t in (_row_ts(r) for r in actions) if t is not None), None)
    if start is not None and first_action is not None:
        out["time_to_first_action_s"] = round(first_action - start, 3)
    return out


def _read_jsonl(path) -> list[dict] | None:
    """Rows from a .jsonl file, or None when the file is not there."""
    if path is None:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    rows = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def from_paths(transcript_path, audit_path, started_wts=None) -> dict:
    """from_rows over the two files. A missing file reads as no rows."""
    return from_rows(_read_jsonl(transcript_path) or [],
                     _read_jsonl(audit_path) or [], started_wts=started_wts)


def from_record(rec: dict) -> dict | None:
    """The metrics for one rollout record, or None when it carries no source.

    Sources, in order: `transcript_rows` / `actions` already on the record;
    else the files the record names - `agent.transcript_path` or
    `<rollout_dir>/transcript.jsonl`, and `audit_path`, `action_log.path` or
    `<rollout_dir>/audit/actions.jsonl`. None (not a dict of Nones) when
    neither rows nor a readable file exist, so a caller can tell "nothing
    to count" from "counted nothing".
    """
    if not isinstance(rec, dict):
        return None
    agent = rec.get("agent") if isinstance(rec.get("agent"), dict) else {}
    started = agent.get("started_wts")
    rollout_dir = rec.get("rollout_dir")

    transcript = rec.get("transcript_rows")
    if transcript is None:
        for cand in (agent.get("transcript_path"),
                     Path(rollout_dir) / "transcript.jsonl" if rollout_dir else None):
            transcript = _read_jsonl(cand)
            if transcript is not None:
                break
    actions = rec.get("actions")
    if actions is None:
        for cand in (rec.get("audit_path"),
                     (rec.get("action_log") or {}).get("path")
                     if isinstance(rec.get("action_log"), dict) else None,
                     Path(rollout_dir) / "audit" / "actions.jsonl" if rollout_dir else None):
            actions = _read_jsonl(cand)
            if actions is not None:
                break
    if transcript is None and actions is None:
        return None
    return from_rows(transcript or [], actions or [], started_wts=started)
