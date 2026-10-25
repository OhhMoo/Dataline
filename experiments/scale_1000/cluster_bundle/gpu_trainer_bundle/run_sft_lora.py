#!/usr/bin/env python3
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
