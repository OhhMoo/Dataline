#!/usr/bin/env bash
# commerce analytics. ./run.sh with no args lists what it can do.
set -euo pipefail
cd "$(dirname "$0")"
REPO="$PWD"

if [ -f .envrc ]; then
  # shellcheck disable=SC1091
  . ./.envrc
fi

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python

export DBT_PROFILES_DIR="$REPO/config"
export DBT_TARGET="${DBT_TARGET:-dev}"
: "${LANDING_ROOT:=data/landing}"
WORLD="$(cd "$REPO/.." && pwd)"
if [ -z "${DBT_WAREHOUSE_PATH:-}" ]; then
  DBT_WAREHOUSE_PATH="$WORLD/var/warehouse/warehouse.duckdb"
fi
export DBT_WAREHOUSE_PATH
# where the dashboard service keeps the deployed dashboards and their cache
: "${DASHBOARDS_DIR:=$WORLD/var/bi/dashboards}"
: "${BI_CACHE_DIR:=$WORLD/build/bi/refresh}"

dbt_() {
  local cmd="$1"; shift
  dbt --no-use-colors "$cmd" --project-dir "$REPO/dbt_project" \
      --target "$DBT_TARGET" --target-path "$REPO/dbt_project/target" \
      --log-path "$REPO/dbt_project/logs" "$@"
}

case "${1:-}" in
  env)
    echo "target     $DBT_TARGET"
    echo "warehouse  $DBT_WAREHOUSE_PATH"
    echo "landing    $LANDING_ROOT"
    echo "dashboards $DASHBOARDS_DIR"
    ;;

  # what landed: partitions per entity and the newest one
  landed)
    for d in "$LANDING_ROOT"/*/; do
      [ -d "$d" ] || continue
      n=$(find "$d" -maxdepth 1 -name 'dt=*' -type d | wc -l | tr -d ' ')
      newest=$(find "$d" -maxdepth 1 -name 'dt=*' -type d | sort | tail -1)
      echo "$(basename "$d"): $n partition(s), newest $(basename "${newest:-none}")"
    done
    ;;

  load)    "$PY" loader/load_extracts.py --landing "$LANDING_ROOT" --db "$DBT_WAREHOUSE_PATH" ;;
  parse)   dbt_ parse ;;
  compile) dbt_ compile ;;
  build)   dbt_ build ;;
  test)    dbt_ test ;;
  # the semantic layer: metrics, semantic models and saved queries dbt parsed
  sl)      dbt_ ls --resource-type metric --resource-type semantic_model --resource-type saved_query ;;
  # what the 06:00 bi_refresh DAG runs: every dashboard recomputed and logged
  refresh)
    "$PY" scripts/refresh_dashboards.py --dashboards "$DASHBOARDS_DIR" \
        --db "$DBT_WAREHOUSE_PATH" --out "$BI_CACHE_DIR"
    ;;
  dag-check) "$PY" scripts/dag_check.py ;;
  check)   dbt_ parse; "$PY" scripts/dag_check.py ;;
  # what a dashboard reads, upstream
  exposure)
    [ -n "${2:-}" ] || { echo "usage: ./run.sh exposure <name>" >&2; exit 2; }
    dbt_ ls --select "+exposure:$2"
    ;;

  ""|-h|--help|help)
    cat <<'USAGE'
./run.sh env | landed | load | parse | compile | build | test | sl | refresh | dag-check | check | exposure <name>
USAGE
    ;;

  *)
    echo "no such target: $1" >&2
    echo "./run.sh env | landed | load | parse | compile | build | test | sl | refresh | dag-check | check | exposure <name>" >&2
    exit 2
    ;;
esac
