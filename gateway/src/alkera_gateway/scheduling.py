"""Which scheduler an EPISODE runs with.

`FileScheduler` is the library default and it is honest: it executes dbt jobs
and refuses everything else with `not_executed` rather than reporting success
for work it did not do. That is the right default for a gateway serving an
arbitrary config.

It is the wrong thing for an episode. An episode is supposed to be a working
data engineer's computer, and re-running a loader is the single most common
thing that engineer does. `world.gateway_scheduler.WorldScheduler` executes a
job's real declared `exec` block and derives the run status from the result.

It has existed since R003 and NO EPISODE HAD EVER USED IT, because every entry
point constructed `Gateway(config)` with no `scheduler=`. The cost was measured:
in GT004's first five Opus rollouts the best run called `sched_trigger` five
times, every call reported success, nothing executed, and the agent proceeded
believing it had run its pipeline.

The dependency arrow stays pointed one way (agent.md 10.2): `world/` imports
nothing from the gateway, and the import below is local and conditional, the
same shape `core.py` already uses for the fingerprint.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

STATE = Path("var") / "sched" / "state.json"


def episode_scheduler(config):
    """`WorldScheduler` when the config names a real world, else None.

    None means the caller keeps `FileScheduler`, which is honest but narrow.
    A world root without scheduler state is not an error - a task may ship no
    scheduler at all - but a world root WITH state and no importable world
    package is, and it raises rather than silently downgrading the episode to a
    scheduler that cannot execute. Silently downgrading is the exact failure
    this module exists to end.
    """
    root = getattr(config, "world_root", None)
    if root is None:
        return None
    root = Path(root)
    if not (root / STATE).is_file():
        log.info("no %s under %s; keeping the built-in scheduler", STATE, root)
        return None
    try:
        from world.gateway_scheduler import WorldScheduler
    except ImportError as exc:                       # pragma: no cover
        raise RuntimeError(
            f"{root / STATE} exists, so this episode has a scheduler the agent "
            f"is expected to drive, but the `world` package is not importable: "
            f"{exc}. Refusing to fall back to a scheduler that cannot execute "
            f"the jobs - an agent would be told its pipeline ran when it did "
            f"not.") from exc
    log.info("episode scheduler: WorldScheduler(%s)", root)
    return WorldScheduler(root)
