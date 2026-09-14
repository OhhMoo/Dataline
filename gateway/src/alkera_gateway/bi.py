"""Dashboard definitions, metrics, cached tiles and virtual refresh history."""

import json

import yaml

from .records import Observation, ToolError, canonical, digest
from .surfaces import Dashboard


class BIStore:
    def __init__(self, config, warehouse, clock):
        self.definitions = config.dashboards
        self.state = config.bi_state_dir
        self.warehouse = warehouse
        self.clock = clock

    def path(self, dashboard, directory, suffix):
        if not dashboard or any(
            c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in dashboard
        ):
            raise ToolError("invalid_dashboard", "Use a dashboard ID containing a-z, 0-9, _ or -")
        path = (directory / f"{dashboard}.{suffix}").resolve()
        if not path.is_relative_to(directory):
            raise ToolError("path_denied", "Dashboard path escapes its directory")
        return path

    def get(self, dashboard, observation):
        path = self.path(dashboard, self.definitions, "yml")
        spec = Dashboard.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        if spec.dashboard != dashboard:
            raise ToolError("invalid_dashboard", "Dashboard ID does not match its filename")
        observation.targets = observation.read_targets = [f"dashboard:{dashboard}"]
        return spec.model_dump()

    def list(self, observation):
        result = {
            "dashboards": [
                self.get(path.stem, observation) for path in sorted(self.definitions.glob("*.yml"))
            ]
        }
        observation.targets = observation.read_targets = [
            f"dashboard:{spec['dashboard']}" for spec in result["dashboards"]
        ]
        return result

    def update(self, dashboard, definition, observation):
        spec = Dashboard.model_validate(definition)
        if spec.dashboard != dashboard:
            raise ToolError("invalid_dashboard", "Dashboard ID does not match the target")
        path = self.path(dashboard, self.definitions, "yml")
        self.definitions.mkdir(parents=True, exist_ok=True)
        observation.op_class = "write"
        observation.targets = [f"dashboard:{dashboard}"]
        if path.exists():
            observation.flags.append("overwrite")
        path.write_text(yaml.safe_dump(spec.model_dump(), sort_keys=True), encoding="utf-8")
        return {"dashboard": dashboard, "definition_hash": digest(spec.model_dump())}

    def compute(self, spec, observation):
        panels, targets = {}, set()
        for panel in spec["panels"]:
            query = panel["sql"]
            if panel["row_filter"]:
                query = f"SELECT * FROM ({query}) AS tile WHERE {panel['row_filter']}"
            current = Observation()
            panels[panel["name"]] = self.warehouse.execute(query, current, read_only=True)
            targets.update(current.read_targets)
        observation.read_targets = sorted(set(observation.read_targets) | targets)
        return panels

    def cached(self, dashboard, observation):
        self.get(dashboard, observation)
        path = self.path(dashboard, self.state, "json")
        if not path.exists():
            raise ToolError("not_refreshed", "Dashboard has no cached refresh")
        return json.loads(path.read_text(encoding="utf-8"))

    def refresh(self, dashboard, observation):
        spec = self.get(dashboard, observation)
        panels = self.compute(spec, observation)
        watermarks = {}
        for panel in spec["panels"]:
            if panel["watermark_sql"]:
                current = Observation()
                watermarks[panel["name"]] = self.warehouse.execute(
                    panel["watermark_sql"], current, read_only=True
                )
                observation.read_targets = sorted(
                    set(observation.read_targets + current.read_targets)
                )
        path = self.path(dashboard, self.state, "json")
        previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        events = previous.get("refresh_events", [])
        event = {
            "id": len(events) + 1,
            "ran_at": self.clock(),
            "source_watermarks": watermarks,
            "definition_hash": digest(spec),
        }
        result = {
            "dashboard": dashboard,
            "panels": panels,
            "last_refreshed_at": self.clock(),
            "refresh_events": [*events, event],
        }
        self.state.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(canonical(result))
        temporary.replace(path)
        observation.op_class = "write"
        observation.targets = [f"dashboard:{dashboard}"]
        return result
