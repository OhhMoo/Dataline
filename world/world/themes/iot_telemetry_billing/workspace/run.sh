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

dbt_() {
  dbt --no-use-colors "$1" --project-dir "$REPO/dbt_project" \
      --target "$DBT_TARGET" --target-path "$REPO/dbt_project/target" \
      --log-path "$REPO/dbt_project/logs"
}

case "${1:-}" in
  env)
    echo "target     $DBT_TARGET"
    echo "warehouse  $DBT_WAREHOUSE_PATH"
    ;;
  load)
    "$PY" loader/load_usage.py --landing "$REPO/data/landing" --db "$DBT_WAREHOUSE_PATH"
    ;;
  parse) dbt_ parse ;;
  compile) dbt_ compile ;;
  build) dbt_ build ;;
  test) dbt_ test ;;
  ""|-h|--help|help)
    echo "./run.sh env | load | parse | compile | build | test"
    ;;
  *)
    echo "no such target: $1" >&2
    exit 2
    ;;
esac
