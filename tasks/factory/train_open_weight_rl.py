#!/usr/bin/env python3
"""Open-weight SFT/RL loss verification and GPU training-bundle generation.

Reads the split exported by `tasks.factory.export_rl_dataset` and runs in two
modes:
  1. `verify-cpu`: stdlib-only checks of the training contract.
     - Split disjointness: train and eval_holdout share no task and no twin group.
     - Label masking: only assistant tokens carry labels (-100 elsewhere).
     - Closed-form losses and analytical gradients against central finite
       differences, on reference fixtures and on the live records: masked SFT
       cross-entropy, DAPO/GRPO clipped surrogate with asymmetric clip and
       collapsed-group filtering, phase-decomposed step advantages and
       twin-contrastive advantages.
  2. `generate-gpu-bundle`: writes TRL/PEFT and verl entrypoints for
     Qwen2.5-Coder-7B/14B/32B-Instruct together with the verify-cpu report,
     the 24k observation-compacted loader (`left_obs_first`: final assistant
     repair/report turns are never cut), DPO margin-loss verification
     alongside SFT/RLOO/DAPO, and the 4x/8x cluster configs
     `configs/fsdp_8xh100_24k.yaml` / `configs/deepspeed_zero3_4xa100_24k.json`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Sequence

DEFAULT_DATA_DIR = Path("/home/ubuntu/alkera_factory_1017/rl_dataset_v1")
DEFAULT_BUNDLE_DIR = Path("/home/ubuntu/alkera_factory_1017/gpu_trainer_bundle")
TRAIN_SFT_FILE = "train_sft.jsonl"
TRAIN_GRPO_FILE = "train_grpo.jsonl"
TRAIN_DPO_FILE = "train_dpo_pairs.jsonl"
EVAL_GRPO_FILE = "eval_holdout_grpo.jsonl"
CARD_FILE = "dataset_card.json"
VERIFICATION_FILE = "cpu_loss_verification.json"
DEFAULT_MAX_SEQ_TOKENS = 24576
FSDP_24K_CONFIG = "configs/fsdp_8xh100_24k.yaml"
DEEPSPEED_24K_CONFIG = "configs/deepspeed_zero3_4xa100_24k.json"
SUPPORTED_CLUSTER_GPUS = (4, 8)
SUPPORTED_MODELS = (
    "Qwen/Qwen2.5-Coder-7B-Instruct",
    "Qwen/Qwen2.5-Coder-14B-Instruct",
    "Qwen/Qwen2.5-Coder-32B-Instruct",
)

IGNORE_INDEX = -100
CLIP_LOW = 0.80
CLIP_HIGH = 1.28
ALPHA_OUTCOME = 0.7
GRPO_EPS = 1e-8
COLLAPSE_VAR = 1e-12
PHASES = (
    "discovery",
    "epistemic_investigation",
    "pipeline_repair",
    "verification_and_execution",
    "governance_and_reporting",
)

# Live checks expand each message to at most this many synthetic token
# positions; the masking contract is per message, so a capped window per
# message covers every message boundary without materializing 8M tokens.
LIVE_TOKENS_PER_MESSAGE = 3
LIVE_VOCAB = 6
FD_STEP = 1e-6
FD_TOL = 1e-5
ADV_TOL = 1e-5


# ---------------------------------------------------------------------------
# Losses and analytical gradients
# ---------------------------------------------------------------------------

def log_softmax(logits: Sequence[float]) -> list[float]:
    peak = max(logits)
    log_z = peak + math.log(sum(math.exp(x - peak) for x in logits))
    return [x - log_z for x in logits]


def sft_loss_and_grad(
    logits: Sequence[Sequence[float]],
    labels: Sequence[int],
    weight: float = 1.0,
) -> tuple[float, list[list[float]]]:
    """Masked mean token cross-entropy and d loss / d logits.

    Positions labelled IGNORE_INDEX contribute nothing to the loss and get an
    exactly zero gradient row.
    """
    active = [t for t, y in enumerate(labels) if y != IGNORE_INDEX]
    n = len(active)
    grad = [[0.0] * len(row) for row in logits]
    if n == 0:
        return 0.0, grad
    loss = 0.0
    for t in active:
        logp = log_softmax(logits[t])
        loss -= logp[labels[t]]
        for v, lp in enumerate(logp):
            grad[t][v] = weight * (math.exp(lp) - (1.0 if v == labels[t] else 0.0)) / n
    return weight * loss / n, grad


def dapo_loss_and_grad(
    logp_new: Sequence[float],
    logp_old: Sequence[float],
    advantages: Sequence[float],
    mask: Sequence[int],
    clip_low: float = CLIP_LOW,
    clip_high: float = CLIP_HIGH,
) -> tuple[float, list[float]]:
    """Token-mean clipped surrogate -mean(min(r*A, clip(r)*A)) and d loss / d logp_new."""
    active = [t for t, m in enumerate(mask) if m]
    n = len(active)
    grad = [0.0] * len(logp_new)
    if n == 0:
        return 0.0, grad
    total = 0.0
    for t in active:
        ratio = math.exp(logp_new[t] - logp_old[t])
        adv = advantages[t]
        unclipped = ratio * adv
        clipped = min(max(ratio, clip_low), clip_high) * adv
        if unclipped <= clipped:
            total += unclipped
            grad[t] = -unclipped / n
        else:
            total += clipped
    return -total / n, grad


def _sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    z = math.exp(x)
    return z / (1.0 + z)


def _log_sigmoid(x: float) -> float:
    # -softplus(-x), computed without overflow for large |x|.
    return -(math.log1p(math.exp(-abs(x))) + max(-x, 0.0))


def dpo_loss_and_grad(
    chosen_policy_logps: Sequence[float],
    rejected_policy_logps: Sequence[float],
    chosen_ref_logps: Sequence[float],
    rejected_ref_logps: Sequence[float],
    beta: float = 0.1,
    chosen_mask: Sequence[int] | None = None,
    rejected_mask: Sequence[int] | None = None,
) -> tuple[float, list[float], list[float]]:
    """DPO margin loss and its exact gradient w.r.t. the *policy* token log-probs.

    L = -log sigma(beta * [(logpi_theta(y+) - logpi_ref(y+))
    - (logpi_theta(y-) - logpi_ref(y-))]) where each sequence
    log-prob is the sum over ``loss_mask == 1`` (assistant) tokens only.

    Hence dL/dlogpi_theta(y+_t) = -beta * sigma(-beta * Delta)
    on chosen assistant tokens and the opposite sign on rejected assistant
    tokens; masked (``loss_mask == 0``) positions get exactly ``0.0``. A
    gradient-*descent* step therefore moves chosen log-probs up and rejected
    log-probs down. Reference log-probs are constants here (no gradient).
    """
    c_mask = list(chosen_mask) if chosen_mask is not None else [1] * len(chosen_policy_logps)
    r_mask = list(rejected_mask) if rejected_mask is not None else [1] * len(rejected_policy_logps)
    if len(c_mask) != len(chosen_policy_logps) or len(r_mask) != len(rejected_policy_logps):
        raise ValueError("DPO masks must align with their policy logp sequences")
    if len(chosen_ref_logps) != len(chosen_policy_logps) or len(rejected_ref_logps) != len(rejected_policy_logps):
        raise ValueError("DPO reference logps must align with their policy logp sequences")
    pol_c = sum(float(p) for p, m in zip(chosen_policy_logps, c_mask) if m)
    ref_c = sum(float(p) for p, m in zip(chosen_ref_logps, c_mask) if m)
    pol_r = sum(float(p) for p, m in zip(rejected_policy_logps, r_mask) if m)
    ref_r = sum(float(p) for p, m in zip(rejected_ref_logps, r_mask) if m)
    delta = (pol_c - ref_c) - (pol_r - ref_r)
    loss = -_log_sigmoid(beta * delta)
    coeff = beta * _sigmoid(-beta * delta)
    grad_chosen = [-coeff if m else 0.0 for m in c_mask]
    grad_rejected = [coeff if m else 0.0 for m in r_mask]
    return loss, grad_chosen, grad_rejected


def dpo_sequence_loss(
    policy_chosen: float, policy_rejected: float, ref_chosen: float, ref_rejected: float, beta: float = 0.1
) -> float:
    """Sequence-logp convenience wrapper around :func:`dpo_loss_and_grad`."""
    return dpo_loss_and_grad([policy_chosen], [policy_rejected], [ref_chosen], [ref_rejected], beta=beta)[0]


def _trainer_message_tokens(message: dict[str, Any]) -> int:
    if message.get("approx_tokens") is not None:
        return int(message["approx_tokens"])
    chars = len(str(message.get("content") or "")) + len(str(message.get("reasoning_content") or ""))
    for call in message.get("tool_calls") or []:
        try:
            chars += len(str(call["function"]["arguments"]))
        except (KeyError, TypeError):
            chars += len(json.dumps(call, ensure_ascii=False, sort_keys=True))
    return max(1, math.ceil(chars / 4))


def assistant_token_count(messages: Sequence[dict[str, Any]]) -> int:
    return sum(_trainer_message_tokens(m) for m in messages if m.get("role") == "assistant")


def _builtin_compact_messages(
    messages: Sequence[dict[str, Any]], *, max_seq_tokens: int = DEFAULT_MAX_SEQ_TOKENS, target_obs_chars: int = 1200
) -> dict[str, Any]:
    """Trainer-local fallback: compact only tool observations, never assistant turns."""
    compacted = [dict(m) for m in messages]
    marker = "\n...[observation compacted]...\n"

    def total() -> int:
        return sum(_trainer_message_tokens(m) for m in compacted)

    def set_tool(i: int, content: str) -> None:
        compacted[i]["content"] = content
        compacted[i]["approx_tokens"] = max(
            1, math.ceil((len(content) + len(str(compacted[i].get("reasoning_content") or ""))) / 4)
        )

    changed = 0
    for i, message in enumerate(compacted):
        if message.get("role") != "tool" or not isinstance(message.get("content"), str):
            continue
        content = message["content"]
        if len(content) > target_obs_chars:
            keep = max(1, target_obs_chars - len(marker))
            half = keep // 2
            set_tool(i, content[:half] + marker + content[-(keep - half):])
            changed += 1
    target = target_obs_chars
    while total() > max_seq_tokens and target > 256:
        target = max(256, target // 2)
        for i, message in enumerate(compacted):
            if message.get("role") != "tool" or not isinstance(message.get("content"), str):
                continue
            if len(message["content"]) > target:
                keep = max(1, target - len(marker))
                half = keep // 2
                content = message["content"]
                set_tool(i, content[:half] + marker + content[-(keep - half):])
                changed += 1
            if total() <= max_seq_tokens:
                break
    original_tokens = sum(_trainer_message_tokens(m) for m in messages)
    return {
        "messages": compacted,
        "original_tokens": original_tokens,
        "compacted_tokens": total(),
        "assistant_tokens_before": assistant_token_count(messages),
        "assistant_tokens_after": assistant_token_count(compacted),
        "fits_max_seq_tokens": total() <= max_seq_tokens,
        "compacted_tool_turns": changed,
        "compactor": "builtin_fallback",
    }


def compact_messages_for_training(
    messages: Sequence[dict[str, Any]], *, max_seq_tokens: int = DEFAULT_MAX_SEQ_TOKENS, target_obs_chars: int = 1200
) -> dict[str, Any]:
    """Observation-compact a trajectory, preferring `observation_compactor` when available.

    The external compactor's output is validated (same key sets, assistant
    messages byte-identical); any failure falls back to the built-in
    head+tail tool-observation compactor above.
    """
    original = [dict(m) for m in messages]
    try:
        from tasks.factory.observation_compactor import compact_trajectory_messages as _external

        result = _external([dict(m) for m in messages], max_seq_tokens=int(max_seq_tokens), target_obs_chars=int(target_obs_chars))
        candidate = result.get("messages") if isinstance(result, dict) else None
        ok = (
            isinstance(candidate, list)
            and len(candidate) == len(original)
            and all(
                isinstance(a, dict) and set(a) == set(b) and a.get("role") == b.get("role")
                and (b.get("role") != "assistant" or json.dumps(b, ensure_ascii=False, sort_keys=True) == json.dumps(a, ensure_ascii=False, sort_keys=True))
                for b, a in zip(original, candidate)
            )
        )
        if ok:
            out = dict(result)
            out["messages"] = candidate
            out["compactor"] = "observation_compactor"
            return out
    except Exception:
        pass
    return _builtin_compact_messages(original, max_seq_tokens=int(max_seq_tokens), target_obs_chars=int(target_obs_chars))


def apply_left_obs_first_truncation(
    messages: Sequence[dict[str, Any]], max_seq_tokens: int = DEFAULT_MAX_SEQ_TOKENS
) -> dict[str, Any]:
    """Fit a trajectory into the budget by sacrificing observations from the left first.

    Semantics of ``left_obs_first``: (1) observation-compact tool turns, then
    (2) replace the *oldest* remaining tool observations with a placeholder,
    then (3) drop the oldest non-assistant messages. Assistant messages --
    in particular the final ``pipeline_repair`` / ``final_report.json`` turns
    at the tail -- are never truncated, altered or dropped, so
    ``truncated_assistant_tokens`` is always exactly 0. If assistant tokens
    alone exceeded the budget the row is reported as not fitting rather than
    cutting supervision.
    """
    result = compact_messages_for_training(messages, max_seq_tokens=int(max_seq_tokens))
    compacted: list[dict[str, Any]] = [dict(m) for m in result["messages"]]

    def total() -> int:
        return sum(_trainer_message_tokens(m) for m in compacted)

    truncated_tool_turns = int(result.get("compacted_tool_turns") or 0)
    dropped_messages = 0
    if total() > max_seq_tokens:
        for i, message in enumerate(compacted):
            if total() <= max_seq_tokens:
                break
            if message.get("role") == "tool" and message.get("content") != "[observation compacted: left_obs_first]":
                message["content"] = "[observation compacted: left_obs_first]"
                message["approx_tokens"] = 1
                truncated_tool_turns += 1
    if total() > max_seq_tokens:
        # Explicit oldest-first drop of non-assistant messages.
        remaining = list(compacted)
        dropped_messages = 0
        while sum(_trainer_message_tokens(m) for m in remaining) > max_seq_tokens:
            idx = next((i for i, m in enumerate(remaining) if m.get("role") != "assistant"), None)
            if idx is None:
                break
            remaining.pop(idx)
            dropped_messages += 1
        compacted = remaining
    return {
        "messages": compacted,
        "original_tokens": sum(_trainer_message_tokens(m) for m in messages),
        "compacted_tokens": total(),
        "assistant_tokens_before": assistant_token_count(messages),
        "assistant_tokens_after": assistant_token_count(compacted),
        "truncated_assistant_tokens": 0,
        "fits_max_seq_tokens": total() <= max_seq_tokens,
        "compacted_tool_turns": truncated_tool_turns,
        "dropped_non_assistant_messages": dropped_messages,
        "truncation_side": "left_obs_first",
    }


def load_rows_within_token_budget(
    rows: Sequence[dict[str, Any]], *, max_seq_tokens: int = DEFAULT_MAX_SEQ_TOKENS, truncation_side: str = "left_obs_first"
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return copies of rows fitted to ``max_seq_tokens`` plus aggregate loader stats.

    With ``left_obs_first`` (the default) zero assistant tokens are truncated
    at any budget; with ``right`` messages are cut from the tail (legacy
    behaviour, kept only for ablation); with ``none`` rows pass through.
    """
    if truncation_side not in ("left_obs_first", "right", "none"):
        raise ValueError(f"unknown truncation_side: {truncation_side!r}")
    out: list[dict[str, Any]] = []
    stats: dict[str, Any] = {
        "rows": 0, "rows_fitting": 0, "assistant_tokens_before": 0, "assistant_tokens_after": 0,
        "truncated_assistant_tokens": 0, "max_compacted_tokens": 0, "max_seq_tokens": int(max_seq_tokens),
        "truncation_side": truncation_side,
    }
    for row in rows:
        messages = row["messages"]
        before = assistant_token_count(messages)
        if truncation_side == "none":
            new_messages = [dict(m) for m in messages]
            fits = sum(_trainer_message_tokens(m) for m in new_messages) <= max_seq_tokens
        elif truncation_side == "right":
            new_messages, running = [], 0
            for m in messages:
                n = _trainer_message_tokens(m)
                if running + n > max_seq_tokens:
                    break
                new_messages.append(dict(m))
                running += n
            fits = True
        else:
            fitted = apply_left_obs_first_truncation(messages, int(max_seq_tokens))
            new_messages = fitted["messages"]
            fits = bool(fitted["fits_max_seq_tokens"])
        after = assistant_token_count(new_messages)
        copied = dict(row)
        copied["messages"] = new_messages
        copied["loss_mask"] = [int(m.get("loss_mask", 1 if m.get("role") == "assistant" else 0)) for m in new_messages]
        # Re-encode the RLE from the (possibly compacted) per-message tokens.
        runs: list[list[int]] = []
        for m in new_messages:
            mask = int(m.get("loss_mask", 1 if m.get("role") == "assistant" else 0))
            n = _trainer_message_tokens(m)
            if runs and runs[-1][0] == mask:
                runs[-1][1] += n
            else:
                runs.append([mask, n])
        copied["token_loss_mask_rle"] = runs
        out.append(copied)
        stats["rows"] += 1
        stats["rows_fitting"] += int(fits)
        stats["assistant_tokens_before"] += before
        stats["assistant_tokens_after"] += after
        if truncation_side == "right":
            stats["truncated_assistant_tokens"] += max(0, before - after)
        stats["max_compacted_tokens"] = max(stats["max_compacted_tokens"], sum(_trainer_message_tokens(m) for m in new_messages))
    stats["assistant_tokens_preserved"] = stats["assistant_tokens_before"] == stats["assistant_tokens_after"]
    return out, stats


def grpo_advantages(rewards: Sequence[float]) -> tuple[list[float], bool]:
    k = len(rewards)
    mean = sum(rewards) / k if k else 0.0
    var = sum((r - mean) ** 2 for r in rewards) / k if k else 0.0
    collapsed = var <= COLLAPSE_VAR or k <= 1
    if collapsed:
        return [0.0] * k, True
    std = math.sqrt(var)
    return [(r - mean) / (std + GRPO_EPS) for r in rewards], False


def rloo_advantages(rewards: Sequence[float]) -> list[float]:
    k = len(rewards)
    if k <= 1:
        return [0.0] * k
    total = sum(rewards)
    return [r - (total - r) / (k - 1) for r in rewards]


def twin_advantages(grpo: Sequence[float], penalties: Sequence[float], lambda_twin: float) -> list[float]:
    mean_pen = sum(penalties) / len(penalties) if penalties else 0.0
    return [a - lambda_twin * (p - mean_pen) for a, p in zip(grpo, penalties)]


def phase_step_advantage(grpo: float, phase_z: float, alpha: float = ALPHA_OUTCOME) -> float:
    return alpha * grpo + (1.0 - alpha) * phase_z


def finite_difference(fn: Callable[[list[float]], float], x: Sequence[float], index: int,
                      step: float = FD_STEP) -> float:
    hi = list(x)
    lo = list(x)
    hi[index] += step
    lo[index] -= step
    return (fn(hi) - fn(lo)) / (2.0 * step)


def _close(a: float, b: float, tol: float = FD_TOL) -> bool:
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


# ---------------------------------------------------------------------------
# Reference fixtures with closed-form answers
# ---------------------------------------------------------------------------

def _check_fd_sft(logits: list[list[float]], labels: list[int], weight: float = 1.0) -> bool:
    _, grad = sft_loss_and_grad(logits, labels, weight)
    for t in range(len(logits)):
        for v in range(len(logits[t])):
            def f(row: list[float], t=t) -> float:
                patched = [list(r) for r in logits]
                patched[t] = row
                return sft_loss_and_grad(patched, labels, weight)[0]
            if not _close(grad[t][v], finite_difference(f, logits[t], v)):
                return False
    return True


def _check_fd_dapo(new: list[float], old: list[float], adv: list[float], mask: list[int]) -> bool:
    _, grad = dapo_loss_and_grad(new, old, adv, mask)
    for t in range(len(new)):
        ratio = math.exp(new[t] - old[t])
        # The clipped surrogate is not differentiable exactly at the clip edges.
        if any(abs(ratio - edge) < 1e-4 for edge in (CLIP_LOW, CLIP_HIGH)):
            continue
        fd = finite_difference(lambda x: dapo_loss_and_grad(x, old, adv, mask)[0], new, t)
        if not _close(grad[t], fd):
            return False
    return True


def reference_checks() -> dict[str, dict[str, Any]]:
    checks: dict[str, dict[str, Any]] = {}

    uniform = [[0.0] * 4, [0.0] * 4, [0.0] * 4]
    loss, grad = sft_loss_and_grad(uniform, [1, IGNORE_INDEX, 3])
    expected_row = [0.25 / 2, -0.75 / 2, 0.25 / 2, 0.25 / 2]
    checks["sft_uniform_closed_form"] = {
        "passed": _close(loss, math.log(4)) and all(_close(a, b) for a, b in zip(grad[0], expected_row))
        and grad[1] == [0.0] * 4,
        "loss": loss,
        "expected_loss": math.log(4),
    }
    rng = random.Random(7)
    logits = [[rng.uniform(-2, 2) for _ in range(5)] for _ in range(4)]
    labels = [2, IGNORE_INDEX, 0, IGNORE_INDEX]
    checks["sft_finite_difference"] = {"passed": _check_fd_sft(logits, labels, weight=0.4)}
    checks["sft_masked_rows_zero_gradient"] = {
        "passed": all(v == 0.0 for t in (1, 3) for v in sft_loss_and_grad(logits, labels)[1][t]),
    }
    checks["sft_all_masked_is_zero"] = {
        "passed": sft_loss_and_grad(logits, [IGNORE_INDEX] * 4) == (0.0, [[0.0] * 5 for _ in range(4)]),
    }

    # r = 1.5 > 1.28 with A > 0 clips (zero grad); r = 0.5 < 0.8 with A < 0
    # clips; r = 1.1 inside the band is unclipped (grad = -r*A/n).
    old = [0.0, 0.0, 0.0, 0.0]
    new = [math.log(1.5), math.log(0.5), math.log(1.1), 0.3]
    adv = [2.0, -1.0, 0.5, 9.0]
    mask = [1, 1, 1, 0]
    loss, grad = dapo_loss_and_grad(new, old, adv, mask)
    expected_loss = -(1.28 * 2.0 + 0.8 * -1.0 + 1.1 * 0.5) / 3
    checks["dapo_asymmetric_clip_closed_form"] = {
        "passed": _close(loss, expected_loss) and grad[0] == 0.0 and grad[1] == 0.0
        and _close(grad[2], -1.1 * 0.5 / 3) and grad[3] == 0.0,
        "loss": loss,
        "expected_loss": expected_loss,
    }
    rng = random.Random(11)
    new = [rng.uniform(-0.5, 0.5) for _ in range(12)]
    adv = [rng.uniform(-2, 2) for _ in range(12)]
    mask = [rng.randint(0, 1) for _ in range(12)]
    checks["dapo_finite_difference"] = {"passed": _check_fd_dapo(new, [0.0] * 12, adv, mask)}
    checks["dapo_on_policy_equals_negative_mean_advantage"] = {
        "passed": _close(dapo_loss_and_grad([0.0] * 3, [0.0] * 3, [1.0, -2.0, 4.0], [1, 1, 1])[0], -1.0),
    }

    rewards = [1.0, 0.5, 0.0, 0.5]
    grpo, collapsed = grpo_advantages(rewards)
    std = math.sqrt(sum((r - 0.5) ** 2 for r in rewards) / 4)
    checks["grpo_closed_form"] = {
        "passed": not collapsed and all(_close(a, (r - 0.5) / (std + GRPO_EPS)) for a, r in zip(grpo, rewards))
        and abs(sum(grpo)) < 1e-9,
    }
    checks["grpo_collapsed_group_zero"] = {
        "passed": grpo_advantages([0.7, 0.7, 0.7]) == ([0.0, 0.0, 0.0], True),
    }
    rloo = rloo_advantages(rewards)
    checks["rloo_closed_form"] = {
        "passed": _close(rloo[0], 1.0 - 1.0 / 3.0) and abs(sum(rloo)) < 1e-9,
    }
    twin = twin_advantages([1.0, 0.0, -1.0], [0.0, 1.0, 0.0], 0.5)
    checks["twin_closed_form"] = {
        "passed": _close(twin[0], 1.0 + 0.5 / 3) and _close(twin[1], -0.5 * 2 / 3) and abs(sum(twin)) < 1e-9,
    }
    checks["twin_without_penalty_variance_equals_grpo"] = {
        "passed": twin_advantages([0.3, -0.3], [1.0, 1.0], 0.5) == [0.3, -0.3],
    }
    checks["phase_step_closed_form"] = {
        "passed": _close(phase_step_advantage(1.0, -1.0), 0.7 - 0.3),
    }

    # DPO: chosen seq logp -3 (ref -3.5), rejected seq logp -3 (ref -2.5), so
    # delta = 0.5 - (-0.5) = 1.0 and L = -log sigmoid(beta * delta).
    dpo_beta = 0.1
    dpo_loss, dpo_gc, dpo_gr = dpo_loss_and_grad(
        [-1.0, -2.0, -9.0], [-3.0, -9.0], [-1.5, -2.0, 0.0], [-2.5, 0.0],
        beta=dpo_beta, chosen_mask=[1, 1, 0], rejected_mask=[1, 0],
    )
    dpo_coeff = dpo_beta * _sigmoid(-dpo_beta * 1.0)
    checks["dpo_margin_loss_closed_form"] = {
        "passed": _close(dpo_loss, -_log_sigmoid(dpo_beta * 1.0))
        and _close(dpo_loss, math.log1p(math.exp(-dpo_beta * 1.0))),
        "loss": dpo_loss,
        "expected_loss": -_log_sigmoid(dpo_beta * 1.0),
    }
    checks["dpo_gradient_closed_form"] = {
        # dL/dlogp_chosen < 0 and dL/dlogp_rejected > 0, so a descent step
        # (-grad) is positive on chosen tokens and negative on rejected ones;
        # masked positions have exactly zero gradient.
        "passed": all(_close(g, -dpo_coeff) for g in dpo_gc[:2]) and _close(dpo_gr[0], dpo_coeff)
        and dpo_gc[2] == 0.0 and dpo_gr[1] == 0.0
        and all(g > 0 for g in (-dpo_gc[0], -dpo_gc[1])) and -dpo_gr[0] < 0,
        "grad_chosen": dpo_gc,
        "grad_rejected": dpo_gr,
    }

    def _dpo_fd_ok() -> bool:
        chosen = [-0.7, -1.3, -0.2]
        rejected = [-1.1, -0.4]
        ref_c = [-0.9, -1.0, -0.3]
        ref_r = [-1.0, -0.6]
        mask_c, mask_r = [1, 1, 0], [1, 1]
        _, gc, gr = dpo_loss_and_grad(chosen, rejected, ref_c, ref_r, beta=0.3, chosen_mask=mask_c, rejected_mask=mask_r)
        for t in range(len(chosen)):
            fd = finite_difference(
                lambda x, t=t: dpo_loss_and_grad(x, rejected, ref_c, ref_r, beta=0.3, chosen_mask=mask_c, rejected_mask=mask_r)[0],
                chosen, t,
            )
            if not _close(gc[t], fd):
                return False
        for t in range(len(rejected)):
            fd = finite_difference(
                lambda x, t=t: dpo_loss_and_grad(chosen, x, ref_c, ref_r, beta=0.3, chosen_mask=mask_c, rejected_mask=mask_r)[0],
                rejected, t,
            )
            if not _close(gr[t], fd):
                return False
        return gc[2] == 0.0

    checks["dpo_finite_difference"] = {"passed": _dpo_fd_ok()}
    checks["dpo_all_masked_sequences_have_zero_sequence_gradient"] = {
        "passed": dpo_loss_and_grad([1.0], [1.0], [0.0], [0.0], beta=0.1, chosen_mask=[0], rejected_mask=[0])[1:] == ([0.0], [0.0]),
    }

    # Collapsed groups are filtered before the surrogate, so their tokens get no gradient.
    groups = {"live": [1.0, 0.0, 0.5], "collapsed": [0.4, 0.4, 0.4]}
    grads = {}
    for name, rs in groups.items():
        adv_g, collapsed_g = grpo_advantages(rs)
        if collapsed_g:
            grads[name] = [0.0] * len(rs)
            continue
        _, g = dapo_loss_and_grad([0.0] * len(rs), [0.0] * len(rs), adv_g, [1] * len(rs))
        grads[name] = g
    checks["dapo_collapsed_group_zero_gradient"] = {
        "passed": grads["collapsed"] == [0.0, 0.0, 0.0] and any(g != 0.0 for g in grads["live"]),
    }
    return checks


# ---------------------------------------------------------------------------
# Live dataset checks
# ---------------------------------------------------------------------------

def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def twin_keys(row: dict[str, Any]) -> set[str]:
    keys = {f"task:{row['task_id']}"}
    if row.get("twin_group_id"):
        keys.add(f"twin:{row['twin_group_id']}")
    if row.get("theme") and row.get("fault_topology_id"):
        keys.add(f"scenario:{row['theme']}:{row['fault_topology_id']}")
    return keys


def _rng(*parts: Any) -> random.Random:
    digest = hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).hexdigest()
    return random.Random(int(digest[:16], 16))


def token_labels(row: dict[str, Any], tokens_per_message: int = LIVE_TOKENS_PER_MESSAGE) -> tuple[list[int], list[int], list[str]]:
    """Expand messages to (labels, mask, phases) over a capped window per message."""
    labels: list[int] = []
    mask: list[int] = []
    phases: list[str] = []
    rng = _rng("labels", row["id"])
    for message in row["messages"]:
        n = min(int(message["approx_tokens"]), tokens_per_message)
        for _ in range(n):
            m = int(message["loss_mask"])
            mask.append(m)
            labels.append(rng.randrange(LIVE_VOCAB) if m else IGNORE_INDEX)
            phases.append(str(message["causal_phase"]))
    return labels, mask, phases


def check_split(train_rows: list[dict[str, Any]], eval_rows: list[dict[str, Any]],
                sft_rows: list[dict[str, Any]]) -> dict[str, Any]:
    train_keys = set().union(*(twin_keys(r) for r in train_rows)) if train_rows else set()
    eval_keys = set().union(*(twin_keys(r) for r in eval_rows)) if eval_rows else set()
    shared = sorted(train_keys & eval_keys)
    eval_ids = {r["id"] for r in eval_rows}
    return {
        "passed": not shared
        and all(r.get("split") == "train" for r in train_rows + sft_rows)
        and all(r.get("split") == "eval_holdout" for r in eval_rows)
        and not ({r["id"] for r in sft_rows} & eval_ids),
        "train_tasks": len({r["task_id"] for r in train_rows}),
        "eval_tasks": len({r["task_id"] for r in eval_rows}),
        "shared_keys": shared,
    }


def check_masking(rows: list[dict[str, Any]]) -> dict[str, Any]:
    bad: list[str] = []
    for row in rows:
        msgs = row["messages"]
        ok = len(row["loss_mask"]) == len(msgs) and all(
            m["loss_mask"] == (1 if m["role"] == "assistant" else 0) == row["loss_mask"][i]
            for i, m in enumerate(msgs)
        )
        ok &= sum(n for _, n in row["token_loss_mask_rle"]) == sum(m["approx_tokens"] for m in msgs)
        issued: set[str] = set()
        for m in msgs:
            if m["role"] == "assistant":
                issued.update(c["id"] for c in m.get("tool_calls") or [])
            elif m["role"] == "tool" and m.get("tool_call_id") not in issued:
                ok = False
        if not ok:
            bad.append(row["id"])
    return {"passed": not bad, "rows": len(rows), "bad_rows": bad}


def check_live_sft(rows: list[dict[str, Any]], fd_rows: int = 8) -> dict[str, Any]:
    masked_positions = active_positions = 0
    nonzero_masked = 0
    fd_failures: list[str] = []
    for i, row in enumerate(rows):
        labels, _mask, _ = token_labels(row)
        rng = _rng("sft-logits", row["id"])
        logits = [[rng.uniform(-3, 3) for _ in range(LIVE_VOCAB)] for _ in labels]
        weight = float(row.get("sft_trajectory_weight") or 1.0)
        _, grad = sft_loss_and_grad(logits, labels, weight)
        for t, y in enumerate(labels):
            if y == IGNORE_INDEX:
                masked_positions += 1
                nonzero_masked += any(v != 0.0 for v in grad[t])
            else:
                active_positions += 1
        if i < fd_rows:
            probe = [t for t, y in enumerate(labels) if y != IGNORE_INDEX][:2] + \
                    [t for t, y in enumerate(labels) if y == IGNORE_INDEX][:2]
            for t in probe:
                def f(row_logits: list[float], t=t) -> float:
                    patched = list(logits)
                    patched[t] = row_logits
                    return sft_loss_and_grad(patched, labels, weight)[0]
                if not all(_close(grad[t][v], finite_difference(f, logits[t], v)) for v in range(LIVE_VOCAB)):
                    fd_failures.append(f"{row['id']}@{t}")
    return {
        "passed": nonzero_masked == 0 and not fd_failures and active_positions > 0,
        "rows": len(rows),
        "masked_positions": masked_positions,
        "active_positions": active_positions,
        "masked_positions_with_gradient": nonzero_masked,
        "finite_difference_failures": fd_failures,
    }


def _groups(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        out[row["task_id"]].append(row)
    return {k: sorted(v, key=lambda r: r["rollout_id"]) for k, v in sorted(out.items())}


def check_live_advantages(rows: list[dict[str, Any]], lambda_twin: float) -> dict[str, Any]:
    mismatches: list[str] = []
    phase_residual_bad: list[str] = []
    collapsed = 0
    for task_id, group in _groups(rows).items():
        rewards = [float(r["reward"]) for r in group]
        grpo, is_collapsed = grpo_advantages(rewards)
        collapsed += int(is_collapsed)
        rloo = rloo_advantages(rewards)
        twin = twin_advantages(grpo, [float(r["policy_bias_penalty"]) for r in group], lambda_twin)
        for row, g, l, w in zip(group, grpo, rloo, twin):
            for key, want in (("grpo_advantage", g), ("rloo_advantage", l), ("twin_contrastive_advantage", w)):
                if abs(float(row[key]) - want) > ADV_TOL:
                    mismatches.append(f"{row['id']}:{key}")
            if bool(row["group"]["non_collapsed"]) == is_collapsed:
                mismatches.append(f"{row['id']}:non_collapsed")
        # A_step = alpha*A_grpo + (1-alpha)*z with z zero-mean per phase, so the residual is zero-mean.
        for phase in PHASES:
            residual = sum(float(r["phase_step_advantages"][phase]) - ALPHA_OUTCOME * float(r["grpo_advantage"])
                           for r in group)
            if abs(residual) > ADV_TOL * max(1, len(group)) * 10:
                phase_residual_bad.append(f"{task_id}:{phase}")
    return {
        "passed": not mismatches and not phase_residual_bad,
        "groups": len(_groups(rows)),
        "collapsed_groups": collapsed,
        "advantage_mismatches": mismatches,
        "phase_residual_nonzero": phase_residual_bad,
        "lambda_twin": lambda_twin,
    }


def check_live_dapo(rows: list[dict[str, Any]], fd_rows: int = 8) -> dict[str, Any]:
    kept = dropped = 0
    masked_with_grad = 0
    clipped = 0
    fd_failures: list[str] = []
    for i, row in enumerate(rows):
        if not row["group"]["non_collapsed"]:
            dropped += 1
            continue
        kept += 1
        _labels, mask, phases = token_labels(row)
        adv = [float(row["phase_step_advantages"][p]) if m else 0.0 for m, p in zip(mask, phases)]
        rng = _rng("dapo", row["id"])
        old = [rng.uniform(-4.0, -0.1) for _ in mask]
        new = [o + rng.uniform(-0.4, 0.4) for o in old]
        _, grad = dapo_loss_and_grad(new, old, adv, mask)
        for t, m in enumerate(mask):
            if not m:
                masked_with_grad += int(grad[t] != 0.0)
            else:
                ratio = math.exp(new[t] - old[t])
                clipped += int(not (CLIP_LOW <= ratio <= CLIP_HIGH))
        if i < fd_rows and not _check_fd_dapo(new, old, adv, mask):
            fd_failures.append(row["id"])
    return {
        "passed": masked_with_grad == 0 and not fd_failures and kept > 0,
        "rows_kept_non_collapsed": kept,
        "rows_dropped_collapsed": dropped,
        "masked_positions_with_gradient": masked_with_grad,
        "clipped_active_positions": clipped,
        "finite_difference_failures": fd_failures,
        "clip_bounds": [CLIP_LOW, CLIP_HIGH],
    }


def check_live_dpo_pairs(pairs: list[dict[str, Any]], eval_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Structural DPO-pair checks; a missing pairs file is reported, not failed (v1 datasets)."""
    if not pairs:
        return {"passed": True, "present": False, "pairs": 0}
    eval_tasks = {r["task_id"] for r in eval_rows}
    bad: list[str] = []
    for pair in pairs:
        chosen, rejected = pair.get("chosen") or {}, pair.get("rejected") or {}
        ok = (
            pair.get("split") == "train"
            and pair.get("task_id") not in eval_tasks
            and chosen.get("task_id") == rejected.get("task_id") == pair.get("task_id")
            and float(pair.get("reward_chosen", 0.0)) == 1.0
            and float(pair.get("reward_margin", 0.0)) > 0
            and abs(float(pair.get("reward_chosen", 0.0)) - float(pair.get("reward_rejected", 0.0)) - float(pair.get("reward_margin", 0.0))) < 1e-5
            and abs(float(pair.get("progress_chosen", 0.0)) - float(pair.get("progress_rejected", 0.0)) - float(pair.get("progress_margin", 0.0))) < 1e-5
            and isinstance(chosen.get("messages"), list) and isinstance(rejected.get("messages"), list)
        )
        if not ok:
            bad.append(str(pair.get("id")))
    return {
        "passed": not bad,
        "present": True,
        "pairs": len(pairs),
        "tasks": len({p.get("task_id") for p in pairs}),
        "bad_pairs": bad,
        "dpo_beta_reference": 0.1,
        "dpo_loss_formula": "-log sigmoid(beta*((pol_c-ref_c)-(pol_r-ref_r)))",
    }


def verify_cpu(data_dir: Path) -> dict[str, Any]:
    data_dir = Path(data_dir)
    card = json.loads((data_dir / CARD_FILE).read_text(encoding="utf-8"))
    sft = load_jsonl(data_dir / TRAIN_SFT_FILE)
    train = load_jsonl(data_dir / TRAIN_GRPO_FILE)
    held = load_jsonl(data_dir / EVAL_GRPO_FILE)
    dpo_path = data_dir / TRAIN_DPO_FILE
    dpo_pairs = load_jsonl(dpo_path) if dpo_path.is_file() else []
    lambda_twin = float((card.get("twin") or {}).get("lambda_twin") or 0.5)

    reference = reference_checks()
    live = {
        "split_disjoint": check_split(train, held, sft),
        "label_masking": check_masking(sft + train + held),
        "sft_loss_gradient": check_live_sft(sft),
        "advantages_recomputed": check_live_advantages(train + held, lambda_twin),
        "dapo_surrogate_gradient": check_live_dapo(train),
        "dpo_pairs": check_live_dpo_pairs(dpo_pairs, held),
    }
    return {
        "schema_version": 1,
        "data_files": {
            name: hashlib.sha256((data_dir / name).read_bytes()).hexdigest()
            for name in (TRAIN_SFT_FILE, TRAIN_GRPO_FILE, EVAL_GRPO_FILE, CARD_FILE)
            if (data_dir / name).is_file()
        },
        "rows": {"train_sft": len(sft), "train_grpo": len(train), "eval_holdout_grpo": len(held), "train_dpo_pairs": len(dpo_pairs)},
        "settings": {
            "ignore_index": IGNORE_INDEX,
            "clip_bounds": [CLIP_LOW, CLIP_HIGH],
            "alpha_outcome": ALPHA_OUTCOME,
            "lambda_twin": lambda_twin,
            "live_tokens_per_message": LIVE_TOKENS_PER_MESSAGE,
            "finite_difference_step": FD_STEP,
            "finite_difference_tolerance": FD_TOL,
        },
        "reference_checks": reference,
        "live_checks": live,
        "all_passed": all(c["passed"] for c in reference.values()) and all(c["passed"] for c in live.values()),
    }


# ---------------------------------------------------------------------------
# GPU bundle
# ---------------------------------------------------------------------------

EXECUTABLE_FILES = ("launch_gpu.sh", "run_sft_lora.py", "run_offline_rloo_dpo.py", "run_online_grpo_verl.py")


def generate_gpu_bundle(data_dir: Path, out_dir: Path) -> dict[str, Any]:
    report = verify_cpu(data_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, body in BUNDLE_FILES.items():
        path = out_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        if name in EXECUTABLE_FILES:
            path.chmod(0o755)
    (out_dir / VERIFICATION_FILE).write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest = {
        "data_dir": str(Path(data_dir)),
        "verification_all_passed": report["all_passed"],
        "cluster_configs": [FSDP_24K_CONFIG, DEEPSPEED_24K_CONFIG],
        "supported_cluster_gpus": list(SUPPORTED_CLUSTER_GPUS),
        "supported_models": list(SUPPORTED_MODELS),
        "max_seq_tokens": DEFAULT_MAX_SEQ_TOKENS,
        "truncation_side": "left_obs_first",
        "files": {
            p.relative_to(out_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(out_dir.rglob("*"))
            if p.is_file() and p.name != "manifest.json"
        },
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"report": report, "manifest": manifest}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("verify-cpu", "generate-gpu-bundle"), default="verify-cpu")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_BUNDLE_DIR)
    parser.add_argument("--json", type=Path, help="verify-cpu: also write the report here")
    args = parser.parse_args(argv)

    if args.mode == "verify-cpu":
        report = verify_cpu(args.data_dir)
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        report = generate_gpu_bundle(args.data_dir, args.out_dir)["report"]
    summary = {
        "all_passed": report["all_passed"],
        "rows": report["rows"],
        "failed": sorted(
            [f"reference:{k}" for k, v in report["reference_checks"].items() if not v["passed"]]
            + [f"live:{k}" for k, v in report["live_checks"].items() if not v["passed"]]
        ),
    }
    print(json.dumps(summary, sort_keys=True))
    return 0 if report["all_passed"] else 1


# Bundle file bodies, written verbatim by generate_gpu_bundle. They were only
# dry-run checked on a CPU host; they have not been executed on a GPU.

_BUNDLE_RUN_SFT_LORA_PY = r'''#!/usr/bin/env python3
"""LoRA cold-start SFT on train_sft.jsonl (transformers Trainer + PEFT).

Labels are built explicitly, message by message. The conversation prefix up to
and including message j is rendered with the tokenizer chat template
(tokenize=False), tokenized, and the token-length difference to the previous
prefix is the span of message j. Each new prefix must extend the previous one
token for token, otherwise the example is dropped (never mis-labelled).
Assistant spans (loss_mask == 1) keep their ids after the generation header.
Every other position gets label -100, so system, user and tool observation
tokens contribute zero loss.

Example weighting: custom loss. Each example's mean token NLL is multiplied by
sft_trajectory_weight, renormalised so the dataset mean weight is 1.0. This
keeps the effective learning rate comparable to unweighted SFT. A weighted
sampler was not used because Trainer's sampler hook changes across versions.

Heavy libraries (torch, transformers, peft) are imported lazily inside train().
--dry-run uses only the stdlib: it validates the data and prints a JSON plan.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import sys

# ---------------------------------------------------------------------------
# Shared stdlib helpers (duplicated across bundle scripts on purpose).
# ---------------------------------------------------------------------------

DEFAULT_DATA_DIR = "/home/ubuntu/alkera_factory_1017/rl_dataset_v1"
TRAIN_SPLIT = "train"
HOLDOUT_SPLIT = "eval_holdout"
HOLDOUT_FILE = "eval_holdout_grpo.jsonl"
ROLES = ("system", "user", "assistant", "tool")
PHASES = (
    "discovery",
    "epistemic_investigation",
    "pipeline_repair",
    "verification_and_execution",
    "governance_and_reporting",
)
SFT_KEYS = (
    "id", "task_id", "governance_mode", "role", "split", "prompt", "messages",
    "loss_mask", "token_loss_mask_rle", "sft_trajectory_weight", "reward",
)
GRPO_KEYS = (
    "id", "task_id", "run_id", "rollout_id", "theme", "fault_topology_id",
    "governance_mode", "role", "twin_group_id", "split", "prompt", "messages",
    "loss_mask", "token_loss_mask_rle", "reward", "passed", "outcome_class",
    "grpo_advantage", "rloo_advantage", "twin_contrastive_advantage",
    "policy_bias_penalty", "bias_kind", "phase_step_advantages",
    "seven_step_progress", "dapo_clip_bounds", "group",
)
MAX_REPORTED_ERRORS = 50
LIBS = ("torch", "transformers", "peft", "trl", "accelerate", "datasets", "verl", "flash_attn")


class DatasetError(Exception):
    pass


def load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            if not isinstance(obj, dict):
                raise DatasetError(f"{path}:{lineno}: expected a JSON object")
            rows.append(obj)
    return rows


def validate_rows(rows, path, required_keys, expected_split=TRAIN_SPLIT):
    """Return a list of error strings. Empty list means the rows are valid."""
    errors = []
    for idx, row in enumerate(rows, 1):
        where = f"{os.path.basename(path)}:{idx} id={row.get('id')!r}"
        missing = [k for k in required_keys if k not in row]
        if missing:
            errors.append(f"{where}: missing keys {missing}")
            continue
        if row["split"] != expected_split:
            errors.append(f"{where}: split={row['split']!r}, expected {expected_split!r}")
        msgs, mask = row["messages"], row["loss_mask"]
        if not isinstance(msgs, list) or not msgs:
            errors.append(f"{where}: messages must be a non-empty list")
            continue
        if not isinstance(mask, list) or len(mask) != len(msgs):
            errors.append(f"{where}: loss_mask length {len(mask) if isinstance(mask, list) else None} != messages length {len(msgs)}")
            continue
        issued = set()
        for j, (msg, bit) in enumerate(zip(msgs, mask)):
            at = f"{where} message[{j}]"
            if not isinstance(msg, dict):
                errors.append(f"{at}: not an object")
                continue
            role = msg.get("role")
            if role not in ROLES:
                errors.append(f"{at}: unknown role {role!r}")
            if type(bit) is not int or bit not in (0, 1):
                errors.append(f"{at}: loss_mask value {bit!r} is not 0/1")
            elif (bit == 1) != (role == "assistant"):
                errors.append(f"{at}: loss_mask={bit} but role={role!r} (must be 1 iff assistant)")
            if "loss_mask" in msg and msg["loss_mask"] != bit:
                errors.append(f"{at}: message loss_mask {msg['loss_mask']!r} != row loss_mask {bit!r}")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") if isinstance(tc, dict) else None
                    if not isinstance(fn, dict) or not tc.get("id") or not fn.get("name"):
                        errors.append(f"{at}: malformed tool_call {tc!r:.120}")
                        continue
                    if not isinstance(fn.get("arguments"), str):
                        errors.append(f"{at}: tool_call {tc['id']!r} arguments must be a JSON string")
                    issued.add(tc["id"])
            elif role == "tool":
                tid = msg.get("tool_call_id")
                if tid not in issued:
                    errors.append(f"{at}: tool_call_id {tid!r} does not answer an earlier assistant tool_call")
    return errors


def require_valid(rows, path, required_keys, expected_split=TRAIN_SPLIT):
    errors = validate_rows(rows, path, required_keys, expected_split)
    if errors:
        shown = "\n  ".join(errors[:MAX_REPORTED_ERRORS])
        more = "" if len(errors) <= MAX_REPORTED_ERRORS else f"\n  ... {len(errors) - MAX_REPORTED_ERRORS} more"
        raise DatasetError(f"{path}: {len(errors)} validation error(s):\n  {shown}{more}")


def load_train_file(data_dir, name, required_keys):
    path = os.path.join(data_dir, name)
    if not os.path.isfile(path):
        raise DatasetError(f"missing dataset file: {path}")
    rows = load_jsonl(path)
    if not rows:
        raise DatasetError(f"{path}: no rows")
    require_valid(rows, path, required_keys, TRAIN_SPLIT)
    return path, rows


def holdout_check(data_dir, train_rows):
    """Read the holdout file only to prove it is disjoint from training rows."""
    path = os.path.join(data_dir, HOLDOUT_FILE)
    if not os.path.isfile(path):
        return {"path": path, "present": False, "used_for_training": False}
    rows = load_jsonl(path)
    bad = [r.get("id") for r in rows if r.get("split") != HOLDOUT_SPLIT]
    if bad:
        raise DatasetError(f"{path}: {len(bad)} row(s) without split={HOLDOUT_SPLIT!r}, e.g. {bad[:3]}")
    id_overlap = sorted({r["id"] for r in train_rows} & {r.get("id") for r in rows})
    if id_overlap:
        raise DatasetError(f"{path}: {len(id_overlap)} holdout id(s) also in training data, e.g. {id_overlap[:3]}")
    task_overlap = sorted({r["task_id"] for r in train_rows} & {r.get("task_id") for r in rows})
    return {
        "path": path,
        "present": True,
        "rows": len(rows),
        "used_for_training": False,
        "id_overlap": 0,
        "task_id_overlap": len(task_overlap),
    }


def token_stats(rows, max_seq_len=None):
    roles = {r: 0 for r in ROLES}
    total = assistant = tool_calls = reasoning = rle_mismatch = over = 0
    longest = 0
    for row in rows:
        row_total = 0
        for msg in row["messages"]:
            n = int(msg.get("approx_tokens") or 0)
            roles[msg["role"]] = roles.get(msg["role"], 0) + 1
            row_total += n
            if msg["role"] == "assistant":
                assistant += n
                tool_calls += len(msg.get("tool_calls") or [])
                if msg.get("reasoning_content"):
                    reasoning += 1
        rle_on = sum(int(n) for m, n in row.get("token_loss_mask_rle") or [] if m == 1)
        row_on = sum(int(m.get("approx_tokens") or 0) for m in row["messages"] if m["role"] == "assistant")
        if rle_on != row_on:
            rle_mismatch += 1
        total += row_total
        longest = max(longest, row_total)
        if max_seq_len and row_total > max_seq_len:
            over += 1
    out = {
        "approx_tokens_total": total,
        "approx_tokens_assistant": assistant,
        "approx_tokens_masked_out": total - assistant,
        "approx_tokens_longest_row": longest,
        "messages_by_role": roles,
        "assistant_tool_calls": tool_calls,
        "assistant_messages_with_reasoning": reasoning,
        "rows_rle_mismatch_warning": rle_mismatch,
    }
    if max_seq_len:
        out["rows_over_max_seq_len_approx"] = over
    return out


def _obs_approx_tokens(msg):
    if msg.get("approx_tokens") is not None:
        return int(msg["approx_tokens"])
    chars = len(str(msg.get("content") or "")) + len(str(msg.get("reasoning_content") or ""))
    for tc in msg.get("tool_calls") or []:
        try:
            chars += len(str(tc["function"]["arguments"]))
        except Exception:
            chars += len(json.dumps(tc, ensure_ascii=False))
    return max(1, math.ceil(chars / 4))


def compact_tool_observations(messages, max_seq_tokens=24576, target_obs_chars=1200):
    """Compact role=='tool' observations only; assistant turns stay byte-identical.

    Long observations keep head+tail; if a row is still over budget the oldest
    tool observations are replaced by a placeholder first (left_obs_first), so
    final pipeline_repair / final_report.json assistant turns are never cut.
    Returns (messages, stats) with truncated_assistant_tokens always 0.
    """
    out = [dict(m) for m in messages]
    marker = "\n...[observation compacted]...\n"

    def total():
        return sum(_obs_approx_tokens(m) for m in out)

    def set_tool(i, content):
        out[i]["content"] = content
        out[i]["approx_tokens"] = max(1, math.ceil(len(content) / 4))

    changed = 0
    for i, msg in enumerate(out):
        if msg.get("role") != "tool" or not isinstance(msg.get("content"), str):
            continue
        content = msg["content"]
        if len(content) > target_obs_chars:
            keep = max(1, target_obs_chars - len(marker))
            half = keep // 2
            set_tool(i, content[:half] + marker + content[-(keep - half):])
            changed += 1
    target = target_obs_chars
    while total() > max_seq_tokens and target > 256:
        target = max(256, target // 2)
        for i, msg in enumerate(out):
            if msg.get("role") != "tool" or not isinstance(msg.get("content"), str):
                continue
            if len(msg["content"]) > target:
                keep = max(1, target - len(marker))
                half = keep // 2
                c = msg["content"]
                set_tool(i, c[:half] + marker + c[-(keep - half):])
                changed += 1
            if total() <= max_seq_tokens:
                break
    if total() > max_seq_tokens:
        for i, msg in enumerate(out):
            if total() <= max_seq_tokens:
                break
            if msg.get("role") == "tool":
                set_tool(i, "[observation compacted: left_obs_first]")
                out[i]["approx_tokens"] = 1
                changed += 1
    stats = {
        "original_tokens": sum(_obs_approx_tokens(m) for m in messages),
        "compacted_tokens": total(),
        "assistant_tokens_before": sum(_obs_approx_tokens(m) for m in messages if m.get("role") == "assistant"),
        "assistant_tokens_after": sum(_obs_approx_tokens(m) for m in out if m.get("role") == "assistant"),
        "truncated_assistant_tokens": 0,
        "fits_max_seq_tokens": total() <= max_seq_tokens,
        "compacted_tool_turns": changed,
    }
    return out, stats


def prepare_messages(messages, max_seq_len=24576, truncation_side="left_obs_first"):
    """Apply the requested truncation semantics at message level before tokenisation."""
    if truncation_side == "left_obs_first":
        return compact_tool_observations(messages, max_seq_tokens=max_seq_len)
    return [dict(m) for m in messages], {
        "truncated_assistant_tokens": 0,
        "truncation_side": truncation_side,
        "fits_max_seq_tokens": sum(_obs_approx_tokens(m) for m in messages) <= max_seq_len,
    }


def libs_available():
    return {name: importlib.util.find_spec(name) is not None for name in LIBS}


def emit(obj):
    print(json.dumps(obj, indent=2, sort_keys=False))


# ---------------------------------------------------------------------------
# Chat-template tokenisation with explicit labels (shared with the offline script).
# ---------------------------------------------------------------------------

class TemplateAlignmentError(Exception):
    pass


def parse_tool_arguments(raw):
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    return val if isinstance(val, dict) else raw


def template_supports_tools(tokenizer):
    tmpl = tokenizer.chat_template
    if isinstance(tmpl, dict):
        tmpl = tmpl.get("tool_use") or tmpl.get("default") or ""
    return "tool_calls" in (tmpl or "")


def to_chat(messages, include_reasoning, tool_mode):
    """Strip dataset-only keys and map to what HF chat templates expect."""
    conv = []
    for msg in messages:
        role = msg["role"]
        content = msg.get("content") or ""
        if role == "assistant":
            text = content
            reasoning = msg.get("reasoning_content")
            if include_reasoning and reasoning:
                text = f"<think>\n{reasoning.strip()}\n</think>\n\n{content}"
            out = {"role": "assistant", "content": text}
            calls = msg.get("tool_calls") or []
            if calls and tool_mode == "template":
                out["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["function"]["name"],
                            "arguments": parse_tool_arguments(tc["function"]["arguments"]),
                        },
                    }
                    for tc in calls
                ]
            elif calls:
                blocks = [
                    "<tool_call>\n"
                    + json.dumps(
                        {"name": tc["function"]["name"], "arguments": parse_tool_arguments(tc["function"]["arguments"])},
                        ensure_ascii=False,
                    )
                    + "\n</tool_call>"
                    for tc in calls
                ]
                out["content"] = (text + "\n" if text else "") + "\n".join(blocks)
            conv.append(out)
        elif role == "tool":
            if tool_mode == "template":
                conv.append({"role": "tool", "tool_call_id": msg.get("tool_call_id"), "content": content})
            else:
                conv.append({"role": "user", "content": f"<tool_response>\n{content}\n</tool_response>"})
        else:
            conv.append({"role": role, "content": content})
    return conv


def _render(tokenizer, conv, tools, add_generation_prompt):
    kwargs = {"tokenize": False, "add_generation_prompt": add_generation_prompt}
    if tools:
        kwargs["tools"] = tools
    return tokenizer.apply_chat_template(conv, **kwargs)


def _encode(tokenizer, text):
    return tokenizer(text, add_special_tokens=False)["input_ids"]


def build_example(tokenizer, messages, *, include_reasoning, tool_mode, tools, max_seq_len, message_weights=None, truncation_side="left_obs_first"):
    """Tokenise one conversation. Returns ids, labels (-100 outside assistant) and per-token weights.

    truncation_side="left_obs_first": after building the full conversation,
    leftmost masked (label -100, i.e. observation) tokens are dropped first;
    assistant label tokens are never dropped, so the final pipeline_repair /
    final_report.json turns cannot be cut. Other sides keep legacy slicing.
    """
    conv = to_chat(messages, include_reasoning, tool_mode)
    ids, labels, weights = [], [], []
    header_fallbacks = 0
    truncated = False
    for j, msg in enumerate(messages):
        start = len(ids)
        cur = _encode(tokenizer, _render(tokenizer, conv[: j + 1], tools, False))
        if cur[:start] != ids:
            raise TemplateAlignmentError(f"message {j}: rendered prefix is not token-stable with the previous prefix")
        if msg["role"] == "assistant":
            gen_start = start
            if j > 0:
                head = _encode(tokenizer, _render(tokenizer, conv[:j], tools, True))
                if start <= len(head) <= len(cur) and cur[: len(head)] == head:
                    gen_start = len(head)
                else:
                    header_fallbacks += 1
            w = 1.0 if message_weights is None else float(message_weights[j])
            labels.extend([-100] * (gen_start - start) + cur[gen_start:])
            weights.extend([0.0] * (gen_start - start) + [w] * (len(cur) - gen_start))
        else:
            labels.extend([-100] * (len(cur) - start))
            weights.extend([0.0] * (len(cur) - start))
        ids = cur
        if truncation_side != "left_obs_first" and len(ids) >= max_seq_len:
            truncated = len(ids) > max_seq_len or j < len(messages) - 1
            break
    if truncation_side == "left_obs_first":
        if len(ids) > max_seq_len:
            truncated = True
            excess = len(ids) - max_seq_len
            keep = []
            for idx, lab in enumerate(labels):
                if excess and lab == -100:
                    excess -= 1
                    continue
                keep.append(idx)
            # If assistant tokens alone exceed the budget they are kept whole
            # (reported via truncated) rather than cutting the tail turns.
            ids = [ids[i] for i in keep]
            labels = [labels[i] for i in keep]
            weights = [weights[i] for i in keep]
    else:
        ids, labels, weights = ids[:max_seq_len], labels[:max_seq_len], weights[:max_seq_len]
    if not (len(ids) == len(labels) == len(weights)):
        raise TemplateAlignmentError("ids/labels/weights length mismatch")
    return {
        "input_ids": ids,
        "labels": labels,
        "token_weights": weights,
        "n_label_tokens": sum(1 for x in labels if x != -100),
        "truncated": truncated,
        "header_fallbacks": header_fallbacks,
    }


def resolve_tool_mode(tokenizer, requested):
    if requested != "auto":
        return requested
    return "template" if template_supports_tools(tokenizer) else "json"


def load_tools(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        tools = json.load(fh)
    if not isinstance(tools, list):
        raise DatasetError(f"{path}: --tools-json must contain a JSON list of tool specs")
    return tools


# ---------------------------------------------------------------------------
# Script-specific code.
# ---------------------------------------------------------------------------

DEFAULT_LORA_TARGETS = "q_proj,k_proj,v_proj,o_proj,gate_proj,up_proj,down_proj"


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    p.add_argument("--model", default="Qwen/Qwen2.5-Coder-14B-Instruct",
                   help="HF model id or path. Tested choices: Qwen/Qwen2.5-Coder-14B-Instruct (default), "
                        "Qwen/Qwen3-14B (use --include-reasoning; its template is not prefix-stable otherwise).")
    p.add_argument("--output-dir", default="outputs/sft_lora")
    p.add_argument("--max-seq-len", type=int, default=24576)
    p.add_argument("--truncation-side", choices=("left_obs_first", "right", "left"), default="left_obs_first",
                   help="left_obs_first (default): compact/drop tool observations from the left; "
                        "final assistant pipeline_repair/final_report.json turns are never cut.")
    p.add_argument("--include-reasoning", action="store_true",
                   help="Prepend reasoning_content as <think>...</think> to assistant content.")
    p.add_argument("--tool-format", choices=("auto", "template", "json"), default="auto",
                   help="template: pass tool_calls/tool role to the chat template. json: serialise as text.")
    p.add_argument("--tools-json", default=None, help="Optional JSON list of tool specs passed as tools=.")
    p.add_argument("--weighting", choices=("loss", "none"), default="loss",
                   help="loss: per-example NLL times normalised sft_trajectory_weight.")
    p.add_argument("--lora-r", type=int, default=64)
    p.add_argument("--lora-alpha", type=int, default=128)
    p.add_argument("--lora-dropout", type=float, default=0.05)
    p.add_argument("--lora-target", default=DEFAULT_LORA_TARGETS)
    p.add_argument("--lr", type=float, default=1e-4)
    p.add_argument("--epochs", type=float, default=2.0)
    p.add_argument("--per-device-batch-size", type=int, default=1)
    p.add_argument("--grad-accum", type=int, default=8)
    p.add_argument("--warmup-ratio", type=float, default=0.03)
    p.add_argument("--logprob-chunk", type=int, default=4096,
                   help="Sequence chunk for the lm_head/log-softmax, avoids materialising full-vocab logits.")
    p.add_argument("--attn-impl", default="auto", help="auto | flash_attention_2 | sdpa | eager")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--max-rows", type=int, default=0, help="Debug: use only the first N rows (0 = all).")
    p.add_argument("--trust-remote-code", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args(argv)


def normalised_weights(rows, mode):
    if mode == "none":
        return [1.0] * len(rows)
    raw = [float(r["sft_trajectory_weight"]) for r in rows]
    if any(not math.isfinite(w) or w < 0 for w in raw):
        raise DatasetError("sft_trajectory_weight must be finite and >= 0")
    mean = sum(raw) / len(raw)
    if mean <= 0:
        raise DatasetError("all sft_trajectory_weight values are zero")
    return [w / mean for w in raw]


def hyperparams(args):
    return {
        "model": args.model,
        "supported_models": ["Qwen/Qwen2.5-Coder-7B-Instruct", "Qwen/Qwen2.5-Coder-14B-Instruct", "Qwen/Qwen2.5-Coder-32B-Instruct"],
        "max_seq_len": args.max_seq_len,
        "truncation_side": getattr(args, "truncation_side", "left_obs_first"),
        "include_reasoning": args.include_reasoning,
        "tool_format": args.tool_format,
        "weighting": args.weighting,
        "lora": {"r": args.lora_r, "alpha": args.lora_alpha, "dropout": args.lora_dropout,
                 "target_modules": args.lora_target.split(",")},
        "lr": args.lr,
        "lr_scheduler": "cosine",
        "warmup_ratio": args.warmup_ratio,
        "epochs": args.epochs,
        "per_device_batch_size": args.per_device_batch_size,
        "grad_accum": args.grad_accum,
        "bf16": True,
        "gradient_checkpointing": True,
        "seed": args.seed,
        "output_dir": args.output_dir,
    }


def build_plan(args):
    path, rows = load_train_file(args.data_dir, "train_sft.jsonl", SFT_KEYS)
    if args.max_rows:
        rows = rows[: args.max_rows]
    # 24k observation-compacted loader: assistant turns are never truncated.
    rows = [{**r, "messages": prepare_messages(r["messages"], args.max_seq_len, getattr(args, "truncation_side", "left_obs_first"))[0]} for r in rows]
    weights = normalised_weights(rows, args.weighting)
    raw = [float(r["sft_trajectory_weight"]) for r in rows]
    tools = load_tools(args.tools_json)
    plan = {
        "script": "run_sft_lora.py",
        "dry_run": args.dry_run,
        "data_file": path,
        "rows": len(rows),
        "tasks": len({r["task_id"] for r in rows}),
        "by_governance_mode": _count(rows, "governance_mode"),
        "sft_trajectory_weight": {
            "raw_min": min(raw), "raw_max": max(raw), "raw_mean": round(sum(raw) / len(raw), 6),
            "normalised_min": round(min(weights), 6), "normalised_max": round(max(weights), 6),
        },
        "tokens": token_stats(rows, args.max_seq_len),
        "masking": "labels=-100 for every non-assistant token; assistant tokens after the generation header keep ids",
        "truncation_side": getattr(args, "truncation_side", "left_obs_first"),
        "assistant_tokens_truncated": 0,
        "tools_json_entries": len(tools) if tools else 0,
        "holdout": holdout_check(args.data_dir, rows),
        "hyperparams": hyperparams(args),
        "libraries_available": libs_available(),
    }
    return plan, rows, weights, tools


def _count(rows, key):
    out = {}
    for r in rows:
        out[str(r.get(key))] = out.get(str(r.get(key)), 0) + 1
    return dict(sorted(out.items()))


def label_logprobs(torch, policy, input_ids, attention_mask, labels, chunk):
    """Per-token log p(label) for shifted labels, computed in checkpointed chunks."""
    from torch.utils.checkpoint import checkpoint

    base = policy.get_base_model() if hasattr(policy, "get_base_model") else policy
    hidden = base.get_decoder()(input_ids=input_ids, attention_mask=attention_mask, use_cache=False).last_hidden_state
    lm_head = base.get_output_embeddings()
    hidden = hidden[:, :-1, :]
    target = labels[:, 1:]
    valid = target.ne(-100)
    safe = target.clamp(min=0)

    def chunk_logp(h, t):
        logits = lm_head(h).float()
        return logits.gather(-1, t.unsqueeze(-1)).squeeze(-1) - torch.logsumexp(logits, dim=-1)

    parts = []
    for s in range(0, hidden.size(1), chunk):
        h, t = hidden[:, s:s + chunk], safe[:, s:s + chunk]
        if torch.is_grad_enabled():
            parts.append(checkpoint(chunk_logp, h, t, use_reentrant=False))
        else:
            parts.append(chunk_logp(h, t))
    logp = torch.cat(parts, dim=1)
    return logp * valid, valid


def train(args, plan, rows, weights, tools):
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer, Trainer, TrainingArguments

    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=args.trust_remote_code)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tool_mode = resolve_tool_mode(tokenizer, args.tool_format)

    examples, dropped = [], {"template_unstable": 0, "no_label_tokens": 0}
    truncated = header_fallbacks = 0
    for row, w in zip(rows, weights):
        try:
            ex = build_example(tokenizer, row["messages"], include_reasoning=args.include_reasoning,
                               tool_mode=tool_mode, tools=tools, max_seq_len=args.max_seq_len,
                               truncation_side=getattr(args, "truncation_side", "left_obs_first"))
        except TemplateAlignmentError as exc:
            dropped["template_unstable"] += 1
            print(f"[sft] drop {row['id']}: {exc}", file=sys.stderr)
            continue
        if ex["n_label_tokens"] == 0:
            dropped["no_label_tokens"] += 1
            continue
        truncated += ex["truncated"]
        header_fallbacks += ex["header_fallbacks"]
        examples.append({"input_ids": ex["input_ids"], "labels": ex["labels"], "example_weight": w})
    stats = {"tool_mode": tool_mode, "examples": len(examples), "dropped": dropped, "truncated": truncated,
             "header_fallbacks": header_fallbacks,
             "label_tokens": sum(sum(1 for x in e["labels"] if x != -100) for e in examples),
             "tokens": sum(len(e["input_ids"]) for e in examples)}
    print("[sft] tokenisation " + json.dumps(stats), file=sys.stderr)
    if not examples:
        raise SystemExit("no trainable examples after tokenisation")
    if dropped["template_unstable"] > 0.05 * len(rows):
        raise SystemExit("more than 5% of rows are not prefix-stable under this chat template; "
                         "try --include-reasoning, --tool-format json or another model")

    attn = args.attn_impl
    if attn == "auto":
        attn = "flash_attention_2" if importlib.util.find_spec("flash_attn") else "sdpa"
    model = AutoModelForCausalLM.from_pretrained(args.model, torch_dtype=torch.bfloat16, attn_implementation=attn,
                                                 trust_remote_code=args.trust_remote_code)
    model.config.use_cache = False
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    policy = get_peft_model(model, LoraConfig(
        r=args.lora_r, lora_alpha=args.lora_alpha, lora_dropout=args.lora_dropout,
        target_modules=args.lora_target.split(","), bias="none", task_type="CAUSAL_LM"))
    policy.print_trainable_parameters()

    chunk = args.logprob_chunk

    class WeightedSFT(torch.nn.Module):
        def __init__(self, inner):
            super().__init__()
            self.policy = inner

        def forward(self, input_ids, attention_mask, labels, example_weight):
            logp, valid = label_logprobs(torch, self.policy, input_ids, attention_mask, labels, chunk)
            per_example = -logp.sum(1) / valid.sum(1).clamp(min=1)
            return {"loss": (per_example * example_weight.to(per_example.dtype)).mean()}

    class ListDataset(torch.utils.data.Dataset):
        def __init__(self, items):
            self.items = items

        def __len__(self):
            return len(self.items)

        def __getitem__(self, i):
            return self.items[i]

    pad_id = tokenizer.pad_token_id

    def collate(features):
        n = max(len(f["input_ids"]) for f in features)
        ids = torch.full((len(features), n), pad_id, dtype=torch.long)
        lab = torch.full((len(features), n), -100, dtype=torch.long)
        att = torch.zeros((len(features), n), dtype=torch.long)
        for i, f in enumerate(features):
            k = len(f["input_ids"])
            ids[i, :k] = torch.tensor(f["input_ids"])
            lab[i, :k] = torch.tensor(f["labels"])
            att[i, :k] = 1
        w = torch.tensor([f["example_weight"] for f in features], dtype=torch.float32)
        return {"input_ids": ids, "attention_mask": att, "labels": lab, "example_weight": w}

    class ObjectiveTrainer(Trainer):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            # The objective ignores num_items_in_batch, so Trainer must divide by grad-accum itself.
            self.model_accepts_loss_kwargs = False

        def compute_loss(self, model, inputs, return_outputs=False, **kwargs):
            out = model(**inputs)
            return (out["loss"], out) if return_outputs else out["loss"]

    targs = TrainingArguments(
        output_dir=args.output_dir, per_device_train_batch_size=args.per_device_batch_size,
        gradient_accumulation_steps=args.grad_accum, learning_rate=args.lr, lr_scheduler_type="cosine",
        warmup_ratio=args.warmup_ratio, num_train_epochs=args.epochs, bf16=True, logging_steps=1,
        save_strategy="no", report_to="none", remove_unused_columns=False, dataloader_num_workers=0,
        ddp_find_unused_parameters=False, seed=args.seed, max_grad_norm=1.0)
    trainer = ObjectiveTrainer(model=WeightedSFT(policy), args=targs, train_dataset=ListDataset(examples),
                               data_collator=collate)
    trainer.train()
    if trainer.is_world_process_zero():
        os.makedirs(args.output_dir, exist_ok=True)
        policy.save_pretrained(args.output_dir)
        tokenizer.save_pretrained(args.output_dir)
        with open(os.path.join(args.output_dir, "bundle_run.json"), "w", encoding="utf-8") as fh:
            json.dump({"plan": plan, "tokenisation": stats}, fh, indent=2)
        print(f"[sft] adapter saved to {args.output_dir}", file=sys.stderr)


def main(argv=None):
    args = parse_args(argv)
    try:
        plan, rows, weights, tools = build_plan(args)
    except DatasetError as exc:
        print(f"dataset validation failed: {exc}", file=sys.stderr)
        return 1
    if args.dry_run or os.environ.get("RANK", "0") == "0":
        emit(plan)
    if args.dry_run:
        return 0
    train(args, plan, rows, weights, tools)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

_BUNDLE_RUN_OFFLINE_RLOO_DPO_PY = r'''#!/usr/bin/env python3
"""Offline policy improvement on train_grpo.jsonl: AWR, RLOO or DPO.

Only rows whose group.non_collapsed is true are used for AWR/RLOO (groups
with reward std > 0). Groups are keyed by task_id. DPO pairs from
train_dpo_pairs.jsonl are used directly when that file is present, so
desaturated / cross-model pairs are not subject to the collapsed filter.

--algo awr
    Advantage-weighted regression on assistant tokens:
        loss = -sum(w * log p(assistant tokens)) / n_assistant_tokens
    w comes from phase_step_advantages[message.causal_phase] (per token, by
    message), or from --advantage-key as one weight per sequence. Weights are
    clipped to [-adv_clip, adv_clip] (linear) or to (0, adv_clip] (exp).
--algo rloo
    Same weighted regression with the leave-one-out rloo_advantage of each
    sequence as the per-token weight on its assistant tokens.
--algo dpo
    Preference loss L = -log sigmoid(beta*((pol_chosen-ref_chosen) -
    (pol_rejected-ref_rejected))) over train_dpo_pairs.jsonl pairs, or one
    pair per task_id group (highest-reward chosen vs lowest rejected, kept
    when the reward difference > --min-margin) as a fallback. The default
    --dpo-impl manual computes the DPO loss on assistant tokens only, with the
    adapter-disabled model as the reference. --dpo-impl trl uses TRL DPOTrainer;
    it scores every completion token, so tool observations inside the
    trajectory are NOT masked on that path. If the TRL API does not match, the
    script falls back to the manual loss.

Labels use the same chat-template prefix-diff construction as run_sft_lora.py:
non-assistant positions are -100 and contribute nothing.

Heavy libraries are imported lazily inside the train functions. --dry-run uses
only the stdlib.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import statistics
import sys

# ---------------------------------------------------------------------------
# Shared stdlib helpers (duplicated across bundle scripts on purpose).
# ---------------------------------------------------------------------------

DEFAULT_DATA_DIR = "/home/ubuntu/alkera_factory_1017/rl_dataset_v1"
TRAIN_SPLIT = "train"
HOLDOUT_SPLIT = "eval_holdout"
HOLDOUT_FILE = "eval_holdout_grpo.jsonl"
ROLES = ("system", "user", "assistant", "tool")
PHASES = (
    "discovery",
    "epistemic_investigation",
    "pipeline_repair",
    "verification_and_execution",
    "governance_and_reporting",
)
SFT_KEYS = (
    "id", "task_id", "governance_mode", "role", "split", "prompt", "messages",
    "loss_mask", "token_loss_mask_rle", "sft_trajectory_weight", "reward",
)
GRPO_KEYS = (
    "id", "task_id", "run_id", "rollout_id", "theme", "fault_topology_id",
    "governance_mode", "role", "twin_group_id", "split", "prompt", "messages",
    "loss_mask", "token_loss_mask_rle", "reward", "passed", "outcome_class",
    "grpo_advantage", "rloo_advantage", "twin_contrastive_advantage",
    "policy_bias_penalty", "bias_kind", "phase_step_advantages",
    "seven_step_progress", "dapo_clip_bounds", "group",
)
MAX_REPORTED_ERRORS = 50
LIBS = ("torch", "transformers", "peft", "trl", "accelerate", "datasets", "verl", "flash_attn")


class DatasetError(Exception):
    pass


def load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            if not isinstance(obj, dict):
                raise DatasetError(f"{path}:{lineno}: expected a JSON object")
            rows.append(obj)
    return rows


def validate_rows(rows, path, required_keys, expected_split=TRAIN_SPLIT):
    """Return a list of error strings. Empty list means the rows are valid."""
    errors = []
    for idx, row in enumerate(rows, 1):
        where = f"{os.path.basename(path)}:{idx} id={row.get('id')!r}"
        missing = [k for k in required_keys if k not in row]
        if missing:
            errors.append(f"{where}: missing keys {missing}")
            continue
        if row["split"] != expected_split:
            errors.append(f"{where}: split={row['split']!r}, expected {expected_split!r}")
        msgs, mask = row["messages"], row["loss_mask"]
        if not isinstance(msgs, list) or not msgs:
            errors.append(f"{where}: messages must be a non-empty list")
            continue
        if not isinstance(mask, list) or len(mask) != len(msgs):
            errors.append(f"{where}: loss_mask length {len(mask) if isinstance(mask, list) else None} != messages length {len(msgs)}")
            continue
        issued = set()
        for j, (msg, bit) in enumerate(zip(msgs, mask)):
            at = f"{where} message[{j}]"
            if not isinstance(msg, dict):
                errors.append(f"{at}: not an object")
                continue
            role = msg.get("role")
            if role not in ROLES:
                errors.append(f"{at}: unknown role {role!r}")
            if type(bit) is not int or bit not in (0, 1):
                errors.append(f"{at}: loss_mask value {bit!r} is not 0/1")
            elif (bit == 1) != (role == "assistant"):
                errors.append(f"{at}: loss_mask={bit} but role={role!r} (must be 1 iff assistant)")
            if "loss_mask" in msg and msg["loss_mask"] != bit:
                errors.append(f"{at}: message loss_mask {msg['loss_mask']!r} != row loss_mask {bit!r}")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") if isinstance(tc, dict) else None
                    if not isinstance(fn, dict) or not tc.get("id") or not fn.get("name"):
                        errors.append(f"{at}: malformed tool_call {tc!r:.120}")
                        continue
                    if not isinstance(fn.get("arguments"), str):
                        errors.append(f"{at}: tool_call {tc['id']!r} arguments must be a JSON string")
                    issued.add(tc["id"])
            elif role == "tool":
                tid = msg.get("tool_call_id")
                if tid not in issued:
                    errors.append(f"{at}: tool_call_id {tid!r} does not answer an earlier assistant tool_call")
    return errors


def require_valid(rows, path, required_keys, expected_split=TRAIN_SPLIT):
    errors = validate_rows(rows, path, required_keys, expected_split)
    if errors:
        shown = "\n  ".join(errors[:MAX_REPORTED_ERRORS])
        more = "" if len(errors) <= MAX_REPORTED_ERRORS else f"\n  ... {len(errors) - MAX_REPORTED_ERRORS} more"
        raise DatasetError(f"{path}: {len(errors)} validation error(s):\n  {shown}{more}")


def load_train_file(data_dir, name, required_keys):
    path = os.path.join(data_dir, name)
    if not os.path.isfile(path):
        raise DatasetError(f"missing dataset file: {path}")
    rows = load_jsonl(path)
    if not rows:
        raise DatasetError(f"{path}: no rows")
    require_valid(rows, path, required_keys, TRAIN_SPLIT)
    return path, rows


def holdout_check(data_dir, train_rows):
    """Read the holdout file only to prove it is disjoint from training rows."""
    path = os.path.join(data_dir, HOLDOUT_FILE)
    if not os.path.isfile(path):
        return {"path": path, "present": False, "used_for_training": False}
    rows = load_jsonl(path)
    bad = [r.get("id") for r in rows if r.get("split") != HOLDOUT_SPLIT]
    if bad:
        raise DatasetError(f"{path}: {len(bad)} row(s) without split={HOLDOUT_SPLIT!r}, e.g. {bad[:3]}")
    id_overlap = sorted({r["id"] for r in train_rows} & {r.get("id") for r in rows})
    if id_overlap:
        raise DatasetError(f"{path}: {len(id_overlap)} holdout id(s) also in training data, e.g. {id_overlap[:3]}")
    task_overlap = sorted({r["task_id"] for r in train_rows} & {r.get("task_id") for r in rows})
    return {
        "path": path,
        "present": True,
        "rows": len(rows),
        "used_for_training": False,
        "id_overlap": 0,
        "task_id_overlap": len(task_overlap),
    }


def token_stats(rows, max_seq_len=None):
    roles = {r: 0 for r in ROLES}
    total = assistant = tool_calls = reasoning = rle_mismatch = over = 0
    longest = 0
    for row in rows:
        row_total = 0
        for msg in row["messages"]:
            n = int(msg.get("approx_tokens") or 0)
            roles[msg["role"]] = roles.get(msg["role"], 0) + 1
            row_total += n
            if msg["role"] == "assistant":
                assistant += n
                tool_calls += len(msg.get("tool_calls") or [])
                if msg.get("reasoning_content"):
                    reasoning += 1
        rle_on = sum(int(n) for m, n in row.get("token_loss_mask_rle") or [] if m == 1)
        row_on = sum(int(m.get("approx_tokens") or 0) for m in row["messages"] if m["role"] == "assistant")
        if rle_on != row_on:
            rle_mismatch += 1
        total += row_total
        longest = max(longest, row_total)
        if max_seq_len and row_total > max_seq_len:
            over += 1
    out = {
        "approx_tokens_total": total,
        "approx_tokens_assistant": assistant,
        "approx_tokens_masked_out": total - assistant,
        "approx_tokens_longest_row": longest,
        "messages_by_role": roles,
        "assistant_tool_calls": tool_calls,
        "assistant_messages_with_reasoning": reasoning,
        "rows_rle_mismatch_warning": rle_mismatch,
    }
    if max_seq_len:
        out["rows_over_max_seq_len_approx"] = over
    return out


def _obs_approx_tokens(msg):
    if msg.get("approx_tokens") is not None:
        return int(msg["approx_tokens"])
    chars = len(str(msg.get("content") or "")) + len(str(msg.get("reasoning_content") or ""))
    for tc in msg.get("tool_calls") or []:
        try:
            chars += len(str(tc["function"]["arguments"]))
        except Exception:
            chars += len(json.dumps(tc, ensure_ascii=False))
    return max(1, math.ceil(chars / 4))


def compact_tool_observations(messages, max_seq_tokens=24576, target_obs_chars=1200):
    """Compact role=='tool' observations only; assistant turns stay byte-identical.

    Long observations keep head+tail; if a row is still over budget the oldest
    tool observations are replaced by a placeholder first (left_obs_first), so
    final pipeline_repair / final_report.json assistant turns are never cut.
    Returns (messages, stats) with truncated_assistant_tokens always 0.
    """
    out = [dict(m) for m in messages]
    marker = "\n...[observation compacted]...\n"

    def total():
        return sum(_obs_approx_tokens(m) for m in out)

    def set_tool(i, content):
        out[i]["content"] = content
        out[i]["approx_tokens"] = max(1, math.ceil(len(content) / 4))

    changed = 0
    for i, msg in enumerate(out):
        if msg.get("role") != "tool" or not isinstance(msg.get("content"), str):
            continue
        content = msg["content"]
        if len(content) > target_obs_chars:
            keep = max(1, target_obs_chars - len(marker))
            half = keep // 2
            set_tool(i, content[:half] + marker + content[-(keep - half):])
            changed += 1
    target = target_obs_chars
    while total() > max_seq_tokens and target > 256:
        target = max(256, target // 2)
        for i, msg in enumerate(out):
            if msg.get("role") != "tool" or not isinstance(msg.get("content"), str):
                continue
            if len(msg["content"]) > target:
                keep = max(1, target - len(marker))
                half = keep // 2
                c = msg["content"]
                set_tool(i, c[:half] + marker + c[-(keep - half):])
                changed += 1
            if total() <= max_seq_tokens:
                break
    if total() > max_seq_tokens:
        for i, msg in enumerate(out):
            if total() <= max_seq_tokens:
                break
            if msg.get("role") == "tool":
                set_tool(i, "[observation compacted: left_obs_first]")
                out[i]["approx_tokens"] = 1
                changed += 1
    stats = {
        "original_tokens": sum(_obs_approx_tokens(m) for m in messages),
        "compacted_tokens": total(),
        "assistant_tokens_before": sum(_obs_approx_tokens(m) for m in messages if m.get("role") == "assistant"),
        "assistant_tokens_after": sum(_obs_approx_tokens(m) for m in out if m.get("role") == "assistant"),
        "truncated_assistant_tokens": 0,
        "fits_max_seq_tokens": total() <= max_seq_tokens,
        "compacted_tool_turns": changed,
    }
    return out, stats


def prepare_messages(messages, max_seq_len=24576, truncation_side="left_obs_first"):
    """Apply the requested truncation semantics at message level before tokenisation."""
    if truncation_side == "left_obs_first":
        return compact_tool_observations(messages, max_seq_tokens=max_seq_len)
    return [dict(m) for m in messages], {
        "truncated_assistant_tokens": 0,
        "truncation_side": truncation_side,
        "fits_max_seq_tokens": sum(_obs_approx_tokens(m) for m in messages) <= max_seq_len,
    }


def libs_available():
    return {name: importlib.util.find_spec(name) is not None for name in LIBS}


def emit(obj):
    print(json.dumps(obj, indent=2, sort_keys=False))


# ---------------------------------------------------------------------------
# Chat-template tokenisation with explicit labels (same as run_sft_lora.py).
# ---------------------------------------------------------------------------

class TemplateAlignmentError(Exception):
    pass


def parse_tool_arguments(raw):
    if isinstance(raw, dict):
        return raw
    try:
        val = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    return val if isinstance(val, dict) else raw


def template_supports_tools(tokenizer):
    tmpl = tokenizer.chat_template
    if isinstance(tmpl, dict):
        tmpl = tmpl.get("tool_use") or tmpl.get("default") or ""
    return "tool_calls" in (tmpl or "")


def to_chat(messages, include_reasoning, tool_mode):
    """Strip dataset-only keys and map to what HF chat templates expect."""
    conv = []
    for msg in messages:
        role = msg["role"]
        content = msg.get("content") or ""
        if role == "assistant":
            text = content
            reasoning = msg.get("reasoning_content")
            if include_reasoning and reasoning:
                text = f"<think>\n{reasoning.strip()}\n</think>\n\n{content}"
            out = {"role": "assistant", "content": text}
            calls = msg.get("tool_calls") or []
            if calls and tool_mode == "template":
                out["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["function"]["name"],
                            "arguments": parse_tool_arguments(tc["function"]["arguments"]),
                        },
                    }
                    for tc in calls
                ]
            elif calls:
                blocks = [
                    "<tool_call>\n"
                    + json.dumps(
                        {"name": tc["function"]["name"], "arguments": parse_tool_arguments(tc["function"]["arguments"])},
                        ensure_ascii=False,
                    )
                    + "\n</tool_call>"
                    for tc in calls
                ]
                out["content"] = (text + "\n" if text else "") + "\n".join(blocks)
            conv.append(out)
        elif role == "tool":
            if tool_mode == "template":
                conv.append({"role": "tool", "tool_call_id": msg.get("tool_call_id"), "content": content})
            else:
                conv.append({"role": "user", "content": f"<tool_response>\n{content}\n</tool_response>"})
        else:
            conv.append({"role": role, "content": content})
    return conv


def _render(tokenizer, conv, tools, add_generation_prompt):
    kwargs = {"tokenize": False, "add_generation_prompt": add_generation_prompt}
    if tools:
        kwargs["tools"] = tools
    return tokenizer.apply_chat_template(conv, **kwargs)


def _encode(tokenizer, text):
    return tokenizer(text, add_special_tokens=False)["input_ids"]


def build_example(tokenizer, messages, *, include_reasoning, tool_mode, tools, max_seq_len, message_weights=None, truncation_side="left_obs_first"):
    """Tokenise one conversation. Returns ids, labels (-100 outside assistant) and per-token weights.

    truncation_side="left_obs_first": after building the full conversation,
    leftmost masked (label -100, i.e. observation) tokens are dropped first;
    assistant label tokens are never dropped, so the final pipeline_repair /
    final_report.json turns cannot be cut. Other sides keep legacy slicing.
    """
    conv = to_chat(messages, include_reasoning, tool_mode)
    ids, labels, weights = [], [], []
    header_fallbacks = 0
    truncated = False
    for j, msg in enumerate(messages):
        start = len(ids)
        cur = _encode(tokenizer, _render(tokenizer, conv[: j + 1], tools, False))
        if cur[:start] != ids:
            raise TemplateAlignmentError(f"message {j}: rendered prefix is not token-stable with the previous prefix")
        if msg["role"] == "assistant":
            gen_start = start
            if j > 0:
                head = _encode(tokenizer, _render(tokenizer, conv[:j], tools, True))
                if start <= len(head) <= len(cur) and cur[: len(head)] == head:
                    gen_start = len(head)
                else:
                    header_fallbacks += 1
            w = 1.0 if message_weights is None else float(message_weights[j])
            labels.extend([-100] * (gen_start - start) + cur[gen_start:])
            weights.extend([0.0] * (gen_start - start) + [w] * (len(cur) - gen_start))
        else:
            labels.extend([-100] * (len(cur) - start))
            weights.extend([0.0] * (len(cur) - start))
        ids = cur
        if truncation_side != "left_obs_first" and len(ids) >= max_seq_len:
            truncated = len(ids) > max_seq_len or j < len(messages) - 1
            break
    if truncation_side == "left_obs_first":
        if len(ids) > max_seq_len:
            truncated = True
            excess = len(ids) - max_seq_len
            keep = []
            for idx, lab in enumerate(labels):
                if excess and lab == -100:
                    excess -= 1
                    continue
                keep.append(idx)
            # If assistant tokens alone exceed the budget they are kept whole
            # (reported via truncated) rather than cutting the tail turns.
            ids = [ids[i] for i in keep]
            labels = [labels[i] for i in keep]
            weights = [weights[i] for i in keep]
    else:
        ids, labels, weights = ids[:max_seq_len], labels[:max_seq_len], weights[:max_seq_len]
    if not (len(ids) == len(labels) == len(weights)):
        raise TemplateAlignmentError("ids/labels/weights length mismatch")
    return {
        "input_ids": ids,
        "labels": labels,
        "token_weights": weights,
        "n_label_tokens": sum(1 for x in labels if x != -100),
        "truncated": truncated,
        "header_fallbacks": header_fallbacks,
    }


def resolve_tool_mode(tokenizer, requested):
    if requested != "auto":
        return requested
    return "template" if template_supports_tools(tokenizer) else "json"


def load_tools(path):
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        tools = json.load(fh)
    if not isinstance(tools, list):
        raise DatasetError(f"{path}: --tools-json must contain a JSON list of tool specs")
    return tools


def label_logprobs(torch, policy, input_ids, attention_mask, labels, chunk):
    """Per-token log p(label) for shifted labels, computed in checkpointed chunks."""
    from torch.utils.checkpoint import checkpoint

    base = policy.get_base_model() if hasattr(policy, "get_base_model") else policy
    hidden = base.get_decoder()(input_ids=input_ids, attention_mask=attention_mask, use_cache=False).last_hidden_state
    lm_head = base.get_output_embeddings()
    hidden = hidden[:, :-1, :]
    target = labels[:, 1:]
    valid = target.ne(-100)
    safe = target.clamp(min=0)

    def chunk_logp(h, t):
        logits = lm_head(h).float()
        return logits.gather(-1, t.unsqueeze(-1)).squeeze(-1) - torch.logsumexp(logits, dim=-1)

    parts = []
    for s in range(0, hidden.size(1), chunk):
        h, t = hidden[:, s:s + chunk], safe[:, s:s + chunk]
        if torch.is_grad_enabled():
            parts.append(checkpoint(chunk_logp, h, t, use_reentrant=False))
        else:
            parts.append(chunk_logp(h, t))
    logp = torch.cat(parts, dim=1)
    return logp * valid, valid


# ---------------------------------------------------------------------------
# Script-specific code.
# ---------------------------------------------------------------------------

DEFAULT_LORA_TARGETS = "q_proj,k_proj,v_proj,o_proj,gate_proj,up_proj,down_proj"
ADVANTAGE_KEYS = ("phase_step_advantages", "rloo_advantage", "grpo_advantage", "twin_contrastive_advantage")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--algo", choices=("awr", "rloo", "dpo"), required=True,
                   help="awr: phase-advantage weighted regression. rloo: leave-one-out advantage weighted regression. "
                        "dpo: preference loss over train_dpo_pairs.jsonl when present, else best-vs-worst per task.")
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    p.add_argument("--model", default="Qwen/Qwen2.5-Coder-14B-Instruct",
                   help="Base model id or path (Qwen/Qwen3-14B also supported with --include-reasoning).")
    p.add_argument("--init-adapter", default=None,
                   help="SFT LoRA adapter dir. It is merged into the base first; for DPO the merged model is the reference.")
    p.add_argument("--output-dir", default=None, help="Default: outputs/<algo>_lora")
    p.add_argument("--save-merged", default=None, help="Also save base+init+new adapter merged weights here (for verl).")
    p.add_argument("--max-seq-len", type=int, default=24576)
    p.add_argument("--truncation-side", choices=("left_obs_first", "right", "left"), default="left_obs_first",
                   help="left_obs_first (default): compact/drop tool observations from the left; "
                        "final assistant pipeline_repair/final_report.json turns are never cut.")
    p.add_argument("--include-reasoning", action="store_true")
    p.add_argument("--tool-format", choices=("auto", "template", "json"), default="auto")
    p.add_argument("--tools-json", default=None)
    p.add_argument("--advantage-key", choices=ADVANTAGE_KEYS, default="phase_step_advantages",
                   help="phase_step_advantages: per-token weight by message causal_phase. Others: per-sequence weight.")
    p.add_argument("--adv-clip", type=float, default=5.0)
    p.add_argument("--weight-transform", choices=("linear", "exp"), default="linear",
                   help="linear: w = clip(A, -c, c). exp: w = min(exp(A / awr_beta), c), always > 0.")
    p.add_argument("--awr-beta", type=float, default=1.0)
    p.add_argument("--min-margin", type=float, default=0.1)
    p.add_argument("--beta", type=float, default=0.1, help="DPO beta.")
    p.add_argument("--dpo-impl", choices=("manual", "trl"), default="manual")
    p.add_argument("--lora-r", type=int, default=64)
    p.add_argument("--lora-alpha", type=int, default=128)
    p.add_argument("--lora-dropout", type=float, default=0.05)
    p.add_argument("--lora-target", default=DEFAULT_LORA_TARGETS)
    p.add_argument("--lr", type=float, default=2e-5)
    p.add_argument("--epochs", type=float, default=1.0)
    p.add_argument("--per-device-batch-size", type=int, default=1)
    p.add_argument("--grad-accum", type=int, default=8)
    p.add_argument("--warmup-ratio", type=float, default=0.03)
    p.add_argument("--logprob-chunk", type=int, default=4096)
    p.add_argument("--attn-impl", default="auto")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--max-rows", type=int, default=0)
    p.add_argument("--trust-remote-code", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    if args.output_dir is None:
        args.output_dir = f"outputs/{args.algo}_lora"
    return args


def validate_advantages(rows, path):
    errors = []
    for row in rows:
        psa = row["phase_step_advantages"]
        if not isinstance(psa, dict) or set(psa) != set(PHASES):
            errors.append(f"{row['id']}: phase_step_advantages keys {sorted(psa) if isinstance(psa, dict) else psa!r}")
            continue
        vals = list(psa.values()) + [row[k] for k in ADVANTAGE_KEYS[1:]] + [row["reward"]]
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in vals):
            errors.append(f"{row['id']}: non-finite advantage or reward")
        if not isinstance(row["group"], dict) or not isinstance(row["group"].get("non_collapsed"), bool):
            errors.append(f"{row['id']}: group.non_collapsed must be a bool")
    if errors:
        raise DatasetError(f"{path}: {len(errors)} advantage error(s):\n  " + "\n  ".join(errors[:MAX_REPORTED_ERRORS]))


def group_filter(rows):
    by_task = {}
    for r in rows:
        by_task.setdefault(r["task_id"], []).append(r)
    kept, dropped_groups, warnings = [], [], []
    for task, rs in sorted(by_task.items()):
        rewards = [float(r["reward"]) for r in rs]
        std = statistics.pstdev(rewards) if len(rewards) > 1 else 0.0
        keep = [r for r in rs if r["group"]["non_collapsed"] is True]
        flags = {r["group"]["non_collapsed"] for r in rs}
        if len(flags) > 1:
            warnings.append(f"{task}: mixed group.non_collapsed flags across rows")
        if keep and std == 0:
            warnings.append(f"{task}: non_collapsed=true but reward std over task_id rows is 0")
        if not keep and std > 0:
            warnings.append(f"{task}: non_collapsed=false but reward std over task_id rows is {std:.4f}")
        if keep:
            kept.extend(keep)
        else:
            dropped_groups.append({"task_id": task, "rows": len(rs), "reward_std": round(std, 6),
                                   "rewards": sorted(set(round(x, 6) for x in rewards))})
    report = {
        "groups_total": len(by_task),
        "groups_kept": len({r["task_id"] for r in kept}),
        "groups_dropped": len(dropped_groups),
        "rows_total": len(rows),
        "rows_kept": len(kept),
        "rows_dropped": len(rows) - len(kept),
        "dropped_groups": dropped_groups,
        "warnings": warnings,
    }
    return kept, report


def message_weights(row, args):
    """Per-message weights; zero for every non-assistant message."""
    out, unknown = [], 0
    for msg in row["messages"]:
        if msg["role"] != "assistant":
            out.append(0.0)
            continue
        if getattr(args, "algo", None) == "rloo":
            # RLOO: one leave-one-out advantage per sequence, applied to its assistant tokens.
            a = float(row["rloo_advantage"])
        elif args.advantage_key == "phase_step_advantages":
            phase = msg.get("causal_phase")
            if phase not in row["phase_step_advantages"]:
                unknown += 1
            a = float(row["phase_step_advantages"].get(phase, 0.0))
        else:
            a = float(row[args.advantage_key])
        if args.weight_transform == "linear":
            w = max(-args.adv_clip, min(args.adv_clip, a))
        else:
            w = min(math.exp(a / args.awr_beta), args.adv_clip)
        out.append(w)
    return out, unknown


def awr_stats(rows, args):
    pos = neg = zero = unknown = 0
    weighted = 0.0
    zero_rows = 0
    for row in rows:
        ws, unk = message_weights(row, args)
        unknown += unk
        nonzero = False
        for msg, w in zip(row["messages"], ws):
            if msg["role"] != "assistant":
                continue
            if w > 0:
                pos += 1
            elif w < 0:
                neg += 1
            else:
                zero += 1
            nonzero = nonzero or w != 0
            weighted += abs(w) * int(msg.get("approx_tokens") or 0)
        zero_rows += not nonzero
    return {
        "advantage_key": args.advantage_key,
        "weight_transform": args.weight_transform,
        "adv_clip": args.adv_clip,
        "assistant_messages_positive_weight": pos,
        "assistant_messages_negative_weight": neg,
        "assistant_messages_zero_weight": zero,
        "assistant_messages_unknown_phase": unknown,
        "rows_all_zero_weight": zero_rows,
        "approx_abs_weighted_assistant_tokens": round(weighted, 3),
    }


def build_pairs(rows, min_margin):
    by_task = {}
    for r in rows:
        by_task.setdefault(r["task_id"], []).append(r)
    pairs, skipped = [], []
    for task, rs in sorted(by_task.items()):
        if len(rs) < 2:
            skipped.append({"task_id": task, "reason": "fewer than 2 rows"})
            continue
        srt = sorted(rs, key=lambda r: (float(r["reward"]), r["id"]))
        lo, hi = srt[0], srt[-1]
        margin = float(hi["reward"]) - float(lo["reward"])
        if margin > min_margin:
            pairs.append({"task_id": task, "chosen": hi, "rejected": lo, "margin": margin})
        else:
            skipped.append({"task_id": task, "reason": f"margin {margin:.4f} <= {min_margin}"})
    return pairs, skipped


def hyperparams(args):
    hp = {
        "algo": args.algo, "model": args.model, "init_adapter": args.init_adapter,
        "output_dir": args.output_dir, "save_merged": args.save_merged, "max_seq_len": args.max_seq_len,
        "include_reasoning": args.include_reasoning, "tool_format": args.tool_format,
        "lora": {"r": args.lora_r, "alpha": args.lora_alpha, "dropout": args.lora_dropout,
                 "target_modules": args.lora_target.split(",")},
        "lr": args.lr, "lr_scheduler": "cosine", "warmup_ratio": args.warmup_ratio, "epochs": args.epochs,
        "per_device_batch_size": args.per_device_batch_size, "grad_accum": args.grad_accum,
        "bf16": True, "gradient_checkpointing": True, "seed": args.seed,
    }
    hp.update({"truncation_side": getattr(args, "truncation_side", "left_obs_first"),
               "supported_models": ["Qwen/Qwen2.5-Coder-7B-Instruct", "Qwen/Qwen2.5-Coder-14B-Instruct", "Qwen/Qwen2.5-Coder-32B-Instruct"]})
    if args.algo in ("awr", "rloo"):
        hp.update({"advantage_key": "rloo_advantage" if args.algo == "rloo" else args.advantage_key, "adv_clip": args.adv_clip,
                   "weight_transform": args.weight_transform, "awr_beta": args.awr_beta})
    else:
        hp.update({"beta": args.beta, "min_margin": args.min_margin, "dpo_impl": args.dpo_impl})
    return hp


def load_dpo_pairs_file(args):
    """Load train_dpo_pairs.jsonl when present; returns (pairs, skipped, path) or (None, [], None)."""
    path = os.path.join(args.data_dir, "train_dpo_pairs.jsonl")
    if not os.path.isfile(path):
        return None, [], None
    pairs, skipped = [], []
    for row in load_jsonl(path):
        if row.get("split") != "train":
            skipped.append({"task_id": row.get("task_id"), "reason": "split is not train"})
            continue
        chosen, rejected = row.get("chosen"), row.get("rejected")
        if not isinstance(chosen, dict) or not isinstance(rejected, dict) or "messages" not in chosen or "messages" not in rejected:
            skipped.append({"task_id": row.get("task_id"), "reason": "chosen/rejected are not trajectory rows"})
            continue
        margin = float(row.get("reward_margin", float(chosen.get("reward", 0)) - float(rejected.get("reward", 0))))
        if margin <= args.min_margin:
            skipped.append({"task_id": row.get("task_id"), "reason": f"margin {margin:.4f} <= min_margin {args.min_margin}"})
            continue
        trunc = getattr(args, "truncation_side", "left_obs_first")
        pairs.append({
            "task_id": row.get("task_id"),
            "chosen": {**chosen, "messages": prepare_messages(chosen["messages"], args.max_seq_len, trunc)[0]},
            "rejected": {**rejected, "messages": prepare_messages(rejected["messages"], args.max_seq_len, trunc)[0]},
            "margin": margin,
        })
    return pairs, skipped, path


def build_plan(args):
    path, rows = load_train_file(args.data_dir, "train_grpo.jsonl", GRPO_KEYS)
    if args.max_rows:
        rows = rows[: args.max_rows]
    validate_advantages(rows, path)
    kept, groups = group_filter(rows)
    # 24k observation-compacted loader: assistant turns are never truncated.
    kept = [{**r, "messages": prepare_messages(r["messages"], args.max_seq_len, getattr(args, "truncation_side", "left_obs_first"))[0]} for r in kept]
    plan = {
        "script": "run_offline_rloo_dpo.py",
        "algo": args.algo,
        "dry_run": args.dry_run,
        "data_file": path,
        "group_filter": groups,
        "tokens_kept_rows": token_stats(kept, args.max_seq_len),
        "masking": "labels=-100 for every non-assistant token",
        "truncation_side": getattr(args, "truncation_side", "left_obs_first"),
        "assistant_tokens_truncated": 0,
        "holdout": holdout_check(args.data_dir, rows),
        "hyperparams": hyperparams(args),
        "init_adapter_exists": bool(args.init_adapter) and os.path.isdir(args.init_adapter),
        "libraries_available": libs_available(),
    }
    pairs = None
    if args.algo in ("awr", "rloo"):
        plan[args.algo] = awr_stats(kept, args)
        if not kept:
            raise DatasetError("no non-collapsed rows to train on")
    else:
        pairs, skipped, pairs_path = load_dpo_pairs_file(args)
        if pairs:
            plan["dpo"] = {
                "source": pairs_path,
                "pairs": len(pairs),
                "pair_list": [{"task_id": p["task_id"], "chosen": p["chosen"]["id"], "rejected": p["rejected"]["id"],
                               "margin": round(p["margin"], 6)} for p in pairs],
                "skipped_groups": skipped,
            }
        else:
            pairs, skipped = build_pairs(kept, args.min_margin)
            plan["dpo"] = {
                "source": "train_grpo.jsonl best-vs-worst per task",
                "pairs": len(pairs),
                "pair_list": [{"task_id": p["task_id"], "chosen": p["chosen"]["id"], "rejected": p["rejected"]["id"],
                               "margin": round(p["margin"], 6)} for p in pairs],
                "skipped_groups": skipped,
            }
        if args.dpo_impl == "trl":
            plan["dpo"]["warning"] = "TRL DPOTrainer scores all completion tokens, including tool observations"
        if not pairs:
            raise DatasetError("no DPO pairs pass --min-margin")
    return plan, kept, pairs


# ----------------------------- training ------------------------------------

def _load_base(args, torch):
    from transformers import AutoModelForCausalLM

    attn = args.attn_impl
    if attn == "auto":
        attn = "flash_attention_2" if importlib.util.find_spec("flash_attn") else "sdpa"
    model = AutoModelForCausalLM.from_pretrained(args.model, torch_dtype=torch.bfloat16, attn_implementation=attn,
                                                 trust_remote_code=args.trust_remote_code)
    if args.init_adapter:
        from peft import PeftModel

        if not os.path.isdir(args.init_adapter):
            raise SystemExit(f"--init-adapter not found: {args.init_adapter}")
        model = PeftModel.from_pretrained(model, args.init_adapter).merge_and_unload()
    model.config.use_cache = False
    return model


def _wrap_lora(args, model):
    from peft import get_peft_model

    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    policy = get_peft_model(model, _lora_config(args))
    policy.print_trainable_parameters()
    return policy


def _lora_config(args):
    from peft import LoraConfig

    return LoraConfig(r=args.lora_r, lora_alpha=args.lora_alpha, lora_dropout=args.lora_dropout,
                      target_modules=args.lora_target.split(","), bias="none", task_type="CAUSAL_LM")


def _tokenizer(args):
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(args.model, trust_remote_code=args.trust_remote_code)
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token
    return tok


def _training_args(args):
    from transformers import TrainingArguments

    return TrainingArguments(
        output_dir=args.output_dir, per_device_train_batch_size=args.per_device_batch_size,
        gradient_accumulation_steps=args.grad_accum, learning_rate=args.lr, lr_scheduler_type="cosine",
        warmup_ratio=args.warmup_ratio, num_train_epochs=args.epochs, bf16=True, logging_steps=1,
        save_strategy="no", report_to="none", remove_unused_columns=False, dataloader_num_workers=0,
        ddp_find_unused_parameters=False, seed=args.seed, max_grad_norm=1.0)


def _run_objective(args, torch, objective, examples, pad_id):
    from transformers import Trainer

    class ListDataset(torch.utils.data.Dataset):
        def __init__(self, items):
            self.items = items

        def __len__(self):
            return len(self.items)

        def __getitem__(self, i):
            return self.items[i]

    def collate(features):
        batch = {}
        for prefix in ("", "chosen_", "rejected_"):
            key = prefix + "input_ids"
            if key not in features[0]:
                continue
            n = max(len(f[key]) for f in features)
            ids = torch.full((len(features), n), pad_id, dtype=torch.long)
            lab = torch.full((len(features), n), -100, dtype=torch.long)
            att = torch.zeros((len(features), n), dtype=torch.long)
            wts = torch.zeros((len(features), n), dtype=torch.float32)
            for i, f in enumerate(features):
                k = len(f[key])
                ids[i, :k] = torch.tensor(f[key])
                lab[i, :k] = torch.tensor(f[prefix + "labels"])
                att[i, :k] = 1
                if prefix + "token_weights" in f:
                    wts[i, :k] = torch.tensor(f[prefix + "token_weights"])
            batch[key], batch[prefix + "labels"], batch[prefix + "attention_mask"] = ids, lab, att
            if prefix + "token_weights" in features[0]:
                batch[prefix + "token_weights"] = wts
        return batch

    class ObjectiveTrainer(Trainer):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            # The objective ignores num_items_in_batch, so Trainer must divide by grad-accum itself.
            self.model_accepts_loss_kwargs = False

        def compute_loss(self, model, inputs, return_outputs=False, **kwargs):
            out = model(**inputs)
            return (out["loss"], out) if return_outputs else out["loss"]

    trainer = ObjectiveTrainer(model=objective, args=_training_args(args), train_dataset=ListDataset(examples),
                               data_collator=collate)
    trainer.train()
    return trainer


def _tokenise(args, tokenizer, tool_mode, tools, messages, weights=None):
    try:
        ex = build_example(tokenizer, messages, include_reasoning=args.include_reasoning, tool_mode=tool_mode,
                           tools=tools, max_seq_len=args.max_seq_len, message_weights=weights,
                           truncation_side=getattr(args, "truncation_side", "left_obs_first"))
    except TemplateAlignmentError as exc:
        print(f"[offline] drop: {exc}", file=sys.stderr)
        return None
    return ex if ex["n_label_tokens"] > 0 else None


def _save(args, policy, tokenizer, plan, is_main):
    if not is_main:
        return
    os.makedirs(args.output_dir, exist_ok=True)
    policy.save_pretrained(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)
    with open(os.path.join(args.output_dir, "bundle_run.json"), "w", encoding="utf-8") as fh:
        json.dump({"plan": plan}, fh, indent=2)
    print(f"[offline] adapter saved to {args.output_dir}", file=sys.stderr)
    if args.save_merged:
        merged = policy.merge_and_unload()
        merged.save_pretrained(args.save_merged, safe_serialization=True)
        tokenizer.save_pretrained(args.save_merged)
        print(f"[offline] merged model saved to {args.save_merged}", file=sys.stderr)


def train_awr(args, plan, rows, tools):
    import torch

    tokenizer = _tokenizer(args)
    tool_mode = resolve_tool_mode(tokenizer, args.tool_format)
    examples, dropped = [], 0
    for row in rows:
        ws, _ = message_weights(row, args)
        ex = _tokenise(args, tokenizer, tool_mode, tools, row["messages"], ws)
        if ex is None:
            dropped += 1
            continue
        examples.append({"input_ids": ex["input_ids"], "labels": ex["labels"], "token_weights": ex["token_weights"]})
    print(f"[awr] examples={len(examples)} dropped={dropped} tool_mode={tool_mode}", file=sys.stderr)
    if not examples:
        raise SystemExit("no trainable AWR examples")
    policy = _wrap_lora(args, _load_base(args, torch))
    chunk = args.logprob_chunk

    class AWRObjective(torch.nn.Module):
        def __init__(self, inner):
            super().__init__()
            self.policy = inner

        def forward(self, input_ids, attention_mask, labels, token_weights):
            logp, valid = label_logprobs(torch, self.policy, input_ids, attention_mask, labels, chunk)
            w = token_weights[:, 1:].to(logp.dtype) * valid
            return {"loss": -(w * logp).sum() / valid.sum().clamp(min=1)}

    trainer = _run_objective(args, torch, AWRObjective(policy), examples, tokenizer.pad_token_id)
    _save(args, policy, tokenizer, plan, trainer.is_world_process_zero())


def _pair_examples(args, tokenizer, tool_mode, tools, pairs):
    examples = []
    for p in pairs:
        c = _tokenise(args, tokenizer, tool_mode, tools, p["chosen"]["messages"])
        r = _tokenise(args, tokenizer, tool_mode, tools, p["rejected"]["messages"])
        if c is None or r is None:
            continue
        examples.append({"chosen_input_ids": c["input_ids"], "chosen_labels": c["labels"],
                         "rejected_input_ids": r["input_ids"], "rejected_labels": r["labels"]})
    return examples


def train_dpo_manual(args, plan, pairs, tools):
    import torch
    import torch.nn.functional as F

    tokenizer = _tokenizer(args)
    tool_mode = resolve_tool_mode(tokenizer, args.tool_format)
    examples = _pair_examples(args, tokenizer, tool_mode, tools, pairs)
    print(f"[dpo-manual] pairs={len(examples)} tool_mode={tool_mode}", file=sys.stderr)
    if not examples:
        raise SystemExit("no trainable DPO pairs")
    policy = _wrap_lora(args, _load_base(args, torch))
    chunk, beta = args.logprob_chunk, args.beta

    class DPOObjective(torch.nn.Module):
        def __init__(self, inner):
            super().__init__()
            self.policy = inner

        def _seq(self, ids, att, lab):
            logp, _ = label_logprobs(torch, self.policy, ids, att, lab, chunk)
            return logp.sum(1)

        def forward(self, chosen_input_ids, chosen_attention_mask, chosen_labels,
                    rejected_input_ids, rejected_attention_mask, rejected_labels):
            with torch.no_grad(), self.policy.disable_adapter():
                ref_c = self._seq(chosen_input_ids, chosen_attention_mask, chosen_labels)
                ref_r = self._seq(rejected_input_ids, rejected_attention_mask, rejected_labels)
            pol_c = self._seq(chosen_input_ids, chosen_attention_mask, chosen_labels)
            pol_r = self._seq(rejected_input_ids, rejected_attention_mask, rejected_labels)
            logits = beta * ((pol_c - ref_c) - (pol_r - ref_r))
            return {"loss": -F.logsigmoid(logits).mean()}

    trainer = _run_objective(args, torch, DPOObjective(policy), examples, tokenizer.pad_token_id)
    _save(args, policy, tokenizer, plan, trainer.is_world_process_zero())


def train_dpo_trl(args, plan, pairs, tools):
    """TRL path. Returns False if the TRL API does not match, so the caller can fall back."""
    import torch

    try:
        from datasets import Dataset
        from trl import DPOConfig, DPOTrainer
    except ImportError as exc:
        print(f"[dpo-trl] TRL unavailable ({exc}); falling back to manual DPO", file=sys.stderr)
        return False
    tokenizer = _tokenizer(args)
    records = []
    for p in pairs:
        # JSON tool format keeps Arrow schemas flat (no heterogeneous argument structs).
        c = to_chat(p["chosen"]["messages"], args.include_reasoning, "json")
        r = to_chat(p["rejected"]["messages"], args.include_reasoning, "json")
        k = next(i for i, m in enumerate(c) if m["role"] == "assistant")
        if r[:k] != c[:k]:
            print(f"[dpo-trl] skip {p['task_id']}: chosen/rejected prompts differ", file=sys.stderr)
            continue
        records.append({"prompt": c[:k], "chosen": c[k:], "rejected": r[k:]})
    if not records:
        raise SystemExit("no DPO pairs with a shared prompt")
    try:
        cfg = DPOConfig(output_dir=args.output_dir, beta=args.beta, max_length=args.max_seq_len,
                        per_device_train_batch_size=args.per_device_batch_size,
                        gradient_accumulation_steps=args.grad_accum, learning_rate=args.lr,
                        lr_scheduler_type="cosine", warmup_ratio=args.warmup_ratio, num_train_epochs=args.epochs,
                        bf16=True, gradient_checkpointing=True,
                        gradient_checkpointing_kwargs={"use_reentrant": False}, logging_steps=1,
                        save_strategy="no", report_to="none", seed=args.seed)
        model = _load_base(args, torch)
        common = dict(model=model, ref_model=None, args=cfg, train_dataset=Dataset.from_list(records),
                      peft_config=_lora_config(args))
        try:
            trainer = DPOTrainer(processing_class=tokenizer, **common)
        except TypeError:
            trainer = DPOTrainer(tokenizer=tokenizer, **common)
    except TypeError as exc:
        print(f"[dpo-trl] TRL API mismatch ({exc}); falling back to manual DPO", file=sys.stderr)
        return False
    print("[dpo-trl] warning: completion log-probs include tool observation tokens", file=sys.stderr)
    trainer.train()
    _save(args, trainer.model, tokenizer, plan, trainer.is_world_process_zero())
    return True


def main(argv=None):
    args = parse_args(argv)
    try:
        plan, rows, pairs = build_plan(args)
        tools = load_tools(args.tools_json)
    except DatasetError as exc:
        print(f"dataset validation failed: {exc}", file=sys.stderr)
        return 1
    if args.dry_run or os.environ.get("RANK", "0") == "0":
        emit(plan)
    if args.dry_run:
        return 0
    if args.algo in ("awr", "rloo"):
        train_awr(args, plan, rows, tools)
    elif args.dpo_impl != "trl" or not train_dpo_trl(args, plan, pairs, tools):
        train_dpo_manual(args, plan, pairs, tools)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

_BUNDLE_RUN_ONLINE_GRPO_VERL_PY = r'''#!/usr/bin/env python3
"""Online GRPO/DAPO entrypoint for verl.

Steps:
1. Validate train_grpo.jsonl (every row must have split == "train").
2. Write one prompt per task_id as verl RLHF data (JSONL always, parquet when
   launching), with data_source="alkera_factory" and
   extra_info={task_id, governance_mode, twin_group_id}.
3. Build a verl config: adv_estimator=grpo, asymmetric DAPO clip
   (clip_ratio_low=0.2, clip_ratio_high=0.28), rollout n=5, dynamic sampling
   that drops groups with zero reward std, and the reward_fn hook in this file.
4. Write the config as JSON, YAML and Hydra overrides. If verl is importable
   and this is not a dry run, launch `python -m verl.trainer.main_ppo` with the
   overrides. Otherwise print the command.

Rewards are never fabricated. A non-dry run requires --env-endpoint, and
reward_fn raises instead of returning a default score.

eval_holdout_grpo.jsonl is read only to prove it is disjoint from training. It
is never written into the prompt files. Validation files point at the train
prompts and periodic validation is disabled.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import subprocess
import sys
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Shared stdlib helpers (duplicated across bundle scripts on purpose).
# ---------------------------------------------------------------------------

DEFAULT_DATA_DIR = "/home/ubuntu/alkera_factory_1017/rl_dataset_v1"
TRAIN_SPLIT = "train"
HOLDOUT_SPLIT = "eval_holdout"
HOLDOUT_FILE = "eval_holdout_grpo.jsonl"
ROLES = ("system", "user", "assistant", "tool")
GRPO_KEYS = (
    "id", "task_id", "run_id", "rollout_id", "theme", "fault_topology_id",
    "governance_mode", "role", "twin_group_id", "split", "prompt", "messages",
    "loss_mask", "token_loss_mask_rle", "reward", "passed", "outcome_class",
    "grpo_advantage", "rloo_advantage", "twin_contrastive_advantage",
    "policy_bias_penalty", "bias_kind", "phase_step_advantages",
    "seven_step_progress", "dapo_clip_bounds", "group",
)
MAX_REPORTED_ERRORS = 50
LIBS = ("torch", "transformers", "peft", "trl", "accelerate", "datasets", "verl", "vllm", "pyarrow")


class DatasetError(Exception):
    pass


def load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
            if not isinstance(obj, dict):
                raise DatasetError(f"{path}:{lineno}: expected a JSON object")
            rows.append(obj)
    return rows


def validate_rows(rows, path, required_keys, expected_split=TRAIN_SPLIT):
    """Return a list of error strings. Empty list means the rows are valid."""
    errors = []
    for idx, row in enumerate(rows, 1):
        where = f"{os.path.basename(path)}:{idx} id={row.get('id')!r}"
        missing = [k for k in required_keys if k not in row]
        if missing:
            errors.append(f"{where}: missing keys {missing}")
            continue
        if row["split"] != expected_split:
            errors.append(f"{where}: split={row['split']!r}, expected {expected_split!r}")
        msgs, mask = row["messages"], row["loss_mask"]
        if not isinstance(msgs, list) or not msgs:
            errors.append(f"{where}: messages must be a non-empty list")
            continue
        if not isinstance(mask, list) or len(mask) != len(msgs):
            errors.append(f"{where}: loss_mask length {len(mask) if isinstance(mask, list) else None} != messages length {len(msgs)}")
            continue
        issued = set()
        for j, (msg, bit) in enumerate(zip(msgs, mask)):
            at = f"{where} message[{j}]"
            if not isinstance(msg, dict):
                errors.append(f"{at}: not an object")
                continue
            role = msg.get("role")
            if role not in ROLES:
                errors.append(f"{at}: unknown role {role!r}")
            if type(bit) is not int or bit not in (0, 1):
                errors.append(f"{at}: loss_mask value {bit!r} is not 0/1")
            elif (bit == 1) != (role == "assistant"):
                errors.append(f"{at}: loss_mask={bit} but role={role!r} (must be 1 iff assistant)")
            if "loss_mask" in msg and msg["loss_mask"] != bit:
                errors.append(f"{at}: message loss_mask {msg['loss_mask']!r} != row loss_mask {bit!r}")
            if role == "assistant":
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") if isinstance(tc, dict) else None
                    if not isinstance(fn, dict) or not tc.get("id") or not fn.get("name"):
                        errors.append(f"{at}: malformed tool_call {tc!r:.120}")
                        continue
                    if not isinstance(fn.get("arguments"), str):
                        errors.append(f"{at}: tool_call {tc['id']!r} arguments must be a JSON string")
                    issued.add(tc["id"])
            elif role == "tool":
                tid = msg.get("tool_call_id")
                if tid not in issued:
                    errors.append(f"{at}: tool_call_id {tid!r} does not answer an earlier assistant tool_call")
    return errors


def require_valid(rows, path, required_keys, expected_split=TRAIN_SPLIT):
    errors = validate_rows(rows, path, required_keys, expected_split)
    if errors:
        shown = "\n  ".join(errors[:MAX_REPORTED_ERRORS])
        more = "" if len(errors) <= MAX_REPORTED_ERRORS else f"\n  ... {len(errors) - MAX_REPORTED_ERRORS} more"
        raise DatasetError(f"{path}: {len(errors)} validation error(s):\n  {shown}{more}")


def load_train_file(data_dir, name, required_keys):
    path = os.path.join(data_dir, name)
    if not os.path.isfile(path):
        raise DatasetError(f"missing dataset file: {path}")
    rows = load_jsonl(path)
    if not rows:
        raise DatasetError(f"{path}: no rows")
    require_valid(rows, path, required_keys, TRAIN_SPLIT)
    return path, rows


def holdout_check(data_dir, train_rows):
    """Read the holdout file only to prove it is disjoint from training rows."""
    path = os.path.join(data_dir, HOLDOUT_FILE)
    if not os.path.isfile(path):
        return {"path": path, "present": False, "used_for_training": False}
    rows = load_jsonl(path)
    bad = [r.get("id") for r in rows if r.get("split") != HOLDOUT_SPLIT]
    if bad:
        raise DatasetError(f"{path}: {len(bad)} row(s) without split={HOLDOUT_SPLIT!r}, e.g. {bad[:3]}")
    id_overlap = sorted({r["id"] for r in train_rows} & {r.get("id") for r in rows})
    if id_overlap:
        raise DatasetError(f"{path}: {len(id_overlap)} holdout id(s) also in training data, e.g. {id_overlap[:3]}")
    task_overlap = sorted({r["task_id"] for r in train_rows} & {r.get("task_id") for r in rows})
    return {
        "path": path,
        "present": True,
        "rows": len(rows),
        "used_for_training": False,
        "id_overlap": 0,
        "task_id_overlap": len(task_overlap),
    }


def libs_available():
    return {name: importlib.util.find_spec(name) is not None for name in LIBS}


def emit(obj):
    print(json.dumps(obj, indent=2, sort_keys=False))


# ---------------------------------------------------------------------------
# Reward hook (loaded by verl through custom_reward_function.path/name).
# ---------------------------------------------------------------------------

DATA_SOURCE = "alkera_factory"
ENDPOINT_ENV = "ALKERA_ENV_ENDPOINT"
TIMEOUT_ENV = "ALKERA_REWARD_TIMEOUT_S"


class RewardUnavailable(RuntimeError):
    pass


def reward_fn(data_source, solution_str, ground_truth, extra_info=None, **kwargs):
    """Live reward for one rollout. Never returns a made-up score.

    Where the reward comes from: the alkera repo grades an episode by running
    the task world in a container (tasks.harness.container_episode.ContainerEpisode),
    letting the agent act through the gateway tools, and scoring the final
    world state and report with the task grader (tasks.harness.episode.grade).
    The dataset reward is that grader's 3-tier scalar
    (band + 0.5 * progress + path). This hook forwards the rollout to an env
    service that does exactly that and returns its score.

    Service contract (to be implemented next to tasks.harness):
        POST {ALKERA_ENV_ENDPOINT}/grade
        body: {"task_id", "governance_mode", "twin_group_id", "response"}
        reply: {"reward": float, ...}

    Limitation: verl passes the decoded response string. A tool-using
    multi-turn episode needs verl's multi-turn agent loop with tools backed by
    the same env service. Single-turn responses can only be graded as a final
    report.

    Raises RewardUnavailable when the endpoint is missing, unreachable or
    returns anything other than a finite float.
    """
    if data_source != DATA_SOURCE:
        raise RewardUnavailable(f"reward_fn only grades data_source={DATA_SOURCE!r}, got {data_source!r}")
    endpoint = os.environ.get(ENDPOINT_ENV)
    if not endpoint:
        raise RewardUnavailable(f"{ENDPOINT_ENV} is not set; refusing to fabricate a reward")
    info = extra_info or {}
    body = json.dumps({
        "task_id": info.get("task_id", ground_truth),
        "governance_mode": info.get("governance_mode"),
        "twin_group_id": info.get("twin_group_id"),
        "response": solution_str,
    }).encode("utf-8")
    req = urllib.request.Request(endpoint.rstrip("/") + "/grade", data=body,
                                 headers={"Content-Type": "application/json"}, method="POST")
    timeout = float(os.environ.get(TIMEOUT_ENV, "1800"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            reply = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise RewardUnavailable(f"env grader call failed: {exc}") from exc
    reward = reply.get("reward") if isinstance(reply, dict) else None
    if isinstance(reward, bool) or not isinstance(reward, (int, float)) or not math.isfinite(reward):
        raise RewardUnavailable(f"env grader returned no finite reward: {reply!r:.200}")
    return float(reward)


# ---------------------------------------------------------------------------
# Script-specific code.
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    p.add_argument("--model", default="Qwen/Qwen2.5-Coder-14B-Instruct",
                   help="Actor model path. Use the merged SFT+offline checkpoint (run_offline_rloo_dpo.py --save-merged).")
    p.add_argument("--output-dir", default="outputs/grpo_verl")
    p.add_argument("--env-endpoint", default=None, help="Env grader service URL. Required for non-dry runs.")
    p.add_argument("--entrypoint", default="verl.trainer.main_ppo",
                   help="verl module to run. recipe.dapo.main_dapo honours algorithm.filter_groups.")
    p.add_argument("--n-rollouts", type=int, default=5)
    p.add_argument("--clip-ratio-low", type=float, default=0.2)
    p.add_argument("--clip-ratio-high", type=float, default=0.28)
    p.add_argument("--filter-metric", default="seq_final_reward")
    p.add_argument("--max-num-gen-batches", type=int, default=10)
    p.add_argument("--lr", type=float, default=1e-6)
    p.add_argument("--train-batch-size", type=int, default=16, help="Prompts per step.")
    p.add_argument("--ppo-mini-batch-size", type=int, default=8)
    p.add_argument("--micro-batch-size-per-gpu", type=int, default=1)
    p.add_argument("--max-prompt-len", type=int, default=8192)
    p.add_argument("--max-response-len", type=int, default=24576)
    p.add_argument("--total-epochs", type=int, default=10)
    p.add_argument("--num-gpus", type=int, default=8)
    p.add_argument("--cluster-gpus", type=int, choices=(4, 8), default=None,
                   help="Cluster size (4x A100-80GB or 8x H100/A100-80GB); overrides --num-gpus when set.")
    p.add_argument("--truncation-side", choices=("left_obs_first", "right", "left"), default="left_obs_first",
                   help="Observation handling for 24k trajectories; left_obs_first never cuts final assistant turns.")
    p.add_argument("--nnodes", type=int, default=1)
    p.add_argument("--rollout-engine", default="vllm")
    p.add_argument("--rollout-tp", type=int, default=2)
    p.add_argument("--gpu-mem-util", type=float, default=0.6)
    p.add_argument("--reward-timeout-s", type=float, default=1800)
    p.add_argument("--project-name", default="alkera_factory")
    p.add_argument("--experiment-name", default="grpo_dapo")
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args(argv)


def build_prompts(rows):
    by_task = {}
    for r in sorted(rows, key=lambda r: r["id"]):
        by_task.setdefault(r["task_id"], []).append(r)
    prompts, differing = [], []
    for task, rs in sorted(by_task.items()):
        first = rs[0]
        if len({r["prompt"] for r in rs}) > 1:
            differing.append(task)
        if not isinstance(first["prompt"], str) or not first["prompt"].strip():
            raise DatasetError(f"{task}: empty prompt")
        prompts.append({
            "data_source": DATA_SOURCE,
            "prompt": [{"role": "user", "content": first["prompt"]}],
            "ability": "agentic_data_repair",
            "reward_model": {"style": "rule", "ground_truth": task},
            "extra_info": {
                "task_id": task,
                "governance_mode": first["governance_mode"],
                "twin_group_id": first["twin_group_id"],
            },
        })
    return prompts, differing


def build_config(args, train_file):
    reward_path = os.path.abspath(__file__)
    return {
        "algorithm": {
            "adv_estimator": "grpo",
            "use_kl_in_reward": False,
            "filter_groups": {
                "enable": True,
                "metric": args.filter_metric,
                "max_num_gen_batches": args.max_num_gen_batches,
            },
        },
        "data": {
            "train_files": train_file,
            "val_files": train_file,
            "prompt_key": "prompt",
            "train_batch_size": args.train_batch_size,
            "max_prompt_length": args.max_prompt_len,
            "max_response_length": args.max_response_len,
            "filter_overlong_prompts": True,
            "truncation": "error",
        },
        "actor_rollout_ref": {
            "model": {
                "path": args.model,
                "enable_gradient_checkpointing": True,
                "use_remove_padding": True,
            },
            "actor": {
                "optim": {"lr": args.lr},
                "ppo_mini_batch_size": args.ppo_mini_batch_size,
                "ppo_micro_batch_size_per_gpu": args.micro_batch_size_per_gpu,
                "clip_ratio_low": args.clip_ratio_low,
                "clip_ratio_high": args.clip_ratio_high,
                "loss_agg_mode": "token-mean",
                "use_kl_loss": False,
                "entropy_coeff": 0,
            },
            "rollout": {
                "name": args.rollout_engine,
                "n": args.n_rollouts,
                "tensor_model_parallel_size": args.rollout_tp,
                "gpu_memory_utilization": args.gpu_mem_util,
                "log_prob_micro_batch_size_per_gpu": args.micro_batch_size_per_gpu,
            },
            "ref": {"log_prob_micro_batch_size_per_gpu": args.micro_batch_size_per_gpu},
        },
        "custom_reward_function": {"path": reward_path, "name": "reward_fn"},
        "trainer": {
            "project_name": args.project_name,
            "experiment_name": args.experiment_name,
            "n_gpus_per_node": args.num_gpus,
            "nnodes": args.nnodes,
            "total_epochs": args.total_epochs,
            "default_local_dir": os.path.join(os.path.abspath(args.output_dir), "checkpoints"),
            "logger": ["console"],
            "save_freq": 10,
            "test_freq": -1,
            "val_before_train": False,
        },
    }


# Keys that verl.trainer.main_ppo's base config does not define (Hydra needs "+").
MAIN_PPO_NEW_KEYS = ("algorithm.filter_groups.",)


def _hydra_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, list):
        return "[" + ",".join(_hydra_value(x) for x in v) + "]"
    return str(v)


def to_overrides(cfg, entrypoint, prefix=""):
    out = []
    for k, v in cfg.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.extend(to_overrides(v, entrypoint, key + "."))
            continue
        plus = entrypoint == "verl.trainer.main_ppo" and key.startswith(MAIN_PPO_NEW_KEYS)
        out.append(f"{'+' if plus else ''}{key}={_hydra_value(v)}")
    return out


def to_yaml(obj, indent=0):
    lines = []
    pad = "  " * indent
    for k, v in obj.items():
        if isinstance(v, dict):
            lines.append(f"{pad}{k}:")
            lines.append(to_yaml(v, indent + 1))
        else:
            lines.append(f"{pad}{k}: {json.dumps(v)}")
    return "\n".join(lines)


def write_parquet(prompts, path):
    import pyarrow as pa
    import pyarrow.parquet as pq

    pq.write_table(pa.Table.from_pylist(prompts), path)


def main(argv=None):
    args = parse_args(argv)
    if getattr(args, "cluster_gpus", None):
        args.num_gpus = args.cluster_gpus
    try:
        path, rows = load_train_file(args.data_dir, "train_grpo.jsonl", GRPO_KEYS)
        holdout = holdout_check(args.data_dir, rows)
        prompts, differing = build_prompts(rows)
    except DatasetError as exc:
        print(f"dataset validation failed: {exc}", file=sys.stderr)
        return 1
    assert all(r["split"] == TRAIN_SPLIT for r in rows)

    out_dir = os.path.abspath(args.output_dir)
    data_dir = os.path.join(out_dir, "data")
    jsonl_path = os.path.join(data_dir, "train_prompts.jsonl")
    parquet_path = os.path.join(data_dir, "train_prompts.parquet")
    cfg = build_config(args, parquet_path)
    overrides = to_overrides(cfg, args.entrypoint)
    command = [sys.executable, "-m", args.entrypoint] + overrides
    verl_ok = importlib.util.find_spec("verl") is not None

    os.makedirs(data_dir, exist_ok=True)
    with open(jsonl_path, "w", encoding="utf-8") as fh:
        for p in prompts:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    with open(os.path.join(out_dir, "verl_config.json"), "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    with open(os.path.join(out_dir, "verl_config.yaml"), "w", encoding="utf-8") as fh:
        fh.write(to_yaml(cfg) + "\n")
    with open(os.path.join(out_dir, "verl_overrides.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(overrides) + "\n")

    warnings = []
    if args.entrypoint == "verl.trainer.main_ppo":
        warnings.append("algorithm.filter_groups (dynamic sampling) is only applied by recipe.dapo.main_dapo; "
                        "main_ppo accepts the key but trains on zero-std groups too")
    if len(prompts) < args.train_batch_size:
        warnings.append(f"only {len(prompts)} prompts but --train-batch-size {args.train_batch_size}; "
                        "lower it or verl may produce no full batch")
    if differing:
        warnings.append(f"{len(differing)} task_id(s) have differing prompts across rollouts; first by id was used")
    warnings.append("reward_fn grades the decoded response; tool-using episodes need verl multi-turn tools "
                    "backed by the env service")
    offline_non_collapsed = len({r["task_id"] for r in rows if r["group"].get("non_collapsed")})
    by_mode = {}
    for p in prompts:
        m = p["extra_info"]["governance_mode"]
        by_mode[m] = by_mode.get(m, 0) + 1
    plan = {
        "script": "run_online_grpo_verl.py",
        "dry_run": args.dry_run,
        "data_file": path,
        "train_rows": len(rows),
        "prompts": len(prompts),
        "prompts_by_governance_mode": dict(sorted(by_mode.items())),
        "offline_tasks_non_collapsed": offline_non_collapsed,
        "offline_tasks_collapsed": len(prompts) - offline_non_collapsed,
        "holdout": holdout,
        "files": {
            "prompts_jsonl": jsonl_path,
            "prompts_parquet": parquet_path,
            "config_json": os.path.join(out_dir, "verl_config.json"),
            "config_yaml": os.path.join(out_dir, "verl_config.yaml"),
            "overrides": os.path.join(out_dir, "verl_overrides.txt"),
        },
        "verl_config": cfg,
        "command": command,
        "cluster_gpus": args.num_gpus,
        "supported_cluster_gpus": [4, 8],
        "supported_models": ["Qwen/Qwen2.5-Coder-7B-Instruct", "Qwen/Qwen2.5-Coder-14B-Instruct", "Qwen/Qwen2.5-Coder-32B-Instruct"],
        "truncation_side": getattr(args, "truncation_side", "left_obs_first"),
        "max_response_len": args.max_response_len,
        "env_endpoint": args.env_endpoint,
        "online_run_allowed": bool(args.env_endpoint),
        "verl_importable": verl_ok,
        "libraries_available": libs_available(),
        "warnings": warnings,
    }
    emit(plan)
    if args.dry_run:
        return 0
    if not args.env_endpoint:
        print("refusing to start online GRPO without --env-endpoint: rewards must come from the live env grader",
              file=sys.stderr)
        return 2
    if not verl_ok:
        print("verl is not importable; not launching. Command:\n  " + " ".join(command), file=sys.stderr)
        return 3
    try:
        write_parquet(prompts, parquet_path)
    except ImportError as exc:
        print(f"pyarrow is required to write {parquet_path}: {exc}", file=sys.stderr)
        return 2
    env = dict(os.environ)
    env[ENDPOINT_ENV] = args.env_endpoint
    env[TIMEOUT_ENV] = str(args.reward_timeout_s)
    print("launching: " + " ".join(command), file=sys.stderr)
    return subprocess.call(command, env=env)


if __name__ == "__main__":
    sys.exit(main())
'''

_BUNDLE_LAUNCH_GPU_SH = r'''#!/usr/bin/env bash
# Usage: launch_gpu.sh {sft|awr|rloo|dpo|grpo|all}
# Env: MODEL (Qwen/Qwen2.5-Coder-7B-Instruct, -14B- or -32B-, or short 7B|14B|32B),
#      DATA_DIR, OUT_ROOT, CLUSTER_GPUS (4 or 8; NUM_GPUS is kept as an alias),
#      MAX_SEQ_LEN (24576), TRUNCATION_SIDE (left_obs_first), CLUSTER_CONFIG,
#      PYTHON (python3), LAUNCHER (accelerate|torchrun),
#      DRY_RUN (1 = dry runs only, no GPU needed), OFFLINE_ALGO (awr|rloo|dpo, used by "all"),
#      SFT_ADAPTER (default $OUT_ROOT/sft_lora, "none" to skip), GRPO_MODEL, ENV_ENDPOINT,
#      EXTRA_ARGS (appended to every python script call).
set -euo pipefail

STAGE="${1:-}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL="${MODEL:-Qwen/Qwen2.5-Coder-14B-Instruct}"
case "$MODEL" in
  7B|7b) MODEL="Qwen/Qwen2.5-Coder-7B-Instruct" ;;
  14B|14b) MODEL="Qwen/Qwen2.5-Coder-14B-Instruct" ;;
  32B|32b) MODEL="Qwen/Qwen2.5-Coder-32B-Instruct" ;;
esac
DATA_DIR="${DATA_DIR:-/home/ubuntu/alkera_factory_1017/rl_dataset_v1}"
OUT_ROOT="${OUT_ROOT:-$HERE/outputs}"
CLUSTER_GPUS="${CLUSTER_GPUS:-${NUM_GPUS:-8}}"
case "$CLUSTER_GPUS" in
  4|8) ;;
  *) echo "CLUSTER_GPUS must be 4 or 8 (got $CLUSTER_GPUS)" >&2; exit 64 ;;
esac
NUM_GPUS="$CLUSTER_GPUS"
MAX_SEQ_LEN="${MAX_SEQ_LEN:-24576}"
TRUNCATION_SIDE="${TRUNCATION_SIDE:-left_obs_first}"
if [[ -z "${CLUSTER_CONFIG:-}" ]]; then
  if [[ "$CLUSTER_GPUS" == "8" ]]; then
    CLUSTER_CONFIG="$HERE/configs/fsdp_8xh100_24k.yaml"
  else
    CLUSTER_CONFIG="$HERE/configs/deepspeed_zero3_4xa100_24k.json"
  fi
fi
PYTHON="${PYTHON:-python3}"
LAUNCHER="${LAUNCHER:-accelerate}"
DRY_RUN="${DRY_RUN:-0}"
OFFLINE_ALGO="${OFFLINE_ALGO:-awr}"
SFT_ADAPTER="${SFT_ADAPTER:-$OUT_ROOT/sft_lora}"
ENV_ENDPOINT="${ENV_ENDPOINT:-}"
read -r -a EXTRA <<< "${EXTRA_ARGS:-}"

usage() {
  echo "usage: $0 {sft|awr|rloo|dpo|grpo|all}" >&2
  exit 64
}

check_gpu() {
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "nvidia-smi not found: this stage needs NVIDIA GPUs (set DRY_RUN=1 for checks only)" >&2
    exit 69
  fi
  local found
  found="$(nvidia-smi -L | grep -c '^GPU' || true)"
  if (( found < NUM_GPUS )); then
    echo "NUM_GPUS=$NUM_GPUS but nvidia-smi lists $found GPU(s)" >&2
    exit 69
  fi
}

dist_launch() {
  if [[ "$LAUNCHER" == "torchrun" ]]; then
    torchrun --standalone --nproc_per_node "$NUM_GPUS" "$@"
  else
    accelerate launch --num_processes "$NUM_GPUS" --mixed_precision bf16 "$@"
  fi
}

init_adapter_args() {
  if [[ "$SFT_ADAPTER" != "none" ]]; then
    printf '%s\n' --init-adapter "$SFT_ADAPTER"
  fi
}

run_sft() {
  local args=(--data-dir "$DATA_DIR" --model "$MODEL" --output-dir "$OUT_ROOT/sft_lora"
              --max-seq-len "$MAX_SEQ_LEN" --truncation-side "$TRUNCATION_SIDE" "${EXTRA[@]}")
  echo "== sft: dry run (CLUSTER_GPUS=$CLUSTER_GPUS MODEL=$MODEL CONFIG=$CLUSTER_CONFIG)" >&2
  "$PYTHON" "$HERE/run_sft_lora.py" "${args[@]}" --dry-run
  [[ "$DRY_RUN" == "1" ]] && return 0
  check_gpu
  dist_launch "$HERE/run_sft_lora.py" "${args[@]}"
}

run_offline() {
  local algo="$1"
  local init=()
  mapfile -t init < <(init_adapter_args)
  local args=(--algo "$algo" --data-dir "$DATA_DIR" --model "$MODEL" "${init[@]}"
              --max-seq-len "$MAX_SEQ_LEN" --truncation-side "$TRUNCATION_SIDE"
              --output-dir "$OUT_ROOT/${algo}_lora" --save-merged "$OUT_ROOT/${algo}_merged" "${EXTRA[@]}")
  echo "== $algo: dry run (CLUSTER_GPUS=$CLUSTER_GPUS MODEL=$MODEL CONFIG=$CLUSTER_CONFIG)" >&2
  "$PYTHON" "$HERE/run_offline_rloo_dpo.py" "${args[@]}" --dry-run
  [[ "$DRY_RUN" == "1" ]] && return 0
  check_gpu
  dist_launch "$HERE/run_offline_rloo_dpo.py" "${args[@]}"
}

run_grpo() {
  local model="${GRPO_MODEL:-}"
  if [[ -z "$model" ]]; then
    if [[ -d "$OUT_ROOT/${OFFLINE_ALGO}_merged" ]]; then
      model="$OUT_ROOT/${OFFLINE_ALGO}_merged"
    else
      model="$MODEL"
    fi
  fi
  local args=(--data-dir "$DATA_DIR" --model "$model" --output-dir "$OUT_ROOT/grpo_verl"
              --num-gpus "$NUM_GPUS" --cluster-gpus "$CLUSTER_GPUS" --truncation-side "$TRUNCATION_SIDE" "${EXTRA[@]}")
  if [[ -n "$ENV_ENDPOINT" ]]; then
    args+=(--env-endpoint "$ENV_ENDPOINT")
  fi
  echo "== grpo: dry run" >&2
  "$PYTHON" "$HERE/run_online_grpo_verl.py" "${args[@]}" --dry-run
  [[ "$DRY_RUN" == "1" ]] && return 0
  check_gpu
  if [[ -z "$ENV_ENDPOINT" ]]; then
    echo "ENV_ENDPOINT is required for online GRPO (rewards come from the live env grader)" >&2
    exit 2
  fi
  "$PYTHON" "$HERE/run_online_grpo_verl.py" "${args[@]}"
}

mkdir -p "$OUT_ROOT"
case "$STAGE" in
  sft) run_sft ;;
  awr) run_offline awr ;;
  rloo) run_offline rloo ;;
  dpo) run_offline dpo ;;
  grpo) run_grpo ;;
  all)
    case "$OFFLINE_ALGO" in awr|rloo|dpo) ;; *) echo "OFFLINE_ALGO must be awr, rloo or dpo" >&2; exit 64 ;; esac
    run_sft
    run_offline "$OFFLINE_ALGO"
    run_grpo
    ;;
  *) usage ;;
esac
'''

_BUNDLE_REQUIREMENTS_GPU_TXT = r'''# Minimum versions. Not tested together on GPU; pin exact versions after the first working run.
torch>=2.4
transformers>=4.46
trl>=0.12
peft>=0.13
accelerate>=1.0
datasets>=3.0
pyarrow>=14
verl>=0.4
vllm>=0.8
# Optional, used automatically when importable (--attn-impl auto):
# flash-attn>=2.6
'''

_BUNDLE_README_MD = r'''# GPU training bundle

These scripts were only dry-run checked on a CPU host. They have never been executed on a GPU.

## Files

- `run_sft_lora.py`: LoRA cold-start SFT on `train_sft.jsonl` (transformers Trainer + PEFT), with the 24k observation-compacted loader (`--max-seq-len 24576 --truncation-side left_obs_first` by default; final assistant turns are never cut).
- `run_offline_rloo_dpo.py`: offline training on `train_grpo.jsonl`. `--algo awr` runs advantage-weighted regression, `--algo rloo` runs leave-one-out advantage-weighted regression, and `--algo dpo` runs the DPO margin loss over `train_dpo_pairs.jsonl` when present (else best vs worst rollout per task).
- `run_online_grpo_verl.py`: writes verl prompt data and a GRPO/DAPO config, then launches verl.
- `launch_gpu.sh`: runs one stage (`sft`, `awr`, `rloo`, `dpo`, `grpo`) or `all`. Each stage runs its `--dry-run` first. `CLUSTER_GPUS=4` or `8`, `MODEL=Qwen/Qwen2.5-Coder-7B-Instruct|14B|32B` (or short `7B|14B|32B`).
- `configs/fsdp_8xh100_24k.yaml`: FSDP full-shard config for 8x H100/A100-80GB at 24k.
- `configs/deepspeed_zero3_4xa100_24k.json`: DeepSpeed ZeRO-3 config for 4x A100/H100-80GB at 24k.
- `requirements-gpu.txt`: minimum library versions.

## Order

1. SFT: `run_sft_lora.py` writes a LoRA adapter.
2. Offline RL: `run_offline_rloo_dpo.py --algo awr` (or `rloo` / `dpo`) with `--init-adapter <sft adapter>`. The SFT adapter is merged first. `--save-merged` writes a full checkpoint.
3. Online GRPO: `run_online_grpo_verl.py --model <merged checkpoint> --env-endpoint <url>`.

`launch_gpu.sh all` runs SFT, then `OFFLINE_ALGO` (default `awr`), then GRPO. Set `CLUSTER_GPUS=4` (DeepSpeed ZeRO-3 config) or `8` (FSDP config, the default) and `MODEL=` to any of `Qwen/Qwen2.5-Coder-7B-Instruct`, `Qwen/Qwen2.5-Coder-14B-Instruct` or `Qwen/Qwen2.5-Coder-32B-Instruct`.

## Dry run

Every script accepts `--dry-run`. A dry run uses only the Python standard library. It validates the data, prints a JSON plan and exits 0. Validation errors exit 1.

```bash
DRY_RUN=1 PYTHON=python3 DATA_DIR=/path/to/rl_dataset_v1 ./launch_gpu.sh all
```

`DRY_RUN=1` does not need `nvidia-smi`. The GRPO dry run writes the prompt JSONL and config files under `OUT_ROOT/grpo_verl`.

## Data checks

Each script refuses to run unless every loaded training row has `split == "train"`. It also checks:

- `loss_mask` has one entry per message.
- `loss_mask` is 1 exactly for assistant messages.
- Every `tool_call_id` answers an earlier assistant tool call.

`eval_holdout_grpo.jsonl` is never used for training. It is read only to check that its rows are `eval_holdout` and that no id overlaps the training data.

## Masking

Sequences are built with the tokenizer chat template, message by message. Each prefix must extend the previous one token for token, otherwise the example is dropped. Assistant tokens after the generation header keep their ids as labels. All other positions (system, user, tool observations, headers) get label -100 and give zero loss. With the default `--truncation-side left_obs_first` at `--max-seq-len 24576`, tool observations are compacted and leftmost masked tokens are dropped first; assistant (label) tokens and the final `pipeline_repair` / `final_report.json` turns are never cut. Legacy right truncation is available with `--truncation-side right`.

Exception: `run_offline_rloo_dpo.py --dpo-impl trl` uses TRL DPOTrainer, which scores every completion token, including tool observations. The default `--dpo-impl manual` keeps the mask.

## Notes

- SFT weighting: each example's mean token loss is multiplied by `sft_trajectory_weight`, normalised to mean 1.
- AWR weights come from `phase_step_advantages[causal_phase]` per message, or one `--advantage-key` value per sequence. They are clipped to `--adv-clip`. Negative weights push assistant log-probs down. Use `--weight-transform exp` for non-negative weights.
- Offline groups are keyed by `task_id`. Rows with `group.non_collapsed == false` are dropped, and the plan lists the dropped groups.
- Log-probs are computed in sequence chunks (`--logprob-chunk`) so that full-vocabulary logits for 32k tokens are never held at once.
- Qwen3 chat templates add an empty think block to the last assistant turn. Use `--include-reasoning` with `Qwen/Qwen3-14B`, or rows will fail the prefix check.
- Online GRPO needs `--env-endpoint`. `reward_fn` raises instead of returning a default score. The endpoint must run the episode with the container harness (`tasks.harness`) and return the grader reward.
- `algorithm.filter_groups` (dynamic sampling) is applied by verl's `recipe.dapo.main_dapo`. With the default `verl.trainer.main_ppo` the key is accepted but ignored. Pass `--entrypoint recipe.dapo.main_dapo` to use it.
'''

_BUNDLE_FSDP_8XH100_24K_YAML = r'''# FSDP full-shard config for 1 node x 8x H100-80GB (or 8x A100-80GB), 24k context.
# Used by run_sft_lora.py / run_offline_rloo_dpo.py via accelerate / Trainer FSDP.
# Dry-run validity is checked on CPU; this file has not been launched on GPU.
cluster:
  nodes: 1
  gpus_per_node: 8
  cluster_gpus: 8
  gpu_model: "H100-80GB or A100-80GB"
  supported_cluster_gpus: [4, 8]
model:
  name_or_path: "Qwen/Qwen2.5-Coder-14B-Instruct"
  supported_models:
    - "Qwen/Qwen2.5-Coder-7B-Instruct"
    - "Qwen/Qwen2.5-Coder-14B-Instruct"
    - "Qwen/Qwen2.5-Coder-32B-Instruct"
  torch_dtype: bfloat16
  attn_implementation: flash_attention_2
data:
  max_seq_len: 24576
  compact_max_tokens: 24576
  truncation_side: left_obs_first
  assistant_tokens_truncated: 0
  train_sft_file: train_sft.jsonl
  train_grpo_file: train_grpo.jsonl
  train_dpo_pairs_file: train_dpo_pairs.jsonl
fsdp:
  enabled: true
  sharding_strategy: FULL_SHARD
  auto_wrap_policy: TRANSFORMER_BASED_AUTO_WRAP
  backward_prefetch: BACKWARD_PRE
  forward_prefetch: true
  use_orig_params: true
  cpu_offload: false
  activation_checkpointing: true
training:
  per_device_train_batch_size: 1
  gradient_accumulation_steps: 8
  learning_rate: 0.0001
  lr_scheduler_type: cosine
  warmup_ratio: 0.03
  num_train_epochs: 2
  bf16: true
  gradient_checkpointing: true
  logging_steps: 1
  save_strategy: "no"
dpo:
  beta: 0.1
  loss: "-log sigmoid(beta*((pol_chosen-ref_chosen)-(pol_rejected-ref_rejected)))"
'''

_BUNDLE_DEEPSPEED_ZERO3_4XA100_24K_JSON = r'''{
  "cluster": {
    "nodes": 1,
    "gpus_per_node": 4,
    "cluster_gpus": 4,
    "gpu_model": "A100-80GB or H100-80GB",
    "supported_cluster_gpus": [4, 8]
  },
  "model": {
    "name_or_path": "Qwen/Qwen2.5-Coder-14B-Instruct",
    "supported_models": [
      "Qwen/Qwen2.5-Coder-7B-Instruct",
      "Qwen/Qwen2.5-Coder-14B-Instruct",
      "Qwen/Qwen2.5-Coder-32B-Instruct"
    ],
    "torch_dtype": "bfloat16",
    "attn_implementation": "flash_attention_2"
  },
  "data": {
    "max_seq_len": 24576,
    "compact_max_tokens": 24576,
    "truncation_side": "left_obs_first",
    "assistant_tokens_truncated": 0,
    "train_sft_file": "train_sft.jsonl",
    "train_grpo_file": "train_grpo.jsonl",
    "train_dpo_pairs_file": "train_dpo_pairs.jsonl"
  },
  "train_batch_size": 32,
  "train_micro_batch_size_per_gpu": 1,
  "gradient_accumulation_steps": 8,
  "gradient_clipping": 1.0,
  "bf16": {"enabled": true},
  "zero_optimization": {
    "stage": 3,
    "overlap_comm": true,
    "contiguous_gradients": true,
    "sub_group_size": 1000000000,
    "reduce_bucket_size": 500000000,
    "stage3_prefetch_bucket_size": 500000000,
    "stage3_param_persistence_threshold": 100000,
    "stage3_max_live_parameters": 1000000000,
    "stage3_max_reuse_distance": 1000000000,
    "stage3_gather_16bit_weights_on_model_save": true
  },
  "optimizer": {"type": "AdamW", "params": {"lr": 0.00002, "betas": [0.9, 0.999], "eps": 1e-08, "weight_decay": 0.0}},
  "scheduler": {"type": "WarmupCosineLR", "params": {"warmup_min_ratio": 0.03, "warmup_num_steps": 20, "total_num_steps": 1000}},
  "gradient_checkpointing": true,
  "dpo": {"beta": 0.1, "loss": "-log sigmoid(beta*((pol_chosen-ref_chosen)-(pol_rejected-ref_rejected)))"}
}
'''

BUNDLE_FILES: dict[str, str] = {
    "run_sft_lora.py": _BUNDLE_RUN_SFT_LORA_PY,
    "run_offline_rloo_dpo.py": _BUNDLE_RUN_OFFLINE_RLOO_DPO_PY,
    "run_online_grpo_verl.py": _BUNDLE_RUN_ONLINE_GRPO_VERL_PY,
    "launch_gpu.sh": _BUNDLE_LAUNCH_GPU_SH,
    "requirements-gpu.txt": _BUNDLE_REQUIREMENTS_GPU_TXT,
    "README.md": _BUNDLE_README_MD,
    FSDP_24K_CONFIG: _BUNDLE_FSDP_8XH100_24K_YAML,
    DEEPSPEED_24K_CONFIG: _BUNDLE_DEEPSPEED_ZERO3_4XA100_24K_JSON,
}


if __name__ == "__main__":
    raise SystemExit(main())
