"""Regenerate gateway/requirements-snowflake.lock (the Snowflake plug's clients).

    gateway/.venv/bin/python gateway/docker/lock_snowflake_clients.py

dbt-snowflake caps certifi below 2025.4.26, so it cannot be resolved inside
gateway/uv.lock without holding back every image's CA bundle (pyproject's
`snowflake` extra says why). It is compiled apart, by the script every backend's
client lock shares: `lock_warehouse_clients.py snowflake`, which this is - kept
because the lock's header names it.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _generic():
    path = Path(__file__).resolve().parent / "lock_warehouse_clients.py"
    spec = importlib.util.spec_from_file_location("lock_warehouse_clients", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    raise SystemExit(_generic().lock("snowflake"))
