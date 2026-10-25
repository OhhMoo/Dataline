"""Ensure the repository root is on sys.path whenever PYTHONPATH includes world/.

This allows pre-materialized task scripts (`make_fixture_world.py`, `tests/grade.py`)
that were generated on `/home/ubuntu/env_project` to import `tasks.factory.*` on
any cluster checkout (e.g. `/hopper/home/miyao/dataenv/env_project`) without
modifying the sealed task files.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO = str(Path(__file__).resolve().parent.parent)
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)
