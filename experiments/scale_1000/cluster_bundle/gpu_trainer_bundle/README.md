# GPU training bundle

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
