"""Unit and integration tests for `tasks.factory.compiler.task_materializer`."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tasks.factory.compiler.blueprint_matrix import compile_catalog  # noqa: E402
from tasks.factory.compiler.task_materializer import (  # noqa: E402
    TaskMaterializer,
    build_world_from_task,
    discover_adapters,
    materialize,
    run_baselines_for_task,
)
from tasks.factory.narrative.discoverability_and_spoiler_linter import lint_task  # noqa: E402

TEST_OUT_BASE = Path("/home/ubuntu/alkera_factory_1017/test_materialized")


@pytest.fixture(scope="module")
def test_out_root() -> Path:
    TEST_OUT_BASE.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix=f"run_{os.getpid()}_", dir=str(TEST_OUT_BASE)))
    return run_dir


@pytest.fixture(scope="module")
def three_mode_blueprints() -> list[dict]:
    catalog = compile_catalog()
    selected: list[dict] = []
    for mode in ("act-authorized", "withhold-blocked", "escalate-ambiguous"):
        match = next(row for row in catalog if row["theme"] == "revenue_daily"
                     and row["governance_mode"] == mode)
        selected.append(match)
    return selected


@pytest.fixture(scope="module")
def materialized_three_tasks(three_mode_blueprints: list[dict], test_out_root: Path) -> list[Path]:
    engine = TaskMaterializer(
        out_tasks_dir=test_out_root / "tasks",
        prefer_peer_adapters=True,
        scratch=test_out_root / "scratch",
    )
    paths: list[Path] = []
    for bp in three_mode_blueprints:
        task_dir = engine.materialize(bp)
        paths.append(task_dir)
    return paths


def test_adapter_discovery_and_fallback_toggle() -> None:
    peer_adapters = discover_adapters(prefer_peer_adapters=True)
    assert peer_adapters["kimi"]["available"] is True
    assert peer_adapters["codex"]["available"] is True
    assert peer_adapters["grok"]["available"] is True
    assert peer_adapters["opus"]["available"] is True
    assert len(peer_adapters["available_themes"]) >= 5

    fallback_adapters = discover_adapters(prefer_peer_adapters=False)
    assert fallback_adapters["codex"]["available"] is False
    assert fallback_adapters["grok"]["available"] is False
    assert fallback_adapters["opus"]["available"] is False


def test_required_files_and_task_toml_contract(materialized_three_tasks: list[Path]) -> None:
    assert len(materialized_three_tasks) == 3
    required_rel_paths = [
        "task.toml",
        "prompt.md",
        "prompts/default.md",
        "prompts/vague.md",
        "fixtures/make_fixture_world.py",
        "fixtures/seed.yaml",
        "fixtures/blueprint.json",
        "fixtures/workflow/users.yaml",
        "fixtures/workflow/issues.yaml",
        "fixtures/workflow/kb.yaml",
        "fixtures/workflow/chat.yaml",
        "solution/solve.py",
        "solution/factory_events_oracle.py",
        "solution/factory_events_partial.py",
        "solution/wrong_precedence.py",
        "solution/duplicate_counted.py",
        "solution/boundary_violated.py",
        "tests/manifest.yaml",
        "tests/spec.json",
        "tests/governance.json",
        "tests/grade.py",
        "tests/run_baselines.py",
    ]
    for task_dir in materialized_three_tasks:
        for rel in required_rel_paths:
            fpath = task_dir / rel
            assert fpath.is_file(), f"Missing {rel} in {task_dir}"
            assert fpath.stat().st_size > 0, f"Empty {rel} in {task_dir}"

        assert (task_dir / "fixtures" / "next_nights").is_dir()

        parsed_toml = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
        assert "task" in parsed_toml
        assert "grading" in parsed_toml
        assert "Prompt" in parsed_toml
        assert "surfaces" in parsed_toml
        assert "variants" in parsed_toml and "default" in parsed_toml["variants"]
        assert "prompt_variants" in parsed_toml
        assert parsed_toml["variants"]["default"].get("overlay") is None
        assert parsed_toml["surfaces"]["dead_by_design"] == [
            "dbt",
            "deploy",
            "~ingest_alerts",
            "~ingest_list",
            "~ingest_runs",
        ]

        manifest = yaml.safe_load((task_dir / "tests" / "manifest.yaml").read_text(encoding="utf-8"))
        spec = json.loads((task_dir / "tests" / "spec.json").read_text(encoding="utf-8"))
        check_ids = [c["id"] for c in manifest["checks"]]
        for expected_id in ("C0", "R1", "R2", "R3", "R4", "D1", "D2", "S1", "H2"):
            assert expected_id in check_ids
        assert set(check_ids) == set(spec["checks"].keys())


def test_make_fixture_world_and_baselines_all_three_modes(
    materialized_three_tasks: list[Path],
    test_out_root: Path,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    expected_modes = {"act-authorized", "withhold-blocked", "escalate-ambiguous"}

    def _verify_one_task(task_dir: Path) -> str:
        bp = json.loads((task_dir / "fixtures" / "blueprint.json").read_text(encoding="utf-8"))
        mode = bp["governance_mode"]

        world_dir = test_out_root / f"world_{task_dir.name}"
        grading_dir = test_out_root / f"grading_{task_dir.name}"
        if world_dir.exists():
            shutil.rmtree(world_dir)
        if grading_dir.exists():
            shutil.rmtree(grading_dir)

        proc_build = subprocess.run(
            [
                sys.executable,
                str(task_dir / "fixtures" / "make_fixture_world.py"),
                "--root",
                str(world_dir),
                "--grading",
                str(grading_dir),
                "--seed",
                str(bp["seed"]),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc_build.returncode == 0, f"make_fixture_world.py failed: {proc_build.stderr}"
        assert (world_dir / "var" / "warehouse" / "warehouse.duckdb").is_file()
        assert (grading_dir / "manifest.yaml").is_file()
        assert (grading_dir / "spec.json").is_file()

        # Run baselines via tests/run_baselines.py and verify all 5 baselines behave as specified
        scratch_dir = test_out_root / f"baselines_{task_dir.name}"
        proc_base = subprocess.run(
            [
                sys.executable,
                str(task_dir / "tests" / "run_baselines.py"),
                "--scratch",
                str(scratch_dir),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc_base.returncode == 0, (
            f"run_baselines.py failed for {task_dir.name}:\n{proc_base.stderr}\n{proc_base.stdout}"
        )
        summary = json.loads((task_dir / "tests" / "baselines" / "default.json").read_text(encoding="utf-8"))
        baselines = summary["baselines"]
        assert baselines["oracle"]["passed"] is True
        assert baselines["oracle"]["total"] == baselines["oracle"]["max"]
        assert baselines["no_op"]["passed"] is False
        assert baselines["wrong_precedence"]["passed"] is False
        assert baselines["duplicate_counted"]["passed"] is False
        assert baselines["boundary_violated"]["passed"] is False

        assert lint_task(task_dir).ok
        return mode

    with ThreadPoolExecutor(max_workers=3) as pool:
        seen_modes = set(pool.map(_verify_one_task, materialized_three_tasks))

    assert seen_modes == expected_modes
