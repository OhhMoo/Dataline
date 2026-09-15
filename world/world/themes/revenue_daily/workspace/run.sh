#!/usr/bin/env bash
# Task runner for the revenue pipeline.
#
# Everything CI runs, and everything the runbooks tell you to run, is a target
# here. If a step only exists in someone's shell history it will be wrong by
# the time you need it.
set -euo pipefail

cd "$(dirname "$0")"
REPO="$PWD"

# .envrc is the committed environment; it sources .env on top of itself if
# you have one. The scheduled runs read neither.
if [ -f .envrc ]; then
  # shellcheck disable=SC1091
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
LANDING_ROOT="${LANDING_ROOT:-data/landing/daily_revenue}"

DBT=(dbt --no-use-colors)
DBT_ARGS=(--project-dir "$REPO/dbt_project" --target "$DBT_TARGET"
          --target-path "$REPO/dbt_project/target"
          --log-path "$REPO/dbt_project/logs")

usage() {
  cat <<'USAGE'
usage: ./run.sh <target>

  env          print the resolved configuration (no secrets)
  load         rebuild raw.daily_revenue from the landing zone
  parse        dbt parse
  compile      dbt compile
  build        dbt build (run + test)
  test         dbt test
  dag-check    check dags/ against the pipeline the scheduler recorded
  check        parse + dag-check   (what CI runs on every push)
USAGE
}

case "${1:-}" in
  env)
    echo "repo              $REPO"
    echo "dbt target        $DBT_TARGET"
    echo "dbt profiles dir  $DBT_PROFILES_DIR"
    echo "warehouse path    $DBT_WAREHOUSE_PATH"
    echo "landing root      $LANDING_ROOT"
    ;;
  load)
    "$PY" loader/load_daily_revenue.py --landing "$LANDING_ROOT" \
          --db "$DBT_WAREHOUSE_PATH"
    ;;
  parse)   "${DBT[@]}" parse   "${DBT_ARGS[@]}" ;;
  compile) "${DBT[@]}" compile "${DBT_ARGS[@]}" ;;
  build)   "${DBT[@]}" build   "${DBT_ARGS[@]}" ;;
  test)    "${DBT[@]}" test    "${DBT_ARGS[@]}" ;;
  dag-check) "$PY" scripts/dag_check.py ;;
  check)
    "${DBT[@]}" parse "${DBT_ARGS[@]}"
    "$PY" scripts/dag_check.py
    ;;
  ""|-h|--help|help) usage ;;
  *) echo "unknown target: $1" >&2; usage >&2; exit 2 ;;
esac
