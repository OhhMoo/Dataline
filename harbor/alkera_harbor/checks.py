"""Release gates every generated task must pass before it is moved into place.

Each function returns a list of problems (empty = pass); the adapter fails the
task on any. They encode the intake checks labs run (TB3 CI, Harbor's quality
rubric; see buyer_requirements.md): canary everywhere, no tests/solution baked
into the agent image, no grading material reachable by the agent, nothing the
packager would silently drop, and no host path from the export machine.
"""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path

from . import digest, dockerenv
from .constants import CANARY_MD, CANARY_TEXT, TASKS_DIR
from .context import GitView
from .harness import is_task_package_name

#: Files that must carry the canary in their first lines (contract 1.8).
CANARY_FILES = ("task.toml", "instruction.md", "environment/Dockerfile",
                "tests/Dockerfile", "tests/test.sh", "solution/solve.sh")
CANARY_OPTIONAL = ("tests/verify.py",)
#: The agent environment may never contain a file with one of these names.
FORBIDDEN_ENV_NAMES = ("grade.py", "expected.duckdb", "solve.sh", "solution.sh")
#: Below this size a content match is noise (an empty __init__.py, `{}`).
MIN_FINGERPRINT_BYTES = 200

_COPY_LEAK = re.compile(r"^\s*(COPY|ADD)\b[^\n]*\b(tests|solution)\b", re.I | re.M)
_PLATFORM_PIN = re.compile(r"^\s*FROM\s+--platform", re.I | re.M)


def canary_problems(task_dir: Path) -> list[str]:
    task_dir = Path(task_dir)
    problems = []
    for rel in CANARY_FILES + CANARY_OPTIONAL:
        path = task_dir / rel
        if not path.is_file():
            if rel in CANARY_FILES:
                problems.append(f"{rel}: missing")
            continue
        head = path.read_text(encoding="utf-8", errors="replace").split("\n")[:3]
        if rel == "instruction.md":
            if not head or head[0] != CANARY_MD:
                problems.append(f"{rel}: first line is not the canary comment")
        elif not any(CANARY_TEXT in line and line.lstrip().startswith("#")
                     for line in head):
            problems.append(f"{rel}: no canary comment in its first lines")
    return problems


def dockerfile_problems(task_dir: Path) -> list[str]:
    problems = []
    for rel in ("environment/Dockerfile", "tests/Dockerfile"):
        path = Path(task_dir) / rel
        if not path.is_file():
            problems.append(f"{rel}: missing")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if _PLATFORM_PIN.search(text):
            problems.append(f"{rel}: FROM --platform pins an architecture")
        if rel.startswith("environment/"):
            for match in _COPY_LEAK.finditer(text):
                problems.append(f"{rel}: bakes tests/solution into the agent "
                                f"image: {match.group(0).strip()!r}")
    if (Path(task_dir) / "environment" / "docker-compose.yaml").exists():
        problems.append("environment/docker-compose.yaml: the contract is a "
                        "single Dockerfile (portable to Dockerfile-only providers)")
    return problems


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def grading_fingerprints(tasks_dir: Path = TASKS_DIR) -> dict[str, str]:
    """sha256 -> repo path of every grader / oracle file of every task package
    (GT*, BI*, ...: harness.is_task_package_name).

    `tasks/*/tests/**` (minus `tests/fixtures/`, which ARE world sources) and
    `tasks/*/solution/**`, as git sees them (so build caches are not counted).
    """
    repo = tasks_dir.parent
    candidates: list[tuple[str, Path]] = []
    for task in sorted(p for p in tasks_dir.iterdir()
                       if p.is_dir() and is_task_package_name(p.name)):
        for sub in ("tests", "solution"):
            root = task / sub
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.is_symlink():
                    continue
                rel = path.relative_to(repo).as_posix()
                if sub == "tests" and "/tests/fixtures/" in f"/{rel}":
                    continue
                if "__pycache__" in path.parts or path.suffix == ".pyc":
                    continue
                if path.stat().st_size < MIN_FINGERPRINT_BYTES:
                    continue
                candidates.append((rel, path))
    ignored = GitView(repo).ignored(rel for rel, _ in candidates)
    out: dict[str, str] = {}
    for rel, path in candidates:
        if rel not in ignored:
            out.setdefault(_sha(path), rel)
    return out


def environment_problems(task_dir: Path, fingerprints: dict[str, str],
                         grading_dir: Path | None = None) -> list[str]:
    """No grader, oracle or grading-zone file anywhere in environment/."""
    env = Path(task_dir) / "environment"
    forbidden = dict(fingerprints)
    if grading_dir is not None and Path(grading_dir).is_dir():
        # top-level grading files only: snapshot sub-trees (pre_episode/ ...)
        # are copies of world files by design
        for path in sorted(Path(grading_dir).iterdir()):
            if path.is_file() and path.stat().st_size >= MIN_FINGERPRINT_BYTES:
                forbidden.setdefault(_sha(path), f"grading zone: {path.name}")
    problems = []
    for path in sorted(env.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(task_dir).as_posix()
        if path.name in FORBIDDEN_ENV_NAMES:
            problems.append(f"{rel}: a grading/oracle file name in the agent image")
            continue
        if path.stat().st_size >= MIN_FINGERPRINT_BYTES:
            source = forbidden.get(_sha(path))
            if source:
                problems.append(f"{rel}: identical to {source}")
    return problems


def unpublishable_problems(task_dir: Path) -> list[str]:
    return [f"{rel}: present on disk but Harbor's packager would drop it"
            for rel in digest.unpublishable(task_dir)]


def static_fingerprint(task_dir: Path, task_id: str) -> str:
    """sha256 over everything in a task that must be identical in EVERY task:
    environment/ and tests/ minus the per-task payload (`episode/`) and minus
    the task's own package copy (any path naming `task_id`). Two tasks of one
    export with different fingerprints mean a template or a context source
    changed while the export ran."""
    digest = hashlib.sha256()
    task_dir = Path(task_dir)
    for top in ("environment", "tests"):
        root = task_dir / top
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(task_dir).as_posix()
            parts = rel.split("/")
            if parts[1] == "episode" or task_id in parts:
                continue
            digest.update(f"{rel}\0{_sha(path)}\n".encode())
    return digest.hexdigest()


def environment_env_problems(task_dir: Path) -> list[str]:
    """task.toml [environment.env] == the image's ENV minus `PATH`, plus the
    per-arm variables of episode.json: exactly that, with the Dockerfile's
    values. `PATH` must NOT be there: Harbor's docker provider merges a task's
    env into the HOST's `docker compose` process (harbor 0.23.0
    environments/docker/docker.py:617-674 `_compose_env_vars`; see
    dockerenv.NOT_MIRRORED)."""
    task_dir = Path(task_dir)
    try:
        toml_env = (tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8"))
                    .get("environment", {}).get("env", {}))
        image_env = dockerenv.parse_env(
            (task_dir / "environment" / "Dockerfile").read_text(encoding="utf-8"))
        episode_env = json.loads((task_dir / "environment" / "episode" / "episode.json")
                                 .read_text(encoding="utf-8")).get("env", {})
    except (OSError, ValueError) as exc:
        return [f"[environment.env] cannot be checked: {exc}"]
    expected = {k: v for k, v in image_env.items() if dockerenv.selected(k)}
    expected.update(episode_env)
    problems = []
    for name in sorted(set(toml_env) | set(expected)):
        if name in dockerenv.NOT_MIRRORED and name in toml_env:
            problems.append(f"[environment.env] sets {name}; it must not (Harbor's "
                            f"docker provider would use it for the host's "
                            f"`docker compose`, see dockerenv.NOT_MIRRORED)")
        elif name not in toml_env:
            source = "episode.json env" if name in episode_env else "the image's ENV"
            problems.append(f"[environment.env] lacks {name} from {source}")
        elif name not in expected:
            problems.append(f"[environment.env].{name} is neither an image ENV "
                            f"nor a per-arm variable")
        elif toml_env[name] != expected[name]:
            problems.append(f"[environment.env].{name}={toml_env[name]!r}, "
                            f"expected {expected[name]!r}")
    return problems


def payload_problems(task_dir: Path) -> list[str]:
    """What the environment Dockerfile COPYs unconditionally must exist, and
    must hold a file (git and Harbor's packager drop empty directories)."""
    episode = Path(task_dir) / "environment" / "episode"
    problems = []
    for name in ("world", "workspace", "home"):
        path = episode / name
        if not path.is_dir() or not any(p.is_file() for p in path.rglob("*")):
            problems.append(f"environment/episode/{name}: missing or holds no file")
    for name in ("episode.json", "gateway.json"):
        if not (episode / name).is_file():
            problems.append(f"environment/episode/{name}: missing")
    if (episode / "world" / "workspace").exists():
        problems.append("environment/episode/world/workspace: must not exist (the "
                        "image links /world/workspace -> ../workspace)")
    return problems
