#!/usr/bin/env bash
# A task worktree with its OWN gateway/.venv, for parallel lanes.
#
#   scripts/worktree_setup.sh t5-tls-dials            # -> ../tool_implementation.wt/t5-tls-dials
#                                                     #    on branch next/t5-tls-dials
#   scripts/worktree_setup.sh t5-tls-dials <base>     # cut from <base> (default: HEAD)
#
# Why its own venv: gateway/.venv installs alkera_gateway editable, so a venv shared
# between worktrees imports whichever worktree it was synced in, not the one under test.
# uv links from its cache, so this takes seconds. Extras match the main checkout's venv.
#
# Why its own pytest temp roots (printed at the end; pytest needs them to exist):
# concurrent runs share /tmp/pytest-of-$USER and prune each other's basetemps, and
# /tmp is a small tmpfs that runs out of inodes before bytes. The gateway suite's
# root stays short (/tmp/p-<task>: unix socket paths are <= 108 bytes); the others
# go to disk.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASK=${1:?task slug, e.g. t5-tls-dials}
BASE=${2:-HEAD}
ROOT=${ALKERA_WORKTREE_ROOT:-$(dirname "$REPO")/$(basename "$REPO").wt}
WT=$ROOT/$TASK

mkdir -p "$ROOT"
git -C "$REPO" worktree add -q -b "next/$TASK" "$WT" "$BASE"
( cd "$WT/gateway" && uv sync -q --locked --python 3.11 \
    --extra native --extra dbt --extra notebook --extra ds --extra bigquery )
"$WT/gateway/.venv/bin/python" -c "import alkera_gateway, sys; p = alkera_gateway.__file__; \
assert p.startswith('$WT/'), p; print('$WT ready:', p)"
mkdir -p "/tmp/p-$TASK" "$HOME/.cache/pt/$TASK"
echo "  gateway suite: PYTEST_DEBUG_TEMPROOT=/tmp/p-$TASK"
echo "  other suites:  PYTEST_DEBUG_TEMPROOT=$HOME/.cache/pt/$TASK"
