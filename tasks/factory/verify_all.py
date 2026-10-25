#!/usr/bin/env python3
"""Multiprocess mechanical gate for a directory of materialized Alkera tasks.

Gates, per task directory:

1. Discoverability and zero-spoiler lint
   (``tasks.factory.narrative.discoverability_and_spoiler_linter.lint_task``).
2. Sanitization. Use a staged world or the task's declared builder, then check
   that ``sanitize_world_files`` leaves no ``authoring`` leak and that a second
   pass makes no change.
3. Baseline ladder, when ``solution/`` and ``tests/`` exist. Grade staged
   worlds or run the task's packaged baseline builder. A missing or failed arm
   blocks the gate.

Exit status is 0 only when every non-skipped gate passes.
``--phase2`` also checks the original shipped workspace, the upgraded
governance/hazard linter evidence, and the oracle plus five rival baselines.

    python -m tasks.factory.verify_all --tasks-dir DIR --workers 16 --json OUT
    python tasks/factory/verify_all.py --tasks-dir DIR --workers 16 --json OUT
"""
from __future__ import annotations

import argparse
import ast
import concurrent.futures
import importlib
import json
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

if __package__ in (None, "") and __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tasks.factory.narrative.discoverability_and_spoiler_linter import lint_task

ORACLE_OUTCOMES = frozenset({"resolved", "withheld_with_evidence"})
FUTURE_NIGHTS = ("R3", "R4")
WITHHOLD_MODES = frozenset({"withhold-blocked", "escalate-ambiguous"})
WORLD_PARENTS = (
    "worlds",
    "baselines",
    "baselines/worlds",
    "fixtures/worlds",
    "tests/worlds",
    "tests/baselines",
    "tests/baselines/worlds",
    "staged",
    "staged/worlds",
)
DOC_SUFFIXES = {".json", ".yaml", ".yml"}
_PASSING = frozenset({"pass", "skipped"})
REPO = Path(__file__).resolve().parents[2]
_REUSE_STORED_BASELINES = False
PHASE2_BASELINES = ("oracle", "no_op", "partial_fix", "wrong_precedence",
                    "duplicate_counted", "boundary_violated")
_PHASE2_TEXT_SUFFIXES = frozenset({".py", ".sql", ".md", ".yaml", ".yml",
                                   ".json", ".toml", ".sh", ".txt", ".log", ".csv"})
_PHASE2_PRIVATE = re.compile(r"(?<!_)(?<!factory)_future|authoring", re.IGNORECASE)
_PHASE2_ORACLE = re.compile(
    r"(?:^|_)(?:oracle|solution|fixed|corrected|fix|fixes|repair|patched)(?:$|_)",
    re.IGNORECASE)
_PHASE2_FIX_FLAGS = frozenset({"dedup_events", "fold_all", "keep_void_as_zero",
                               "unconditional_rewrite", "null_version_default",
                               "strict_headers", "incremental_fold_only",
                               "boundary_on_dirty_only"})


def _builder_environment() -> dict[str, str]:
    env = dict(os.environ)
    roots = (REPO, REPO / "world", REPO / "tasks/harness", REPO / "gateway/src")
    env["PYTHONPATH"] = os.pathsep.join([*(str(path) for path in roots),
                                          env.get("PYTHONPATH", "")])
    return env


def _is_built_world(path: Path) -> bool:
    if not path.is_dir():
        return False
    markers = (
        path / "var" / "warehouse" / "warehouse.duckdb",
        path / "var" / "sched" / "state.json",
        path / "var" / "workflow",
        path / "var" / "world" / "profile.json",
    )
    return any(marker.exists() for marker in markers)


def _staged_worlds(task: Path) -> dict[str, Path]:
    from tasks.factory.grader.baseline_runner import BASELINES

    found: dict[str, Path] = {}
    for relative in WORLD_PARENTS:
        parent = task / relative
        if not parent.is_dir():
            continue
        for name in BASELINES:
            candidate = parent / name
            if name not in found and _is_built_world(candidate) and (
                    candidate / "var" / "warehouse" / "warehouse.duckdb").is_file():
                found[name] = candidate
    return found


def _built_world(task: Path) -> Path | None:
    staged = _staged_worlds(task)
    if "oracle" in staged:
        return staged["oracle"]
    if staged:
        return next(iter(staged.values()))
    for candidate in (task / "world", task / "fixtures" / "world", task):
        if _is_built_world(candidate):
            return candidate
    return None


def _grading_dir(task: Path) -> Path | None:
    for candidate in (task / "tests", task / "grading", task / "tests" / "grading"):
        if (candidate / "manifest.yaml").is_file() and (candidate / "spec.json").is_file():
            return candidate
    return None


def _variant(grading: Path) -> str:
    path = grading / "variant.json"
    if not path.is_file():
        return "default"
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get("variant")
    except (OSError, json.JSONDecodeError):
        return "default"
    return str(value or "default")


def _governance(task: Path) -> dict:
    path = task / "tests" / "governance.json"
    if not path.is_file():
        blueprint = task / "fixtures" / "blueprint.json"
        path = blueprint if blueprint.is_file() else path
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    if "mode" not in data and data.get("governance_mode"):
        data = dict(data)
        data["mode"] = data["governance_mode"]
    return data


def _artifact(world: Path) -> Path | None:
    for relative in ("final_report.json", "workspace/final_report.json"):
        candidate = world / relative
        if candidate.is_file():
            return candidate
    return None


def _single_worlds_dir(found: dict[str, Path], baselines: tuple[str, ...]) -> Path | None:
    if set(found) != set(baselines):
        return None
    parents = {path.parent.resolve() for path in found.values()}
    if len(parents) != 1:
        return None
    return next(iter(parents))


def _load_outcome(task: Path):
    path = task / "task.toml"
    if not path.is_file():
        return None, None, None
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return None, None, f"task.toml: {exc}"
    matrix = document.get("matrix") if isinstance(document, dict) else None
    if not isinstance(matrix, dict):
        return None, None, None
    role = matrix.get("role") if isinstance(matrix.get("role"), str) else None
    raw = matrix.get("outcome")
    if raw is None:
        return None, role, None
    try:
        from tasks.harness.outcome import parse_outcome_spec
        return parse_outcome_spec(raw), role, None
    except Exception as exc:
        return None, role, f"outcome spec: {exc}"


def _check_map(result: dict) -> dict:
    checks = result.get("checks") or {}
    if isinstance(checks, list):
        return {row["id"]: row for row in checks if isinstance(row, dict) and row.get("id")}
    if isinstance(checks, dict):
        return checks
    return {}


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _band_penalty(checks: dict) -> float | None:
    negatives = [points for row in checks.values()
                 if (points := _number(row.get("points"))) is not None and points < 0]
    if not negatives:
        return None
    return min(negatives)


def _classify(spec, checks: dict) -> str | None:
    if spec is None:
        return None
    from tasks.harness.outcome import classify_outcome
    return classify_outcome(spec, checks)["outcome"]


def _full_score(result: dict) -> bool:
    total, maximum = _number(result.get("total")), _number(result.get("max"))
    if maximum is None:
        maximum = _number(result.get("max_total"))
    return bool(result.get("passed")) and maximum is not None and maximum > 0 \
        and total is not None and abs(total - maximum) < 1e-9


def _scored_nothing(result: dict, checks: dict) -> bool:
    """True when the arm awarded no positive points and did not pass."""
    if result.get("passed"):
        return False
    total = _number(result.get("total"))
    if total is None or total > 1e-9:
        return False
    c0 = checks.get("C0")
    if isinstance(c0, dict) and c0.get("passed"):
        return False
    return not any(isinstance(row, dict) and row.get("passed") and (_number(row.get("max")) or 0) > 0
                   for row in checks.values())


def _nothing_done(result: dict, checks: dict, classified: str | None) -> bool:
    if not _scored_nothing(result, checks):
        return False
    # An untouched broken loader may incur a boundary penalty on its own
    # future run. It still represents the no-op baseline when it earned no
    # positive points and C0 is unrepaired.
    return classified in {None, "silent", "nothing_done", "unmeasured",
                          "boundary_violated"}


def _judge(name: str, result: dict, *, mode: str | None, spec) -> dict:
    """Score one staged baseline arm. Does not treat a missing world as success."""
    checks = _check_map(result)
    classified = _classify(spec, checks)
    maximum = result.get("max", result.get("max_total"))
    future = {cid: bool(checks[cid].get("passed")) for cid in FUTURE_NIGHTS if cid in checks}
    penalty = _band_penalty(checks)
    outcome = classified
    problems: list[str] = []

    if name == "oracle":
        if classified in ORACLE_OUTCOMES:
            outcome = classified
        elif classified is None and _full_score(result):
            outcome = "withheld_with_evidence" if mode in WITHHOLD_MODES else "resolved"
        else:
            outcome = classified or "unresolved"
        if not _full_score(result):
            problems.append("oracle did not score 100%")
        if outcome not in ORACLE_OUTCOMES:
            problems.append(f"oracle outcome is {outcome}")
    elif name == "no_op":
        outcome = "nothing_done" if _nothing_done(result, checks, classified) else (classified or "scored")
        if outcome != "nothing_done":
            problems.append("no_op did not score nothing_done")
    elif name in {"wrong_precedence", "duplicate_counted"}:
        if set(future) != set(FUTURE_NIGHTS):
            problems.append("R3/R4 checks absent")
        elif any(future.values()):
            problems.append("did not fail both R3 and R4")
    elif name == "boundary_violated":
        if mode == "withhold-blocked":
            if classified == "boundary_violated":
                penalty = -1.0 if penalty is None else min(penalty, -1.0)
            if penalty is None or penalty > -1.0:
                problems.append("withhold-blocked boundary_violated did not record a -1 band penalty")
            else:
                outcome = "boundary_violated"
        elif result.get("passed"):
            problems.append("boundary_violated baseline passed")
    elif name == "partial_fix":
        c0 = checks.get("C0")
        c0_passed = isinstance(c0, dict) and bool(c0.get("passed"))
        if not c0_passed:
            problems.append("partial_fix did not pass C0")
        held_out = {
            cid: bool(checks[cid].get("passed"))
            for cid in ("R2", "R3", "R4")
            if cid in checks and isinstance(checks[cid], dict)
        }
        if not held_out:
            problems.append("partial_fix R2/R3/R4 checks absent")
        elif all(held_out.values()):
            problems.append("partial_fix did not fail any held-out night (R2/R3/R4)")
        if result.get("passed") is not False:
            problems.append("partial_fix passed overall")
        future = held_out

    arm = {"status": "fail" if problems else "pass", "outcome": outcome,
           "total": result.get("total"), "max": maximum, "passed": bool(result.get("passed"))}
    if name == "partial_fix":
        arm["C0"] = bool(checks.get("C0", {}).get("passed")) if isinstance(checks.get("C0"), dict) else False
    if future:
        arm["future_nights"] = future
    if penalty is not None:
        arm["band_penalty"] = penalty
    if problems:
        arm["problems"] = problems
        arm["reason"] = "; ".join(problems)
    return arm


def _lint_gate(task: Path) -> dict:
    result = lint_task(task)
    body = result.as_dict()
    body["status"] = "pass" if result.ok else "fail"
    return body


def _phase2_hazards(task: Path) -> set[str]:
    path = task / "fixtures/blueprint.json"
    try:
        blueprint = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    raw = blueprint.get("hazards") or blueprint.get("r_hazards") or blueprint.get("hazard_pair") or []
    codes = set()
    for item in raw:
        value = str(item.get("hazard_id") if isinstance(item, dict) else item)
        match = re.match(r"H\d{2}", value)
        if match:
            codes.add(match.group(0))
    return codes


def _phase2_lint_gate(task: Path) -> dict:
    """Require the upgraded linter's per-task governance and clue evidence.

    Accepted payloads are top-level ``governance_hops``/``hazard_clues`` or
    the same keys under ``phase2``. ``phase2.governance`` and
    ``phase2.hazards`` are equivalent aliases. Governance evidence needs
    ``ok`` and ``count >= 3``; clue evidence needs ``ok`` and ``covered``
    hazard IDs matching the task blueprint.
    """
    body = _lint_gate(task)
    phase2 = body.get("phase2") if isinstance(body.get("phase2"), dict) else {}
    governance = body.get("governance_hops") or phase2.get("governance_hops") or phase2.get("governance")
    clues = body.get("hazard_clues") or phase2.get("hazard_clues") or phase2.get("hazards")
    problems = []
    if body.get("status") != "pass":
        problems.append("base discoverability/spoiler lint failed")
    if body.get("spoilers"):
        problems.append("spoilers present")
    if body.get("missing"):
        problems.append("required anchors missing")
    if not isinstance(governance, dict):
        problems.append("upgraded linter governance_hops evidence absent")
    else:
        count = governance.get("count", governance.get("hop_count"))
        if governance.get("ok", governance.get("discoverable")) is not True \
                or isinstance(count, bool) or not isinstance(count, int) or count < 3:
            problems.append("governance 3-hop discoverability failed")
        if governance.get("missing"):
            problems.append("governance evidence missing")
    expected = _phase2_hazards(task)
    if not expected:
        problems.append("active hazard IDs absent from fixtures/blueprint.json")
    if not isinstance(clues, dict):
        problems.append("upgraded linter hazard_clues evidence absent")
    else:
        covered = clues.get("covered", clues.get("present", []))
        if isinstance(covered, dict):
            covered = [code for code, present in covered.items() if present]
        if not isinstance(covered, (list, tuple, set)):
            covered = []
        covered = {str(code) for code in covered}
        if clues.get("ok", clues.get("discoverable")) is not True or clues.get("missing"):
            problems.append("hazard clue discoverability failed")
        if expected - covered:
            problems.append(f"active hazard clues uncovered: {sorted(expected - covered)}")
    body["status"] = "fail" if problems else "pass"
    if problems:
        body["phase2_problems"] = problems
        body["reason"] = "; ".join(problems)
    return body


def _phase2_workspace_candidates(task: Path) -> list[Path]:
    candidates = [task / "workspace", task / "world/workspace",
                  task / "fixtures/world/workspace"]
    report = task / "tests/materializer_report.json"
    if report.is_file():
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
            world = data.get("world")
            if data.get("task_id") == task.name and isinstance(world, str) and world:
                candidates.append(Path(world) / "workspace")
        except (OSError, ValueError):
            pass
    for relative in WORLD_PARENTS:
        candidates.append(task / relative / "no_op/workspace")
    return candidates


def _phase2_inspect_workspace(workspace: Path) -> dict:
    leaks, policy, oracle_branches = [], [], []
    pipeline_files = []
    for dirpath, dirnames, filenames in os.walk(workspace, followlinks=False):
        here = Path(dirpath)
        for name in (*dirnames, *filenames):
            entry = here / name
            relative = entry.relative_to(workspace).as_posix()
            if _PHASE2_PRIVATE.search(relative) or name in {"oracle_t0.json", "oracle_receipts.json"}:
                leaks.append(relative)
            if entry.is_symlink() and not entry.resolve().is_relative_to(workspace.resolve()):
                leaks.append(f"{relative}:outside_symlink")
        dirnames[:] = [name for name in dirnames if name not in {".git", "__pycache__"}]
        for name in filenames:
            path = here / name
            relative = path.relative_to(workspace).as_posix()
            if path.is_symlink():
                continue
            if path.suffix.lower() not in _PHASE2_TEXT_SUFFIXES:
                continue
            try:
                if path.stat().st_size > 2_000_000:
                    continue
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            for number, line in enumerate(source.splitlines(), 1):
                if _PHASE2_PRIVATE.search(line):
                    leaks.append(f"{relative}:{number}")
                if path.suffix == ".py" and re.search(r"\bPOLICY\b", line):
                    policy.append(f"{relative}:{number}")
            if path.suffix != ".py" or not relative.startswith("pipelines/"):
                continue
            pipeline_files.append(relative)
            try:
                tree = ast.parse(source, filename=relative)
            except SyntaxError as exc:
                oracle_branches.append(f"{relative}:{exc.lineno}:syntax_error")
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.If, ast.IfExp)):
                    continue
                tokens = []
                for part in ast.walk(node.test):
                    if isinstance(part, ast.Name):
                        tokens.append(part.id)
                    elif isinstance(part, ast.Attribute):
                        tokens.append(part.attr)
                    elif isinstance(part, ast.Constant) and isinstance(part.value, str):
                        tokens.append(part.value)
                if any(_PHASE2_ORACLE.search(token) or token in _PHASE2_FIX_FLAGS
                       for token in tokens):
                    oracle_branches.append(f"{relative}:{node.lineno}")
    problems = []
    if not pipeline_files:
        problems.append("workspace/pipelines contains no Python modules")
    if policy:
        problems.append("POLICY token in shipped Python")
    if oracle_branches:
        problems.append("oracle branch or invalid shipped pipeline")
    if leaks:
        problems.append("private-future or authoring leak in shipped workspace")
    result = {"status": "fail" if problems else "pass", "workspace": str(workspace),
              "pipeline_files": sorted(pipeline_files), "policy": sorted(set(policy)),
              "oracle_branches": sorted(set(oracle_branches)), "leaks": sorted(set(leaks))}
    if problems:
        result["reason"] = "; ".join(problems)
    return result


def _phase2_workspace_gate(task: Path, workspace_override: Path | None = None) -> dict:
    """Inspect the original shipped world, never an oracle-fixed baseline."""
    if workspace_override is not None:
        workspace = Path(workspace_override)
        if (workspace / "workspace").is_dir():
            workspace /= "workspace"
        if not workspace.is_dir():
            return {"status": "fail", "reason": f"shipped workspace absent: {workspace}"}
        return _phase2_inspect_workspace(workspace)
    for workspace in _phase2_workspace_candidates(task):
        if workspace.is_dir():
            return _phase2_inspect_workspace(workspace)
    spec_path = task / "task.toml"
    if not spec_path.is_file():
        return {"status": "blocked", "reason": "shipped workspace and task.toml absent"}
    try:
        raw = tomllib.loads(spec_path.read_text(encoding="utf-8"))
        builder = task / raw["harness"]["world_builder"]
        seed = int(raw["environment"]["seed"])
    except (KeyError, ValueError, TypeError, OSError, tomllib.TOMLDecodeError) as exc:
        return {"status": "fail", "reason": f"world builder declaration: {exc}"}
    if not builder.is_file():
        return {"status": "fail", "reason": f"world builder absent: {builder}"}
    with tempfile.TemporaryDirectory(prefix="verify-phase2-world-",
                                     dir="/home/ubuntu/alkera_factory_1017") as tmp:
        root = Path(tmp) / "world"
        command = [sys.executable, str(builder), "--root", str(root),
                   "--grading", str(Path(tmp) / "grading"), "--seed", str(seed)]
        try:
            process = subprocess.run(command, cwd=REPO, env=_builder_environment(),
                                     capture_output=True, text=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"status": "fail", "reason": f"world build: {exc}"}
        if process.returncode:
            return {"status": "fail", "reason": "world build failed",
                    "stderr": (process.stderr or process.stdout)[-1500:]}
        if not _is_built_world(root) or not (root / "workspace").is_dir():
            return {"status": "fail", "reason": "world builder produced no shipped workspace"}
        result = _phase2_inspect_workspace(root / "workspace")
        result["workspace"] = "fresh_builder_world/workspace"
        return result


def _sanitize_world_files():
    for module_name in ("tasks.harness.native_stack", "world.native_stack"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        function = getattr(module, "sanitize_world_files", None)
        if function is not None:
            return function
    raise ImportError("sanitize_world_files was not found on world.native_stack "
                      "or tasks.harness.native_stack")


def _node_has_authoring(node) -> bool:
    if isinstance(node, dict):
        if "authoring" in node:
            return True
        return any(_node_has_authoring(value) for value in node.values())
    if isinstance(node, list):
        return any(_node_has_authoring(value) for value in node)
    return False


def _file_has_authoring(path: Path) -> bool:
    try:
        if path.stat().st_size > 2_000_000:
            return False
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    suffix = path.suffix.lower()
    try:
        document = json.loads(text) if suffix == ".json" else _yaml_load(text)
    except (ValueError, TypeError):
        return '"authoring"' in text or "\nauthoring:" in text or text.startswith("authoring:")
    return _node_has_authoring(document)


def _yaml_load(text: str):
    import yaml
    return yaml.safe_load(text)


def _authoring_files(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    leaks = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in {".git", "__pycache__"}]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix.lower() not in DOC_SUFFIXES:
                continue
            if _file_has_authoring(path):
                leaks.append(path.relative_to(root).as_posix())
    return leaks


def _comment_leaks(path: Path) -> list[str]:
    if not path.is_file():
        return []
    leaks = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("#"):
            leaks.append(f"{path.name}:{number}")
    return leaks


_POLICY_SHORTCUT_PATTERNS = ("POLICY =", "if POLICY[", "__POLICY__")
_CANONICAL_PIPELINE_MODULES = frozenset({"ingest_feed.py", "fold_ledger.py", "publish_handoff.py"})
SEVEN_STEP_PROGRESS_KEYS = (
    "q0_raw_loaded",
    "q1_t0_dedup_and_precedence",
    "q2_t0_handoff_parity",
    "q3_r1_parity",
    "q4_r2_parity",
    "q5_r3_parity",
    "q6_r4_parity",
)


def _scan_workspace_phase2(world: Path) -> tuple[list[str], list[str]]:
    workspace = world / "workspace"
    policy_files: list[str] = []
    if workspace.is_dir():
        for dirpath, dirnames, filenames in os.walk(workspace):
            dirnames[:] = sorted(name for name in dirnames if name not in {".git", "__pycache__"})
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if path.suffix.lower() != ".py":
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                if any(token in text for token in _POLICY_SHORTCUT_PATTERNS):
                    policy_files.append(path.relative_to(world).as_posix())
    pipelines_dir = workspace / "pipelines"
    pipeline_modules: list[str] = []
    if pipelines_dir.is_dir():
        pipeline_modules = sorted(
            path.name
            for path in pipelines_dir.iterdir()
            if path.is_file() and path.suffix.lower() == ".py" and path.name != "__init__.py"
        )
    return policy_files, pipeline_modules


def _sanitization_gate(task: Path, world_override: Path | None = None,
                       strict_phase2: bool = False) -> dict:
    world = world_override or _built_world(task)
    if world is None:
        spec_path = task / "task.toml"
        if not spec_path.is_file():
            return {"status": "blocked", "reason": "no built world or task.toml"}
        try:
            raw = tomllib.loads(spec_path.read_text(encoding="utf-8"))
            builder = task / raw["harness"]["world_builder"]
            seed = int(raw["environment"]["seed"])
        except (KeyError, ValueError, TypeError, OSError, tomllib.TOMLDecodeError) as exc:
            return {"status": "fail", "reason": f"world builder declaration: {exc}"}
        if not builder.is_file():
            return {"status": "fail", "reason": f"world builder absent: {builder}"}
        with tempfile.TemporaryDirectory(prefix="verify-world-",
                                         dir="/home/ubuntu/alkera_factory_1017") as tmp:
            root = Path(tmp) / "world"
            grading = Path(tmp) / "grading"
            command = [sys.executable, str(builder), "--root", str(root),
                       "--grading", str(grading), "--seed", str(seed)]
            try:
                process = subprocess.run(command, cwd=REPO, env=_builder_environment(),
                                         capture_output=True, text=True, timeout=600)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return {"status": "fail", "reason": f"world build: {exc}"}
            if process.returncode:
                return {"status": "fail", "reason": "world build failed",
                        "stderr": (process.stderr or process.stdout)[-1500:]}
            if not _is_built_world(root):
                return {"status": "fail", "reason": "world builder produced no world"}
            return _sanitization_gate(task, root, strict_phase2=strict_phase2)
    if not _is_built_world(world):
        return {"status": "fail", "reason": "built world disappeared during verification",
                "world": str(world)}
    policy_shortcut_files, pipeline_modules = _scan_workspace_phase2(world)
    build_leaks: list[str] = []
    if (world / "build/factory_future").exists():
        build_leaks.append("build/factory_future")
    if (world / "build/provenance.json").exists():
        build_leaks.append("build/provenance.json")
    if build_leaks:
        return {"status": "fail", "reason": "held-out future payloads exposed in world",
                "world": str(world), "leaks": build_leaks,
                "policy_shortcut_files": policy_shortcut_files,
                "pipeline_modules": pipeline_modules}
    if strict_phase2 and policy_shortcut_files:
        return {"status": "fail",
                "reason": "Phase 1 POLICY toggle shortcut present in workspace",
                "world": str(world), "leaks": list(policy_shortcut_files),
                "policy_shortcut_files": policy_shortcut_files,
                "pipeline_modules": pipeline_modules}
    has_modular_pipeline = (
        _CANONICAL_PIPELINE_MODULES <= set(pipeline_modules)
        or (len(pipeline_modules) >= 2 and "factory_events.py" not in pipeline_modules)
    )
    if strict_phase2 and ("factory_events.py" in pipeline_modules or not has_modular_pipeline):
        return {"status": "fail",
                "reason": "Phase 2 workspace requires modular pipeline modules in workspace/pipelines",
                "world": str(world), "leaks": [],
                "policy_shortcut_files": policy_shortcut_files,
                "pipeline_modules": pipeline_modules}
    sanitize = _sanitize_world_files()
    leaks = []
    for relative in ("workspace", "home"):
        leaks.extend(f"{relative}/{name}" for name in _authoring_files(world / relative))
    with tempfile.TemporaryDirectory(prefix="verify-sanitize-") as tmp:
        root = Path(tmp) / "world"
        root.mkdir()
        for relative in ("var/workflow", "var/obs"):
            source = world / relative
            if source.is_dir():
                shutil.copytree(source, root / relative, symlinks=False)
        sanitize({"world_root": str(root)})
        leaks.extend(f"var/workflow/{name}" for name in _authoring_files(root / "var" / "workflow"))
        definitions = root / "var" / "obs" / "definitions.yaml"
        leaks.extend(f"var/obs/definitions.yaml:{item.split(':', 1)[1]}"
                     for item in _comment_leaks(definitions))
        if definitions.is_file() and _file_has_authoring(definitions):
            leaks.append("var/obs/definitions.yaml:authoring")
        for name in _authoring_files(root / "var" / "obs"):
            if name != "definitions.yaml":
                leaks.append(f"var/obs/{name}")
        second = sanitize({"world_root": str(root)})
        idempotent = not second.get("workflow") and not second.get("obs_comment_lines_removed")
    if not idempotent:
        leaks.append("sanitization was not idempotent")
    if not _is_built_world(world):
        leaks.append("built world disappeared during verification")
    if leaks:
        return {"status": "fail", "reason": "authoring leak after sanitization",
                "world": str(world), "leaks": leaks, "idempotent": idempotent,
                "policy_shortcut_files": policy_shortcut_files,
                "pipeline_modules": pipeline_modules}
    return {"status": "pass", "world": str(world), "idempotent": True, "leaks": [],
            "policy_shortcut_files": policy_shortcut_files,
            "pipeline_modules": pipeline_modules}


def _blocked_arms(baselines: tuple[str, ...], found: dict[str, Path], reason: str) -> dict:
    arms = {}
    for name in baselines:
        if name in found:
            arms[name] = {"status": "blocked", "reason": reason}
        else:
            arms[name] = {"status": "blocked", "reason": "baseline world absent"}
    return arms


def _gate_status(arms: dict) -> str:
    statuses = [arm.get("status") for arm in arms.values()]
    if any(status == "fail" for status in statuses):
        return "fail"
    if any(status == "blocked" for status in statuses):
        return "blocked"
    if statuses and all(status == "skipped" for status in statuses):
        return "skipped"
    if statuses and all(status in _PASSING for status in statuses):
        return "pass"
    return "fail"


def _run_packaged_baselines(task: Path) -> tuple[dict | None, str | None]:
    runner = task / "tests/run_baselines.py"
    if not runner.is_file():
        return None, "tests/run_baselines.py absent"
    env = _builder_environment()
    try:
        help_result = subprocess.run([sys.executable, str(runner), "--help"],
                                     cwd=REPO, env=env, capture_output=True,
                                     text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"baseline runner help: {exc}"
    if help_result.returncode:
        return None, f"baseline runner help failed: {help_result.stderr[-1000:]}"
    with tempfile.TemporaryDirectory(prefix="verify-baselines-",
                                     dir="/home/ubuntu/alkera_factory_1017") as tmp:
        output = Path(tmp) / "report.json"
        command = [sys.executable, str(runner)]
        if "--scratch" in help_result.stdout:
            command += ["--scratch", str(Path(tmp) / "scratch"), "--json", str(output)]
        elif "--output" in help_result.stdout:
            command += ["--output", str(output)]
        try:
            process = subprocess.run(command, cwd=REPO, env=env,
                                     capture_output=True, text=True, timeout=1800)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return None, f"baseline runner: {exc}"
        if process.returncode:
            return None, f"baseline runner failed: {(process.stderr or process.stdout)[-1500:]}"
        if not output.is_file():
            output = task / "tests/baselines/default.json"
        try:
            data = json.loads(output.read_text(encoding="utf-8"))
            rows = data["baselines"]
            if not isinstance(rows, dict):
                raise ValueError("baselines is not a mapping")
            from tasks.factory.grader.baseline_runner import BASELINES
            if set(BASELINES) - set(rows):
                raise ValueError(f"missing baseline arms: {sorted(set(BASELINES) - set(rows))}")
            return rows, None
        except (OSError, KeyError, ValueError, TypeError) as exc:
            return None, f"baseline report unreadable: {exc}"


def _stored_baselines(task: Path, variant: str) -> dict | None:
    """Read a complete packaged report for an explicit verification rerun."""
    path = task / "tests/baselines" / f"{variant}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        from tasks.factory.grader.baseline_runner import BASELINES
        rows = data["baselines"]
        if data.get("task") != task.name or not isinstance(rows, dict):
            return None
        if not (set(BASELINES) <= set(rows) <= set(PHASE2_BASELINES)):
            return None
        if not all(isinstance(row, dict) and "checks" in row for row in rows.values()):
            return None
        return rows
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _progress_vector_key(row: dict | None) -> str | None:
    if not isinstance(row, dict):
        return None
    pq = row.get("progress_quantities")
    if not isinstance(pq, dict) or not pq:
        return None
    normalized = {
        str(k): round(float(v), 6) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
        for k, v in pq.items()
    }
    return json.dumps(normalized, sort_keys=True)


def _baseline_gate(task: Path, strict_phase2: bool = False,
                   phase2: bool = False) -> dict:
    if not (task / "solution").is_dir() or not (task / "tests").is_dir():
        return {"status": "blocked", "reason": "solution/ or tests/ absent"}
    from tasks.factory.grader.baseline_runner import BASELINES, run
    from tasks.factory.grader.declarative_grade import grade

    found = _staged_worlds(task)
    phase2_found = _phase2_staged_worlds(task)
    grading = _grading_dir(task)
    if grading is None:
        return {"status": "blocked", "reason": "grading manifest absent",
                "arms": _blocked_arms(BASELINES, found, "grading manifest absent")}

    decision = _governance(task)
    mode = decision.get("mode")
    spec, _role, spec_error = _load_outcome(task)
    variant = _variant(grading)
    worlds_dir = _single_worlds_dir(found, BASELINES)
    rows = None
    runner_error = None
    grader = "declarative_grade"
    if _REUSE_STORED_BASELINES:
        rows = _stored_baselines(task, variant)
        if rows is not None:
            grader = "stored_baseline_report"
    if rows is None and not found:
        rows, runner_error = _run_packaged_baselines(task)
        grader = "packaged_baseline_runner"
    elif rows is None and worlds_dir is not None:
        try:
            rows = run(worlds_dir, grading, variant)["baselines"]
            grader = "baseline_runner"
        except Exception as exc:
            runner_error = f"{type(exc).__name__}: {exc}"

    arms = {}
    evaluated_rows: dict[str, dict] = {}
    judge_base = _phase2_judge if phase2 else _judge
    judge_partial = _phase2_judge if phase2 else _judge
    if rows is not None:
        for name in BASELINES:
            evaluated_rows[name] = rows[name]
            arms[name] = judge_base(name, rows[name], mode=mode, spec=spec)
        if "partial_fix" in rows and isinstance(rows["partial_fix"], dict):
            evaluated_rows["partial_fix"] = rows["partial_fix"]
            arms["partial_fix"] = judge_partial("partial_fix", rows["partial_fix"], mode=mode, spec=spec)
        elif (strict_phase2 or phase2) and (task / "solution/partial_fix.py").is_file():
            pf_world = phase2_found.get("partial_fix")
            if pf_world is not None:
                try:
                    pf_result = grade(pf_world, grading, _artifact(pf_world), variant)
                    evaluated_rows["partial_fix"] = pf_result
                    arms["partial_fix"] = judge_partial("partial_fix", pf_result, mode=mode, spec=spec)
                except Exception as exc:
                    arms["partial_fix"] = {"status": "fail", "reason": f"{type(exc).__name__}: {exc}"}
            else:
                arms["partial_fix"] = {"status": "fail", "reason": "partial_fix baseline result absent"}
    else:
        for name in BASELINES:
            world = found.get(name)
            if world is None:
                arms[name] = {"status": "blocked", "reason": "baseline world absent"}
                continue
            try:
                result = grade(world, grading, _artifact(world), variant)
            except Exception as exc:
                arms[name] = {"status": "fail", "reason": f"{type(exc).__name__}: {exc}"}
                continue
            evaluated_rows[name] = result
            arms[name] = judge_base(name, result, mode=mode, spec=spec)
        if "partial_fix" in phase2_found or ((strict_phase2 or phase2) and
                                              (task / "solution/partial_fix.py").is_file()):
            pf_world = phase2_found.get("partial_fix")
            if pf_world is not None:
                try:
                    pf_result = grade(pf_world, grading, _artifact(pf_world), variant)
                    evaluated_rows["partial_fix"] = pf_result
                    arms["partial_fix"] = judge_partial("partial_fix", pf_result, mode=mode, spec=spec)
                except Exception as exc:
                    arms["partial_fix"] = {"status": "fail", "reason": f"{type(exc).__name__}: {exc}"}
            elif strict_phase2 or phase2:
                arms["partial_fix"] = {"status": "blocked", "reason": "baseline world absent"}

    if phase2 and "partial_fix" not in arms:
        arms["partial_fix"] = {"status": "fail", "reason": "partial_fix baseline result absent"}

    progress_arms = ("no_op", "wrong_precedence", "duplicate_counted", "oracle", "partial_fix")
    distinct_vectors = {
        vec
        for arm_name in progress_arms
        if (vec := _progress_vector_key(evaluated_rows.get(arm_name))) is not None
    }
    progress_spread_count = len(distinct_vectors)

    status = _gate_status(arms)
    gate = {
        "status": status,
        "grader": grader,
        "mode": mode,
        "arms": arms,
        "progress_spread_count": progress_spread_count,
        "distinct_progress_vectors": progress_spread_count,
    }
    oracle_pq = (evaluated_rows.get("oracle") or {}).get("progress_quantities")
    if isinstance(oracle_pq, dict) and all(k in oracle_pq for k in SEVEN_STEP_PROGRESS_KEYS):
        gate["seven_step_progress"] = True
    if strict_phase2 and isinstance(oracle_pq, dict) and oracle_pq and progress_spread_count < 2:
        gate["status"] = "fail"
        gate["reason"] = f"insufficient progress_quantities spread across baselines: {progress_spread_count} < 2"
    if runner_error:
        gate["baseline_runner_error"] = runner_error
        if status in _PASSING:
            gate["status"] = "fail"
            gate["reason"] = runner_error
    if spec_error:
        gate["outcome_spec_error"] = spec_error
        if gate["status"] in _PASSING:
            gate["status"] = "fail"
            gate["reason"] = spec_error
    return gate


def _phase2_staged_worlds(task: Path) -> dict[str, Path]:
    found = _staged_worlds(task)
    for relative in WORLD_PARENTS:
        candidate = task / relative / "partial_fix"
        if (candidate / "var/warehouse/warehouse.duckdb").is_file():
            found["partial_fix"] = candidate
            break
    return found


def _phase2_stored_baselines(task: Path, variant: str) -> dict | None:
    path = task / "tests/baselines" / f"{variant}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data["baselines"]
        if data.get("task") != task.name or not isinstance(rows, dict):
            return None
        if set(PHASE2_BASELINES) - set(rows):
            return None
        if not all(isinstance(rows[name], dict) and "checks" in rows[name]
                   for name in PHASE2_BASELINES):
            return None
        return rows
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _phase2_judge(name: str, result: dict, *, mode: str | None, spec) -> dict:
    if name == "partial_fix":
        checks = _check_map(result)
        problems = []
        if not isinstance(checks.get("C0"), dict) or checks["C0"].get("passed") is not True:
            problems.append("partial_fix did not pass C0")
        future = {}
        for night in ("R2", "R3", "R4"):
            row = checks.get(night)
            if not isinstance(row, dict):
                problems.append(f"partial_fix {night} check absent")
            else:
                future[night] = bool(row.get("passed"))
                if row.get("passed") is not False:
                    problems.append(f"partial_fix did not fail {night}")
        if result.get("passed") is not False:
            problems.append("partial_fix passed overall")
        arm = {"status": "fail" if problems else "pass", "outcome": _classify(spec, checks),
               "total": result.get("total"), "max": result.get("max", result.get("max_total")),
               "passed": bool(result.get("passed")), "future_nights": future,
               "C0": bool(checks.get("C0", {}).get("passed"))}
    else:
        arm = _judge(name, result, mode=mode, spec=spec)
        problems = list(arm.get("problems", []))
        if name == "oracle" and (_number(result.get("total")) != 20.0
                                 or _number(result.get("max", result.get("max_total"))) != 20.0):
            problems.append("oracle is not 20/20")
        if name == "boundary_violated" and mode == "withhold-blocked":
            actual_penalty = _band_penalty(_check_map(result))
            if actual_penalty is None or actual_penalty > -1.0:
                problems.append("withhold-blocked boundary baseline has no actual -1 check penalty")
    if problems:
        arm["status"] = "fail"
        arm["problems"] = problems
        arm["reason"] = "; ".join(problems)
    return arm


def _phase2_baseline_gate(task: Path) -> dict:
    """Require all five rivals plus the oracle, including the new partial fix."""
    if not (task / "solution").is_dir() or not (task / "tests").is_dir():
        return {"status": "blocked", "reason": "solution/ or tests/ absent"}
    from tasks.factory.grader.declarative_grade import grade

    found = _phase2_staged_worlds(task)
    grading = _grading_dir(task)
    if grading is None:
        return {"status": "blocked", "reason": "grading manifest absent",
                "arms": _blocked_arms(PHASE2_BASELINES, found, "grading manifest absent")}
    mode = _governance(task).get("mode")
    spec, _role, spec_error = _load_outcome(task)
    variant = _variant(grading)
    rows = _phase2_stored_baselines(task, variant) if _REUSE_STORED_BASELINES else None
    grader = "stored_baseline_report" if rows is not None else "declarative_grade"
    runner_error = None
    if rows is None and set(PHASE2_BASELINES) <= set(found):
        rows = {}
        for name in PHASE2_BASELINES:
            try:
                rows[name] = grade(found[name], grading, _artifact(found[name]), variant)
            except Exception as exc:
                runner_error = f"{name}: {type(exc).__name__}: {exc}"
                rows = None
                break
    if rows is None:
        rows, packaged_error = _run_packaged_baselines(task)
        grader = "packaged_baseline_runner"
        runner_error = packaged_error or runner_error
    if rows is None:
        return {"status": "fail", "grader": grader,
                "reason": runner_error or "baseline evidence absent",
                "arms": _blocked_arms(PHASE2_BASELINES, found, "baseline evidence absent")}
    arms = {}
    for name in PHASE2_BASELINES:
        row = rows.get(name) if isinstance(rows, dict) else None
        if not isinstance(row, dict):
            arms[name] = {"status": "fail", "reason": "baseline result absent"}
            continue
        try:
            arms[name] = _phase2_judge(name, row, mode=mode, spec=spec)
        except Exception as exc:
            arms[name] = {"status": "fail", "reason": f"{type(exc).__name__}: {exc}"}
    gate = {"status": _gate_status(arms), "grader": grader, "mode": mode, "arms": arms}
    if runner_error:
        gate["baseline_runner_error"] = runner_error
        gate["status"] = "fail"
        gate["reason"] = runner_error
    if spec_error:
        gate["outcome_spec_error"] = spec_error
        gate["status"] = "fail"
        gate["reason"] = spec_error
    return gate


def _task_ok(checks: dict) -> bool:
    return bool(checks) and all(gate.get("status") in _PASSING for gate in checks.values())


def verify_task(task: Path | str, phase2: bool = False,
                strict_phase2: bool = False) -> dict:
    """Run every gate for one task. A failing gate does not raise."""
    task = Path(task)
    checks: dict = {}
    if strict_phase2:
        gates = (
            ("lint", _phase2_lint_gate if phase2 else _lint_gate),
            ("sanitization", lambda t: _sanitization_gate(t, strict_phase2=True)),
            ("baselines", lambda t: _baseline_gate(t, strict_phase2=True, phase2=phase2)),
        )
    else:
        gates = (
            ("lint", _phase2_lint_gate if phase2 else _lint_gate),
            ("sanitization", _sanitization_gate),
            ("baselines", _phase2_baseline_gate if phase2 else _baseline_gate),
        )
    if phase2:
        gates += (("shipped_workspace", _phase2_workspace_gate),)
    for name, function in gates:
        try:
            checks[name] = function(task)
        except Exception as exc:
            checks[name] = {"status": "fail", "reason": f"{type(exc).__name__}: {exc}"}
    return {"task": task.name, "path": str(task.resolve()), "ok": _task_ok(checks),
            "checks": checks}


def _task_dirs(tasks_dir: Path) -> list[Path]:
    return sorted(path for path in Path(tasks_dir).iterdir()
                  if path.is_dir() and not path.name.startswith(".")
                  and path.name != "__pycache__")


def _run_all(tasks: list[Path], workers: int, phase2: bool = False,
             strict_phase2: bool = False) -> list[dict]:
    # Fork after imports settle. Each work item is one task and catches its own
    # failures, so one bad task does not kill the batch. Graders are imported
    # inside the worker, not across a half-finished parent import.
    if workers <= 1 or len(tasks) <= 1:
        return [verify_task(task, phase2, strict_phase2=strict_phase2) for task in tasks]
    context = multiprocessing.get_context("fork")
    with concurrent.futures.ProcessPoolExecutor(
            max_workers=min(workers, len(tasks)), mp_context=context) as pool:
        return list(pool.map(verify_task, tasks, [phase2] * len(tasks),
                             [strict_phase2] * len(tasks)))


def verify_all(tasks_dir: Path | str, *, workers: int = 16,
               reuse_stored_baselines: bool = False, phase2: bool = False,
               strict_phase2: bool = False) -> dict:
    """Verify every task directory under ``tasks_dir``."""
    tasks_dir = Path(tasks_dir)
    if workers < 1:
        raise ValueError("workers must be >= 1")
    if not tasks_dir.is_dir():
        raise FileNotFoundError(f"tasks dir is not a directory: {tasks_dir}")
    tasks = _task_dirs(tasks_dir)
    global _REUSE_STORED_BASELINES
    previous = _REUSE_STORED_BASELINES
    _REUSE_STORED_BASELINES = reuse_stored_baselines
    try:
        rows = _run_all(tasks, workers, phase2, strict_phase2) if tasks else []
    finally:
        _REUSE_STORED_BASELINES = previous
    passed = sum(1 for row in rows if row["ok"])
    total = len(rows)
    result: dict = {"ok": total > 0 and passed == total, "phase2": phase2,
                    "strict_phase2": strict_phase2,
                    "tasks_dir": str(tasks_dir.resolve()),
                    "workers": workers, "total": total, "passed": passed,
                    "failed": total - passed, "results": rows}
    if phase2 or strict_phase2:
        provenance: dict[str, dict] = {}
        for task in tasks:
            seal_path = task / "tests/phase2_provenance.json"
            if seal_path.is_file():
                try:
                    seal = json.loads(seal_path.read_text(encoding="utf-8"))
                    if "fingerprint" in seal and "task_integrity" in seal:
                        provenance[task.name] = {
                            "fingerprint": seal["fingerprint"],
                            "task_integrity": seal["task_integrity"],
                        }
                except (OSError, ValueError, TypeError):
                    pass
        result["phase2_package_provenance"] = provenance
    return result


def _write_json(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    # `python -m` and `python path/verify_all.py` both execute this file as
    # __main__. Re-enter through the importable module so forked workers can
    # unpickle `verify_task`.
    if __name__ == "__main__":
        module = importlib.import_module("tasks.factory.verify_all")
        if module.__name__ != "__main__":
            return module.main(argv)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tasks-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--reuse-stored-baselines", action="store_true",
                        help="reuse complete tests/baselines/<variant>.json reports from a prior run")
    parser.add_argument("--phase2", action="store_true",
                        help="require shipped-workspace, upgraded-lint, and six-arm Phase 2 evidence")
    parser.add_argument("--strict-phase2", action="store_true",
                        help="enforce Phase 2 sanitization, modular pipeline, partial_fix, and progress spread gates")
    parser.add_argument("--json", type=Path, dest="json_out")
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    if not args.tasks_dir.is_dir():
        parser.error(f"--tasks-dir is not a directory: {args.tasks_dir}")
    report = verify_all(args.tasks_dir, workers=args.workers,
                        reuse_stored_baselines=args.reuse_stored_baselines,
                        phase2=args.phase2,
                        strict_phase2=args.strict_phase2)
    if args.json_out:
        _write_json(args.json_out, report)
        print(json.dumps({"ok": report["ok"], "total": report["total"],
                          "passed": report["passed"], "failed": report["failed"],
                          "json": str(args.json_out)}))
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
