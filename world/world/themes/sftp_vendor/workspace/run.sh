#!/usr/bin/env bash
# vendor feeds. ./run.sh with no args lists what it can do.
set -euo pipefail
cd "$(dirname "$0")"
REPO="$PWD"

# .envrc is committed and sources .env on top of itself if you have one.
if [ -f .envrc ]; then
  # shellcheck disable=SC1091
  . ./.envrc
fi

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python

export DBT_PROFILES_DIR="$REPO/config"
export DBT_TARGET="${DBT_TARGET:-dev}"
: "${DROP_ROOT:=data/drop}"
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
    echo "drop root  $DROP_ROOT"
    ;;

  # what the 05:30 poll does: count the delivery sentinels. a vendor that has
  # not finished uploading has a .csv and no .done, and that is the whole
  # point of the sentinel.
  poll)
    n=0
    for d in "$DROP_ROOT"/*/; do
      [ -d "$d" ] || continue
      done_n=$(find "$d" -maxdepth 1 -name '*.done' | wc -l | tr -d ' ')
      csv_n=$(find "$d" -maxdepth 1 -name '*.csv' | wc -l | tr -d ' ')
      echo "$(basename "$d"): $csv_n csv, $done_n sentinel(s)"
      n=$((n + done_n))
    done
    [ "$n" -gt 0 ] || { echo "no sentinels anywhere under $DROP_ROOT" >&2; exit 1; }
    ;;

  load)
    "$PY" loader/vendor_load.py --drop "$DROP_ROOT" --db "$DBT_WAREHOUSE_PATH"
    ;;

  parse)   dbt_ parse ;;
  compile) dbt_ compile ;;
  build)   dbt_ build ;;
  test)    dbt_ test ;;
  dag-check) "$PY" scripts/dag_check.py ;;
  check)   dbt_ parse; "$PY" scripts/dag_check.py ;;

  # backfill) TODO. right now: ./run.sh load re-reads the whole drop folder,
  # which is a backfill, because the loader is overwrite-by-filename. if you
  # need a single day, delete the other files first. yes, really.

  ""|-h|--help|help)
    cat <<'USAGE'
./run.sh env | poll | load | parse | compile | build | test | dag-check | check
USAGE
    ;;

  *)
    echo "no such target: $1" >&2
    echo "./run.sh env | poll | load | parse | compile | build | test | dag-check | check" >&2
    exit 2
    ;;
esac
