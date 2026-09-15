import json
import shlex
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .records import Observation, ToolError, canonical

# A trigger either does the work, or it says - in the run record, in the log
# blob and in the tool envelope - that it did not. It never reports `success`
# for work it did not do. `not_executed` is that third status: the job exists,
# the scheduler was asked to run it, and this adapter could not, so nothing
# ran. It is deliberately NOT "success" and deliberately NOT "failed": a
# grader that counts failures should not count this as a pipeline that ran and
# broke, and an agent must not read it as a pipeline that ran and worked.
NOT_EXECUTED = "not_executed"

# dbt subcommands this adapter is willing to run. Same allow-list as the
# world-side executor (world/world/executor.py:219), so a job that executes
# here executes there.
DBT_SUBCOMMANDS = frozenset({"build", "run", "test", "compile", "seed", "snapshot"})
SELECT_FLAGS = ("--select", "-s", "--models", "-m")


class NotExecutable(Exception):
    """This adapter cannot run the work a job declares. Carries the reason.

    Never raised out of a tool call: `trigger` converts it into a recorded
    `not_executed` run, which is louder than an exception because it survives
    into the run history the grader reads.
    """


def dbt_invocation(job: dict) -> dict:
    """The dbt call a job declares: {command, selector, full_refresh, vars}.

    Raises `NotExecutable` when the job declares something that is not dbt, or
    dbt carrying an option this adapter cannot pass through. Refusing the
    unknown option matters as much as refusing the unknown command: silently
    dropping `--exclude` would run a DIFFERENT command than the job declares
    and then report it as the job, which is the same lie in a smaller package.
    """
    command = job.get("command")
    selector = job.get("selector")
    if command in {"dbt_build", "dbt_run"}:
        return {
            "command": "build" if command == "dbt_build" else "run",
            "selector": selector or "*",
            "full_refresh": False,
            "vars": {},
        }
    argv = []
    if isinstance(command, str) and command.strip():
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise NotExecutable(
                f"command {command!r} does not parse as a command line ({exc})"
            ) from None
    if argv and Path(argv[0]).stem == "dbt":
        return _parse_dbt_argv(argv, command)
    if selector:
        # Legacy shape, kept because it executes today: a job carrying an
        # explicit dbt selector runs `dbt run --select <selector>`. No shipped
        # world uses it - measured across the four themes and GT001-GT004 -
        # but narrowing a shape that currently runs is not this fix's job.
        return {"command": "run", "selector": selector, "full_refresh": False, "vars": {}}
    if not isinstance(command, str) or not command.strip():
        raise NotExecutable("the job declares no command")
    raise NotExecutable(f"command {command!r} is not a dbt invocation")


def _parse_dbt_argv(argv: list, command: str) -> dict:
    if len(argv) < 2:
        raise NotExecutable(f"command {command!r} names no dbt subcommand")
    subcommand = argv[1]
    if subcommand not in DBT_SUBCOMMANDS:
        raise NotExecutable(
            f"dbt subcommand {subcommand!r} is not one this scheduler will run "
            f"(allowed: {', '.join(sorted(DBT_SUBCOMMANDS))})"
        )
    selector, full_refresh, variables, index = None, False, {}, 2
    while index < len(argv):
        token = argv[index]
        if token in SELECT_FLAGS and index + 1 < len(argv):
            selector, index = argv[index + 1], index + 2
        elif token == "--full-refresh":
            # Destructive stays FLAGGED, not blocked (agent.md 9.1): the call
            # goes through and the flag rides out on the result.
            full_refresh, index = True, index + 1
        elif token == "--vars" and index + 1 < len(argv):
            try:
                parsed = yaml.safe_load(argv[index + 1])
            except yaml.YAMLError as exc:
                raise NotExecutable(
                    f"--vars in {command!r} is not valid YAML/JSON ({exc})"
                ) from None
            if not isinstance(parsed, dict):
                raise NotExecutable(f"--vars in {command!r} is not a mapping")
            variables, index = parsed, index + 2
        else:
            raise NotExecutable(
                f"command {command!r} carries {token!r}, which this scheduler "
                f"cannot pass through to dbt"
            )
    return {
        "command": subcommand,
        "selector": selector or "*",
        "full_refresh": full_refresh,
        "vars": variables,
    }


def dbt_action(plan: dict) -> str:
    return (
        "dbt "
        + plan["command"]
        + (f" --select {plan['selector']}" if plan["selector"] != "*" else "")
        + (" --full-refresh" if plan["full_refresh"] else "")
    )


class Scheduler(Protocol):
    def list(self) -> dict: ...
    def trigger(self, job_id: str, execution_date: str, variables: dict) -> dict: ...
    def state(self, run_id: str | None) -> dict: ...
    def logs(self, run_id: str | None) -> dict: ...


class FileScheduler:
    """Adapter for Track A's deterministic scheduler state.

    It executes exactly one kind of work: dbt, through `commands.dbt`. That is
    the whole of what the gateway can run on its own - it has no live-world
    control plane, no theme generator and no loader scripts, which is why
    `world.gateway_scheduler.WorldScheduler` exists and should be injected
    whenever a world has one.

    What it will not do is report `success` for anything else. Until this was
    fixed, every job whose command was not literally `dbt_build`/`dbt_run`
    returned `{"intent": <command>}` with `status: success` and ran nothing -
    measured inert on 14 of the 20 job shapes the four themes and GT001-GT004
    ship, including GT002's `dbt build --select warehouse`, which is a real dbt
    job that simply spelled its command as a command line. Those now either run
    (the dbt-shaped ones, parsed rather than string-matched) or record
    `status: not_executed` with the reason.
    """

    def __init__(self, path: Path, commands, clock):
        self.path = path
        self.logs_dir = path.parent / "logs"
        self.commands = commands
        self.clock = clock

    def load(self):
        state = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(state.get("jobs"), (dict, list)) or not isinstance(
            state.get("runs"), list
        ):
            raise ToolError("invalid_scheduler", "Scheduler state needs jobs and runs")
        return state

    def list(self):
        jobs = self.load()["jobs"]
        if isinstance(jobs, dict):
            jobs = [{"id": job_id, **spec} for job_id, spec in sorted(jobs.items())]
        return {"jobs": jobs}

    def trigger(self, job_id: str, execution_date: str, variables: dict):
        try:
            datetime.fromisoformat(execution_date.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ToolError(
                "invalid_date", "execution_date must be an ISO date or timestamp"
            ) from exc
        state = self.load()
        jobs = state["jobs"]
        if isinstance(jobs, dict):
            spec = jobs.get(job_id)
            job = {"id": job_id, **spec} if spec else None
        else:
            job = next((item for item in jobs if item["id"] == job_id), None)
        if job is None:
            raise ToolError("job_not_found", f"Unknown job: {job_id}")
        vnow = state.get("vnow", self.clock())
        run = {
            "id": f"run-{len(state['runs']) + 1:06d}",
            "job_id": job_id,
            "execution_date": execution_date,
            "started_at": vnow,
            "status": "running",
        }
        state["runs"].append(run)
        self.save(state)
        command = job.get("command")
        executed = False
        returncode = 1
        try:
            try:
                plan = dbt_invocation(job)
            except NotExecutable as exc:
                plan, reason = None, str(exc)
            if plan is not None:
                observation = Observation()
                outcome = self.commands.dbt(
                    plan["command"],
                    plan["selector"],
                    {**plan["vars"], **variables, "execution_date": execution_date},
                    plan["full_refresh"],
                    observation,
                )
                executed = True
                action = dbt_action(plan)
                returncode = int(outcome.get("returncode", 0))
                failed = returncode != 0 or bool(outcome.get("timed_out"))
                run["status"] = "failed" if failed else "success"
                run["executed"] = True
                run["action"] = action
                if failed:
                    run["error"] = f"{action} exited {returncode}" + (
                        " after the command timeout" if outcome.get("timed_out") else ""
                    )
                result = {**outcome, "action": action, "executed": True, "flags": observation.flags}
            else:
                # The one thing this surface will never do again: report
                # success for work it did not do. Nothing ran, so the run says
                # so, the log blob says so, no virtual time is consumed, and
                # `returncode` stays 1 so the gateway files the call as an
                # error row rather than an `ok` one.
                message = (
                    f"job {job_id!r} was NOT executed: {reason}. This scheduler "
                    f"adapter executes dbt jobs only; nothing ran and nothing "
                    f"changed. Run the job's work directly (its script, SQL or "
                    f"loader) and do not treat this run as the job having run."
                )
                run["status"] = NOT_EXECUTED
                run["executed"] = False
                run["error"] = message
                result = {
                    "status": NOT_EXECUTED,
                    "executed": False,
                    "command": command,
                    "variables": variables,
                    "error": message,
                }
        except Exception as exc:
            run["status"] = "failed"
            run["executed"] = executed
            run["error"] = str(exc)
            result = {"error": str(exc), "executed": executed}
            raise
        finally:
            # Virtual time is consumed only by work that actually ran. A job
            # the scheduler refused to run must not age the world's clock by
            # its nominal duration - that would be the same lie told in the
            # time domain.
            duration = int(job.get("duration_s", 0)) if executed else 0
            finished = datetime.fromisoformat(vnow.replace("Z", "+00:00")) + timedelta(
                seconds=duration
            )
            run["finished_at"] = finished.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            if "vnow" in state:
                state["vnow"] = run["finished_at"]
            self.save(state)
            self.save_log(run["id"], result)
        # `returncode` is the gateway's convention for "the thing you asked me
        # to run did not succeed" (core.py:103). It rides on the envelope and
        # is not stored in the run record, matching what the world's own
        # WorldScheduler already publishes.
        return {**run, "returncode": returncode}

    def save(self, state):
        pending = self.path.with_suffix(".pending")
        pending.write_bytes(canonical(state) + b"\n")
        pending.replace(self.path)

    def save_log(self, run_id, result):
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        path = self.logs_dir / f"{run_id}.json"
        pending = path.with_suffix(".pending")
        pending.write_bytes(canonical(result) + b"\n")
        pending.replace(path)

    def find(self, run_id):
        for index, run in enumerate(self.load()["runs"], 1):
            if run.get("id", f"run-{index:06d}") == run_id:
                if "id" not in run:
                    run = {"id": f"run-{index:06d}", **run}
                return run
        raise ToolError("run_not_found", f"Unknown run: {run_id}")

    def state(self, run_id):
        if run_id is None:
            state = self.load()
            state["runs"] = [
                {"id": f"run-{index:06d}", **run} for index, run in enumerate(state["runs"], 1)
            ]
            return state
        return self.find(run_id)

    def logs(self, run_id):
        if run_id is None:
            return {
                "runs": [
                    {
                        "id": run["id"],
                        "job": run.get("job", run.get("job_id")),
                        "status": run["status"],
                        "detail": run.get("detail"),
                    }
                    for run in self.state(None)["runs"]
                ]
            }
        run = self.find(run_id)
        path = self.logs_dir / f"{run_id}.json"
        if path.exists():
            result = json.loads(path.read_text(encoding="utf-8"))
        else:
            result = run.get("result")
        return {"run_id": run_id, "result": result}


class Panel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    sql: str
    title: str | None = None
    metric: str | None = None
    row_filter: str | None = None
    watermark_sql: str | None = None


class Metric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    expression: str
    owner: str | None = None
    doc: str | None = None


class Dashboard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(default=1, ge=1, le=1)
    dashboard: str
    title: str | None = None
    owner: str | None = None
    metrics: list[Metric] = Field(default_factory=list)
    panels: list[Panel] = Field(min_length=1)


def dashboard_path(directory: Path, dashboard: str, suffix="yml") -> Path:
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789_-"
    if not dashboard or any(char not in allowed for char in dashboard):
        raise ToolError("invalid_dashboard", "Use a dashboard ID containing a-z, 0-9, _ or -")
    path = (directory / f"{dashboard}.{suffix}").resolve()
    if not path.is_relative_to(directory):
        raise ToolError("path_denied", "Dashboard must stay inside the dashboard directory")
    return path


def load_dashboard(directory: Path, dashboard: str) -> Dashboard:
    path = dashboard_path(directory, dashboard)
    try:
        spec = Dashboard.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (ValidationError, yaml.YAMLError, UnicodeError) as exc:
        raise ToolError("invalid_dashboard", f"Invalid definition {path.name}: {exc}") from None
    if spec.dashboard != dashboard:
        raise ToolError("invalid_dashboard", f"Dashboard ID does not match {path.name}")
    return spec


def render_dashboard(directory: Path, dashboard: str, warehouse, observation):
    spec = load_dashboard(directory, dashboard)
    panels = {}
    tables = set()
    for panel in spec.panels:
        panel_observation = Observation()
        query = panel.sql
        if panel.row_filter:
            query = f"SELECT * FROM ({query}) AS tile WHERE {panel.row_filter}"
        panels[panel.name] = warehouse.execute(query, panel_observation, read_only=True)
        tables.update(panel_observation.read_targets)
    observation.targets = [f"dashboard:{dashboard}", *sorted(tables)]
    observation.read_targets = observation.targets
    return {"dashboard": dashboard, "title": spec.title, "panels": panels}
