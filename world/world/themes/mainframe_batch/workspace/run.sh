#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# CLAIMS BATCH - operator task runner
#
# Every step the runbook names is a target here. Steps are not to be run by
# hand from shell history; if a step is missing, raise a change and add it.
#
# Change control: CR-2026-014
# ---------------------------------------------------------------------------
set -euo pipefail

cd "$(dirname "$0")"
REPO="$PWD"
CFG="$REPO/conf/claims.cfg"

if [ ! -f "$CFG" ]; then
  echo "FATAL: site configuration not found: $CFG" >&2
  exit 3
fi

# Read one key out of the INI. Deliberately literal: an operator reading this
# file must be able to see exactly which value is being taken from where.
cfg() {
  awk -v section="[$1]" -v key="$2" '
    $0 ~ /^\[/ { in_section = ($0 == section); next }
    in_section && $1 == key { $1=""; $2=""; sub(/^[ \t]*/, ""); print; exit }
  ' "$CFG"
}

# Environment overlay. .envrc is under change control; it sources an
# operator's own .env on top of itself when one is present.
if [ -f .envrc ]; then
  # shellcheck disable=SC1091
  . ./.envrc
fi

PYTHON_BIN="${PYTHON:-python3}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || PYTHON_BIN=python

LANDING_ROOT="$(cfg landing root)"
DATA_FILE="$(cfg landing data_file)"
RECORD_LENGTH="$(cfg source record_length)"
PROFILES_DIR="$(cfg dbt profiles_dir)"

export DBT_PROFILES_DIR="$REPO/$PROFILES_DIR"
export DBT_TARGET="${DBT_TARGET:-$(cfg dbt default_target)}"
if [ -z "${DBT_WAREHOUSE_PATH:-}" ]; then
  DBT_WAREHOUSE_PATH="$(cd "$REPO/.." && pwd)/var/warehouse/warehouse.duckdb"
fi
export DBT_WAREHOUSE_PATH

run_dbt() {
  dbt --no-use-colors "$1" \
      --project-dir "$REPO/dbt_project" \
      --target "$DBT_TARGET" \
      --target-path "$REPO/dbt_project/target" \
      --log-path "$REPO/dbt_project/logs"
}

usage() {
  cat <<'USAGE'
Usage: ./run.sh TARGET

  env          Report the resolved configuration.
  verify       Verify the landing partitions: presence and record length.
  load         Run the batch load step (loader/load_extract.py).
  parse        dbt parse.
  compile      dbt compile.
  build        dbt build (run and test).
  test         dbt test.
  dag-check    Compare dags/ against the recorded run history.
  check        parse and dag-check. This is what the verification job runs.
USAGE
}

case "${1:-}" in

  env)
    echo "repository        $REPO"
    echo "site config       $CFG"
    echo "landing root      $LANDING_ROOT"
    echo "record length     $RECORD_LENGTH"
    echo "dbt profiles dir  $DBT_PROFILES_DIR"
    echo "dbt target        $DBT_TARGET"
    echo "warehouse path    $DBT_WAREHOUSE_PATH"
    ;;

  # The extract is fixed-block with no record delimiters. A partition whose
  # byte length is not a multiple of the record length is a short read, and a
  # short read must be rejected here rather than half-loaded downstream.
  verify)
    if [ ! -d "$LANDING_ROOT" ]; then
      echo "FATAL: landing root not present: $LANDING_ROOT" >&2
      exit 4
    fi
    FOUND=0
    RC=0
    for PART in "$LANDING_ROOT"/dt=*; do
      [ -d "$PART" ] || continue
      FILE="$PART/$DATA_FILE"
      if [ ! -f "$FILE" ]; then
        echo "MISSING  $FILE"
        RC=1
        continue
      fi
      BYTES=$(wc -c < "$FILE" | tr -d ' ')
      if [ $((BYTES % RECORD_LENGTH)) -ne 0 ]; then
        echo "SHORT    $FILE ($BYTES bytes, not a multiple of $RECORD_LENGTH)"
        RC=1
      fi
      FOUND=$((FOUND + 1))
    done
    if [ "$FOUND" -eq 0 ]; then
      echo "FATAL: no partitions under $LANDING_ROOT" >&2
      exit 4
    fi
    echo "verified $FOUND partition(s)"
    exit "$RC"
    ;;

  load)
    "$PYTHON_BIN" loader/load_extract.py \
        --landing "$LANDING_ROOT" --db "$DBT_WAREHOUSE_PATH"
    ;;

  parse)     run_dbt parse ;;
  compile)   run_dbt compile ;;
  build)     run_dbt build ;;
  test)      run_dbt test ;;
  dag-check) "$PYTHON_BIN" scripts/dag_check.py ;;

  check)
    run_dbt parse
    "$PYTHON_BIN" scripts/dag_check.py
    ;;

  ""|-h|--help|help) usage ;;
  *) echo "FATAL: unknown target: $1" >&2; usage >&2; exit 2 ;;
esac
