#!/usr/bin/env bash
# GT008 reference solution.
#
#   solution.sh <world.json> <audit_dir>
#
# The world manifest is the json tests/build_world.py printed. Everything runs
# through a real gateway (solution.py), so the reference solution goes through
# exactly the surface an agent goes through and its action log is the
# gateway's own.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python}"
exec "$PY" "$HERE/solution.py" --world "$1" --audit-dir "$2"
