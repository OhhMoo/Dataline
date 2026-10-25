#!/usr/bin/env bash
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
