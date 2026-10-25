#!/usr/bin/env python3
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
