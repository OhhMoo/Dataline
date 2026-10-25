# Experiments

This directory holds the blueprint catalog, verification status records, and implementation report for the Alkera 1,080-task environment factory.

## Layout

- `scale_1000/`
  - `implementation_report.md`: Consolidated architecture, calibration, cross-model evaluation, and RL dataset report.
  - `catalog_1080.jsonl`: Deterministic 1,080-blueprint matrix (`12 themes × 15 fault topologies × 3 governance modes × 2 schedule seeds`).
  - `CALIBRATION_EVIDENCE.json`: Live container rollout provenance index.
  - `status/`: Per-phase verification and completion records (`DONE_*.json`).
  - `output`: Symlink to `/home/ubuntu/alkera_factory_1017` (materialized task packages, container rollouts, and exported RL/SFT datasets, kept outside `.git` for container mount containment).

## Key Commands

```bash
# 1. Stage-0 mechanical and 5-rival verification on materialized pilot tasks
PYTHONPATH=.:world gateway/.venv/bin/python -m tasks.factory.verify_all \
  --tasks-dir /home/ubuntu/alkera_factory_1017/phase2_pilot_51 \
  --reuse-stored-baselines --phase2 --strict-phase2 --workers 16

# 2. Cross-model K=3 container evaluation matrix
PYTHONPATH=.:world gateway/.venv/bin/python -m tasks.factory.cross_model_matrix \
  --prefer-k 3 --out /home/ubuntu/alkera_factory_1017/cross_model_frontier_report.json

# 3. Export 24k observation-compacted RL/SFT/DPO dataset and verify CPU gradients
PYTHONPATH=.:world gateway/.venv/bin/python -m tasks.factory.train_open_weight_rl \
  --mode verify-cpu --data-dir /home/ubuntu/alkera_factory_1017/rl_dataset_v2
```
