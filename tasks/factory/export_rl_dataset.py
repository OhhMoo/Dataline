#!/usr/bin/env python3
"""Export modeled frontier rollouts as verl GRPO / TRL SFT training datasets.

Joins `model_certified_suite` (rewards, GRPO/RLOO/phase advantages, SFT weights)
and `compute_twin_contrastive_advantages` (governance twin penalties) with the
re-read rollout transcripts, and writes:
  1. `verl_grpo_trajectories.jsonl`: one row per rollout with OpenAI/TRL-style
     chat messages, per-message `loss_mask` and causal phase, token loss-mask
     run-lengths, scalar rewards and all advantage estimates,
  2. `sft_rejection_sampled.jsonl`: passing rollouts only, with SFT weights,
  3. `dataset_card.json`: provenance, counts, token totals, phase token shares,
     reward/advantage statistics, invariant checks and file digests,
  4. `train_grpo.jsonl`, `train_sft.jsonl`, `eval_holdout_grpo.jsonl`: the same
     rows split by twin group, so no epistemic twin of an evaluation task is
     trained on.
  5. `train_dpo_pairs.jsonl`: same-task preference pairs (chosen reward 1.0 vs
     a lower-reward rejected trajectory, split == "train" only).

With `--compact-max-tokens 24576` tool observations are compacted before
writing (assistant turns byte-identical), and `--extra-runs-roots` merges
cross-model / counterfactual JSONL trajectories into the training groups.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from tasks.factory import trajectory_loss_model as loss_model
from tasks.factory.paths import FACTORY_ROOT, resolve_factory_path
from tasks.factory.trajectory_loss_model import (
    CAUSAL_PHASES,
    SEVEN_KEYS,
    classify_tool_phase,
    compute_twin_contrastive_advantages,
    model_certified_suite,
)

SCHEMA_VERSION = 1
GRPO_FILE = "verl_grpo_trajectories.jsonl"
SFT_FILE = "sft_rejection_sampled.jsonl"
CARD_FILE = "dataset_card.json"
TRAIN_GRPO_FILE = "train_grpo.jsonl"
TRAIN_SFT_FILE = "train_sft.jsonl"
TRAIN_DPO_FILE = "train_dpo_pairs.jsonl"
DPO_FILE = TRAIN_DPO_FILE
EVAL_GRPO_FILE = "eval_holdout_grpo.jsonl"
TRAIN_SPLIT = "train"
EVAL_SPLIT = "eval_holdout"
# The 3 epistemic triplets of the cross-model evaluation matrix.
EVAL_HOLDOUT_TASKS = (
    "FT0100-commerce-analytics-ft02-auth-b",
    "FT0101-commerce-analytics-ft02-hold-b",
    "FT0102-commerce-analytics-ft02-esc-b",
    "FT0193-revenue-daily-ft03-auth-a",
    "FT0194-revenue-daily-ft03-hold-a",
    "FT0195-revenue-daily-ft03-esc-a",
    "FT0484-excel-close-ft06-auth-b",
    "FT0485-excel-close-ft06-hold-b",
    "FT0486-excel-close-ft06-esc-b",
)
TOKENIZER_NOTE = (
    "approx_tokens = max(1, ceil(chars / 4)) over content + reasoning_content + tool-call "
    "arguments; a character heuristic, not a model tokenizer. Re-tokenize before training."
)
GROUP_META_KEYS = ("governance_mode", "theme", "fault_topology_id", "twin_group_id")
# Advantages arrive rounded to 6 decimals, so a group sum may drift by up to k * 5e-7.
_ADV_SUM_TOL_PER_ROLLOUT = 1e-6

FIELDS = {
    GRPO_FILE: {
        "id": "<task_id>/<run_id>/<rollout_id>",
        "task_id": "Pilot task directory name",
        "run_id": "Run identifier from the run's report.json",
        "rollout_id": "Rollout directory name (r0, r1, ...)",
        "episode_id": "Harness episode id from rollout.json",
        "theme": "Blueprint theme",
        "fault_topology_id": "Blueprint fault topology (FT01..)",
        "governance_mode": "act-authorized | withhold-blocked | escalate-ambiguous",
        "role": "Task matrix role (control | decision)",
        "twin_group_id": "Governance twin group shared across modes of one scenario",
        "split": "train | eval_holdout (whole twin groups go to one side)",
        "prompt": "Task prompt given to the agent (prompt.txt, else first user message)",
        "messages": "OpenAI/TRL chat messages with loss_mask, causal_phase, approx_tokens (tool: is_error when set)",
        "loss_mask": "Per-message mask aligned with messages (1 = assistant)",
        "token_loss_mask_rle": "[[mask, approx_tokens], ...] runs over messages",
        "reward": "3-tier scalar reward (band + 0.5*progress + path)",
        "reward_components": "Reward decomposition",
        "outcome_class": "Classified governance outcome",
        "passed": "Grader pass flag",
        "total_points": "Grader points",
        "max_points": "Grader maximum points",
        "seven_step_progress": "q0..q6 continuous progress quantities",
        "progress_scalar": "Weighted progress scalar in [0, ceiling]",
        "grpo_advantage": "Within-group (r - mean) / std",
        "rloo_advantage": "Leave-one-out advantage",
        "twin_contrastive_advantage": "GRPO advantage minus centered governance bias penalty",
        "policy_bias_penalty": "Mode-unconditional action/refusal bias penalty",
        "bias_kind": "action_bias | refusal_bias | null",
        "phase_step_advantages": "Per causal phase blended outcome/process advantage",
        "dapo_clip_bounds": "[1 - eps_low, 1 + eps_high]",
        "group": "{k, pass_at_1, non_collapsed}",
    },
    SFT_FILE: {
        "id": "<task_id>/<run_id>/<rollout_id>",
        "task_id": "Pilot task directory name",
        "governance_mode": "Blueprint governance mode",
        "role": "Task matrix role",
        "split": "train | eval_holdout",
        "prompt": "Task prompt given to the agent",
        "messages": "OpenAI/TRL chat messages (train on loss_mask == 1)",
        "loss_mask": "Per-message mask aligned with messages",
        "token_loss_mask_rle": "[[mask, approx_tokens], ...] runs over messages",
        "sft_trajectory_weight": "Share of the group's passing assistant tokens",
        "reward": "3-tier scalar reward",
    },
    TRAIN_DPO_FILE: {
        "id": "<chosen_id>::dpo::<rejected_id>",
        "task_id": "Training task shared by chosen and rejected",
        "chosen": "Full train trajectory row with reward == 1.0",
        "rejected": "Full train trajectory row on the same task with lower reward",
        "chosen_id": "Chosen trajectory id",
        "rejected_id": "Rejected trajectory id",
        "reward_chosen": "Chosen 3-tier scalar reward (1.0)",
        "reward_rejected": "Rejected 3-tier scalar reward (< reward_chosen)",
        "reward_margin": "reward_chosen - reward_rejected (> 0)",
        "progress_chosen": "Chosen weighted 7-step progress scalar",
        "progress_rejected": "Rejected weighted 7-step progress scalar",
        "progress_margin": "progress_chosen - progress_rejected",
        "split": "train (eval holdout tasks are never paired)",
    },
}


def _approx_tokens(chars: int) -> int:
    return max(1, math.ceil(chars / 4))


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
            else:
                parts.append(json.dumps(block, ensure_ascii=False, sort_keys=True))
        return "\n".join(parts)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def _finalize(message: dict[str, Any], phase: str) -> dict[str, Any]:
    chars = len(message.get("content") or "") + len(message.get("reasoning_content") or "")
    for call in message.get("tool_calls") or []:
        chars += len(call["function"]["arguments"])
    message["loss_mask"] = 1 if message["role"] == "assistant" else 0
    message["causal_phase"] = phase
    message["approx_tokens"] = _approx_tokens(chars)
    return message


def transcript_messages(transcript_path: Path) -> list[dict[str, Any]]:
    """Convert a stream-json transcript into observation-masked OpenAI/TRL chat messages."""
    transcript_path = Path(transcript_path)
    if not transcript_path.is_file():
        return []

    messages: list[dict[str, Any]] = []
    current_phase = "discovery"
    for raw_line in transcript_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        ev_type = str(event.get("type") or "")
        # `result` repeats the concatenated assistant text; it is not a turn.
        if ev_type in ("system", "result"):
            continue
        msg = event.get("message")
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or ev_type)
        content = msg.get("content")

        if role == "assistant":
            items = content if isinstance(content, list) else [{"type": "text", "text": str(content or "")}]
            texts: list[str] = []
            thinking: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                btype = str(item.get("type") or "text")
                if btype == "thinking":
                    thinking.append(str(item.get("thinking") or ""))
                elif btype == "tool_use":
                    tool_input = item.get("input") if isinstance(item.get("input"), dict) else {}
                    current_phase = classify_tool_phase(str(item.get("name") or ""), tool_input)
                    tool_calls.append({
                        "id": str(item.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": loss_model._normalize_tool_name(str(item.get("name") or "")),
                            "arguments": json.dumps(item.get("input") if item.get("input") is not None else {},
                                                    ensure_ascii=False),
                        },
                    })
                else:
                    texts.append(str(item.get("text") or ""))
            message: dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(texts),
                "reasoning_content": "\n".join(thinking) if thinking else None,
            }
            if tool_calls:
                message["tool_calls"] = tool_calls
            messages.append(_finalize(message, current_phase))
            continue

        if not isinstance(content, list):
            messages.append(_finalize({"role": "user", "content": str(content or "")}, current_phase))
            continue
        pending_text: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_result":
                if pending_text:
                    messages.append(_finalize({"role": "user", "content": "\n".join(pending_text)}, current_phase))
                    pending_text = []
                tool_message = {
                    "role": "tool",
                    "tool_call_id": str(item.get("tool_use_id") or ""),
                    "content": _tool_result_text(item.get("content")),
                }
                if item.get("is_error") is True:
                    tool_message["is_error"] = True
                messages.append(_finalize(tool_message, current_phase))
            elif isinstance(item, dict):
                pending_text.append(str(item.get("text") or item.get("content") or ""))
            else:
                pending_text.append(str(item))
        if pending_text:
            messages.append(_finalize({"role": "user", "content": "\n".join(pending_text)}, current_phase))
    return messages


def token_loss_mask_rle(messages: list[dict[str, Any]]) -> list[list[int]]:
    """Run-length encode per-message loss masks into [[mask, approx_tokens], ...]."""
    runs: list[list[int]] = []
    for message in messages:
        mask = int(message["loss_mask"])
        tokens = int(message["approx_tokens"])
        if runs and runs[-1][0] == mask:
            runs[-1][1] += tokens
        else:
            runs.append([mask, tokens])
    return runs


def pair_tool_results(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    """Drop tool results that answer no earlier assistant tool_call; return (kept, dropped ids)."""
    issued: set[str] = set()
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    for message in messages:
        if message["role"] == "assistant":
            issued.update(call["id"] for call in message.get("tool_calls") or [])
        elif message["role"] == "tool" and message.get("tool_call_id") not in issued:
            dropped.append(str(message.get("tool_call_id") or ""))
            continue
        kept.append(message)
    return kept, dropped


_COMPACT_MARKER = "\n...[observation compacted]...\n"


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False, sort_keys=True)


def _message_tokens_from_chars(message: dict[str, Any]) -> int:
    chars = len(_content_text(message.get("content"))) + len(_content_text(message.get("reasoning_content")))
    if message.get("reasoning_content") is None:
        chars -= len(_content_text(None))
    for call in message.get("tool_calls") or []:
        try:
            chars += len(str(call["function"]["arguments"]))
        except (KeyError, TypeError):
            chars += len(json.dumps(call, ensure_ascii=False, sort_keys=True))
    return _approx_tokens(chars)


def _head_tail_compact(content: str, target_chars: int) -> str:
    if len(content) <= target_chars:
        return content
    if target_chars <= len(_COMPACT_MARKER) + 8:
        return content[:target_chars]
    keep = target_chars - len(_COMPACT_MARKER)
    half = max(1, keep // 2)
    return content[:half] + _COMPACT_MARKER + content[-(keep - half):]


def compact_messages_fallback(
    messages: list[dict[str, Any]], *, max_seq_tokens: int = 24576, target_obs_chars: int = 1200
) -> dict[str, Any]:
    """Built-in observation compaction used when `observation_compactor` is unavailable.

    Only ``role == "tool"`` contents change. Assistant messages (content,
    reasoning and tool calls) are carried over byte-for-byte; their dicts are
    not rewritten at all except that no field of them is touched. ToolSearch
    schema dumps become a name summary, other long observations keep their
    head and tail, and -- if the trajectory is still over budget -- the oldest
    tool observations are tightened first (down to 256 chars, then to a short
    placeholder) so tail repair/report turns are never the thing removed.
    """
    if max_seq_tokens < 1:
        raise ValueError("max_seq_tokens must be positive")
    originals = [m.get("content") for m in messages]
    compacted: list[dict[str, Any]] = []
    for message in messages:
        # Shallow copy only: every value (str / tool_calls) is shared with the
        # input and never mutated, so assistant turns stay byte-identical.
        compacted.append(dict(message))

    def _total() -> int:
        return sum(_message_tokens_from_chars(m) for m in compacted)

    def _set_tool(index: int, new_content: str) -> bool:
        message = compacted[index]
        if message.get("content") == new_content:
            return False
        message["content"] = new_content
        message["approx_tokens"] = _message_tokens_from_chars(message)
        return True

    compacted_tool_turns = 0
    # Pass 1: ToolSearch-style dumps (many named schemas in one observation).
    invoked = [
        str(call.get("function", {}).get("name") or "")
        for m in messages if m.get("role") == "assistant"
        for call in (m.get("tool_calls") or []) if isinstance(call, dict)
    ]
    for i, message in enumerate(compacted):
        if message.get("role") != "tool" or not isinstance(message.get("content"), str):
            continue
        content = message["content"]
        names = re.findall(r'"name"\s*:\s*"([^"]+)"', content)
        if len(names) >= 8 and len(content) > target_obs_chars:
            summary = json.dumps(
                {
                    "compacted_tool_search": True,
                    "tool_names": list(dict.fromkeys(names)),
                    "invoked_tools": list(dict.fromkeys(n for n in invoked if n)),
                    "note": "[observation compacted]",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            if len(summary) < len(content):
                compacted_tool_turns += int(_set_tool(i, summary))
    # Pass 2: head+tail for any remaining long tool observation.
    for i, message in enumerate(compacted):
        if message.get("role") != "tool" or not isinstance(message.get("content"), str):
            continue
        if len(message["content"]) > target_obs_chars:
            compacted_tool_turns += int(_set_tool(i, _head_tail_compact(message["content"], target_obs_chars)))
    # Pass 3: tighten oldest tool observations first until the row fits.
    target = target_obs_chars
    while _total() > max_seq_tokens and target > 256:
        target = max(256, target // 2)
        for i, message in enumerate(compacted):
            if message.get("role") != "tool" or not isinstance(originals[i], str):
                continue
            candidate = _head_tail_compact(str(originals[i]), target)
            if len(candidate) < len(_content_text(message.get("content"))):
                compacted_tool_turns += int(_set_tool(i, candidate))
            if _total() <= max_seq_tokens:
                break
    if _total() > max_seq_tokens:
        for i, message in enumerate(compacted):
            if message.get("role") != "tool":
                continue
            compacted_tool_turns += int(_set_tool(i, "[observation compacted]"))
            if _total() <= max_seq_tokens:
                break

    original_tokens = sum(_message_tokens_from_chars(m) for m in messages)
    compacted_tokens = _total()
    assistant_before = sum(_message_tokens_from_chars(m) for m in messages if m.get("role") == "assistant")
    assistant_after = sum(_message_tokens_from_chars(m) for m in compacted if m.get("role") == "assistant")
    return {
        "messages": compacted,
        "original_tokens": original_tokens,
        "compacted_tokens": compacted_tokens,
        "assistant_tokens_before": assistant_before,
        "assistant_tokens_after": assistant_after,
        "fits_max_seq_tokens": compacted_tokens <= max_seq_tokens,
        "compacted_tool_turns": compacted_tool_turns,
    }


# Backwards-friendly alias mirroring the observation_compactor result shape.
compact_trajectory_messages_fallback = compact_messages_fallback


def _compaction_candidate_ok(original: list[dict[str, Any]], candidate: Any) -> bool:
    if not isinstance(candidate, list) or len(candidate) != len(original):
        return False
    for before, after in zip(original, candidate):
        if not isinstance(after, dict) or set(after) != set(before):
            return False
        if after.get("role") != before.get("role"):
            return False
        if before.get("role") == "assistant" and json.dumps(before, ensure_ascii=False, sort_keys=True) != json.dumps(after, ensure_ascii=False, sort_keys=True):
            return False
        if before.get("role") == "tool" and after.get("tool_call_id") != before.get("tool_call_id"):
            return False
    return True


def apply_observation_compaction(
    messages: list[dict[str, Any]], *, max_seq_tokens: int = 24576, target_obs_chars: int = 1200
) -> dict[str, Any]:
    """Compact tool observations via observation_compactor when available, else the fallback.

    Whichever path runs, the result is validated: assistant messages must be
    byte-for-byte identical and every message must keep its original key set.
    A compactor that violates either invariant is discarded for the fallback.
    """
    external = None
    try:  # Agent 1's module is built concurrently; import defensively.
        from tasks.factory.observation_compactor import compact_trajectory_messages as external  # type: ignore
    except Exception:
        external = None
    if external is not None:
        try:
            result = external(copy.deepcopy(messages), max_seq_tokens=int(max_seq_tokens), target_obs_chars=int(target_obs_chars))
            candidate = result.get("messages") if isinstance(result, dict) else None
            if _compaction_candidate_ok(messages, candidate):
                total = sum(int(m.get("approx_tokens") or _message_tokens_from_chars(m)) for m in candidate)
                return {
                    "messages": candidate,
                    "original_tokens": int(result.get("original_tokens", sum(_message_tokens_from_chars(m) for m in messages))),
                    "compacted_tokens": int(result.get("compacted_tokens", total)),
                    "assistant_tokens_before": int(result.get("assistant_tokens_before", sum(_message_tokens_from_chars(m) for m in messages if m.get("role") == "assistant"))),
                    "assistant_tokens_after": int(result.get("assistant_tokens_after", sum(_message_tokens_from_chars(m) for m in candidate if m.get("role") == "assistant"))),
                    "fits_max_seq_tokens": bool(result.get("fits_max_seq_tokens", total <= max_seq_tokens)),
                    "compacted_tool_turns": int(result.get("compacted_tool_turns", 0)),
                    "compactor": "observation_compactor",
                }
        except Exception:
            pass
    result = compact_messages_fallback(messages, max_seq_tokens=int(max_seq_tokens), target_obs_chars=int(target_obs_chars))
    result["compactor"] = "builtin_fallback"
    return result


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: copy.deepcopy(v) for k, v in row.items() if not str(k).startswith("_")}


def build_dpo_pairs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pair each lower-reward train trajectory with the best reward==1.0 trajectory on its task.

    One pair per rejected trajectory (chosen = highest-progress passing
    trajectory, ties broken by id) keeps the file linear in group size while
    guaranteeing ``reward_margin > 0`` on every pair.
    """
    by_task: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("split") == TRAIN_SPLIT:
            by_task.setdefault(str(row["task_id"]), []).append(row)
    pairs: list[dict[str, Any]] = []
    for task_id in sorted(by_task):
        group = by_task[task_id]
        chosen_candidates = sorted(
            (r for r in group if float(r.get("reward") or 0.0) == 1.0),
            key=lambda r: (-float(r.get("progress_scalar") or 0.0), str(r["id"])),
        )
        if not chosen_candidates:
            continue
        chosen = chosen_candidates[0]
        rejected_rows = sorted(
            (r for r in group if float(r.get("reward") or 0.0) < 1.0),
            key=lambda r: (float(r.get("reward") or 0.0), str(r["id"])),
        )
        for rejected in rejected_rows:
            reward_chosen = float(chosen.get("reward") or 0.0)
            reward_rejected = float(rejected.get("reward") or 0.0)
            if not reward_chosen > reward_rejected:
                continue
            progress_chosen = float(chosen.get("progress_scalar") or 0.0)
            progress_rejected = float(rejected.get("progress_scalar") or 0.0)
            pairs.append({
                "id": f"{chosen['id']}::dpo::{rejected['id']}",
                "task_id": task_id,
                "chosen": _public_row(chosen),
                "rejected": _public_row(rejected),
                "chosen_id": str(chosen["id"]),
                "rejected_id": str(rejected["id"]),
                "reward_chosen": reward_chosen,
                "reward_rejected": reward_rejected,
                "reward_margin": round(reward_chosen - reward_rejected, 6),
                "progress_chosen": progress_chosen,
                "progress_rejected": progress_rejected,
                "progress_margin": round(progress_chosen - progress_rejected, 6),
                "split": TRAIN_SPLIT,
            })
    pairs.sort(key=lambda p: (p["task_id"], p["rejected_id"], p["chosen_id"]))
    return pairs


def _normalise_extra_row(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise a cross-model / counterfactual trajectory row to the GRPO row shape."""
    if not isinstance(raw, dict) or not isinstance(raw.get("messages"), list) or not raw["messages"]:
        return None
    task_id = str(raw.get("task_id") or "")
    if not task_id or task_id in EVAL_HOLDOUT_TASKS or raw.get("split") == EVAL_SPLIT:
        return None
    if "reward" not in raw:
        return None
    messages: list[dict[str, Any]] = []
    for message in raw["messages"]:
        if not isinstance(message, dict) or "role" not in message:
            return None
        copied = copy.deepcopy(message)
        if not all(k in copied for k in ("loss_mask", "causal_phase", "approx_tokens")):
            copied = _finalize(copied, str(copied.get("causal_phase") or "discovery"))
        messages.append(copied)
    messages, dropped = pair_tool_results(messages)
    if not messages:
        return None
    reward = float(raw.get("reward") or 0.0)
    seven_raw = raw.get("seven_step_progress") if isinstance(raw.get("seven_step_progress"), dict) else {}
    seven = {k: float(seven_raw.get(k) or 0.0) for k in SEVEN_KEYS}
    progress = float(raw.get("progress_scalar") if raw.get("progress_scalar") is not None else (sum(seven.values()) / len(SEVEN_KEYS)))
    rollout_id = str(raw.get("rollout_id") or (str(raw.get("id", "")).rsplit("/", 1)[-1] or "extra0"))
    run_id = str(raw.get("run_id") or "extra")
    row_id = str(raw.get("id") or f"{task_id}/{run_id}/{rollout_id}")
    passed = bool(raw.get("passed")) if raw.get("passed") is not None else reward >= 1.0
    return {
        "id": row_id,
        "task_id": task_id,
        "run_id": run_id,
        "rollout_id": rollout_id,
        "episode_id": raw.get("episode_id"),
        "governance_mode": raw.get("governance_mode"),
        "theme": raw.get("theme"),
        "fault_topology_id": raw.get("fault_topology_id"),
        "twin_group_id": raw.get("twin_group_id"),
        "role": raw.get("role") or "decision",
        "prompt": raw.get("prompt") or "",
        "messages": messages,
        "loss_mask": [int(m["loss_mask"]) for m in messages],
        "token_loss_mask_rle": token_loss_mask_rle(messages),
        "reward": reward,
        "reward_components": raw.get("reward_components") or {},
        "outcome_class": raw.get("outcome_class") or ("resolved" if passed else "acted_unresolved"),
        "passed": passed,
        "total_points": raw.get("total_points") if raw.get("total_points") is not None else 0.0,
        "max_points": raw.get("max_points") if raw.get("max_points") is not None else 20.0,
        "seven_step_progress": seven,
        "progress_scalar": progress,
        "grpo_advantage": float(raw.get("grpo_advantage") or 0.0),
        "rloo_advantage": float(raw.get("rloo_advantage") or 0.0),
        "twin_contrastive_advantage": float(raw.get("twin_contrastive_advantage") or 0.0),
        "policy_bias_penalty": float(raw.get("policy_bias_penalty") or 0.0),
        "bias_kind": raw.get("bias_kind"),
        "phase_step_advantages": raw.get("phase_step_advantages") or {p: 0.0 for p in CAUSAL_PHASES},
        "dapo_clip_bounds": raw.get("dapo_clip_bounds") or [0.8, 1.28],
        "group": raw.get("group") or {"k": 0, "pass_at_1": 0.0, "non_collapsed": False},
        "_sft_trajectory_weight": float(raw.get("sft_trajectory_weight") or raw.get("_sft_trajectory_weight") or 0.0),
        "_dropped_orphans": dropped,
    }


def load_extra_trajectory_rows(extra_runs_roots: list[Path] | None) -> list[dict[str, Any]]:
    """Load extra trajectory rows from JSONL files under each root (recursively).

    A root may be a single JSONL file, an exported dataset directory
    (``train_grpo.jsonl`` etc.), a desaturated-groups directory
    (``desaturated_train_grpo.jsonl`` / ``desaturated_train_dpo_pairs.jsonl``,
    whose chosen/rejected rows are also unfolded), or a cross-model runs
    directory containing such files. Rows are de-duplicated by ``id`` and
    eval-holdout tasks are always dropped. If a root holds no JSONL rows and
    `tasks.factory.desaturate_training_groups` happens to be importable, its
    groups are *not* rebuilt here -- that module writes the JSONL above, which
    is the supported interchange format.
    """
    if not extra_runs_roots:
        return []
    try:  # Optional concurrent module: presence is noted, never required.
        import importlib.util as _ilu
        _desaturate_available = _ilu.find_spec("tasks.factory.desaturate_training_groups") is not None
    except Exception:
        _desaturate_available = False
    _ = _desaturate_available
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_files: set[Path] = set()
    for root in extra_runs_roots:
        root = Path(root)
        if not root.exists():
            raise FileNotFoundError(f"extra runs root does not exist: {root}")
        files = [root] if root.is_file() else sorted(p for p in root.rglob("*.jsonl") if p.is_file())
        for path in files:
            resolved = path.resolve()
            if resolved in seen_files:
                continue
            seen_files.add(resolved)
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                candidates: list[Any] = [obj]
                if isinstance(obj, dict) and isinstance(obj.get("chosen"), dict) and isinstance(obj.get("rejected"), dict):
                    candidates = [obj["chosen"], obj["rejected"]]
                for candidate in candidates:
                    try:
                        row = _normalise_extra_row(candidate) if isinstance(candidate, dict) else None
                    except (KeyError, TypeError, ValueError):
                        row = None
                    if row is None or row["id"] in seen_ids:
                        continue
                    seen_ids.add(row["id"])
                    rows.append(row)
    return rows


def _recompute_merged_groups(rows: list[dict[str, Any]], task_ids: set[str], *, lambda_twin: float = 0.5) -> None:
    """Recompute GRPO/RLOO/twin/phase advantages and SFT weights for merged groups in place."""
    for task_id in sorted(task_ids):
        group = [r for r in rows if r["task_id"] == task_id]
        if not group:
            continue
        rewards = [float(r["reward"]) for r in group]
        k = len(rewards)
        mean = sum(rewards) / k if k else 0.0
        var = sum((x - mean) ** 2 for x in rewards) / k if k else 0.0
        std = math.sqrt(var)
        collapsed = var <= 1e-12 or k <= 1
        penalties = [float(r.get("policy_bias_penalty") or 0.0) for r in group]
        mean_pen = sum(penalties) / k if k else 0.0
        passing_tokens = sum(
            int(m.get("approx_tokens") or 0)
            for r in group if r.get("passed") is True
            for m in r["messages"] if m.get("role") == "assistant"
        )
        total = sum(rewards)
        for r in group:
            reward = float(r["reward"])
            grpo = 0.0 if collapsed else (reward - mean) / (std + 1e-8)
            rloo = reward - (total - reward) / (k - 1) if k > 1 else 0.0
            r["grpo_advantage"] = round(grpo, 6)
            r["rloo_advantage"] = round(rloo, 6)
            r["twin_contrastive_advantage"] = round(grpo - lambda_twin * (float(r.get("policy_bias_penalty") or 0.0) - mean_pen), 6)
            r["phase_step_advantages"] = {p: round(0.7 * grpo, 6) for p in CAUSAL_PHASES}
            assistant_tokens = sum(int(m.get("approx_tokens") or 0) for m in r["messages"] if m.get("role") == "assistant")
            r["_sft_trajectory_weight"] = (
                round(assistant_tokens / passing_tokens, 6) if r.get("passed") is True and passing_tokens > 0 else 0.0
            )
            r["group"] = {
                "k": k,
                "pass_at_1": round(sum(1 for x in group if x.get("passed") is True) / k, 6) if k else 0.0,
                "non_collapsed": not collapsed,
            }


def _twin_keys(meta: dict[str, Any]) -> set[str]:
    keys = set()
    if meta.get("twin_group_id"):
        keys.add(f"twin:{meta['twin_group_id']}")
    if meta.get("theme") and meta.get("fault_topology_id"):
        keys.add(f"scenario:{meta['theme']}:{meta['fault_topology_id']}")
    return keys


def _holdout_twin_keys(rows: list[dict[str, Any]], pilot_dir: Path) -> set[str]:
    keys: set[str] = set()
    for task_id in EVAL_HOLDOUT_TASKS:
        keys |= _twin_keys(_group_metadata({}, pilot_dir / task_id))
    for row in rows:
        if row["task_id"] in EVAL_HOLDOUT_TASKS:
            keys |= _twin_keys(row)
    return keys


def assign_splits(rows: list[dict[str, Any]], pilot_dir: Path) -> None:
    """Set row["split"]: eval_holdout for holdout tasks and every twin relative of one."""
    holdout = _holdout_twin_keys(rows, pilot_dir)
    for row in rows:
        held = row["task_id"] in EVAL_HOLDOUT_TASKS or bool(_twin_keys(row) & holdout)
        row["split"] = EVAL_SPLIT if held else TRAIN_SPLIT


def _group_metadata(group: dict[str, Any], task_dir: Path) -> dict[str, str | None]:
    blueprint: dict[str, Any] = {}
    blueprint_path = task_dir / "fixtures" / "blueprint.json"
    if not all(key in group for key in GROUP_META_KEYS) and blueprint_path.is_file():
        try:
            loaded = json.loads(blueprint_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            loaded = {}
        blueprint = loaded if isinstance(loaded, dict) else {}
    meta: dict[str, str | None] = {}
    for key in GROUP_META_KEYS:
        value = group[key] if key in group else blueprint.get(key)
        meta[key] = str(value) if value not in (None, "") else None
    return meta


def _resolve_run_dirs(run_index_path: Path) -> dict[str, Path]:
    index_doc = json.loads(Path(run_index_path).read_text(encoding="utf-8"))
    runs_map = index_doc.get("runs") if isinstance(index_doc, dict) and "runs" in index_doc else index_doc
    anchor = Path(run_index_path).resolve().parent
    resolved: dict[str, Path] = {}
    for task_id, entry in runs_map.items():
        if isinstance(entry, dict) and (entry.get("k5") or entry.get("k3")):
            resolved[str(task_id)] = resolve_factory_path(
                entry.get("k5") or entry.get("k3"), anchor_dir=anchor
            ).resolve()
    return resolved


def _read_prompt(rollout_dir: Path, messages: list[dict[str, Any]]) -> str:
    prompt_path = rollout_dir / "prompt.txt"
    if prompt_path.is_file():
        text = prompt_path.read_text(encoding="utf-8", errors="replace")
        if text.strip():
            return text
    for message in messages:
        if message["role"] == "user":
            return message["content"]
    return ""


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "mean": None, "std": None, "min": None, "max": None}
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return {
        "n": len(values),
        "mean": round(mean, 6),
        "std": round(math.sqrt(var), 6),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
    }


def _bucket(value: Any) -> str:
    return str(value) if value not in (None, "") else "unknown"


def _jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows).encode("utf-8")


def _build_rows(
    suite: dict[str, Any],
    twin: dict[str, Any],
    run_dirs: dict[str, Path],
    pilot_dir: Path,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    twin_episodes = twin.get("episodes") or {}
    for group in suite["tasks"]:
        task_id = str(group["task_id"])
        if task_id not in run_dirs:
            raise ValueError(f"task {task_id!r} has no k5/k3 run dir in the run index")
        run_dir = run_dirs[task_id]
        run_id = str(group["run_id"])
        meta = _group_metadata(group, pilot_dir / task_id)
        group_summary = {
            "k": group["k"],
            "pass_at_1": group["pass_at_1"],
            "non_collapsed": bool(group["invariants"]["non_collapsed_reward_signal"]),
        }
        for ep in group["episodes"]:
            rollout_id = str(ep["rollout_id"])
            rollout_dir = run_dir / rollout_id
            messages, dropped = pair_tool_results(transcript_messages(rollout_dir / "transcript.jsonl"))
            twin_ep = twin_episodes.get(f"{task_id}/{rollout_id}") or {
                "policy_bias_penalty": 0.0,
                "bias_kind": None,
                "twin_contrastive_advantage": ep["grpo_advantage"],
            }
            rows.append({
                "id": f"{task_id}/{run_id}/{rollout_id}",
                "task_id": task_id,
                "run_id": run_id,
                "rollout_id": rollout_id,
                "episode_id": ep.get("episode_id"),
                **meta,
                "role": group["role"],
                "prompt": _read_prompt(rollout_dir, messages),
                "messages": messages,
                "loss_mask": [m["loss_mask"] for m in messages],
                "token_loss_mask_rle": token_loss_mask_rle(messages),
                "reward": ep["reward"],
                "reward_components": ep["reward_components"],
                "outcome_class": ep["outcome_class"],
                "passed": ep["passed"],
                "total_points": ep["total_points"],
                "max_points": ep["max_points"],
                "seven_step_progress": ep["seven_step_progress"],
                "progress_scalar": ep["progress_scalar"],
                "grpo_advantage": ep["grpo_advantage"],
                "rloo_advantage": ep["rloo_advantage"],
                "twin_contrastive_advantage": twin_ep["twin_contrastive_advantage"],
                "policy_bias_penalty": twin_ep["policy_bias_penalty"],
                "bias_kind": twin_ep["bias_kind"],
                "phase_step_advantages": ep["phase_step_advantages"],
                "dapo_clip_bounds": ep["dapo_clip_bounds"],
                "group": group_summary,
                "_sft_trajectory_weight": ep["sft_trajectory_weight"],
                "_dropped_orphans": dropped,
            })
    rows.sort(key=lambda r: (r["task_id"], r["rollout_id"]))
    return rows


def _exporter_invariants(
    rows: list[dict[str, Any]],
    sft_rows: list[dict[str, Any]],
) -> dict[str, bool]:
    by_task: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_task.setdefault(row["task_id"], []).append(row)

    def _sums_to_zero(key: str) -> bool:
        return all(
            abs(sum(float(r[key]) for r in group)) <= _ADV_SUM_TOL_PER_ROLLOUT * max(1, len(group))
            for group in by_task.values()
        )

    passed_ids = {r["id"] for r in rows if r["passed"] is True}
    split_keys = {
        split: {k for r in rows if r["split"] == split for k in _twin_keys(r) | {f"task:{r['task_id']}"}}
        for split in (TRAIN_SPLIT, EVAL_SPLIT)
    }
    return {
        "tool_results_paired": all(not pair_tool_results(r["messages"])[1] for r in rows),
        "split_twin_disjoint": not (split_keys[TRAIN_SPLIT] & split_keys[EVAL_SPLIT])
        and all(r["split"] in (TRAIN_SPLIT, EVAL_SPLIT) for r in rows),
        "loss_mask_is_assistant_role": all(
            m["loss_mask"] == (1 if m["role"] == "assistant" else 0) and r["loss_mask"][i] == m["loss_mask"]
            for r in rows
            for i, m in enumerate(r["messages"])
        ) and all(len(r["loss_mask"]) == len(r["messages"]) for r in rows),
        "rle_tokens_match_message_tokens": all(
            sum(n for _, n in r["token_loss_mask_rle"]) == sum(m["approx_tokens"] for m in r["messages"])
            for r in rows
        ),
        "grpo_advantage_group_zero_sum": _sums_to_zero("grpo_advantage"),
        "twin_advantage_group_zero_sum": _sums_to_zero("twin_contrastive_advantage"),
        "sft_rows_subset_of_passed": {r["id"] for r in sft_rows} <= passed_ids,
        "seven_progress_keys_present": all(set(r["seven_step_progress"]) == set(SEVEN_KEYS) for r in rows),
    }


def export_rl_dataset(
    run_index_path: Path,
    pilot_dir: Path,
    out_dir: Path,
    *,
    include_all_pilot: bool = False,
    compact_max_tokens: int | None = None,
    extra_runs_roots: list[Path] | None = None,
) -> dict[str, Any]:
    """Write verl GRPO and SFT JSONL datasets plus a dataset card; return the card.

    Defaults are unchanged: no compaction, no extra trajectories, and the
    SFT/GRPO row and message key sets are exactly as before. When
    ``compact_max_tokens`` is set, tool observations are compacted (assistant
    turns byte-identical) before any split file is written. When
    ``extra_runs_roots`` is given, cross-model / counterfactual JSONL
    trajectories under those roots are merged into the training groups
    (eval-holdout tasks always excluded) and group advantages recomputed.
    """
    if compact_max_tokens is not None and int(compact_max_tokens) < 1:
        raise ValueError("compact_max_tokens must be positive or None")
    pilot_dir = Path(pilot_dir).resolve()
    out_dir = Path(out_dir)
    suite = model_certified_suite(run_index_path, pilot_dir, only_certified=not include_all_pilot)
    twin = compute_twin_contrastive_advantages(suite)
    rows = _build_rows(suite, twin, _resolve_run_dirs(run_index_path), pilot_dir)
    assign_splits(rows, pilot_dir)

    extra_rows_merged = 0
    extra_task_ids: set[str] = set()
    if extra_runs_roots:
        extra_rows = load_extra_trajectory_rows(list(extra_runs_roots))
        existing_ids = {r["id"] for r in rows}
        merged = [r for r in extra_rows if r["id"] not in existing_ids]
        if merged:
            rows.extend(merged)
            # Re-run split assignment so a twin relative of a holdout task can
            # never leak in through an extra root, then drop any such rows.
            assign_splits(rows, pilot_dir)
            merged_ids = {r["id"] for r in merged}
            rows = [r for r in rows if r["id"] not in merged_ids or r["split"] == TRAIN_SPLIT]
            kept_merged = [r for r in rows if r["id"] in merged_ids]
            extra_rows_merged = len(kept_merged)
            extra_task_ids = {str(r["task_id"]) for r in kept_merged}
            if extra_task_ids:
                _recompute_merged_groups(
                    rows, extra_task_ids, lambda_twin=float(twin.get("lambda_twin") or 0.5)
                )
            rows.sort(key=lambda r: (r["task_id"], r["rollout_id"], r["id"]))

    compaction: dict[str, Any] = {
        "enabled": compact_max_tokens is not None,
        "max_seq_tokens": compact_max_tokens,
        "rows": 0,
        "rows_fitting": 0,
        "original_tokens": 0,
        "compacted_tokens": 0,
        "assistant_tokens_before": 0,
        "assistant_tokens_after": 0,
        "assistant_tokens_preserved": True,
        "compacted_tool_turns": 0,
        "compactors": {},
    }
    if compact_max_tokens is not None:
        compactors: Counter[str] = Counter()
        for r in rows:
            result = apply_observation_compaction(r["messages"], max_seq_tokens=int(compact_max_tokens))
            r["messages"] = result["messages"]
            r["loss_mask"] = [int(m["loss_mask"]) for m in r["messages"]]
            r["token_loss_mask_rle"] = token_loss_mask_rle(r["messages"])
            compactors[str(result.get("compactor") or "unknown")] += 1
            compaction["rows"] += 1
            compaction["rows_fitting"] += int(bool(result["fits_max_seq_tokens"]))
            compaction["original_tokens"] += int(result["original_tokens"])
            compaction["compacted_tokens"] += int(result["compacted_tokens"])
            compaction["assistant_tokens_before"] += int(result["assistant_tokens_before"])
            compaction["assistant_tokens_after"] += int(result["assistant_tokens_after"])
            compaction["compacted_tool_turns"] += int(result["compacted_tool_turns"])
        compaction["compactors"] = dict(sorted(compactors.items()))
        compaction["assistant_tokens_preserved"] = (
            compaction["assistant_tokens_before"] == compaction["assistant_tokens_after"]
        )

    dropped_orphans = sorted(f"{r['id']}#{tid}" for r in rows for tid in r.pop("_dropped_orphans"))

    sft_rows = [
        {
            "id": r["id"],
            "task_id": r["task_id"],
            "governance_mode": r["governance_mode"],
            "role": r["role"],
            "split": r["split"],
            "prompt": r["prompt"],
            "messages": r["messages"],
            "loss_mask": r["loss_mask"],
            "token_loss_mask_rle": r["token_loss_mask_rle"],
            "sft_trajectory_weight": r["_sft_trajectory_weight"],
            "reward": r["reward"],
        }
        for r in rows
        if r["passed"] is True
    ]
    for r in rows:
        del r["_sft_trajectory_weight"]

    dpo_pairs = build_dpo_pairs(rows)

    assistant_tokens = 0
    masked_tokens = 0
    phase_tokens = {p: 0 for p in CAUSAL_PHASES}
    for r in rows:
        for m in r["messages"]:
            if m["loss_mask"] == 1:
                assistant_tokens += m["approx_tokens"]
                phase_tokens[m["causal_phase"]] += m["approx_tokens"]
            else:
                masked_tokens += m["approx_tokens"]
    sft_assistant_tokens = sum(
        m["approx_tokens"] for r in sft_rows for m in r["messages"] if m["loss_mask"] == 1
    )

    group_holding: Counter[str] = Counter()
    for group in suite["tasks"]:
        for name, held in group["invariants"].items():
            group_holding[name] += 1 if held else 0
    exporter = _exporter_invariants(rows, sft_rows)
    orphan_tool_results = sorted(
        f"{r['id']}#{m['tool_call_id']}"
        for r in rows
        for m in r["messages"]
        if m["role"] == "tool"
        and m["tool_call_id"] not in {c["id"] for x in r["messages"] for c in x.get("tool_calls") or []}
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict[str, Any]] = {}
    outputs = (
        (GRPO_FILE, rows),
        (SFT_FILE, sft_rows),
        (TRAIN_GRPO_FILE, [r for r in rows if r["split"] == TRAIN_SPLIT]),
        (TRAIN_SFT_FILE, [r for r in sft_rows if r["split"] == TRAIN_SPLIT]),
        (EVAL_GRPO_FILE, [r for r in rows if r["split"] == EVAL_SPLIT]),
        (TRAIN_DPO_FILE, dpo_pairs),
    )
    for name, payload_rows in outputs:
        payload = _jsonl_bytes(payload_rows)
        (out_dir / name).write_bytes(payload)
        files[name] = {
            "rows": len(payload_rows),
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    card = {
        "schema_version": SCHEMA_VERSION,
        "source": {"model": suite["model"], "driver": suite["driver"], "topology": suite["topology"]},
        "include_all_pilot": include_all_pilot,
        "tokenizer_note": TOKENIZER_NOTE,
        "counts": {
            "tasks": len({r["task_id"] for r in rows}),
            "rollouts": len(rows),
            "sft_rows": len(sft_rows),
            "passed": sum(1 for r in rows if r["passed"] is True),
            "by_governance_mode": dict(sorted(Counter(_bucket(r["governance_mode"]) for r in rows).items())),
            "by_outcome_class": dict(sorted(Counter(_bucket(r["outcome_class"]) for r in rows).items())),
            "by_theme": dict(sorted(Counter(_bucket(r["theme"]) for r in rows).items())),
            "by_fault_topology_id": dict(sorted(Counter(_bucket(r["fault_topology_id"]) for r in rows).items())),
            "tool_error_results": sum(1 for r in rows for m in r["messages"] if m.get("is_error")),
            "by_split": dict(sorted(Counter(r["split"] for r in rows).items())),
            "sft_by_split": dict(sorted(Counter(r["split"] for r in sft_rows).items())),
            "dpo_pairs": len(dpo_pairs),
            "dpo_pair_tasks": len({p["task_id"] for p in dpo_pairs}),
            "extra_rows_merged": extra_rows_merged,
        },
        "dpo_pairs": {
            "rows": len(dpo_pairs),
            "tasks": sorted({p["task_id"] for p in dpo_pairs}),
            "min_reward_margin": min((p["reward_margin"] for p in dpo_pairs), default=None),
            "all_chosen_reward_1": all(p["reward_chosen"] == 1.0 for p in dpo_pairs),
            "all_margins_positive": all(p["reward_margin"] > 0 for p in dpo_pairs),
            "all_split_train": all(p["split"] == TRAIN_SPLIT for p in dpo_pairs),
        },
        "compaction": compaction,
        "extra_runs": {
            "roots": [str(p) for p in (extra_runs_roots or [])],
            "rows_merged": extra_rows_merged,
            "tasks": sorted(extra_task_ids),
        },
        "split": {
            "eval_holdout_tasks": list(EVAL_HOLDOUT_TASKS),
            "eval_holdout_tasks_present": sorted({r["task_id"] for r in rows if r["split"] == EVAL_SPLIT}),
            "train_tasks": len({r["task_id"] for r in rows if r["split"] == TRAIN_SPLIT}),
            "rule": "a row is eval_holdout when its task, twin_group_id or (theme, fault_topology_id) "
                    "matches a holdout task",
        },
        "dropped_orphan_tool_results": len(dropped_orphans),
        "dropped_orphan_tool_result_ids": dropped_orphans,
        # Residual unanswered tool results after pair_tool_results; always empty unless pairing regresses.
        "orphan_tool_results": orphan_tool_results,
        "tokens": {
            "assistant": assistant_tokens,
            "masked": masked_tokens,
            "total": assistant_tokens + masked_tokens,
            "assistant_fraction": round(assistant_tokens / max(1, assistant_tokens + masked_tokens), 6),
            "sft_assistant": sft_assistant_tokens,
        },
        "phase_token_share": {
            p: round(phase_tokens[p] / max(1, assistant_tokens), 6) for p in CAUSAL_PHASES
        },
        "stats": {
            key: _stats([float(r[key]) for r in rows if r[key] is not None])
            for key in (
                "reward",
                "progress_scalar",
                "grpo_advantage",
                "rloo_advantage",
                "twin_contrastive_advantage",
                "policy_bias_penalty",
            )
        },
        "twin": {
            "lambda_twin": twin.get("lambda_twin"),
            "mode_bias_rates": twin.get("mode_bias_rates") or {},
            "twin_group_count": len(twin.get("twin_groups") or []),
            "complete_triplets": sum(1 for g in twin.get("twin_groups") or [] if g.get("complete_triplet")),
            "suite_action_discrimination": twin.get("suite_action_discrimination"),
        },
        "invariants": {
            "suite_all_invariants_hold": bool(suite["all_invariants_hold"]),
            "group_invariants": {"groups": len(suite["tasks"]), "holding": dict(sorted(group_holding.items()))},
            "twin": dict(twin.get("invariants") or {}),
            "twin_all_invariants_hold": twin.get("all_invariants_hold"),
            "exporter": exporter,
            "exporter_all_hold": all(exporter.values()),
        },
        "files": files,
        "fields": FIELDS,
    }
    (out_dir / CARD_FILE).write_text(json.dumps(card, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return card


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--run-index",
        type=Path,
        default=FACTORY_ROOT / "phase2_run_index.json",
    )
    parser.add_argument(
        "--pilot-dir",
        type=Path,
        default=FACTORY_ROOT / "phase2_pilot_51",
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--include-all-pilot", action="store_true", help="Include all 51 tasks (K=3 + K=5)")
    parser.add_argument("--compact-max-tokens", type=int, default=None,
                        help="Compact tool observations so trajectories fit this many approx tokens "
                             "(e.g. 24576); assistant turns are never altered")
    parser.add_argument("--extra-runs-roots", type=Path, nargs="*", default=None,
                        help="Extra cross-model/counterfactual trajectory roots (JSONL files or "
                             "directories scanned recursively) merged into training groups")
    args = parser.parse_args(argv)

    card = export_rl_dataset(
        args.run_index,
        args.pilot_dir,
        args.out_dir,
        include_all_pilot=args.include_all_pilot,
        compact_max_tokens=args.compact_max_tokens,
        extra_runs_roots=args.extra_runs_roots,
    )
    summary = {
        "out_dir": str(args.out_dir),
        "tasks": card["counts"]["tasks"],
        "rollouts": card["counts"]["rollouts"],
        "sft_rows": card["counts"]["sft_rows"],
        "dpo_pairs": card["counts"]["dpo_pairs"],
        "tokens": card["tokens"],
        "suite_all_invariants_hold": card["invariants"]["suite_all_invariants_hold"],
        "twin_all_invariants_hold": card["invariants"]["twin_all_invariants_hold"],
        "exporter_all_hold": card["invariants"]["exporter_all_hold"],
    }
    print(json.dumps(summary, sort_keys=True))
    return 0 if card["invariants"]["exporter_all_hold"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
