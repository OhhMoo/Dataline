#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
REPO="$PWD"
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python

export DBT_PROFILES_DIR="$REPO/config"
export DBT_TARGET="${DBT_TARGET:-dev}"
if [ -z "${DBT_WAREHOUSE_PATH:-}" ]; then
  DBT_WAREHOUSE_PATH="$(cd "$REPO/.." && pwd)/var/warehouse/warehouse.duckdb"
fi
export DBT_WAREHOUSE_PATH

DBT=(dbt --no-use-colors --project-dir "$REPO/dbt_project" --target "$DBT_TARGET"
     --target-path "$REPO/dbt_project/target" --log-path "$REPO/dbt_project/logs")
NIGHTLY="stg_payment_events stg_merchant_terms fct_payments dim_merchant settlement_corrections load_checkpoint interchange_ledger"

case "${1:-}" in
  load)    "$PY" pipelines/load_payments.py --workspace "$REPO" --db "$DBT_WAREHOUSE_PATH" ;;
  build)   "${DBT[@]}" build --select $NIGHTLY ;;
  publish) "$PY" pipelines/publish_settlement.py "$DBT_WAREHOUSE_PATH" "$REPO" ;;
  cutoff)  "$PY" pipelines/clearing_window_close.py "$DBT_WAREHOUSE_PATH" "$REPO" ;;
  check)
    "${DBT[@]}" parse
    "${DBT[@]}" test --select $NIGHTLY
    ;;
  *) echo "usage: ./run.sh {load|build|publish|cutoff|check}" >&2; exit 2 ;;
esac
