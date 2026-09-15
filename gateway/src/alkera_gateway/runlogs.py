"""Per-run output, tied to the scheduler's run history.

`sched_logs` hands back whatever blob a run recorded. That is enough to prove a
run happened and not enough to diagnose why it failed: there is no way to list
which runs even have output, no line numbers to cite as evidence, and no way to
search across runs for the error message an agent is chasing.

This surface reads exactly the same two sources - the scheduler's run list and
the per-run record `sched_logs` serves - and adds enumeration, numbered lines,
and search. It never synthesises log text. A run whose output was never captured
says so in `source` and carries no lines, because inventing a plausible dbt
transcript for a seeded run is precisely the failure this benchmark measures.

Sources, always reported:
  run_output      the process transcript a real execution wrote
  run_result      a structured record with no transcript (e.g. a trigger intent)
  scheduler_state the run's own `detail` field and nothing more
  none            no output was captured for this run
"""

import json

from .records import Observation, ToolError

LINE_CAP = 2000


class RunLogs:
    def __init__(self, scheduler, config):
        self.scheduler = scheduler
        self.max_bytes = config.max_payload_bytes

    def runs(self):
        state = self.scheduler.state(None)
        result = []
        for index, run in enumerate(state.get("runs", []), 1):
            result.append(
                {
                    "run_id": run.get("id") or f"run-{index:06d}",
                    "job": run.get("job") or run.get("job_id"),
                    "status": run.get("status"),
                    "started_at": run.get("vts_start") or run.get("started_at"),
                    "finished_at": run.get("vts_end") or run.get("finished_at"),
                    "detail": run.get("detail"),
                }
            )
        return result

    @staticmethod
    def render(run, result):
        """Turn a recorded run into lines, and name where they came from."""
        if isinstance(result, dict) and isinstance(result.get("output"), str):
            return result["output"], "run_output", None
        if result is not None:
            return (
                json.dumps(result, indent=2, sort_keys=True),
                "run_result",
                "This run recorded a structured result, not a process transcript.",
            )
        if run.get("detail"):
            return (
                str(run["detail"]),
                "scheduler_state",
                "Only the run's `detail` field was recorded; there is no transcript.",
            )
        return "", "none", "No output was captured for this run."

    def body(self, run):
        """Read one run's output. Returns (lines, source, note)."""
        record = self.scheduler.logs(run["run_id"])
        text, source, note = self.render(run, record.get("result"))
        if len(text) > self.max_bytes:
            raise ToolError("file_too_large", "Run output exceeds the configured read limit")
        lines = text.splitlines()
        return lines, source, note

    def list(self, job, status, limit, observation: Observation) -> dict:
        observation.targets = observation.read_targets = ["scheduler"]
        runs = [
            run
            for run in self.runs()
            if (job is None or run["job"] == job) and (status is None or run["status"] == status)
        ]
        runs.reverse()
        entries = []
        for run in runs[:limit]:
            try:
                record = self.scheduler.logs(run["run_id"])
                _, source, _ = self.render(run, record.get("result"))
            except (ToolError, OSError, ValueError) as exc:
                source = "unreadable"
                run = {**run, "read_error": f"{type(exc).__name__}: {exc}"}
            entries.append({**run, "output_source": source})
        return {"runs": entries, "truncated": len(runs) > limit}

    def get(self, run_id: str, observation: Observation) -> dict:
        observation.targets = observation.read_targets = [f"scheduler:{run_id}"]
        run = next((item for item in self.runs() if item["run_id"] == run_id), None)
        if run is None:
            raise ToolError("run_not_found", f"Unknown run: {run_id}")
        lines, source, note = self.body(run)
        return {
            **run,
            "source": source,
            "note": note,
            "lines": [{"line": n, "text": text} for n, text in enumerate(lines[:LINE_CAP], 1)],
            "truncated": len(lines) > LINE_CAP,
        }

    def search(self, query: str, job, limit: int, observation: Observation) -> dict:
        """Search run output, and account for every run it could not read.

        Same rule as docs_search: a run is either searched or named in `skipped`.
        Silently dropping the one run whose transcript holds the error deletes
        the evidence the diagnosis needs.
        """
        observation.targets = observation.read_targets = ["scheduler"]
        runs = [run for run in self.runs() if job is None or run["job"] == job]
        runs.reverse()
        matches, skipped = [], []
        for run in runs:
            try:
                lines, source, _ = self.body(run)
            except (ToolError, OSError, ValueError) as exc:
                code = exc.code if isinstance(exc, ToolError) else "read_failed"
                skipped.append({"run_id": run["run_id"], "code": code, "reason": str(exc)})
                continue
            if source == "none":
                skipped.append(
                    {
                        "run_id": run["run_id"],
                        "code": "no_output",
                        "reason": "No output was captured for this run.",
                    }
                )
                continue
            for number, text in enumerate(lines, 1):
                if query.casefold() in text.casefold():
                    matches.append(
                        {
                            "run_id": run["run_id"],
                            "job": run["job"],
                            "source": source,
                            "line": number,
                            "text": text,
                        }
                    )
                    if len(matches) == limit:
                        return {
                            "matches": matches,
                            "limit_reached": True,
                            "skipped": skipped,
                        }
        return {"matches": matches, "limit_reached": False, "skipped": skipped}
