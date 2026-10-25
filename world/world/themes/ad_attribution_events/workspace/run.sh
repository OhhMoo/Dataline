#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
REPO="$PWD"
if [ -f .envrc ]; then
  . ./.envrc
fi
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python
export DBT_PROFILES_DIR="$REPO/config"
export DBT_TARGET="${DBT_TARGET:-dev}"
export DBT_WAREHOUSE_PATH="${DBT_WAREHOUSE_PATH:-$REPO/../var/warehouse/warehouse.duckdb}"
DBT=(dbt --no-use-colors)
DBT_ARGS=(--project-dir "$REPO/dbt_project" --target "$DBT_TARGET"
          --target-path "$REPO/dbt_project/target" --log-path "$REPO/dbt_project/logs")

case "${1:-}" in
  load)
    "$PY" loader/load_events.py --landing data/landing --config data/config \
      --db "$DBT_WAREHOUSE_PATH"
    ;;
  parse) "${DBT[@]}" parse "${DBT_ARGS[@]}" ;;
  compile) "${DBT[@]}" compile "${DBT_ARGS[@]}" ;;
  build) "${DBT[@]}" build "${DBT_ARGS[@]}" ;;
  test) "${DBT[@]}" test "${DBT_ARGS[@]}" ;;
  attribute)
    "${DBT[@]}" build "${DBT_ARGS[@]}" --select fct_attributed_conversions
    ;;
  lock)
    "${DBT[@]}" build "${DBT_ARGS[@]}" --select partner_payout_handoff billing_lock
    ;;
  ""|-h|--help|help)
    echo "usage: ./run.sh {load|parse|compile|build|test|attribute|lock}"
    ;;
  *) echo "unknown target: $1" >&2; exit 2 ;;
esac
