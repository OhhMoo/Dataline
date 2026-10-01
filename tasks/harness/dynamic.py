"""Dynamic worlds in an episode: the world clock on the agent's command boundary.

Spec: docs/implementation_plan/experimental_ideas/2026-09-30-dynamic-world-design.md
(section 4.1 clock, 5 monitorability, 9 risks); plan Task 6.

OPT-IN, PER TASK. A task declares

    [dynamic]
    m        = 10                 # tick before every m-th MODEL command
    h_hours  = 6.0                # virtual hours per tick
    T_hours  = 168.0              # horizon from the episode's starting vnow
    damage   = "damage.yaml"      # -> world.damage
    shocks   = "shock_spec.yaml"  # -> world.hawkes
    # fault_seed = 7              # optional; default: shock_spec.yaml's own

and nothing else in the harness changes for a task that does not: `load`
returns None, every call site is guarded on it, and the rexec bridge is
started with exactly the argv it always had.

WHERE THE TICK RUNS, AND WHY. Under the native container topology the world
lives on the HOST (`rollout_dir/world`); the episode container mounts only the
working tree, the warehouse FILES and the audit directory, and runs every
agent command as root (the image declares no USER). A ledger "in a root-owned
0700 directory inside the episode" would therefore be readable by the agent,
which IS root there. So nothing author-side is put in the container at all:

  * the tick (`world.clock.Clock` -> `scheduler.advance` -> `damage.sample`)
    runs in THIS process, on the host, as the operator's uid, against the host
    world tree - the same place the grader and the twins run `advance`;
  * the ledgers (`damage.jsonl`, `tick_schedule.json`, `clock_events.jsonl`,
    and a copy of `shock_schedule.json`) are written straight into
    `rollout_dir/dynamic/`, a host directory no container mounts (containment
    P7 question B already proves the rollout directory has no path inside);
  * `var/world/` (live.json, shock_schedule.json) is a world plane P7 question A
    already proves absent from the episode container; the dynamic probe adds an
    explicit unreadability check of both files by name (`probe_paths`).

The ONLY thing the episode side does is say "command n is about to run" and
wait: `gateway/docker/subject/rexec.py serve --clock stdio` writes one line per
model command on its stdout - which is the harness's `docker exec -i` pipe, not
a file and not a socket the agent can dial - and blocks until the harness
answers. That makes the count the MODEL's commands (one rexec request per Bash
tool call), not gateway audit rows, and makes the tick synchronous: no new
command starts while the world advances.
"""
from __future__ import annotations

import json
import shutil
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TRACK_A = REPO / "world"

#: The ledger directory inside a rollout directory.
LEDGER_DIRNAME = "dynamic"
#: In-container paths the probe must find absent/unreadable for a dynamic
#: episode. `/world/var/world` is the live-world control plane; the ledger
#: names are listed too so a future mount that leaked them would light up.
PROBE_PATHS = ("/world/var/world/live.json",
               "/world/var/world/shock_schedule.json",
               "/world/var/world/damage.jsonl",
               "/world/var/world/tick_schedule.json",
               "/world/dynamic", "/workspace/../dynamic", "/dynamic")


class DynamicError(Exception):
    """A dynamic task that cannot run as declared."""


def _world():
    """Import the world package lazily: a static task never loads it here."""
    if str(TRACK_A) not in sys.path:
        sys.path.insert(0, str(TRACK_A))
    from world import clock, damage, hawkes, scheduler   # noqa: E402
    return clock, damage, hawkes, scheduler


@dataclass(frozen=True)
class DynamicSpec:
    m: int
    h_hours: float
    T_hours: float
    damage_path: Path
    shocks_path: Path
    fault_seed: int | None = None       # None: the shock spec's own
    terminal_gamma: float = 1.0

    def record(self) -> dict:
        return {"m": self.m, "h_hours": self.h_hours, "T_hours": self.T_hours,
                "damage": str(self.damage_path), "shocks": str(self.shocks_path),
                "fault_seed_override": self.fault_seed,
                "terminal_gamma": self.terminal_gamma}


def parse(task_root: Path, raw: dict) -> DynamicSpec | None:
    """`[dynamic]` of a task.toml (already parsed), or None when absent."""
    block = raw.get("dynamic")
    if block is None:
        return None
    if not isinstance(block, dict):
        raise DynamicError("[dynamic] must be a table")
    missing = [k for k in ("m", "h_hours", "T_hours", "damage", "shocks")
               if k not in block]
    if missing:
        raise DynamicError(f"[dynamic] is missing {missing}")
    m = block["m"]
    if not isinstance(m, int) or isinstance(m, bool) or m < 1:
        raise DynamicError(f"[dynamic].m must be an int >= 1, got {m!r}")
    out = DynamicSpec(
        m=m, h_hours=float(block["h_hours"]), T_hours=float(block["T_hours"]),
        damage_path=Path(task_root) / str(block["damage"]),
        shocks_path=Path(task_root) / str(block["shocks"]),
        fault_seed=(int(block["fault_seed"]) if "fault_seed" in block else None),
        terminal_gamma=float(block.get("terminal_gamma", 1.0)))
    for p in (out.damage_path, out.shocks_path):
        if not p.is_file():
            raise DynamicError(f"[dynamic] names {p}, which does not exist")
    return out


def load(spec) -> DynamicSpec | None:
    """The task's dynamic spec, from a TaskSpec (or anything with root/raw)."""
    return parse(Path(spec.root), getattr(spec, "raw", {}) or {})


def load_task(task_dir: Path) -> DynamicSpec | None:
    raw = tomllib.loads((Path(task_dir) / "task.toml").read_text(encoding="utf-8"))
    return parse(Path(task_dir), raw)


def require_supported(*, driver: str, topology: str, subject_sandbox: str) -> None:
    """v1 ticks only where rexec is the choke point (spec section 9, MCP driver)."""
    if (driver, topology, subject_sandbox) != ("native", "container", "container"):
        raise DynamicError(
            "a [dynamic] task ticks the world on the rexec command boundary, "
            "which exists only under --driver native --topology container "
            f"--subject-sandbox container; got driver={driver!r} "
            f"topology={topology!r} subject_sandbox={subject_sandbox!r}")


# --------------------------------------------------------------------- staging
def stage(dyn: DynamicSpec, world_root: Path, ledger_dir: Path) -> dict:
    """Sample the episode's shock schedule and install it into the world.

    Runs on the host after the world is built and BEFORE the world's "before"
    picture, so the installed faults are part of t=0. `hawkes.install` writes
    live.json's catalogue and `var/world/shock_schedule.json` (a world plane the
    episode container never mounts); the schedule is also copied into the
    ledger directory now, so it is in the rollout record even if the episode
    dies.
    """
    _, _, hawkes, scheduler = _world()
    spec = hawkes.load_spec(dyn.shocks_path)
    if dyn.fault_seed is not None:
        spec = {**spec, "fault_seed": dyn.fault_seed}
    schedule = hawkes.sample(Path(world_root), spec)
    hawkes.install(Path(world_root), schedule)
    ledger_dir = Path(ledger_dir)
    ledger_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(world_root) / hawkes.SCHEDULE,
                    ledger_dir / "shock_schedule.json")
    shocks = schedule.get("shocks", [])
    return {"fault_seed": schedule.get("fault_seed"),
            "rho_target": schedule.get("rho_target"),
            "rho_empirical": schedule.get("rho_empirical"),
            "shocks": len(shocks),
            "shocks_executed": sum(1 for s in shocks if not s.get("suppressed")),
            "vnow_start": scheduler.load(Path(world_root))["vnow"],
            "ledger_dir": str(ledger_dir),
            "spec": dyn.record()}


# ---------------------------------------------------------------------- driver
@dataclass
class Driver:
    """The host side of the world clock for one episode.

    `on_command(seq, inflight)` is called by the rexec control reader once per
    model command, before that command runs, and returns when the world may
    proceed. Commands are counted only while `armed` and not `paused`: the
    harness's own shell proofs (`hostname` through the same bridge) are not the
    model's commands.
    """
    dyn: DynamicSpec
    world_root: Path
    ledger_dir: Path
    tree_before: dict | None = None
    tree_manifest: object = None       # callable(root) -> manifest; injectable
    armed: bool = False
    paused: bool = False
    clock: object = None
    errors: list = field(default_factory=list)
    events: list = field(default_factory=list)
    agent_touched: set = field(default_factory=set)
    expected: dict | None = None
    finished: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self):
        clock, damage, _, _ = _world()
        self.world_root = Path(self.world_root)
        self.ledger_dir = Path(self.ledger_dir)
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        self._damage = damage
        self._damage_spec = damage.load_spec(self.dyn.damage_path)
        self.clock = clock.Clock(self.world_root,
                                 clock.TickSpec(m=self.dyn.m, h_hours=self.dyn.h_hours,
                                                T_hours=self.dyn.T_hours),
                                 on_tick=self._on_tick)
        if self.tree_before is not None:
            self.expected = dict(self.tree_before)

    # ------------------------------------------------------------- the tick
    def _on_tick(self, root: Path, record: dict) -> None:
        row = self._damage.sample(root, self._damage_spec)
        self._damage.append(self.ledger_dir / "damage.jsonl",
                            {"tick": record["tick"], "phase": record["phase"],
                             **row})

    def _event(self, **kw) -> None:
        kw["wall_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.events.append(kw)
        with open(self.ledger_dir / "clock_events.jsonl", "a",
                  encoding="utf-8") as fh:
            fh.write(json.dumps(kw, sort_keys=True) + "\n")

    def arm(self) -> None:
        self.armed = True

    def disarm(self) -> None:
        self.armed = False

    def on_command(self, seq: int, inflight: int = 0) -> dict | None:
        """One model command is about to run. Ticks first when it is the m-th."""
        with self._lock:
            if not self.armed or self.paused or self.finished:
                self._event(kind="uncounted", seq=seq,
                            why=("finished" if self.finished else
                                 "paused" if self.paused else "not_armed"))
                return None
            started = time.time()
            pre = self._snapshot_pre()
            try:
                rec = self.clock.before_command()
            except Exception as exc:                  # noqa: BLE001 - recorded
                self.errors.append(f"tick before command "
                                   f"{self.clock.commands}: "
                                   f"{type(exc).__name__}: {exc}")
                self._event(kind="tick_error", seq=seq,
                            command=self.clock.commands, error=str(exc)[:2000])
                return None
            if rec is None:
                return None
            self._snapshot_post(pre)
            self._event(kind="tick", seq=seq, command=self.clock.commands,
                        tick=rec["tick"], inflight=inflight,
                        seconds=round(time.time() - started, 3),
                        vnow_before=rec["vnow_before"],
                        vnow_after=rec["vnow_after"])
            return rec

    # The world diff. Ticks change the world on the host (new landing files,
    # the warehouse, var/sched...). `blindness` and the workspace bookkeeping
    # must not read those as the agent's changes, so each in-episode tick is
    # bracketed by a manifest: what changed between the last known state and
    # the pre-tick picture is the agent's (kept, and never laundered by a
    # later tick touching the same file), what changed across the tick is the
    # world's.
    def _snapshot_pre(self):
        if self.expected is None or self.tree_manifest is None:
            return None
        if self.clock.commands + 1 < self.dyn.m or \
                (self.clock.commands + 1) % self.dyn.m:
            return None                               # not a tick: no cost
        pre = self.tree_manifest(self.world_root)
        for k in set(pre) | set(self.expected):
            if pre.get(k) != self.expected.get(k):
                self.agent_touched.add(k)
        return pre

    def _snapshot_post(self, pre) -> None:
        if pre is None:
            return
        post = self.tree_manifest(self.world_root)
        for k in set(pre) | set(post):
            if pre.get(k) != post.get(k):
                if k in post:
                    self.expected[k] = post[k]
                else:
                    self.expected.pop(k, None)

    def baseline(self, tree_before: dict) -> dict:
        """The "before" picture the agent must be judged against: t=0 plus
        every change the world's own ticks made, minus nothing the agent did."""
        if self.expected is None:
            return tree_before
        out = dict(self.expected)
        for k in self.agent_touched:
            if k in tree_before:
                out[k] = tree_before[k]
            else:
                out.pop(k, None)
        return out

    # ---------------------------------------------------------- after agent
    def finish(self) -> dict:
        """Advance to T (post ticks, damage sampled at each), write ledgers."""
        with self._lock:
            self.armed = False
            self.finished = True
            post = []
            try:
                post = self.clock.finish()
            except Exception as exc:                  # noqa: BLE001 - recorded
                self.errors.append(f"finish: {type(exc).__name__}: {exc}")
                self._event(kind="finish_error", error=str(exc)[:2000])
            schedule = self.clock.schedule()
            (self.ledger_dir / "tick_schedule.json").write_text(
                json.dumps(schedule, indent=2, sort_keys=True) + "\n",
                encoding="utf-8")
            src = self.world_root / "var" / "world" / "shock_schedule.json"
            if src.is_file():
                shutil.copyfile(src, self.ledger_dir / "shock_schedule.json")
            ticks = schedule["ticks"]
            return {
                "commands_counted": self.clock.commands,
                "ticks_episode": sum(1 for t in ticks if t["phase"] == "episode"),
                "ticks_post": len(post),
                "vnow_start": schedule.get("vnow_start"),
                "vnow_end": ticks[-1]["vnow_after"] if ticks else schedule.get("vnow_start"),
                "uncounted_commands": sum(1 for e in self.events
                                          if e.get("kind") == "uncounted"),
                "tick_seconds": [e["seconds"] for e in self.events
                                 if e.get("kind") == "tick"],
                "overlapping_ticks": [e["tick"] for e in self.events
                                      if e.get("kind") == "tick" and e.get("inflight")],
                "agent_touched_before_a_tick": sorted(self.agent_touched),
                "errors": list(self.errors),
                "ledgers": sorted(p.name for p in self.ledger_dir.iterdir()),
            }


def control_loop(stdout, stdin, driver: Driver) -> None:
    """Serve rexec's clock control channel until it closes.

    Line protocol (rexec.py serve --clock stdio): the server writes
    `tick? <seq> <inflight>`; the harness runs `driver.on_command` and answers
    `go <seq>`. Anything else on the pipe is recorded and ignored.
    """
    expected = 1                       # per bridge: a restarted one counts from 1
    for raw in iter(stdout.readline, b""):
        line = raw.decode("utf-8", "replace").strip()
        parts = line.split()
        if len(parts) == 3 and parts[0] == "tick?" and parts[1].isdigit() \
                and parts[2].isdigit():
            seq = int(parts[1])
            try:
                if seq != expected:
                    # Out of sequence: something other than the bridge wrote
                    # it (the agent is root in the episode and can reach
                    # /proc/<rexec>/fd/1). Recorded, never ticked on.
                    driver.errors.append(f"control sequence {seq}, expected {expected}")
                    driver._event(kind="sequence_violation", seq=seq,
                                  expected=expected)
                else:
                    expected += 1
                    driver.on_command(seq, int(parts[2]))
            finally:
                try:
                    stdin.write(f"go {seq}\n".encode())
                    stdin.flush()
                except OSError:
                    return
        elif line:
            driver.errors.append(f"unexpected control line: {line[:200]!r}")
            driver._event(kind="unexpected_line", line=line[:200])


#: What the scripted subject runs after its script on a dynamic task: read-only
#: looks at the world that keeps moving. Cycled to `2 * m` commands, so a
#: scripted rollout crosses exactly two episode ticks (commands m and 2m).
SCRIPTED_LOOKS = (
    "ls -1 data/landing/daily_revenue | tail -n 2",
    "./ops/sched list",
    "./ops/sched logs | tail -n 3",
)


def scripted_commands(m: int) -> list[str]:
    return [SCRIPTED_LOOKS[i % len(SCRIPTED_LOOKS)] for i in range(2 * m)]
