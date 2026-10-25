#!/usr/bin/env bash
# Unpack the committed Alkera cluster bundle into ${ALKERA_FACTORY_ROOT}
# (default: <repo_parent>/alkera_factory_1017, outside the git repository).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEFAULT_TARGET="$(cd "${REPO_ROOT}/.." && pwd)/alkera_factory_1017"
TARGET_DIR="${1:-${ALKERA_FACTORY_ROOT:-${DEFAULT_TARGET}}}"
BUNDLE_DIR="${REPO_ROOT}/experiments/scale_1000/cluster_bundle"

mkdir -p "${TARGET_DIR}"

echo "[alkera] Unpacking cluster bundle from ${BUNDLE_DIR} -> ${TARGET_DIR}"

cp -rf "${BUNDLE_DIR}/gpu_trainer_bundle" "${TARGET_DIR}/"
for f in phase2_run_index.json cross_model_frontier_report.json reward_ablation_report.json \
         phase6_stage0_51.json phase6_hardness_51.json phase6_trajectory_loss_model.json; do
  if [ -f "${BUNDLE_DIR}/${f}" ]; then
    cp -f "${BUNDLE_DIR}/${f}" "${TARGET_DIR}/${f}"
  fi
done

tar -xzf "${BUNDLE_DIR}/phase2_pilot_51.tar.gz" -C "${TARGET_DIR}"
tar -xzf "${BUNDLE_DIR}/rl_dataset_v2.tar.gz" -C "${TARGET_DIR}"
tar -xzf "${BUNDLE_DIR}/rollouts_reexport.tar.gz" -C "${TARGET_DIR}"

# Create the compact_24k aliases inside rl_dataset_v2
cp -f "${TARGET_DIR}/rl_dataset_v2/train_sft.jsonl" "${TARGET_DIR}/rl_dataset_v2/train_sft_compact_24k.jsonl"
cp -f "${TARGET_DIR}/rl_dataset_v2/train_grpo.jsonl" "${TARGET_DIR}/rl_dataset_v2/train_grpo_compact_24k.jsonl"
cp -f "${TARGET_DIR}/rl_dataset_v2/train_dpo_pairs.jsonl" "${TARGET_DIR}/rl_dataset_v2/train_dpo_pairs_24k.jsonl"

echo "[alkera] Unpack complete at ${TARGET_DIR}"
echo "[alkera] Export for your shell:"
echo "  export ALKERA_FACTORY_ROOT=\"${TARGET_DIR}\""
