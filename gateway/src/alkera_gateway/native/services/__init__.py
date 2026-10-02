"""Registry of the fake vendor-shaped services (gateway/NATIVE.md).

Each module in NAMES exposes `NAME`, `HOST` (DNS alias in the episode network),
`PORT` and `app` (a FastAPI application). `serve.py` mounts whatever imports;
a missing module is reported by name, never skipped silently. `load(names)`
imports only the named subset (an environment profile's services), in that order.
"""
from __future__ import annotations

import importlib
from collections.abc import Sequence
from types import ModuleType

NAMES = ("jira", "slack", "confluence",
         "metabase", "prometheus", "alertmanager", "loki", "airbyte", "deploy")


def load(names: Sequence[str] | None = None
         ) -> tuple[dict[str, ModuleType], dict[str, str]]:
    """-> (loaded services by name, import errors by name).

    `names` restricts the set (kept in the given order); None means all NAMES.
    A name outside NAMES is a ValueError, not a missing service.
    """
    if names is None:
        names = NAMES
    unknown = [n for n in names if n not in NAMES]
    if unknown:
        raise ValueError(f"unknown native service(s) {', '.join(map(repr, unknown))}; "
                         f"known: {', '.join(NAMES)}")
    loaded, missing = {}, {}
    for name in names:
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
