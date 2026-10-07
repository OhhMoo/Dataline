"""The loader half of environment profiles (gateway/src/alkera_gateway/plugs.py).

A task.toml may name the stack it was authored for with
`[environment].profile = "<role>/<stack>"` and adjust it with an
`[environment.plugs]` table (slot -> plug list or plug name; an empty list
removes the slot). The loader resolves the profile - planned plugs allowed,
since a task may be authored ahead of its tools - and refuses a task whose
descriptive `[environment].surfaces` names a label the profile does not
provide. A task without `profile` loads exactly as before.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import taskspec

from alkera_gateway.plugs import resolve

TASKS = Path(__file__).resolve().parent.parent
SHIPPED = sorted(p.parent.name for p in TASKS.glob("GT*/task.toml"))

TAIL = """
[limits]
wall_clock_seconds = 60
max_tool_calls = 10

[grading]
artifact = "/workspace/final_report.json"
entrypoint = "tests/grade.py"

[harness]
world_builder = "tests/build_world.py"
grade_argv = ["--json"]
pass_requires_checks = ["G1"]
pass_min_total = 1.0
"""


def write_task(tmp_path: Path, environment: str, name: str = "GT000-profile") -> str:
    root = tmp_path / name
    root.mkdir(parents=True)
    (root / "instruction.md").write_text("do the thing\n", encoding="utf-8")
    head = ('[environment]\nseed = 1\nvirtual_now = "2026-01-01T00:00:00Z"\n'
            + environment)
    (root / "task.toml").write_text(head + TAIL, encoding="utf-8", newline="")
    return name


def test_no_profile_loads_as_before(tmp_path):
    name = write_task(tmp_path, 'surfaces = ["duckdb", "anything_at_all"]\n')
    spec = taskspec.load(name, tmp_path)
    assert spec.profile == ""
    assert spec.plugs == {}
    assert spec.profile_planned == []


def test_profile_with_planned_plugs_loads(tmp_path):
    # ds/aws: ml_workers and mlflow are planned (redshift, its warehouse, is
    # partial since the Postgres persona)
    name = write_task(tmp_path, 'profile = "ds/aws"\n'
                                'surfaces = ["redshift", "dbt", "git"]\n')
    spec = taskspec.load(name, tmp_path)
    assert spec.profile == "ds/aws"
    assert spec.plugs == {}
    assert spec.profile_planned == ["ml_workers", "mlflow"]
    # de/aws plans nothing any more
    name = write_task(tmp_path, 'profile = "de/aws"\n'
                                'surfaces = ["redshift", "dbt", "git"]\n', name="GT000-aws")
    assert taskspec.load(name, tmp_path).profile_planned == []


def test_surface_the_profile_lacks_is_refused(tmp_path):
    name = write_task(tmp_path, 'profile = "da/laptop"\n'
                                'surfaces = ["duckdb", "scheduler"]\n'
                                '[environment.plugs]\nnotebook = []\n')
    with pytest.raises(taskspec.TaskSpecError, match="scheduler") as exc:
        taskspec.load(name, tmp_path)
    assert "da/laptop" in str(exc.value)


def test_plugs_are_normalised_to_lists(tmp_path):
    name = write_task(tmp_path, 'profile = "de/laptop"\n'
                                'surfaces = ["postgres"]\n'
                                '[environment.plugs]\nwarehouse = "postgres"\n'
                                'bi = []\n')
    spec = taskspec.load(name, tmp_path)
    assert spec.plugs == {"warehouse": ["postgres"], "bi": []}


def test_plugs_without_profile_is_refused(tmp_path):
    name = write_task(tmp_path, '[environment.plugs]\nnotebook = []\n')
    with pytest.raises(taskspec.TaskSpecError, match="profile"):
        taskspec.load(name, tmp_path)


def test_unknown_profile_is_refused_naming_the_task(tmp_path):
    name = write_task(tmp_path, 'profile = "zz/laptop"\n')
    with pytest.raises(taskspec.TaskSpecError, match="GT000-profile"):
        taskspec.load(name, tmp_path)


# -- the shipped tasks ---------------------------------------------------------

def test_there_are_shipped_tasks():
    assert len(SHIPPED) >= 9


@pytest.mark.parametrize("task_id", SHIPPED)
def test_shipped_task_surfaces_fit_de_laptop(task_id):
    raw = tomllib.loads((TASKS / task_id / "task.toml").read_text(encoding="utf-8"))
    surfaces = set(raw.get("environment", {}).get("surfaces", []))
    assert surfaces <= resolve("de/laptop").surfaces, task_id


@pytest.mark.parametrize("task_id", SHIPPED)
def test_shipped_task_loads_without_a_profile(task_id):
    spec = taskspec.load(task_id)
    assert spec.profile == ""
    assert spec.plugs == {}
