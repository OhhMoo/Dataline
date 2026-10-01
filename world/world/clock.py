"""World clock (offline) and replay: the world keeps running during an episode.

The clock ticks on **agent command boundaries**, not wall time. Before command
number n (1-based), if n % m == 0, the clock runs `scheduler.advance(root, h)`
synchronously, then the command runs. Nothing else moves the world, so the
world never writes while an agent command runs, and the tick sequence is a pure
function of the command count. After the episode, `finish` advances the world
to the horizon T in h-hour steps, the last one shortened.

Every tick is recorded in `tick_schedule.json` (an author-side ledger; it must
never be agent-readable). `replay` drives a twin world (no-op, oracle, ...)
through the same recorded virtual times, letting a `Policy` act only at tick
boundaries.

Elapsed time is always measured from the world's own `vnow`, never summed in
floats: an agent command such as `sched_trigger` moves `vnow` by the job's
duration (scheduler.trigger), and that time counts toward T too.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Callable, Protocol

from . import scheduler

OnTick = Callable[[Path, dict], None]
HOUR = timedelta(hours=1)


def _whole_seconds(hours: float) -> bool:
    return float(hours * 3600).is_integer()


@dataclass(frozen=True)
class TickSpec:
    m: int              # tick before every m-th agent command
    h_hours: float      # virtual hours per tick
    T_hours: float      # horizon, measured from the vnow the clock started at

    def __post_init__(self):
        if not isinstance(self.m, int) or self.m < 1:
            raise ValueError(f"TickSpec.m must be an int >= 1, got {self.m!r}")
        if not 0 < self.h_hours <= scheduler.MAX_HOURS:
            raise ValueError(f"TickSpec.h_hours must be within "
                             f"(0, {scheduler.MAX_HOURS}], got {self.h_hours!r}")
        if self.T_hours < 0:
            raise ValueError(f"TickSpec.T_hours must be >= 0, got {self.T_hours!r}")
        # vnow is stored at 1 s resolution (scheduler.iso); a sub-second step
        # would be silently truncated and the clock could never reach T.
        for name in ("h_hours", "T_hours"):
            if not _whole_seconds(getattr(self, name)):
                raise ValueError(f"TickSpec.{name} must be a whole number of "
                                 f"seconds, got {getattr(self, name)!r}")


class Policy(Protocol):
    name: str

    def act(self, root: Path, tick: int, vnow: str) -> list[str]:
        """Act on the world at a tick boundary; return the actions taken, for
        the log. Called with tick=-1 once before the first tick."""
        ...


def _vnow(root: Path) -> str:
    return scheduler.load(root)["vnow"]


def _hours(a: str, b: str) -> float:
    return (scheduler.parse_vtime(b) - scheduler.parse_vtime(a)) / HOUR


class Clock:
    def __init__(self, root: Path, spec: TickSpec,
                 on_tick: OnTick | None = None):
        self.root = Path(root)
        self.spec = spec
        self.on_tick = on_tick
        self.vnow_start = _vnow(self.root)
        self.commands = 0
        self.ticks: list[dict] = []

    def _tick(self, phase: str, after_command: int | None, hours: float) -> dict:
        before = _vnow(self.root)
        after = scheduler.advance(self.root, hours)["vnow"]
        record = {"tick": len(self.ticks), "phase": phase,
                  "after_command": after_command,
                  "vnow_before": before, "vnow_after": after}
        self.ticks.append(record)
        if self.on_tick is not None:
            self.on_tick(self.root, record)
        return record

    def before_command(self) -> dict | None:
        """Call before every agent command. Returns the tick record if a tick
        fired (on commands m, 2m, ...), else None."""
        self.commands += 1
        if self.commands % self.spec.m:
            return None
        return self._tick("episode", self.commands, self.spec.h_hours)

    def _remaining_seconds(self) -> int:
        elapsed = scheduler.parse_vtime(_vnow(self.root)) \
            - scheduler.parse_vtime(self.vnow_start)
        return int(self.spec.T_hours * 3600) - int(elapsed.total_seconds())

    def finish(self) -> list[dict]:
        """Advance to T in h-hour steps, the last one shortened. Nothing when
        the world is already at or past T (never a negative advance)."""
        step_s = int(self.spec.h_hours * 3600)
        out = []
        while (remaining := self._remaining_seconds()) > 0:
            out.append(self._tick("post", None, min(step_s, remaining) / 3600))
        return out

    def schedule(self) -> dict:
        """tick_schedule.json content. `vnow_start` (an addition to the plan's
        shape) lets `replay` refuse a twin that does not start where the
        episode did."""
        return {"m": self.spec.m, "h_hours": self.spec.h_hours,
                "T_hours": self.spec.T_hours, "vnow_start": self.vnow_start,
                "ticks": [dict(t) for t in self.ticks]}


def replay(root: Path, schedule: dict, policy: Policy,
           on_tick: OnTick | None = None) -> list[dict]:
    """Drive `root` through `schedule`'s ticks with `policy`.

    `policy.act(root, -1, vnow_t0)` first; then for each tick (episode, then
    post): `policy.act(root, tick, vnow_before)`, advance, `on_tick(root,
    record)`. Each advance goes to the tick's recorded `vnow_after` - for a
    no-op policy that is exactly the recorded step; a policy that moved the
    clock itself (scheduler.trigger adds the job's duration) gets the rest of
    the step, so the twin sees the same tick times as the episode. A policy
    that ran past a tick's `vnow_after` gets a zero advance there.

    Returns one record per tick: the schedule's record plus `actions`; the
    actions of the tick=-1 call are in the first record's `actions_initial`.
    """
    root = Path(root)
    start = _vnow(root)
    expected = schedule.get("vnow_start")
    if expected is not None and start != expected:
        raise ValueError(f"replay: world vnow {start} != schedule vnow_start "
                         f"{expected}; the twin must start from the episode's "
                         "starting world")
    initial = list(policy.act(root, -1, start))
    out = []
    for rec in schedule["ticks"]:
        actions = list(policy.act(root, rec["tick"], rec["vnow_before"]))
        hours = max(0.0, _hours(_vnow(root), rec["vnow_after"]))
        scheduler.advance(root, hours)
        record = {**rec, "actions": actions}
        if not out:
            record["actions_initial"] = initial
        out.append(record)
        if on_tick is not None:
            on_tick(root, record)
    return out
