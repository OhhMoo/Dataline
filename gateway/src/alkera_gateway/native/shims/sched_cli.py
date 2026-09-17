"""`./ops/sched` - the scheduler CLI the workspace puts in front of the agent.

    ./ops/sched list
    ./ops/sched logs [RUN_ID | JOB]
    ./ops/sched trigger JOB [--date ISO] [--var KEY=VALUE ...]

WHY THIS EXISTS. `gateway/NATIVE_SURFACES.md` maps `sched_*` onto "Airflow 3
under vclock", and there is no Airflow: until this module existed native mode
had no scheduler surface at all, so three of GT001's probes and two of GT004's
sixteen required surfaces were dead by construction and no task requiring them
could be run under `--driver native` (gateway/NATIVE_INTEGRATION.md, runner-up
finding 2). The world's scheduler is a state file plus an executor, not a
server, so the native surface is a CLI in the repo - the shape `./ops/deploy`
already has - and not a tenth fake service.

EQUIVALENCE IS OBTAINED, NOT ASSERTED. Each subcommand runs the same
`alkera_gateway.operations.Operations` method the MCP tool of that name runs,
against the same injected `world.gateway_scheduler.WorldScheduler`, and reads
`op_class`, `targets` and `read_targets` back off the `records.Observation` the
method filled in. No subcommand names an op_class or a target. The row is
`sched.<mcp_tool>` - `sched.sched_list`, `sched.sched_trigger` - by
NATIVE_DRIVER.md's naming rule, so a grader that suffix-matches `sched_trigger`
matches both transports and `blindness._unattributed` attributes both.

    subcommand      row                   Operations method
    list            sched.sched_list      sched_list
    logs            sched.sched_logs      sched_logs
    trigger         sched.sched_trigger   sched_trigger

THERE IS NO `advance`, AND THAT IS THE POINT. `world/world/scheduler.py` has an
`advance`, and this CLI shipped one for a day; it is gone. `Operations` has no
`sched_advance` and `tools.TOOLS` has no such entry, so an MCP agent cannot move
the world's clock - the GRADER does, between checkpoints. A native subcommand
for it would have been a capability the native agent has and the MCP agent does
not, on the same `task.toml`, which is exactly the contamination that would make
the Phase 4 native-vs-MCP numbers incomparable. Every subcommand above has an
MCP equivalent and reads its classification off the `Operations` method that
equivalent runs; nothing here is hand-classified.

`logs` takes a RUN ID, which is what `sched_logs(run_id=...)` takes, and a JOB
name is accepted as a convenience because `python -m world.scheduler <root>
logs <job>` spells it that way. A job name is resolved by filtering the same
`sched_logs()` listing, so the row is still one `sched_logs` and the targets are
still `scheduler:<what was asked for>`.

THE DEPENDENCY ARROW. Nothing here imports `world/`. The executing scheduler
comes from `alkera_gateway.scheduling.episode_scheduler`, which owns the one
local, conditional import into Track A (agent.md 10.2); without that injection
every `trigger` would report success for work the world never ran, which is the
exact bug that module exists to end.

CONFIGURATION
-------------
ALKERA_GATEWAY_CONFIG  the gateway config JSON this episode's services are
                       served from - the same file, so the CLI and the nine
                       services address one world by construction.
ALKERA_SCHEDULER       `var/sched/state.json`. Used to derive the world when no
                       gateway config is set, and read by `native.audit` for
                       the row's `vts`.
plus the ALKERA_NATIVE_AUDIT / ALKERA_AUDIT_DIR that `audit.record` reads.

EXIT CODES
----------
    0  the subcommand ran
    1  the world refused it (unknown job, bad date, no scheduler state)
    2  the command line was wrong
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from ...config import GatewayConfig
from ...records import Observation, ToolError, json_value
from ..audit import record

ACTOR = "shim"
SERVICE = "sched"
#: How the CLI renders a run to a human. One line per run, because the agent
#: reads this in a terminal and a 30-run json blob is not a log.
LOG_LINE = "{id:<12} {job:<16} {status}"


class SchedulerError(Exception):
    """The world refused the request. Rendered, recorded, exit 1."""


def config() -> GatewayConfig:
    """The gateway config this episode serves, or one derived from the world.

    `ALKERA_GATEWAY_CONFIG` is the same file `native/serve.py` hands the nine
    services, so the CLI resolves this episode's planes exactly as they do. A
    workspace that carries only `ALKERA_SCHEDULER` still works: the world root
    is `<root>/var/sched/state.json` minus its two directories, which is the
    layout `world/world/scheduler.STATE` fixes.
    """
    name = os.environ.get("ALKERA_GATEWAY_CONFIG")
    if name and Path(name).is_file():
        return GatewayConfig.model_validate_json(
            Path(name).read_bytes())
    state = os.environ.get("ALKERA_SCHEDULER")
    if not state:
        raise SchedulerError(
            "neither ALKERA_GATEWAY_CONFIG nor ALKERA_SCHEDULER is set, so "
            "this shell has not been told which world it is working in")
    root = Path(state).resolve().parents[2]
    # Every path GatewayConfig defaults to an ABSOLUTE location outside the
    # world is re-pointed at the world's own conventional one, because
    # `Operations.__init__` stands the whole module stack up - the warehouse
    # included - and a config left on its defaults would have it open
    # `/var/warehouse/warehouse.duckdb`, a file belonging to no episode.
    return GatewayConfig(
        world_root=root, scheduler=Path(state), workspace=root / "workspace",
        warehouse=root / "var" / "warehouse" / "warehouse.duckdb",
        dashboards=root / "var" / "bi" / "dashboards",
        workflow_dir=root / "var" / "workflow",
        observability=root / "var" / "obs" / "observability.yml",
        ingest=root / "var" / "ingest" / "state.json",
        profiles_dir=root / "var" / "dbt")


def operations(cfg: GatewayConfig):
    """`Operations` over this world, with the EXECUTING scheduler injected.

    The injection is the whole point: `Operations(config, clock)` alone gets
    `FileScheduler`, which reports `not_executed` for every job that is not
    dbt-shaped, and an agent told its loader ran when it did not is the failure
    `alkera_gateway.scheduling` was written to end.
    """
    from ...operations import Operations
    from ...scheduling import episode_scheduler

    def clock() -> str:
        try:
            state = json.loads(cfg.scheduler.read_text(encoding="utf-8"))
            return state.get("vnow", cfg.virtual_time)
        except (OSError, ValueError):
            return cfg.virtual_time

    return Operations(cfg, clock, episode_scheduler(cfg))


def emit(tool: str, args: dict, observation: Observation, result: object,
         *, error: dict | None, latency_ms: float) -> dict:
    """One subcommand -> one 9.2.2 row, classified by the observation.

    `native.context.Ledger` does this for the HTTP services and cannot be
    reused: it is built around a FastAPI `Request` and reads the method and path
    off it. What is shared is the rule - every field but `tool`, `args` and
    `transport` comes off the `Observation` an `Operations` method filled in.
    """
    return record(
        tool=f"{SERVICE}.{tool}",
        args=args,
        op_class=observation.op_class,
        targets=observation.targets,
        read_targets=observation.read_targets,
        exit="error" if error else "ok",
        error=error,
        flags=list(observation.flags),
        rows_affected=observation.rows_affected,
        latency_ms=latency_ms,
        result=json_value(result),
        transport="native",
        actor=ACTOR,
    )


def run_list(ops, observation: Observation, _arguments) -> object:
    return ops.sched_list(observation)


def run_logs(ops, observation: Observation, arguments) -> object:
    """`sched_logs`, by run id or - as the world's own CLI spells it - by job.

    A job name cannot be handed to `sched_logs(run_id=...)`: `WorldScheduler`
    raises `Unknown run` for it. So the whole listing is asked for, exactly one
    `sched_logs`, and the rows are filtered here. The observation is left as
    that call classified it and the target is then re-pointed at what the agent
    ASKED for, which is the same string `sched_logs(run_id=<job>)` would have
    recorded - the row must say what was requested, not what it fell back to.
    """
    selector = arguments.selector
    if not selector:
        return ops.sched_logs(None, observation)
    state = ops.sched_state(None, observation.child())
    known = {run.get("id") for run in state.get("runs") or []}
    if selector in known:
        return ops.sched_logs(selector, observation)
    result = ops.sched_logs(None, observation)
    observation.read_targets = observation.targets = [f"scheduler:{selector}"]
    runs = [run for run in result.get("runs") or []
            if run.get("job") == selector]
    if not runs:
        raise SchedulerError(
            f"no run of {selector!r}: it is neither a run id nor a job with a "
            f"run in this world's history")
    return {"runs": runs}


def run_trigger(ops, observation: Observation, arguments) -> object:
    """`sched_trigger`, for an explicit logical date.

    The date defaults to the world's own `vnow` day rather than to today: a
    manual trigger scopes to a logical day (`world/world/scheduler.trigger`),
    and a wall-clock date would file the run under a day the world has never
    reached.
    """
    return ops.sched_trigger(arguments.job, arguments.date or vnow_day(ops),
                             dict(parse_vars(arguments.var)), observation)


def vnow_day(ops) -> str:
    """The world's current virtual day, `YYYY-MM-DD`."""
    return str(ops.clock())[:10]


def parse_vars(pairs: list[str] | None) -> list[tuple[str, str]]:
    out = []
    for pair in pairs or ():
        if "=" not in pair:
            raise SystemExit(f"--var takes KEY=VALUE, got {pair!r}")
        key, value = pair.split("=", 1)
        out.append((key, value))
    return out


def render(subcommand: str, result: object) -> str:
    """What the agent sees. A log is lines; everything else is json."""
    if subcommand == "list":
        jobs = (result or {}).get("jobs") or []
        return "\n".join(str(job.get("id", job)) for job in jobs)
    if subcommand == "logs" and isinstance(result, dict) and "runs" in result:
        return "\n".join(
            LOG_LINE.format(id=str(run.get("id", "")),
                            job=str(run.get("job") or ""),
                            status=str(run.get("status", "")))
            + (f"  {str(run.get('detail')).splitlines()[0]}"
               if run.get("detail") else "")
            for run in result["runs"])
    return json.dumps(result, indent=2, sort_keys=True, default=str)


#: subcommand -> (the MCP tool its row is named after, the function that runs it)
SUBCOMMANDS = {
    "list": ("sched_list", run_list),
    "logs": ("sched_logs", run_logs),
    "trigger": ("sched_trigger", run_trigger),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ops/sched", description="the team's scheduler")
    sub = parser.add_subparsers(dest="subcommand", required=True)
    sub.add_parser("list", help="the jobs this world runs")
    logs = sub.add_parser("logs", help="run history, or one run's output")
    logs.add_argument("selector", nargs="?",
                      help="a run id (run-000012) or a job name")
    trigger = sub.add_parser("trigger", help="run one job now")
    trigger.add_argument("job")
    trigger.add_argument("--date", default="",
                         help="the logical date, ISO; defaults to the world's "
                              "current virtual day")
    trigger.add_argument("--var", action="append",
                         help="KEY=VALUE passed to the job; repeatable")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand, write its row, print what the agent asked for."""
    arguments = build_parser().parse_args(
        list(sys.argv[1:] if argv is None else argv))
    tool, run = SUBCOMMANDS[arguments.subcommand]
    args = {"argv": ["ops/sched", *(sys.argv[1:] if argv is None else argv)],
            "cwd": Path.cwd().as_posix()}
    observation = Observation()
    started = time.monotonic()
    try:
        ops = operations(config())
        result = run(ops, observation, arguments)
    except (SchedulerError, ToolError, ValueError, OSError, RuntimeError) as exc:
        code = getattr(exc, "code", None) or type(exc).__name__
        if not observation.classified:
            # The refusal never reached a module, so nothing classified it.
            # Same rule the services follow (native/context.py): a refusal
            # classifies as a read of nothing, and the row is still written -
            # an attempt that left no row is what would make a world change
            # unattributable.
            observation.op_class = "read"
            observation.read_targets = observation.targets = []
        emit(tool, args, observation, None,
             error={"code": str(code), "message": str(exc)},
             latency_ms=(time.monotonic() - started) * 1000)
        print(f"sched: {exc}", file=sys.stderr)
        return 1
    emit(tool, args, observation, result, error=None,
         latency_ms=(time.monotonic() - started) * 1000)
    text = render(arguments.subcommand, result)
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
