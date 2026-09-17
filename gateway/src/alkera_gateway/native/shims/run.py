"""The shim body: run the real binary, then say what it changed.

    python -m alkera_gateway.native.shims.run <binary> [args...]

Stdin, stdout and stderr are inherited, so the agent sees exactly what the
binary printed, and the shim exits with the binary's own exit code. Nothing
about the call is altered; the shim only observes.

WHAT "TARGETS" MEANS FOR A BINARY
---------------------------------
A workspace snapshot - sha256 per file - is taken before and after. Targets are
the workspace-relative posix paths whose digest appeared, vanished or changed,
named exactly as `files.Workspace.name` names them, so a file an agent edits
through the shim and one it edits through `fs_write` produce the same target
string. Files are otherwise not read.

The snapshot is bounded: above `ALKERA_SHIM_MAX_FILES` (default 20000) files
the scan is abandoned and the row carries `targets: []` with the flag
`workspace_unscanned`, because a row that quietly claims nothing changed is the
one failure mode this shim must not have.

CONFIGURATION
-------------
ALKERA_SHIM_WORKSPACE  the tree to diff; else ALKERA_WORKSPACE; else the cwd.
ALKERA_SHIM_BIN        the shim bin directory, removed from PATH when looking
                       for the real binary - without it the shim finds itself.
ALKERA_SHIM_MAX_FILES  snapshot ceiling, above which the scan is refused.
plus the ALKERA_NATIVE_AUDIT / ALKERA_AUDIT_DIR / ALKERA_VTS that `audit.record`
reads.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ..audit import record

ACTOR = "shim"
DEFAULT_MAX_FILES = 20_000
# Directories whose churn is not the agent's edit: git's object store, virtual
# environments, bytecode caches and dbt's own scratch output.
SKIP_DIRECTORIES = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "target", "logs"}
READ_CHUNK = 1 << 20


def workspace_root() -> Path:
    name = os.environ.get("ALKERA_SHIM_WORKSPACE") or os.environ.get("ALKERA_WORKSPACE")
    return Path(name).resolve() if name else Path.cwd().resolve()


def file_digest(path: Path) -> str:
    sha = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(READ_CHUNK), b""):
            sha.update(chunk)
    return sha.hexdigest()


def snapshot(root: Path, max_files: int) -> dict[str, str] | None:
    """sha256 per workspace file, or None when the tree is too large to scan."""
    digests: dict[str, str] = {}
    if not root.is_dir():
        return digests
    for current, directories, files in os.walk(root):
        directories[:] = [name for name in directories if name not in SKIP_DIRECTORIES]
        for name in files:
            path = Path(current) / name
            if len(digests) >= max_files:
                return None
            try:
                digests[path.relative_to(root).as_posix()] = file_digest(path)
            except OSError:
                # A file that vanished or cannot be opened is not a target; the
                # other snapshot will disagree about it and that is the signal.
                continue
    return digests


def changed(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """Workspace-relative paths whose content differs between two snapshots."""
    return sorted(
        name
        for name in set(before) | set(after)
        if before.get(name) != after.get(name)
    )


def real_binary(name: str) -> str | None:
    """The binary the shim is standing in front of, with the shim dir removed."""
    shim_bin = os.environ.get("ALKERA_SHIM_BIN")
    entries = os.environ.get("PATH", "").split(os.pathsep)
    if shim_bin:
        resolved = Path(shim_bin).resolve()
        entries = [
            entry
            for entry in entries
            if entry and Path(entry).resolve(strict=False) != resolved
        ]
    return shutil.which(name, path=os.pathsep.join(entries))


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        print("usage: python -m alkera_gateway.native.shims.run <binary> [args...]",
              file=sys.stderr)
        return 2
    name, rest = arguments[0], arguments[1:]
    if name == "duckdb":
        # The one binary whose row is not a file diff: its statements are the
        # description of what it did, and `duckdb_cli` runs them through the
        # gateway's own `sql.inspect_sql` so the targets are table names on
        # both transports (NATIVE.md, amendment 2026-09-16).
        from . import duckdb_cli

        return duckdb_cli.main(rest)
    executable = real_binary(name)
    root = workspace_root()
    max_files = int(os.environ.get("ALKERA_SHIM_MAX_FILES") or DEFAULT_MAX_FILES)
    args = {"argv": [name, *rest], "cwd": Path.cwd().as_posix()}

    if executable is None:
        record(
            tool=f"shim.{name}",
            args=args,
            op_class="exec",
            targets=[],
            exit="error",
            error={"code": "binary_missing",
                   "message": f"{name} is not on PATH outside the shim directory"},
            actor=ACTOR,
            flags=["binary_missing"],
        )
        print(f"{name}: not found", file=sys.stderr)
        return 127

    before = snapshot(root, max_files)
    started = time.monotonic()
    completed = subprocess.run([executable, *rest])
    latency_ms = (time.monotonic() - started) * 1000
    after = snapshot(root, max_files) if before is not None else None

    flags: list[str] = []
    if before is None or after is None:
        targets: list[str] = []
        flags.append("workspace_unscanned")
    else:
        targets = changed(before, after)
    failed = completed.returncode != 0
    record(
        tool=f"shim.{name}",
        args=args,
        op_class="exec",
        targets=targets,
        exit="error" if failed else "ok",
        error={"code": "command_failed",
               "message": f"{name} exited {completed.returncode}"} if failed else None,
        latency_ms=latency_ms,
        result={"returncode": completed.returncode, "changed": targets},
        flags=flags,
        actor=ACTOR,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
