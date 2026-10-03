"""Harbor's task content digest, re-implemented byte for byte.

Reference: `harbor.publisher.packager.Packager.compute_content_hash` (0.23.0).
Harbor is not a dependency of this adapter (it needs Python >= 3.12 and the
generator runs in the gateway venv), so the algorithm is re-implemented here
and tests/test_export_digest.py proves equality against the real Packager:

  * files: task.toml, instruction.md, README.md, trajectory.json (each if it
    exists) plus every file under environment/, tests/, solution/, steps/;
  * filtered by the task's own `.gitignore` when it has one, otherwise by
    DEFAULT_IGNORES (gitignore semantics);
  * sorted by POSIX path relative to the task directory;
  * digest = sha256( concat f"{rel}\\0{sha256(file)}\\n" ), as "sha256:<hex>".

`.gitignore` itself is never hashed. File modes are not part of the digest.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path

DEFAULT_IGNORES = ["__pycache__/", "*.pyc", ".DS_Store", "*.swp", "*.swo", "*~"]
SINGLE_FILES = ("task.toml", "instruction.md", "README.md", "trajectory.json")
DIRECTORIES = ("environment", "tests", "solution", "steps")


class DigestError(Exception):
    pass


def _default_ignored(rel: str) -> bool:
    """DEFAULT_IGNORES with gitignore semantics: a pattern without a slash
    matches a path component at any depth; `__pycache__/` matches directories
    only; a matching directory ignores everything below it."""
    parts = rel.split("/")
    for i, part in enumerate(parts):
        is_dir = i < len(parts) - 1
        for pattern in DEFAULT_IGNORES:
            if pattern.endswith("/"):
                if is_dir and fnmatch.fnmatchcase(part, pattern[:-1]):
                    return True
            elif fnmatch.fnmatchcase(part, pattern):
                return True
    return False


def _ignore_fn(task_dir: Path):
    gitignore = task_dir / ".gitignore"
    if not gitignore.exists():
        return _default_ignored
    try:
        import pathspec
    except ImportError as exc:                           # pragma: no cover
        raise DigestError(f"{gitignore} exists and pathspec is not importable; "
                          f"cannot apply its rules the way Harbor does") from exc
    spec = pathspec.PathSpec.from_lines("gitignore",
                                        gitignore.read_text().splitlines())
    return spec.match_file


def collect_files(task_dir: Path) -> list[tuple[str, Path]]:
    """(relative POSIX path, absolute path) of every hashed file, sorted."""
    task_dir = Path(task_dir).resolve()
    found: list[tuple[str, Path]] = []
    for name in SINGLE_FILES:
        path = task_dir / name
        if path.exists():
            found.append((name, path))
    for directory in DIRECTORIES:
        root = task_dir / directory
        if not root.exists():
            continue
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
            for name in filenames:
                path = Path(dirpath) / name
                if path.is_file():
                    found.append((path.relative_to(task_dir).as_posix(), path))
    ignored = _ignore_fn(task_dir)
    found = [(rel, path) for rel, path in found if not ignored(rel)]
    found.sort(key=lambda item: item[0])
    return found


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_hash(task_dir: Path) -> str:
    """The hex digest Harbor's Packager computes for `task_dir`."""
    outer = hashlib.sha256()
    for rel, path in collect_files(task_dir):
        outer.update(f"{rel}\0{file_sha256(path)}\n".encode())
    return outer.hexdigest()


def task_digest(task_dir: Path) -> str:
    """`sha256:<hex>`, the form dataset.toml pins."""
    return "sha256:" + content_hash(task_dir)


def unpublishable(task_dir: Path) -> list[str]:
    """Files present in the task dir that Harbor would NOT package.

    A file the packager drops still reaches a local `docker build`, so a task
    that depends on one works locally and breaks once published. The generator
    refuses to write such a file.
    """
    task_dir = Path(task_dir).resolve()
    hashed = {rel for rel, _ in collect_files(task_dir)}
    out = []
    for dirpath, _, filenames in os.walk(task_dir, followlinks=False):
        for name in filenames:
            rel = (Path(dirpath) / name).relative_to(task_dir).as_posix()
            if rel != ".gitignore" and rel not in hashed:
                out.append(rel)
    return sorted(out)
