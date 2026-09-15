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
    instruction_path: Path
    surface_probes: list[dict] = field(default_factory=list)

    # Which surfaces this task's world MUST have. A rollout that starts with one
    # of these dead is a harness error, not a score - see surface_audit.py. The
    # harness does not invent this list: a task that declares none simply cannot
    # fail the gate, and the report names that as a run condition rather than
    # letting it read as "all surfaces fine".
    required_surfaces: list[str] = field(default_factory=list)
    # Surfaces that are dead ON PURPOSE in this world, each with a reason, so a
    # deliberately minimal world is not flagged forever.
    dead_by_design: list[dict] = field(default_factory=list)
    # name -> sha256 of the prompt file, from [prompt_variants]. A run must be
    # able to name which ticket produced it; see report.py's run conditions.
    prompt_variants: dict[str, str] = field(default_factory=dict)
    prompt_variant_default: str = ""

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


def _dead_by_design(task_id: str, harness: dict) -> list[dict]:
    """`dead_by_design = [{ label = "chat", reason = "..." }, ...]`.

    The reason is mandatory. "Dead on purpose" with no purpose written down is
    just the flag being switched off, which is the thing this whole module
    exists to stop.
    """
    out = []
    for i, entry in enumerate(harness.get("dead_by_design", []) or []):
        if not isinstance(entry, dict) or not entry.get("label"):
            raise TaskSpecError(
                f"{task_id} [harness].dead_by_design[{i}] must be a table with "
                f"a `label`, got {entry!r}")
        if not str(entry.get("reason", "")).strip():
            raise TaskSpecError(
                f"{task_id} [harness].dead_by_design[{i}] ({entry['label']}) has "
                f"no `reason`. A surface declared dead on purpose without the "
                f"purpose written down is an unexplained hole in the world the "
                f"agent is measured in.")
        out.append({"label": str(entry["label"]), "reason": str(entry["reason"])})
    return out


def _check_surface_declarations(task_id: str, probes: list[dict],
                                required: list[str],
                                dead_by_design: list[dict]) -> None:
    """A gate that cannot be evaluated is worse than no gate, so refuse early."""
    labels = {p.get("label") or p["tool"] for p in probes}
    tools = {p["tool"] for p in probes} | {c for p in probes
                                           for c in (p.get("covers") or [])}
    unknown = [r for r in required if r not in labels and r not in tools]
    if unknown:
        raise TaskSpecError(
            f"{task_id} [harness].required_surfaces names {unknown}, which no "
            f"surface_probe declares as a label, a tool or a `covers` entry. A "
            f"required surface nobody probes cannot be verified live, so the "
            f"gate would silently pass. Add the probe.")
    designed = {d["label"] for d in dead_by_design}
    contradiction = sorted(designed & set(required))
    if contradiction:
        raise TaskSpecError(
            f"{task_id} [harness] declares {contradiction} both required and "
            f"dead_by_design. One of the two is wrong and the harness will not "
            f"pick.")


def prompt_variant_of(spec: "TaskSpec") -> str:
    """Which declared prompt variant is the brief this rollout will ship?

    `prompts/variant.py identify` does this for a finished run directory; a run
    must be able to name which ticket produced it, and so must every rate in the
    report. Reading it here means the answer is IN the rollout record rather
    than re-derived later from a prompt file that may since have moved.
    """
    import hashlib
    if not spec.prompt_variants:
        return "n/a (task declares no prompt variants)"
    digest = hashlib.sha256(spec.instruction_path.read_bytes()).hexdigest()
    for name, sha in sorted(spec.prompt_variants.items()):
        if sha == digest:
            return name
    return (f"UNLOCKED (brief sha256 {digest[:12]} matches none of "
            f"{sorted(spec.prompt_variants)})")


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

    probes = list(harness.get("surface_probes", []))
    required_surfaces = list(harness.get("required_surfaces", []))
    dead_by_design = _dead_by_design(task_id, harness)
    _check_surface_declarations(task_id, probes, required_surfaces, dead_by_design)

    variants = raw.get("prompt_variants", {}) or {}
    prompt_variants = {name: str(v["sha256"])
                       for name, v in variants.items()
                       if isinstance(v, dict) and v.get("sha256")}

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
        instruction_path=instruction_path,
        surface_probes=probes,
        required_surfaces=required_surfaces,
        dead_by_design=dead_by_design,
        prompt_variants=prompt_variants,
        prompt_variant_default=str(variants.get("default", "")),
        pass_requires_checks=list(requires),
        pass_min_total=float(_require(harness, "pass_min_total", f"{task_id} [harness]")),
    )
