"""An executing scheduler shaped to the gateway's `Scheduler` protocol.

The gateway declares `Scheduler` as a structural Protocol and takes one by
injection - `Gateway(config, scheduler=...)`, `Operations(config, clock,
scheduler)` - so a world can supply its own without the gateway importing
anything from here and without this module importing anything from there. The
dependency arrow (agent.md 10.2) stays pointed one way: there is deliberately
no `import alkera_gateway` in this file, and there never should be.

Why it exists: the gateway's built-in `FileScheduler` executes dbt jobs but
reports plain `"success"` for every other command - a load, an extract, a
vendor poll - without running anything. Injecting `WorldScheduler` routes
`sched_trigger` through `world.scheduler.trigger`, which runs the job's real
declared work and derives the status from it.

The four frozen tools keep their response shape:
  sched_list    {"jobs": [{"id", ...}]}
  sched_trigger {"id", "job_id", "execution_date", "started_at",
                 "finished_at", "status", ...}   (+ error / executed / effects)
  sched_state   whole state, or one run
  sched_logs    {"runs": [{"id", "job", "status", "detail"}]} or
                {"run_id", "result"}

Errors are raised as ValueError, which the gateway surfaces as a normal tool
error; raising a gateway exception type would require importing one.
"""
from datetime import datetime
from pathlib import Path

from . import scheduler


def _run_id(index: int) -> str:
    return f"run-{index:06d}"


class WorldScheduler:
    def __init__(self, root: Path):
        self.root = Path(root)

    # -- reads ------------------------------------------------------------
    def _state(self) -> dict:
        state = scheduler.load(self.root)
        if not isinstance(state.get("jobs"), dict) or not isinstance(
                state.get("runs"), list):
            raise ValueError("Scheduler state needs jobs and runs")
        return state

    def list(self) -> dict:
        jobs = self._state()["jobs"]
        return {"jobs": [{"id": name, **spec} for name, spec in sorted(jobs.items())]}

    def state(self, run_id: str | None) -> dict:
        state = self._state()
        state["runs"] = [{"id": _run_id(i), **run}
                         for i, run in enumerate(state["runs"], 1)]
        if run_id is None:
            return state
        for run in state["runs"]:
            if run["id"] == run_id:
                return run
        raise ValueError(f"Unknown run: {run_id}")

    def logs(self, run_id: str | None) -> dict:
        if run_id is None:
            return {"runs": [{"id": run["id"], "job": run.get("job"),
                              "status": run["status"], "detail": run.get("detail")}
                             for run in self.state(None)["runs"]]}
        run = self.state(run_id)
        return {"run_id": run_id, "result": {
            "job": run.get("job"), "status": run["status"],
            "action": run.get("action"), "detail": run.get("detail"),
            "executed": run.get("executed"), "error": run.get("error"),
            "effects": run.get("effects")}}

    # -- the write --------------------------------------------------------
    def trigger(self, job_id: str, execution_date: str, variables: dict) -> dict:
        try:
            datetime.fromisoformat(str(execution_date).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                "execution_date must be an ISO date or timestamp") from exc
        state = self._state()
        if job_id not in state["jobs"]:
            raise ValueError(f"Unknown job: {job_id}")
        run = scheduler.trigger(self.root, job_id, variables or None,
                                execution_date)
        return {"id": _run_id(len(state["runs"]) + 1),
                "job_id": job_id,
                "execution_date": execution_date,
                "started_at": run["vts_start"],
                "finished_at": run["vts_end"],
                # `returncode` is the gateway's own convention for "the thing
                # you asked me to run failed" (core.py checks it, as it does
                # for dbt_run). It makes a failed pipeline run visible in the
                # action log's `exit` column instead of only inside the result
                # blob - which is what a grader asking "did the fix work?"
                # has to read. See structure/rounds/R003/LIVE-WORLD.md: this is the one
                # envelope-level difference from FileScheduler.
                "returncode": 0 if run["status"] == "success" else 1,
                **run}
