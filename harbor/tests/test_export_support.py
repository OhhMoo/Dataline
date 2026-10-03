"""Shared helpers for the generator tests (package A). No conftest on purpose:
packages B and C keep tests in this directory too, so these modules put
`harbor/` on sys.path themselves and share helpers through this module.

Harbor cross-checks run against a real Harbor install (Python >= 3.12) in a
subprocess: set ALKERA_HARBOR_PYTHON to its interpreter (or put `harbor` on
PATH). Without one they are skipped, with that reason.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HARBOR_DIR = Path(__file__).resolve().parents[1]
if str(HARBOR_DIR) not in sys.path:
    sys.path.insert(0, str(HARBOR_DIR))

import pytest  # noqa: E402

from alkera_harbor.constants import CANARY_HASH, REPO  # noqa: E402
from alkera_harbor.validate import ValidationError, find_harbor_python  # noqa: E402

SKIP_REASON = ("no Harbor install: set ALKERA_HARBOR_PYTHON to a Python with "
               "harbor>=0.23 (Python >= 3.12) to run the Harbor cross-checks")


def harbor_python() -> str:
    """The Harbor interpreter, or skip the calling test with the reason."""
    try:
        return find_harbor_python()
    except ValidationError:
        pytest.skip(SKIP_REASON)


def run_harbor(script: str, payload) -> dict:
    """Run `script` under the Harbor interpreter; it reads JSON on stdin and
    prints one JSON object as its last stdout line."""
    r = subprocess.run([harbor_python(), "-c", script], input=json.dumps(payload),
                       capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-3000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def make_standin_templates(root: Path, *, env_context: str | None = None,
                           tests_context: str | None = None) -> Path:
    """Minimal environment/ and tests/ templates (the real ones belong to
    packages B and C and may not exist yet). Never written under harbor/."""
    env = root / "environment"
    tests = root / "tests"
    (env / "bin").mkdir(parents=True)
    tests.mkdir(parents=True)
    (env / "Dockerfile").write_text(
        f"{CANARY_HASH}\nFROM python:3.11-slim\n"
        "RUN set -eux; \\\n    true\n"
        "ENV ALKERA_NATIVE_AUDIT=/audit/actions.jsonl\n"
        "ENV ALKERA_SHIM_BIN=/opt/alkera/shims\n"
        "ENV SCHED_URL=http://sched.internal:8120\n"
        "ENV PATH=/opt/alkera/shims:/usr/local/bin:/usr/bin:/bin\n"
        "COPY alkera/ /opt/alkera-src/\n"
        "COPY episode/ /opt/alkera/episode/\n", encoding="utf-8")
    world_up = env / "bin" / "world-up"
    world_up.write_text(f"#!/bin/sh\n{CANARY_HASH}\nexit 0\n", encoding="utf-8")
    world_up.chmod(0o755)
    (env / "context.txt").write_text(env_context if env_context is not None else (
        "# stand-in environment context\n"
        "world/world   world/world\n"
        "gateway/src   gateway/src   exclude=*.egg-info\n"), encoding="utf-8")
    (tests / "Dockerfile").write_text(
        f"{CANARY_HASH}\nFROM python:3.11-slim\nCOPY . /tests\n", encoding="utf-8")
    test_sh = tests / "test.sh"
    test_sh.write_text(f"#!/bin/bash\n{CANARY_HASH}\npython3 /tests/verify.py\n",
                       encoding="utf-8")
    test_sh.chmod(0o755)
    (tests / "verify.py").write_text(
        f"#!/usr/bin/env python3\n{CANARY_HASH}\nprint('stand-in verifier')\n",
        encoding="utf-8")
    (tests / "context.txt").write_text(tests_context if tests_context is not None else (
        "# stand-in verifier context, laid out as the repo\n"
        "tasks/harness   repo/tasks/harness   exclude=evidence/,judge/,redteam/\n"
        "tasks/{task}    repo/tasks/{task}\n"
        "world/world     repo/world/world\n"), encoding="utf-8")
    return root


def tree_bytes(root: Path) -> dict[str, bytes]:
    """relative path -> bytes, for whole-tree equality assertions."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = Path(dirpath) / name
            out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


def test_support_paths_are_sane():
    assert (REPO / "tasks" / "harness" / "taskspec.py").is_file()
    assert (HARBOR_DIR / "alkera_harbor" / "__init__.py").is_file()
    # harbor/ must stay a plain directory: never shadow the real `harbor` package
    assert not (HARBOR_DIR / "__init__.py").exists()
