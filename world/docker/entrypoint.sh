#!/bin/sh
# Episode boot: populate the /world volume once, then hand over to CMD.
# Snapshot restore is the fast path (RL rollouts); seed build proves provenance.
set -e

# Seed-build path only: WORLD_SEED lets a mounted theme seed boot without an
# image rebuild. The snapshot fast path above is unaffected.
WORLD_SEED="${WORLD_SEED:-./seed.yaml}"

if [ ! -e /world/var/sched/state.json ]; then
  if [ -f /snapshot/world.tar ]; then
    python -m world.snapshot restore /snapshot/world.tar /world
  else
    python -m world.build --seed "$WORLD_SEED" --root /world --lock requirements.lock
  fi
fi

exec "$@"
