import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .records import Observation, ToolError, canonical


class Scheduler(Protocol):
    def list(self) -> dict: ...
    def trigger(self, job_id: str, execution_date: str, variables: dict) -> dict: ...
    def state(self, run_id: str | None) -> dict: ...
    def logs(self, run_id: str | None) -> dict: ...


class FileScheduler:
    """Adapter for Track A's deterministic scheduler state."""

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
        try:
            command = job.get("command")
            if command in {"dbt_build", "dbt_run"} or job.get("selector"):
                dbt_command = "build" if command == "dbt_build" else "run"
                result = self.commands.dbt(
                    dbt_command,
                    job.get("selector", "*"),
                    {**variables, "execution_date": execution_date},
                    False,
                    Observation(),
                )
                run["status"] = "success" if result["returncode"] == 0 else "failed"
            else:
                result = {"intent": command, "variables": variables}
                run["status"] = "success"
        except Exception as exc:
            run["status"] = "failed"
            result = {"error": str(exc)}
            raise
        finally:
            duration = int(job.get("duration_s", 0))
            finished = datetime.fromisoformat(vnow.replace("Z", "+00:00")) + timedelta(
                seconds=duration
            )
            run["finished_at"] = finished.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            if "vnow" in state:
                state["vnow"] = run["finished_at"]
            self.save(state)
            self.save_log(run["id"], result)
        return run

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


def render_dashboard(directory: Path, dashboard: str, warehouse, observation):
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789_-"
    if not dashboard or any(char not in allowed for char in dashboard):
        raise ToolError("invalid_dashboard", "Use a dashboard ID containing a-z, 0-9, _ or -")
    path = (directory / f"{dashboard}.yml").resolve()
    if not path.is_relative_to(directory):
        raise ToolError("path_denied", "Dashboard must stay inside the dashboard directory")
    spec = Dashboard.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    if spec.dashboard != dashboard:
        raise ToolError("invalid_dashboard", "Dashboard ID does not match its filename")
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
