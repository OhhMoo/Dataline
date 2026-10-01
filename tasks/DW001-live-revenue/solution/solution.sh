#!/usr/bin/env bash
# DW-1 oracle: fix all three latent defects in a built world, then restate.
#   solution/solution.sh <world-root>
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
PY="${PYTHON:-$REPO/gateway/.venv/bin/python}"
PYTHONPATH="$REPO/world${PYTHONPATH:+:$PYTHONPATH}" "$PY" "$HERE/apply.py" "$1"
