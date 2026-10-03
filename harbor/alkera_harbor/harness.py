"""The rollout harness, imported as the generator uses it.

The generator does not re-implement the episode: it calls the same functions
`tasks/harness/rollout.py` calls for `--driver native --topology container`
(taskspec.load, episode.prepare_task / build_world, native_stack staging,
agent_under_test.build_prompt, container_episode.reroot_profiles /
warehouse_mounts). Those modules live in `tasks/harness/` (a directory of
scripts, not a package), so they are imported by path, once, on first use.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace

from .constants import GATEWAY_SRC, HARNESS_DIR, TASKS_DIR, WORLD_PKG_PARENT

VARIANT_ENV = "ALKERA_TASK_VARIANT"
_MODULES: SimpleNamespace | None = None


def modules() -> SimpleNamespace:
    """`taskspec`, `episode`, `native_stack`, `aut`, `container_episode`."""
    global _MODULES
    if _MODULES is None:
        for path in (HARNESS_DIR, WORLD_PKG_PARENT, GATEWAY_SRC):
            if str(path) not in sys.path:
                sys.path.insert(0, str(path))
        import agent_under_test
        import container_episode
        import episode
        import native_stack
        import taskspec
        _MODULES = SimpleNamespace(taskspec=taskspec, episode=episode,
                                   native_stack=native_stack, aut=agent_under_test,
                                   container_episode=container_episode)
    return _MODULES


@contextmanager
def variant_env(arm: str | None):
    """Export `ALKERA_TASK_VARIANT` for the world builder (it resolves the arm
    at import time, from the environment); restore the previous value after.
    `None` clears it, so a listing never inherits a stale arm."""
    previous = os.environ.get(VARIANT_ENV)
    if arm:
        os.environ[VARIANT_ENV] = arm
    else:
        os.environ.pop(VARIANT_ENV, None)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(VARIANT_ENV, None)
        else:
            os.environ[VARIANT_ENV] = previous


def task_ids(tasks_dir=TASKS_DIR) -> list[str]:
    """Every task package: a `tasks/GT*` directory with a task.toml."""
    return sorted(p.name for p in tasks_dir.iterdir()
                  if p.is_dir() and p.name.startswith("GT")
                  and (p / "task.toml").is_file())


def load_spec(task_id: str, arm: str | None, tasks_dir=TASKS_DIR):
    """`taskspec.load` with the arm given explicitly and nothing inherited."""
    ts = modules().taskspec
    with variant_env(arm):
        return ts.load(task_id, tasks_dir, variant=arm or None)


def arms_of(task_id: str, tasks_dir=TASKS_DIR) -> list[str]:
    """The task's `[variants]` arms, sorted (taskspec's own listing)."""
    spec = load_spec(task_id, None, tasks_dir)
    return list(spec.variant_names) or [""]


def role_of(spec) -> str:
    return spec.matrix.role if spec.matrix is not None else "control"
