#!/bin/sh
# Episode boot: populate the /world volume once, then hand over to CMD.
# Snapshot restore is the fast path (RL rollouts); seed build proves provenance.
set -e

if [ ! -e /world/var/sched/state.json ]; then
  if [ -f /snapshot/world.tar ]; then
    python -m world.snapshot restore /snapshot/world.tar /world
  else
    python -m world.build --seed ./seed.yaml --root /world --lock requirements.lock
  fi
fi

exec "$@"
