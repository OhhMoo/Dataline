"""Registry of the fake vendor-shaped services (gateway/NATIVE.md).

Each module in NAMES exposes `NAME`, `HOST` (DNS alias in the episode network),
`PORT` and `app` (a FastAPI application). `serve.py` mounts whatever imports;
a missing module is reported by name, never skipped silently.
"""
from __future__ import annotations

import importlib
from types import ModuleType

NAMES = ("jira", "slack", "confluence",
         "metabase", "prometheus", "alertmanager", "loki", "airbyte", "deploy")


def load() -> tuple[dict[str, ModuleType], dict[str, str]]:
    """-> (loaded services by name, import errors by name)."""
    loaded, missing = {}, {}
    for name in NAMES:
        try:
            mod = importlib.import_module(f"{__name__}.{name}")
        except Exception as exc:  # a broken service must be visible, not absent
            missing[name] = f"{type(exc).__name__}: {exc}"
            continue
        for attr in ("NAME", "HOST", "PORT", "app"):
            if not hasattr(mod, attr):
                missing[name] = f"module lacks `{attr}`"
                break
        else:
            loaded[name] = mod
    return loaded, missing
