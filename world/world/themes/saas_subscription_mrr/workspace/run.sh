#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
REPO="$PWD"
PY="${PYTHON:-python3}"
export DBT_PROFILES_DIR="$REPO/config"
export DBT_TARGET="${DBT_TARGET:-dev}"
export DBT_WAREHOUSE_PATH="${DBT_WAREHOUSE_PATH:-$REPO/../var/warehouse/warehouse.duckdb}"
DBT=(dbt --no-use-colors --project-dir "$REPO/dbt_project" --target "$DBT_TARGET"
     --target-path "$REPO/dbt_project/target" --log-path "$REPO/dbt_project/logs")
case "${1:-}" in
  ingest)
    "$PY" loader/ingest_webhooks.py --landing "$REPO/data/landing" --db "$DBT_WAREHOUSE_PATH"
    ;;
  fold)   "${DBT[@]}" build --select 'tag:fold_subscriptions' ;;
  close)  "${DBT[@]}" build --select revrec_window_close ;;
  build)  "${DBT[@]}" build ;;
  test)   "${DBT[@]}" test ;;
  parse)  "${DBT[@]}" parse ;;
  ""|-h|--help|help)
    echo "usage: ./run.sh {ingest|fold|close|build|test|parse}"
    ;;
  *) echo "unknown target: $1" >&2; exit 2 ;;
esac
