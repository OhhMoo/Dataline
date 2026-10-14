#!/usr/bin/env bash
# Run one docker-heavy command (an image build, an e2e check, a containment run) under one
# of a few host-wide slots, so parallel worktrees do not oversubscribe the host or the
# 10.210/16 address pool.
#
#   scripts/docker_slot.sh "gateway/.venv/bin/python tasks/harness/postgres_e2e_check.py"
#   scripts/docker_slot.sh --lock postgres_e2e "..."   # also exclusive by name: two
#                                                      # worktrees never run the same
#                                                      # fixed-project check at once
#
# The command runs under `sg docker -c`, from the current directory. Exit status is the
# command's. Env: ALKERA_DOCKER_SLOTS (default 2), ALKERA_DOCKER_SLOT_DIR (default
# ~/.cache/alkera-docker-slots).
set -uo pipefail

SLOTS=${ALKERA_DOCKER_SLOTS:-2}
DIR=${ALKERA_DOCKER_SLOT_DIR:-$HOME/.cache/alkera-docker-slots}
NAME=""
if [ "${1:-}" = "--lock" ]; then NAME=$2; shift 2; fi
[ $# -eq 1 ] || { sed -n '2,15p' "$0"; exit 2; }
CMD=$1
mkdir -p "$DIR"

if [ -n "$NAME" ]; then
  exec 8>"$DIR/name-$NAME.lock"
  flock 8
fi

while :; do
  for i in $(seq 1 "$SLOTS"); do
    exec 9>"$DIR/slot-$i.lock"
    if flock -n 9; then
      echo "docker_slot: slot $i${NAME:+ (lock $NAME)}: $CMD" >&2
      sg docker -c "$CMD"
      exit $?
    fi
    exec 9>&-
  done
  sleep 10
done
