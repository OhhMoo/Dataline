#!/usr/bin/env bash
# Close pipeline task runner.
#
# The close is a controller-signed process. Anything that changes a number
# after sign-off is a restatement, so this script does not have a target that
# writes to the warehouse without also re-reading the workbooks: raw must
# always be exactly what the workbooks say.
set -euo pipefail

cd "$(dirname "$0")"
REPO="$PWD"

# .envrc is committed and sources .env on top of itself if one exists.
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
WORKBOOKS="${CLOSE_WORKBOOKS:-finance/workbooks}"

dbt_cmd() {
  dbt --no-use-colors "$1" \
      --project-dir "$REPO/dbt_project" \
      --target "$DBT_TARGET" \
      --target-path "$REPO/dbt_project/target" \
      --log-path "$REPO/dbt_project/logs"
}

usage() {
  cat <<'USAGE'
usage: ./run.sh <target>

  env          show the resolved configuration
  workbooks    list the workbooks the import will read, and flag lock files
  import       re-import every workbook into raw.gl_lines / raw.manual_adj
  parse        dbt parse
  compile      dbt compile
  build        dbt build (run + test)
  test         dbt test
  dag-check    compare dags/ with the recorded run history
  check        parse + dag-check (what CI runs)

Sign-off steps are in docs/signoff_checklist.md and are not automated.
USAGE
}

case "${1:-}" in
  env)
    echo "repo             $REPO"
    echo "workbooks        $WORKBOOKS"
    echo "dbt target       $DBT_TARGET"
    echo "warehouse path   $DBT_WAREHOUSE_PATH"
    echo "share path       ${CLOSE_SHARE_PATH:-<unset>}"
    echo "materiality      ${MATERIALITY_THRESHOLD:-<unset>} (config/close.yml may differ)"
    ;;

  # A lock file means someone has the workbook open. The import globs
  # CLOSE_*.xlsx so a lock file is not read, but an open workbook is one that
  # is still being edited, and importing it mid-edit is how a period gets
  # imported half-posted.
  workbooks)
    found=0
    for f in "$WORKBOOKS"/CLOSE_*.xlsx; do
      [ -f "$f" ] || continue
      echo "$(basename "$f")"
      found=$((found + 1))
    done
    [ "$found" -gt 0 ] || { echo "no workbooks under $WORKBOOKS" >&2; exit 1; }
    locks=$(find "$WORKBOOKS" -maxdepth 1 -name '~$*.xlsx' | wc -l | tr -d ' ')
    if [ "$locks" -gt 0 ]; then
      echo "WARNING: $locks workbook(s) are open in Excel right now" >&2
      echo "         do not import until they are closed" >&2
      exit 1
    fi
    echo "$found workbook(s), none open"
    ;;

  import)
    "$PY" loader/import_close.py --workbooks "$WORKBOOKS" --db "$DBT_WAREHOUSE_PATH"
    ;;

  parse)     dbt_cmd parse ;;
  compile)   dbt_cmd compile ;;
  build)     dbt_cmd build ;;
  test)      dbt_cmd test ;;
  dag-check) "$PY" scripts/dag_check.py ;;
  check)     dbt_cmd parse; "$PY" scripts/dag_check.py ;;

  ""|-h|--help|help) usage ;;
  *) echo "unknown target: $1" >&2; usage >&2; exit 2 ;;
esac
