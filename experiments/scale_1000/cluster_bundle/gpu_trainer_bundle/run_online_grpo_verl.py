#!/usr/bin/env python3
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
