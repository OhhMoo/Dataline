#!/usr/bin/env python3
"""Loss-mask-preserving compaction of environment observations.

Trajectories exported by ``tasks.factory.export_rl_dataset`` are dominated
by ``loss_mask == 0`` tool observations: one ToolSearch call dumps the
schemas of all 63 MCP tools and ``bash``/``sql_query`` calls return
multi-kilobyte dumps.  Right-truncating such a trajectory at a training
``max_seq_len`` would cut off the final assistant turns (``pipeline_repair``
and the ``governance_and_reporting`` / ``final_report.json`` turn) that
carry ``loss_mask == 1`` supervision.

This module compacts only ``role == "tool"`` observation content, in
three passes, so a trajectory fits a token budget without ever touching
an assistant turn:

1. ToolSearch / schema-dump responses are replaced by a compact JSON
   summary listing every tool name in the namespace plus the full schema
   entries *only* for tools actually invoked in the rollout.
2. Any remaining tool observation longer than ``target_obs_chars`` is
   compacted with head+tail preservation.  JSON observations stay valid
   JSON (long strings / lists are truncated inside the structure and
   fields such as ``error``, ``passed``, ``row_count``, ``business_day``,
   ``ticket`` and ``approved_by`` are preserved wherever they occur);
   plain-text observations keep head, tail and any key summary lines
   around an ``[observation compacted]`` marker.
3. If the trajectory still exceeds ``max_seq_tokens``, the target is
   iteratively tightened (down to 256 chars) on the earliest
   ``discovery`` / ``epistemic_investigation`` tool observations first,
   so the final ``pipeline_repair``, ``verification_and_execution`` and
   ``governance_and_reporting`` turns are preserved.

Hard invariants:

* Every ``role == "assistant"`` message (``content``,
  ``reasoning_content``, ``tool_calls`` and every other field) is
  returned byte-for-byte identical to the input.
* No message is added, dropped or reordered, so ``tool_call_id``
  pairing (``orphan_tool_results == []``) is preserved by construction.

Token accounting follows the exporter: per message
``approx_tokens = max(1, ceil(chars / 4))`` over ``content`` +
``reasoning_content`` + tool-call arguments.  Compacted (and other
non-assistant) messages get their stored ``approx_tokens`` refreshed to
that value, keeping ``token_loss_mask_rle`` consistent with the messages
when :func:`compact_jsonl_file` rewrites a dataset file.  This module is
deliberately standalone (no import of ``export_rl_dataset``) so the
exporter can import it without a circular import.
"""
from __future__ import annotations

import copy
import json
import math
import re
from pathlib import Path
from typing import Any

__all__ = ["compact_trajectory_messages", "compact_jsonl_file"]

MARKER = "\n...[observation compacted]...\n"
_JSON_MARKER = "...[observation compacted]..."
#: Summary fields whose values must survive JSON observation compaction.
KEY_FIELDS = ("error", "passed", "row_count", "business_day", "ticket", "approved_by")
_KEY_FIELD_SET = frozenset(KEY_FIELDS)
#: Phases whose tool observations may be squeezed hardest in pass 3.
EARLY_PHASES = ("discovery", "epistemic_investigation")
#: Final phases that pass 3 must never squeeze.
PROTECTED_PHASES = (
    "pipeline_repair",
    "verification_and_execution",
    "governance_and_reporting",
)
#: Normalised names of tool-search / schema-dump tools.
_TOOLSEARCH_NAMES = frozenset(
    {
        "toolsearch",
        "getdynamictools",
        "getmcptoolstoolcall",
        "getmcptools",
        "getmcptool",
        "mcptools",
        "listtools",
        "searchtools",
    }
)
_SCHEMA_KEYS = ("inputSchema", "input_schema", "parameters")


# ---------------------------------------------------------------------------
# Token accounting (mirrors tasks.factory.export_rl_dataset._finalize)
# ---------------------------------------------------------------------------

def _text_len(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value)
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _message_chars(message: dict[str, Any]) -> int:
    chars = _text_len(message.get("content")) + _text_len(message.get("reasoning_content"))
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        chars += _text_len(function.get("arguments"))
    return chars


def _approx_tokens(chars: int) -> int:
    return max(1, math.ceil(chars / 4))


def _message_tokens(message: dict[str, Any]) -> int:
    return _approx_tokens(_message_chars(message))


def _total_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(_message_tokens(m) for m in messages)


def _assistant_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(_message_tokens(m) for m in messages if m.get("role") == "assistant")


def _set_content(message: dict[str, Any], content: str) -> None:
    message["content"] = content
    # Refresh the stored estimate for this (non-assistant) message so it
    # matches the exporter's approx_tokens / token_loss_mask_rle maths.
    message["approx_tokens"] = _message_tokens(message)


# ---------------------------------------------------------------------------
# Plain-text compaction
# ---------------------------------------------------------------------------

def _key_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if any(key in low for key in _KEY_FIELD_SET) and line not in lines:
            lines.append(line[:300])
    return lines


def _compact_plain_text(text: str, target: int) -> str:
    if len(text) <= target:
        return text
    target = max(16, int(target))
    missing = _key_lines(text)
    key_block = ""
    if missing:
        joined = "\n".join(missing[:6])
        # Only spend budget on a key block if the head would not already
        # surface those lines.
        if joined not in text[: target // 2]:
            key_block = joined + "\n"
    budget = max(16, target - len(key_block))
    head_len = budget // 2
    tail_len = budget - head_len
    head = text[:head_len]
    tail = text[-tail_len:] if tail_len else ""
    return head + MARKER + key_block + tail


# ---------------------------------------------------------------------------
# JSON compaction
# ---------------------------------------------------------------------------

def _truncate_json_value(value: Any, str_limit: int, list_limit: int | None) -> Any:
    if isinstance(value, str):
        if len(value) <= str_limit:
            return value
        if str_limit <= 8:
            return value[:str_limit] + "..."
        keep = max(2, str_limit // 2)
        return value[:keep] + _JSON_MARKER + value[-keep:]
    if isinstance(value, dict):
        return {
            k: (
                v
                if k in _KEY_FIELD_SET and not isinstance(v, (dict, list))
                else _truncate_json_value(v, str_limit, list_limit)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        items = [_truncate_json_value(v, str_limit, list_limit) for v in value]
        if list_limit is not None and len(items) > list_limit:
            kept = items[:list_limit]
            # Keep later items that carry key fields even past the cut.
            for item, original in zip(items[list_limit:], value[list_limit:]):
                if _contains_key_field(original):
                    kept.append(item)
            kept.append({"compacted_items_omitted": len(items) - list_limit})
            return kept
        return items
    return value


def _contains_key_field(value: Any) -> bool:
    if isinstance(value, dict):
        return any(k in _KEY_FIELD_SET or _contains_key_field(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_contains_key_field(v) for v in value)
    return False


_MISSING = object()


def _prune_to_key_fields(value: Any) -> Any:
    """Return the subtree of ``value`` that leads to a KEY_FIELDS entry."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if key in _KEY_FIELD_SET:
                if isinstance(child, (dict, list)):
                    sub = _prune_to_key_fields(child)
                    result[key] = sub if sub is not _MISSING else _truncate_json_value(child, 32, 3)
                else:
                    result[key] = child
            else:
                sub = _prune_to_key_fields(child)
                if sub is not _MISSING:
                    result[key] = sub
        return result if result else _MISSING
    if isinstance(value, list):
        items = []
        for child in value:
            sub = _prune_to_key_fields(child)
            if sub is not _MISSING:
                items.append(sub)
        return items if items else _MISSING
    return _MISSING


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _compact_json_text(parsed: Any, original_text: str, target: int) -> str:
    target = max(16, int(target))
    minified = _dumps(parsed)
    if len(minified) <= target:
        return minified

    # Most generous truncations first: shrink long strings, then lists.
    attempts = (
        (512, None), (256, None), (128, None), (64, None),
        (32, None), (16, None), (8, None),
        (128, 10), (64, 10), (32, 5), (16, 5), (8, 3), (8, 2), (8, 1),
    )
    for str_limit, list_limit in attempts:
        candidate = _dumps(_truncate_json_value(parsed, str_limit, list_limit))
        if len(candidate) <= target:
            return candidate

    # ToolSearch-shaped JSON gets a dedicated, name-preserving shrink.
    if _looks_like_schema_dump(parsed):
        shrunk = _shrink_schema_summary(parsed, target, invoked_names=None)
        if shrunk is not None and len(shrunk) < len(original_text):
            return shrunk

    pruned = _prune_to_key_fields(parsed)
    best_valid: str | None = None
    if pruned is not _MISSING:
        candidate = _dumps(pruned)
        if len(candidate) <= target:
            return candidate
        for str_limit, list_limit in ((32, 3), (16, 2), (8, 1)):
            shrunk_candidate = _dumps(_truncate_json_value(pruned, str_limit, list_limit))
            if len(shrunk_candidate) <= target:
                return shrunk_candidate
            if best_valid is None or len(shrunk_candidate) < len(best_valid):
                best_valid = shrunk_candidate

    # Last resort: a valid JSON envelope carrying the key fields (if any)
    # plus a preview of the original observation, trimmed to the target.
    envelope: dict[str, Any] = {"compacted": True}
    if pruned is not _MISSING:
        envelope["preserved"] = pruned
    envelope["preview"] = ""
    base_len = len(_dumps(envelope))
    room = max(0, target - base_len)
    envelope["preview"] = original_text[:room]
    candidate = _dumps(envelope)
    while len(candidate) > target and envelope["preview"]:
        envelope["preview"] = envelope["preview"][: max(0, len(envelope["preview"]) - (len(candidate) - target))]
        candidate = _dumps(envelope)
    if len(candidate) <= target:
        return candidate
    if best_valid is not None and len(best_valid) < len(original_text):
        return best_valid
    return candidate if len(candidate) < len(original_text) else minified[:target]


# ---------------------------------------------------------------------------
# ToolSearch / schema-dump compaction (pass 1)
# ---------------------------------------------------------------------------

def _parse_json(text: str) -> tuple[Any, bool]:
    """Parse ``text`` as JSON, tolerating trailing non-JSON padding."""
    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        return None, False
    try:
        return json.loads(stripped), True
    except (json.JSONDecodeError, ValueError):
        pass
    try:
        value, _ = json.JSONDecoder().raw_decode(stripped)
        return value, True
    except (json.JSONDecodeError, ValueError):
        return None, False


def _entry_name(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return None
    for key in ("tool", "name", "tool_name"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _tool_entries(parsed: Any) -> list[dict[str, Any]]:
    if isinstance(parsed, dict):
        entries: list[dict[str, Any]] = []
        for key in ("tools", "matches"):
            value = parsed.get(key)
            if isinstance(value, list):
                entries.extend(e for e in value if isinstance(e, dict))
        if entries:
            return entries
        if _entry_name(parsed) and any(k in parsed for k in (*_SCHEMA_KEYS, "description")):
            return [parsed]
        result = parsed.get("result")
        if isinstance(result, (dict, list)):
            return _tool_entries(result)
        return []
    if isinstance(parsed, list):
        return [e for e in parsed if isinstance(e, dict) and _entry_name(e)]
    return []


def _looks_like_schema_dump(parsed: Any) -> bool:
    entries = _tool_entries(parsed)
    if not entries:
        return False
    if isinstance(parsed, dict) and ("tools" in parsed or "matches" in parsed):
        return True
    return any(any(k in e for k in (*_SCHEMA_KEYS, "description")) for e in entries)


def _is_toolsearch_call(call_name: str | None) -> bool:
    if not call_name:
        return False
    normalised = re.sub(r"[^a-z0-9]", "", call_name.lower())
    return normalised in _TOOLSEARCH_NAMES or "getmcptools" in normalised


def _build_schema_summary(parsed: Any, invoked_names: set[str] | None) -> dict[str, Any] | None:
    entries = _tool_entries(parsed)
    if not entries:
        return None
    names: list[str] = []
    for entry in entries:
        name = _entry_name(entry)
        if name and name not in names:
            names.append(name)
    if not names:
        return None
    summary: dict[str, Any] = {}
    if isinstance(parsed, dict):
        for key in ("mode", "namespace", "namespaceStatus", "status", "pattern"):
            value = parsed.get(key)
            if isinstance(value, str) and len(value) <= 200:
                summary[key] = value
            elif value is not None and not isinstance(value, (dict, list)):
                summary[key] = value
        for key, value in parsed.items():
            if key in summary or key in ("tools", "matches", "description", "tool", "name"):
                continue
            if value is None or isinstance(value, (str, int, float, bool)):
                if isinstance(value, str) and len(value) > 80:
                    continue
                summary.setdefault(key, value)
    # If the source already carried an explicit tool_names list, keep it.
    if isinstance(parsed, dict) and isinstance(parsed.get("tool_names"), list):
        existing = [str(n) for n in parsed["tool_names"] if isinstance(n, str)]
        for name in existing:
            if name not in names:
                names.append(name)
    summary["tool_names"] = names
    if invoked_names is None:
        kept = [copy.deepcopy(e) for e in entries]
    else:
        kept = [copy.deepcopy(e) for e in entries if _entry_name(e) in invoked_names]
    summary["tools"] = kept
    summary["compacted"] = True
    return summary


def _name_only_entry(entry: dict[str, Any], *, with_schema: bool) -> dict[str, Any]:
    name = _entry_name(entry)
    key = "tool" if "tool" in entry else "name"
    result: dict[str, Any] = {key: name}
    if with_schema:
        for schema_key in _SCHEMA_KEYS:
            if schema_key in entry:
                result[schema_key] = copy.deepcopy(entry[schema_key])
                break
    return result


def _shrink_schema_summary(parsed: Any, target: int, invoked_names: set[str] | None) -> str | None:
    """Shrink a schema dump / summary to ``target`` chars, valid JSON.

    Fidelity is degraded in stages: full entries -> truncated
    descriptions -> schema-only entries -> name-only entries, always
    keeping the namespace and the invoked tools' names.
    """
    summary = _build_schema_summary(parsed, invoked_names)
    if summary is None:
        return None
    target = max(16, int(target))
    full = _dumps(summary)
    if len(full) <= target:
        return full

    entries = _tool_entries(parsed)
    entry_by_name = {_entry_name(e): e for e in entries if _entry_name(e)}
    kept_entries = summary.get("tools") or []

    candidates: list[str] = []
    # 1) Truncate long string values (descriptions) inside full entries.
    for limit in (128, 64, 32, 16, 8):
        candidates.append(_dumps(_truncate_json_value(summary, limit, None)))
    # 2) Drop descriptions, keep schemas.
    no_desc = copy.deepcopy(summary)
    for entry in no_desc.get("tools") or []:
        if isinstance(entry, dict):
            entry.pop("description", None)
    candidates.append(_dumps(no_desc))
    # 3) Identifier + input schema only.
    schema_only = copy.deepcopy(summary)
    schema_only["tools"] = [
        _name_only_entry(entry_by_name[_entry_name(e)], with_schema=True)
        for e in kept_entries
        if _entry_name(e) in entry_by_name
    ]
    candidates.append(_dumps(schema_only))
    schema_only_no_names = {k: v for k, v in schema_only.items() if k != "tool_names"}
    candidates.append(_dumps(schema_only_no_names))
    # 4) Name-only entries (tool_names list retained, then dropped).
    name_only = copy.deepcopy(summary)
    name_only["tools"] = [
        _name_only_entry(entry_by_name[_entry_name(e)], with_schema=False)
        for e in kept_entries
        if _entry_name(e) in entry_by_name
    ]
    candidates.append(_dumps(name_only))
    name_only_no_list = {k: v for k, v in name_only.items() if k != "tool_names"}
    candidates.append(_dumps(name_only_no_list))
    # 5) Minimal envelope: namespace + name-only invoked entries.
    minimal: dict[str, Any] = {}
    if summary.get("namespace") is not None:
        minimal["namespace"] = summary["namespace"]
    minimal["tools"] = name_only["tools"]
    candidates.append(_dumps(minimal))

    for candidate in candidates:
        if len(candidate) <= target:
            return candidate
    # 6) Progressively drop name-only entries (oldest retained first).
    tools = list(minimal.get("tools") or [])
    while tools:
        tools = tools[:-1]
        minimal["tools"] = tools
        candidate = _dumps(minimal)
        if len(candidate) <= target:
            return candidate
    smallest = min(candidates, key=len) if candidates else full
    return smallest if len(smallest) < len(full) else full


def _regex_schema_summary(text: str) -> str | None:
    names: list[str] = []
    for match in re.finditer(r'"(?:tool|name|tool_name)"\s*:\s*"([^"]+)"', text):
        if match.group(1) not in names:
            names.append(match.group(1))
    if not names:
        return None
    summary: dict[str, Any] = {}
    ns = re.search(r'"namespace"\s*:\s*"([^"]+)"', text)
    if ns:
        summary["namespace"] = ns.group(1)
    summary["tool_names"] = names
    summary["tools"] = []
    summary["compacted"] = True
    return _dumps(summary)


def _compact_toolsearch_content(
    text: str, invoked_names: set[str]
) -> str | None:
    parsed, ok = _parse_json(text)
    if ok and _looks_like_schema_dump(parsed):
        summary = _build_schema_summary(parsed, invoked_names)
        if summary is None:
            return None
        compacted = _dumps(summary)
        return compacted if len(compacted) < len(text) else None
    if ok and isinstance(parsed, dict) and ("tools" in parsed or "matches" in parsed):
        summary = _build_schema_summary(parsed, invoked_names)
        if summary is not None:
            compacted = _dumps(summary)
            return compacted if len(compacted) < len(text) else None
        return None
    # Unparseable dump from a known ToolSearch call: keep names as JSON.
    return _regex_schema_summary(text)


# ---------------------------------------------------------------------------
# Generic single-observation compaction (pass 2 entry point)
# ---------------------------------------------------------------------------

def _compact_content(text: str, target: int) -> str:
    if len(text) <= target:
        return text
    parsed, ok = _parse_json(text)
    # Only treat as JSON when the *whole* text parses; a leading-object
    # prefix with trailing data is handled by the ToolSearch pass, and
    # generic head+tail is safer for other embedded-JSON text.
    whole_json = False
    if ok:
        stripped = text.strip()
        if stripped.startswith(("{", "[")):
            try:
                json.loads(stripped)
                whole_json = True
            except (json.JSONDecodeError, ValueError):
                whole_json = False
    if whole_json and isinstance(parsed, (dict, list)):
        return _compact_json_text(parsed, text, target)
    return _compact_plain_text(text, target)


def _recompact_content(
    original_text: str,
    target: int,
    *,
    is_toolsearch: bool,
    invoked_names: set[str],
) -> str:
    if is_toolsearch:
        parsed, ok = _parse_json(original_text)
        if ok and _looks_like_schema_dump(parsed):
            shrunk = _shrink_schema_summary(parsed, target, invoked_names)
            if shrunk is not None:
                return shrunk
        compacted = _compact_toolsearch_content(original_text, invoked_names)
        if compacted is not None:
            # The pass-1 summary may still exceed this tighter target.
            parsed_summary, ok_summary = _parse_json(compacted)
            if ok_summary and len(compacted) > target:
                shrunk = _shrink_schema_summary(parsed_summary, target, invoked_names)
                if shrunk is not None:
                    return shrunk
            return compacted
    return _compact_content(original_text, target)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compact_trajectory_messages(
    messages: list[dict],
    *,
    max_seq_tokens: int = 24576,
    target_obs_chars: int = 1200,
) -> dict:
    """Compact tool observations in ``messages`` without touching assistants.

    Returns a dict with the compacted ``messages`` plus token accounting:
    ``original_tokens``, ``compacted_tokens``, ``assistant_tokens_before``,
    ``assistant_tokens_after``, ``fits_max_seq_tokens`` and
    ``compacted_tool_turns``.  Assistant messages are byte-for-byte
    identical to the input and no message is added, dropped or reordered.
    """
    if max_seq_tokens <= 0:
        raise ValueError("max_seq_tokens must be positive")
    if target_obs_chars <= 0:
        raise ValueError("target_obs_chars must be positive")

    original_messages = list(messages)
    original_tokens = _total_tokens(original_messages)
    assistant_before = _assistant_tokens(original_messages)

    compacted = copy.deepcopy(original_messages)
    # Keep non-assistant approx_tokens estimates consistent with content;
    # assistant messages are never modified in any field.
    for message in compacted:
        if message.get("role") != "assistant":
            message["approx_tokens"] = _message_tokens(message)

    # Tools actually invoked anywhere in this rollout, and (in order) the
    # tool name answering each tool_call_id seen so far.
    invoked_names: set[str] = set()
    for message in original_messages:
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if isinstance(call, dict):
                fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                name = fn.get("name")
                if isinstance(name, str) and name:
                    invoked_names.add(name)

    original_tool_text: dict[int, str] = {}
    toolsearch_indices: set[int] = set()
    call_name_by_id: dict[str, str] = {}

    # ---- Pass 1: ToolSearch / schema dumps -------------------------------
    for idx, message in enumerate(compacted):
        role = message.get("role")
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                if isinstance(call, dict) and call.get("id"):
                    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                    if isinstance(fn.get("name"), str):
                        call_name_by_id[str(call["id"])] = fn["name"]
            continue
        if role != "tool":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        original_tool_text[idx] = content
        call_name = call_name_by_id.get(str(message.get("tool_call_id") or ""))
        parsed, ok = _parse_json(content)
        is_dump = bool(ok and _looks_like_schema_dump(parsed))
        if _is_toolsearch_call(call_name) or is_dump:
            toolsearch_indices.add(idx)
            replacement = _compact_toolsearch_content(content, invoked_names)
            if replacement is not None and replacement != content:
                _set_content(message, replacement)

    # ---- Pass 2: remaining long tool observations ------------------------
    for idx, message in enumerate(compacted):
        if message.get("role") != "tool" or idx in toolsearch_indices:
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        original_tool_text.setdefault(idx, content)
        if len(content) > target_obs_chars:
            replacement = _compact_content(content, target_obs_chars)
            if replacement != content and len(replacement) < len(content):
                _set_content(message, replacement)

    # ---- Pass 3: tighten earliest phases until the budget fits ----------
    # compacted_tokens is defined by the same per-message estimate used
    # for original_tokens; pass 3 uses it so both figures are comparable.
    if _total_tokens(compacted) > max_seq_tokens:
        if target_obs_chars > 256:
            stage_targets = sorted(
                {int(target_obs_chars), 1024, 800, 600, 400, 256}, reverse=True
            )
        else:
            stage_targets = [int(target_obs_chars)]
        candidate_indices = [
            idx
            for idx, m in enumerate(compacted)
            if m.get("role") == "tool" and m.get("causal_phase") in EARLY_PHASES
        ]
        # Secondary sweep: any tool turn not in a protected final phase.
        candidate_indices += [
            idx
            for idx, m in enumerate(compacted)
            if m.get("role") == "tool"
            and m.get("causal_phase") not in PROTECTED_PHASES
            and idx not in candidate_indices
        ]
        for idx in candidate_indices:
            if _total_tokens(compacted) <= max_seq_tokens:
                break
            message = compacted[idx]
            original_text = original_tool_text.get(idx)
            if original_text is None:
                content_now = message.get("content")
                if not isinstance(content_now, str):
                    continue
                original_text = content_now
            for stage in stage_targets:
                if _total_tokens(compacted) <= max_seq_tokens:
                    break
                current = message.get("content")
                if not isinstance(current, str):
                    break
                if stage >= len(current) and idx not in toolsearch_indices:
                    continue
                replacement = _recompact_content(
                    original_text,
                    stage,
                    is_toolsearch=idx in toolsearch_indices,
                    invoked_names=invoked_names,
                )
                if replacement != current and len(replacement) < len(current):
                    _set_content(message, replacement)

    compacted_tokens = _total_tokens(compacted)
    assistant_after = _assistant_tokens(compacted)
    compacted_tool_turns = sum(
        1
        for before, after in zip(original_messages, compacted)
        if before.get("role") == "tool" and before.get("content") != after.get("content")
    )
    return {
        "messages": compacted,
        "original_tokens": int(original_tokens),
        "compacted_tokens": int(compacted_tokens),
        "assistant_tokens_before": int(assistant_before),
        "assistant_tokens_after": int(assistant_after),
        "fits_max_seq_tokens": bool(compacted_tokens <= max_seq_tokens),
        "compacted_tool_turns": int(compacted_tool_turns),
    }


def _token_loss_mask_rle(messages: list[dict[str, Any]]) -> list[list[int]]:
    """Standalone copy of the exporter's run-length encoding."""
    runs: list[list[int]] = []
    for message in messages:
        if "loss_mask" in message:
            mask = int(message["loss_mask"])
        else:
            mask = 1 if message.get("role") == "assistant" else 0
        stored = message.get("approx_tokens")
        tokens = int(stored) if isinstance(stored, int) and stored >= 0 else _message_tokens(message)
        if runs and runs[-1][0] == mask:
            runs[-1][1] += tokens
        else:
            runs.append([mask, tokens])
    return runs


def compact_jsonl_file(
    in_path: Path,
    out_path: Path,
    *,
    max_seq_tokens: int = 24576,
) -> dict:
    """Compact every row of a trajectory JSONL file; return corpus stats.

    Each row's ``messages`` are replaced by their compacted form,
    ``loss_mask`` is re-aligned to the messages and
    ``token_loss_mask_rle`` is recomputed.  All other row fields are
    preserved.  Assistant messages remain byte-for-byte identical.
    """
    in_path = Path(in_path)
    out_path = Path(out_path)
    rows_in = [
        json.loads(line)
        for line in in_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    out_rows: list[dict[str, Any]] = []
    original_totals: list[int] = []
    compacted_totals: list[int] = []
    total_compacted_tool_turns = 0
    fits = 0
    assistant_before_sum = 0
    assistant_after_sum = 0

    for row in rows_in:
        if not isinstance(row, dict):
            out_rows.append(row)
            continue
        messages = row.get("messages")
        if not isinstance(messages, list):
            out_rows.append(row)
            continue
        result = compact_trajectory_messages(messages, max_seq_tokens=max_seq_tokens)
        new_messages = result["messages"]
        # Guarantee an approx_tokens estimate on every message so the
        # recomputed RLE lines up with sum(m["approx_tokens"]).
        for message in new_messages:
            if not isinstance(message.get("approx_tokens"), int):
                message["approx_tokens"] = _message_tokens(message)
        new_row = dict(row)
        new_row["messages"] = new_messages
        new_row["loss_mask"] = [
            int(m["loss_mask"]) if "loss_mask" in m else (1 if m.get("role") == "assistant" else 0)
            for m in new_messages
        ]
        new_row["token_loss_mask_rle"] = _token_loss_mask_rle(new_messages)
        out_rows.append(new_row)

        original_totals.append(int(result["original_tokens"]))
        compacted_totals.append(int(result["compacted_tokens"]))
        total_compacted_tool_turns += int(result["compacted_tool_turns"])
        assistant_before_sum += int(result["assistant_tokens_before"])
        assistant_after_sum += int(result["assistant_tokens_after"])
        if result["fits_max_seq_tokens"]:
            fits += 1

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in out_rows),
        encoding="utf-8",
    )

    return {
        "rows": len(out_rows),
        "row_count": len(out_rows),
        "original_tokens": sum(original_totals),
        "compacted_tokens": sum(compacted_totals),
        "original_tokens_min": min(original_totals) if original_totals else 0,
        "original_tokens_max": max(original_totals) if original_totals else 0,
        "compacted_tokens_min": min(compacted_totals) if compacted_totals else 0,
        "compacted_tokens_max": max(compacted_totals) if compacted_totals else 0,
        "assistant_tokens_before": assistant_before_sum,
        "assistant_tokens_after": assistant_after_sum,
        "compacted_tool_turns": total_compacted_tool_turns,
        "fits_max_seq_tokens": bool(fits == len(out_rows)),
        "fits_max_seq_tokens_count": fits,
        "rows_fitting": fits,
        "fitting_rows": fits,
        "all_fits_max_seq_tokens": bool(fits == len(out_rows)),
        "max_seq_tokens": int(max_seq_tokens),
    }
