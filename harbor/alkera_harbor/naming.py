"""Harbor names for (task, arm) pairs, and the `--task-ids` selector grammar.

One Harbor task per (task, arm): `alkera/<task-id-lowercase>__<arm>`, e.g.
`alkera/gt001-aov-halving__decision`. The directory is the part after the org.
Names are a pure function of the source ids, so they are stable across runs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .constants import ORG

#: Harbor's ORG_NAME_PATTERN (harbor/constants.py), split at the slash.
_SEGMENT = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
#: Ours is stricter: lowercase only, so the name is also a portable path and a
#: valid docker image name component.
_SHORT = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
ARM_SEPARATOR = "__"


class NamingError(ValueError):
    pass


@dataclass(frozen=True, order=True)
class ArmRef:
    """One exportable unit: a source task and one of its `[variants]` arms."""

    task_id: str
    arm: str

    @property
    def short_name(self) -> str:
        return short_name(self.task_id, self.arm)

    @property
    def harbor_name(self) -> str:
        return harbor_name(self.task_id, self.arm)

    @property
    def selector(self) -> str:
        return f"{self.task_id}:{self.arm}"


def short_name(task_id: str, arm: str) -> str:
    """`gt001-aov-halving__decision`: the task directory name."""
    if not arm:
        raise NamingError(f"{task_id}: an arm name is required (every task "
                          f"declares [variants]; `default` is the shipped one)")
    if ARM_SEPARATOR in task_id or ARM_SEPARATOR in arm:
        raise NamingError(f"{task_id}:{arm}: '{ARM_SEPARATOR}' is reserved as "
                          f"the task/arm separator")
    name = f"{task_id.lower()}{ARM_SEPARATOR}{arm.lower()}"
    if not _SHORT.match(name) or ".." in name:
        raise NamingError(f"{task_id}:{arm}: {name!r} is not a valid Harbor "
                          f"package name segment")
    return name


def harbor_name(task_id: str, arm: str, org: str = ORG) -> str:
    """`alkera/gt001-aov-halving__decision`: the `[task].name`."""
    if not _SEGMENT.match(org):
        raise NamingError(f"invalid org {org!r}")
    return f"{org}/{short_name(task_id, arm)}"


def check_unique(refs) -> None:
    """Two arms must never collapse onto one Harbor name (case folding)."""
    seen: dict[str, ArmRef] = {}
    for ref in refs:
        other = seen.setdefault(ref.short_name, ref)
        if other != ref:
            raise NamingError(f"{other.selector} and {ref.selector} both map to "
                              f"{ref.short_name!r}")


def select(available: list[ArmRef], selectors: list[str] | None) -> list[ArmRef]:
    """Resolve `--task-ids` against the available arms, in sorted order.

    Accepted spellings (case-insensitive):
      GT001-aov-halving            every arm of the task
      GT001                        every arm of the task (the GT code alone)
      GT001-aov-halving:decision   one arm
      gt001-aov-halving__decision  one arm, as the Harbor directory name
      alkera/gt001-aov-halving__decision   one arm, as the Harbor task name
    An unknown selector is an error, never silently ignored.
    """
    if not selectors:
        return sorted(available)
    by_task: dict[str, list[ArmRef]] = {}
    for ref in available:
        by_task.setdefault(ref.task_id.lower(), []).append(ref)
        by_task.setdefault(ref.task_id.split("-", 1)[0].lower(), []).append(ref)
    by_short = {ref.short_name: ref for ref in available}
    chosen: set[ArmRef] = set()
    for raw in selectors:
        for sel in (s.strip() for s in raw.split(",")):
            if not sel:
                continue
            key = sel.lower()
            if key.startswith(f"{ORG}/"):
                key = key[len(ORG) + 1:]
            if ":" in key:
                task, _, arm = key.partition(":")
                hits = [r for r in by_task.get(task, []) if r.arm.lower() == arm]
            elif ARM_SEPARATOR in key:
                hits = [by_short[key]] if key in by_short else []
            else:
                hits = by_task.get(key, [])
            if not hits:
                names = sorted({r.task_id for r in available})
                raise NamingError(
                    f"--task-ids {sel!r} matches nothing. Tasks: {', '.join(names)}; "
                    f"an arm is selected as TASK:ARM (see --list).")
            chosen.update(hits)
    return sorted(chosen)
