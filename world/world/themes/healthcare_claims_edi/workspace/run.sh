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
if [ -z "${DBT_WAREHOUSE_PATH:-}" ]; then
  DBT_WAREHOUSE_PATH="$(cd "$REPO/.." && pwd)/var/warehouse/warehouse.duckdb"
fi
export DBT_WAREHOUSE_PATH
LANDING_ROOT="${LANDING_ROOT:-$REPO/data/landing}"

DBT=(dbt --no-use-colors)
DBT_ARGS=(--project-dir "$REPO/dbt_project" --target "$DBT_TARGET"
          --target-path "$REPO/dbt_project/target"
          --log-path "$REPO/dbt_project/logs")

case "${1:-}" in
  env)
    echo "repo              $REPO"
    echo "dbt target        $DBT_TARGET"
    echo "dbt profiles dir  $DBT_PROFILES_DIR"
    echo "warehouse path    $DBT_WAREHOUSE_PATH"
    echo "landing root      $LANDING_ROOT"
    ;;
  load)
    "$PY" loader/load_edi.py --landing "$LANDING_ROOT" --db "$DBT_WAREHOUSE_PATH"
    ;;
  parse)   "${DBT[@]}" parse   "${DBT_ARGS[@]}" ;;
  compile) "${DBT[@]}" compile "${DBT_ARGS[@]}" ;;
  build)   "${DBT[@]}" build   "${DBT_ARGS[@]}" ;;
  test)    "${DBT[@]}" test    "${DBT_ARGS[@]}" ;;
  check)   "${DBT[@]}" parse   "${DBT_ARGS[@]}" ;;
  ""|-h|--help|help)
    echo "usage: ./run.sh {env|load|parse|compile|build|test|check}"
    ;;
  *) echo "unknown target: $1" >&2; exit 2 ;;
esac
