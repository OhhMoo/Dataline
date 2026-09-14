"""Task package -> harness spec. Task-agnostic: only key names are known here.

Everything task-specific (how the world is built, what argv the grader takes,
which checks are load-bearing) is declared in the task's own task.toml under
`[harness]`. If a task does not declare it, the harness refuses to run rather
than inventing a default - an invented pass threshold would silently decide the
Pass@1 number.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

TASKS_DIR = Path(__file__).resolve().parent.parent


class TaskSpecError(Exception):
    pass


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    root: Path
    raw: dict

    instruction: str
    seed: int
    virtual_time: str

    wall_clock_seconds: int
    max_tool_calls: int
    memory_mb: int
    disk_mb: int

    artifact_in_world: str          # e.g. "/workspace/final_report.json"
    grade_entrypoint: Path
    grade_argv: list[str]
    world_builder: Path
    surface_probes: list[dict] = field(default_factory=list)

    pass_requires_checks: list[str] = field(default_factory=list)
    pass_min_total: float = 0.0

    def scores_pass(self, score: dict) -> bool:
        """Did this rollout solve the task? Criterion comes from the task."""
        by_id = {c["id"]: c for c in score.get("checks", [])}
        for cid in self.pass_requires_checks:
            if cid not in by_id or not by_id[cid].get("passed"):
                return False
        return float(score.get("total", 0.0)) >= self.pass_min_total

    def artifact_host_path(self, world: dict) -> Path:
        """Map the in-world artifact path onto the host world tree.

        §9.2.1 puts the agent's tree at /workspace, so an artifact declared at
        /workspace/x lands at <workspace>/x. Anything else is resolved against
        the world root.
        """
        p = self.artifact_in_world
        if p.startswith("/workspace"):
            return Path(world["workspace"]) / p[len("/workspace"):].lstrip("/")
        return Path(world["world_root"]) / p.lstrip("/")


def _require(d: dict, key: str, where: str):
    if key not in d:
        raise TaskSpecError(f"{where} is missing required key '{key}'")
    return d[key]


def load(task_id: str, tasks_dir: Path | None = None) -> TaskSpec:
    root = (tasks_dir or TASKS_DIR) / task_id
    toml_path = root / "task.toml"
    if not toml_path.exists():
        raise TaskSpecError(f"no task.toml at {toml_path}")
    raw = tomllib.loads(toml_path.read_text(encoding="utf-8"))

    harness = raw.get("harness")
    if harness is None:
        raise TaskSpecError(
            f"{task_id}/task.toml has no [harness] section. The harness will not "
            "guess a world builder, a grader argv or a pass threshold - each of "
            "those decides the measured number and belongs to the task.")

    env = raw.get("environment", {})
    limits = raw.get("limits", {})
    grading = raw.get("grading", {})

    instruction_name = raw.get("agent_visible", {}).get("brief", "instruction.md")
    instruction_path = root / instruction_name
    if not instruction_path.exists():
        raise TaskSpecError(f"agent_visible brief not found: {instruction_path}")

    requires = _require(harness, "pass_requires_checks", f"{task_id} [harness]")
    if not requires:
        raise TaskSpecError(f"{task_id} [harness].pass_requires_checks is empty")

    return TaskSpec(
        task_id=task_id,
        root=root,
        raw=raw,
        instruction=instruction_path.read_text(encoding="utf-8"),
        seed=int(env.get("seed", 0)),
        virtual_time=str(env.get("virtual_now", "")),
        wall_clock_seconds=int(_require(limits, "wall_clock_seconds", f"{task_id} [limits]")),
        max_tool_calls=int(_require(limits, "max_tool_calls", f"{task_id} [limits]")),
        memory_mb=int(limits.get("memory_mb", 0)),
        disk_mb=int(limits.get("disk_mb", 0)),
        artifact_in_world=str(_require(grading, "artifact", f"{task_id} [grading]")),
        grade_entrypoint=root / _require(grading, "entrypoint", f"{task_id} [grading]"),
        grade_argv=list(_require(harness, "grade_argv", f"{task_id} [harness]")),
        world_builder=root / _require(harness, "world_builder", f"{task_id} [harness]"),
        surface_probes=list(harness.get("surface_probes", [])),
        pass_requires_checks=list(requires),
        pass_min_total=float(_require(harness, "pass_min_total", f"{task_id} [harness]")),
    )
