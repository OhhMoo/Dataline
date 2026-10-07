"""Registry of the fake vendor-shaped services (gateway/NATIVE.md).

Each module in NAMES or PLUG_NAMES exposes `NAME`, `HOST` (DNS alias in the
episode network), `PORT` and `app` (a FastAPI application). `serve.py` mounts
whatever imports; a missing module is reported by name, never skipped silently.
`load(names)` imports only the named subset (an environment profile's
services), in that order.

Two lists, on purpose:

* NAMES - the nine services an UNPROFILED stack serves. "No profile" is the box
  as it stood before profiles existed (world/world/native_workspace.py's
  EXPLICIT rule), so `load()` with no names is these nine and only these: the
  aggregate /health, containment P6's registry probe, `native_probe.py` and the
  surface gate's `~<name>` sweep all read this list.
* PLUG_NAMES - services newer than that box. They are mounted only when an
  environment profile plugs them by name (`alkera_gateway.plugs`: `bi=powerbi`,
  or a stack whose default is one of them, like `snowflake` -> `sigma`), the
  same EXPLICIT-ONLY rule the Jupyter notebook plug follows. Each is a BI
  vendor over `world.semantic`; see its own module docstring.

A third kind, in neither list: a HOSTED backend (`alkera_gateway.backends`, a
row with `hosted=True`) has its module here too, under its row's name, and
loads only when named - `serve.selected_services` names it when a profile plugs
it. Its port and alias are its registry row's (`hosted()`), not a table here.
"""
from __future__ import annotations

import importlib
from collections.abc import Sequence
from types import ModuleType

NAMES = ("jira", "slack", "confluence",
         "metabase", "prometheus", "alertmanager", "loki", "airbyte", "deploy")
#: Explicit-only: loaded when named, never by `load()` (see the docstring).
PLUG_NAMES = ("powerbi", "tableau", "sigma")


def hosted() -> dict:
    """The registry's hosted backends by name (`backends.Backend` rows with
    `hosted=True`): modules here that load only when named."""
    from ...backends import BACKENDS

    return {name: b for name, b in BACKENDS.items() if b.hosted}


def load(names: Sequence[str] | None = None
         ) -> tuple[dict[str, ModuleType], dict[str, str]]:
    """-> (loaded services by name, import errors by name).

    `names` restricts the set (kept in the given order); None means all NAMES
    - never a PLUG_NAMES service or a hosted backend, which load only when
    named. A name that is none of the three is a ValueError, not a missing
    service.
    """
    if names is None:
        names = NAMES
    known = (*NAMES, *PLUG_NAMES, *hosted())
    unknown = [n for n in names if n not in known]
    if unknown:
        raise ValueError(f"unknown native service(s) {', '.join(map(repr, unknown))}; "
                         f"known: {', '.join(known)}")
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
