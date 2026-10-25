"""Centralized path resolution for the Alkera task factory and cluster portability.

On the original build host, heavy rollout/task artifacts lived at
``/home/ubuntu/alkera_factory_1017`` beside ``/home/ubuntu/env_project``.
On another machine or GPU cluster (e.g. ``/hopper/home/miyao/dataenv/env_project``
with sibling ``/hopper/home/miyao/dataenv/alkera_factory_1017``), set
``ALKERA_FACTORY_ROOT`` or place ``alkera_factory_1017`` beside the repository
root and all factory scripts and run-index paths resolve automatically.
"""
from __future__ import annotations

import os
from pathlib import Path

LEGACY_REPO_PREFIX = "/home/ubuntu/env_project"
LEGACY_FACTORY_PREFIX = "/home/ubuntu/alkera_factory_1017"

REPO_ROOT: Path = Path(
    os.environ.get("ALKERA_REPO_ROOT") or Path(__file__).resolve().parents[2]
).resolve()


def get_factory_root() -> Path:
    """Return the active factory artifact root directory (outside the git repo)."""
    env_val = os.environ.get("ALKERA_FACTORY_ROOT")
    if env_val:
        return Path(env_val).expanduser().resolve()
    sibling = REPO_ROOT.parent / "alkera_factory_1017"
    legacy = Path(LEGACY_FACTORY_PREFIX)
    if sibling.exists() or not legacy.exists():
        return sibling.resolve()
    return legacy


FACTORY_ROOT: Path = get_factory_root()


def remap_path_str(
    raw: str | Path,
    *,
    factory_root: Path | None = None,
    repo_root: Path | None = None,
    anchor_dir: Path | None = None,
) -> str:
    """Rewrite legacy `/home/ubuntu/...` prefixes to the active host's paths."""
    text = str(raw)
    if Path(text).exists():
        return text
    f_root_path = (factory_root or get_factory_root()).resolve()
    r_root_path = (repo_root or REPO_ROOT).resolve()
    if text == LEGACY_FACTORY_PREFIX:
        return str(f_root_path)
    if text.startswith(LEGACY_FACTORY_PREFIX + "/"):
        rel = text[len(LEGACY_FACTORY_PREFIX) + 1:]
        if anchor_dir is not None and (Path(anchor_dir) / rel).exists():
            return str((Path(anchor_dir) / rel).resolve())
        return str(f_root_path / rel)
    if text == LEGACY_REPO_PREFIX:
        return str(r_root_path)
    if text.startswith(LEGACY_REPO_PREFIX + "/"):
        rel = text[len(LEGACY_REPO_PREFIX) + 1:]
        return str(r_root_path / rel)
    return text


def resolve_factory_path(
    raw: str | Path,
    *,
    factory_root: Path | None = None,
    repo_root: Path | None = None,
    anchor_dir: Path | None = None,
) -> Path:
    """Resolve a path that may carry a legacy `/home/ubuntu/...` prefix."""
    return Path(
        remap_path_str(
            raw,
            factory_root=factory_root,
            repo_root=repo_root,
            anchor_dir=anchor_dir,
        )
    )
