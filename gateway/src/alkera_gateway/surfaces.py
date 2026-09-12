import json
from datetime import datetime
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .records import Observation, ToolError, canonical


class Scheduler(Protocol):
    def list(self) -> dict: ...
    def trigger(self, job_id: str, execution_date: str, variables: dict) -> dict: ...
    def state(self, run_id: str) -> dict: ...
    def logs(self, run_id: str) -> dict: ...


class FileScheduler:
    """Synchronous dbt jobs; Track A may inject its own scheduler."""

    def __init__(self, path: Path, commands, clock):
        self.path = path
        self.commands = commands
        self.clock = clock

    def load(self):
        state = json.loads(self.path.read_text(encoding="utf-8"))
        if state.get("version") != 1:
            raise ToolError("invalid_scheduler", "Expected scheduler state version 1")
        return state

    def list(self):
        return {"jobs": self.load()["jobs"]}

    def trigger(self, job_id: str, execution_date: str, variables: dict):
        try:
            datetime.fromisoformat(execution_date)
        except ValueError as exc:
            raise ToolError("invalid_date", "execution_date must be an ISO date or timestamp") from exc
        state = self.load()
        job = next((item for item in state["jobs"] if item["id"] == job_id), None)
        if job is None:
            raise ToolError("job_not_found", f"Unknown job: {job_id}")
        run = {
            "id": f"run-{len(state['runs']) + 1:06d}", "job_id": job_id,
            "execution_date": execution_date, "started_at": self.clock(), "status": "running",
        }
        state["runs"].append(run)
        self.save(state)
        try:
            result = self.commands.dbt(
                "run", job["selector"], {**variables, "execution_date": execution_date}, False,
                Observation(),
            )
            run["status"] = "success" if result["returncode"] == 0 else "failed"
            run["result"] = result
        except Exception as exc:
            run["status"] = "failed"
            run["result"] = {"error": str(exc)}
            raise
        finally:
            run["finished_at"] = self.clock()
            self.save(state)
        return {key: value for key, value in run.items() if key != "result"}

    def save(self, state):
        pending = self.path.with_suffix(".pending")
        pending.write_bytes(canonical(state))
        pending.replace(self.path)

    def find(self, run_id):
        for run in self.load()["runs"]:
            if run["id"] == run_id:
                return run
        raise ToolError("run_not_found", f"Unknown run: {run_id}")

    def state(self, run_id):
        return {key: value for key, value in self.find(run_id).items() if key != "result"}

    def logs(self, run_id):
        return {"run_id": run_id, "result": self.find(run_id).get("result")}


class Dashboard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(default=1, ge=1, le=1)
    title: str
    sql: str


def render_dashboard(directory: Path, dashboard: str, warehouse, observation):
    if not dashboard or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for char in dashboard):
        raise ToolError("invalid_dashboard", "Use a dashboard ID containing a-z, 0-9, _ or -")
    path = (directory / f"{dashboard}.yml").resolve()
    if not path.is_relative_to(directory):
        raise ToolError("path_denied", "Dashboard must stay inside the dashboard directory")
    spec = Dashboard.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    result = warehouse.execute(spec.sql, observation, read_only=True)
    return {"dashboard": dashboard, "title": spec.title, **result}
