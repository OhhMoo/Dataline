"""Ingestion connector control plane (see INGEST_CONTRACT.md).

State lives in var/ingest/state.json beside the scheduler state; every
timestamp is a virtual-clock value. A run's stored status is its terminal
status; reads report "running" while vnow is still before finished_at, so the
state machine advances exactly when the world's clock does. The seeded `sync`
template on each connector is mock plumbing and is stripped from all output.
"""
import json
from datetime import UTC, datetime, timedelta

from .records import ToolError, canonical


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class IngestStore:
    def __init__(self, path, clock):
        self.path = path
        self.clock = clock

    def load(self):
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ToolError(
                "no_ingest_state", "This world has no ingestion control plane"
            ) from None
        if not isinstance(state.get("connectors"), dict) or not all(
            isinstance(state.get(key), list) for key in ("runs", "schemas", "alerts")
        ):
            raise ToolError(
                "invalid_ingest", "Ingest state needs connectors, runs, schemas and alerts"
            )
        return state

    def save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.path.with_suffix(".pending")
        pending.write_bytes(canonical(state) + b"\n")
        pending.replace(self.path)

    def connector(self, state, connector_id):
        spec = state["connectors"].get(connector_id)
        if spec is None:
            raise ToolError("connector_not_found", f"Unknown connector: {connector_id}")
        return {"id": connector_id, **spec}

    def public(self, connector):
        return {key: value for key, value in connector.items() if key != "sync"}

    def effective(self, run):
        vnow = self.clock()
        status = run["status"] if run["finished_at"] <= vnow else "running"
        timeline = [event for event in run["timeline"] if event["at"] <= vnow]
        return {**run, "status": status, "timeline": timeline}

    def last_run(self, state, connector_id):
        runs = [run for run in state["runs"] if run["connector_id"] == connector_id]
        if not runs:
            return None
        run = self.effective(runs[-1])
        return {"id": run["id"], "status": run["status"], "finished_at": run["finished_at"]}

    def list_connectors(self):
        state = self.load()
        return {
            "connectors": [
                {**self.public(self.connector(state, cid)), "last_run": self.last_run(state, cid)}
                for cid in sorted(state["connectors"])
            ]
        }

    def get_connector(self, connector_id):
        state = self.load()
        connector = self.connector(state, connector_id)
        return {**self.public(connector), "last_run": self.last_run(state, connector_id)}

    def list_runs(self, connector_id, limit, continuation_token):
        state = self.load()
        if connector_id is not None:
            self.connector(state, connector_id)
        try:
            offset = int(continuation_token) if continuation_token else 0
        except ValueError:
            raise ToolError(
                "invalid_token", "continuation_token must come from ingest_runs"
            ) from None
        runs = [run for run in state["runs"]
                if connector_id is None or run["connector_id"] == connector_id]
        runs.sort(key=lambda run: (run["started_at"], run["id"]), reverse=True)
        page = runs[offset:offset + limit]
        summaries = [
            {key: self.effective(run)[key]
             for key in ("id", "connector_id", "started_at", "finished_at", "status")}
            for run in page
        ]
        next_offset = offset + limit
        return {
            "runs": summaries,
            "next_token": str(next_offset) if next_offset < len(runs) else None,
        }

    def get_run(self, run_id):
        for run in self.load()["runs"]:
            if run["id"] == run_id:
                return self.effective(run)
        raise ToolError("run_not_found", f"Unknown sync run: {run_id}")

    def get_schema(self, connector_id):
        state = self.load()
        self.connector(state, connector_id)
        return {
            "connector_id": connector_id,
            "tables": [entry for entry in state["schemas"]
                       if entry["connector_id"] == connector_id],
        }

    def list_alerts(self, connector_id, acknowledged):
        alerts = self.load()["alerts"]
        if connector_id is not None:
            alerts = [a for a in alerts if a["connector_id"] == connector_id]
        if acknowledged is not None:
            alerts = [a for a in alerts if a["acknowledged"] == acknowledged]
        return {"alerts": alerts}

    def trigger(self, connector_id):
        state = self.load()
        connector = self.connector(state, connector_id)
        if connector["status"] == "paused":
            raise ToolError("connector_paused", f"Connector is paused: {connector_id}")
        started = _parse(self.clock())
        finished = started + timedelta(seconds=connector["sync"]["duration_s"])
        run = {
            "id": f"sync-{len(state['runs']) + 1:06d}",
            "connector_id": connector_id,
            "started_at": _iso(started),
            "finished_at": _iso(finished),
            "status": "success",
            "tables": connector["sync"]["tables"],
            "timeline": [
                {"at": _iso(started), "event": "started"},
                {"at": _iso(finished), "event": "succeeded"},
            ],
        }
        state["runs"].append(run)
        self.save(state)
        return self.effective(run)

    def set_policy(self, connector_id, table, column_added, column_removed):
        if column_added is None and column_removed is None:
            raise ToolError("invalid_policy", "Name column_added and/or column_removed")
        state = self.load()
        self.connector(state, connector_id)
        for entry in state["schemas"]:
            if entry["connector_id"] == connector_id and entry["table"] == table:
                if column_added is not None:
                    entry["column_added"] = column_added
                if column_removed is not None:
                    entry["column_removed"] = column_removed
                self.save(state)
                return entry
        raise ToolError("table_not_found", f"Connector {connector_id} has no table: {table}")
