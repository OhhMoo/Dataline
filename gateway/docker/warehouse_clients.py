"""The warehouse client sets the backend registry names, read with the stdlib only.

    python gateway/docker/warehouse_clients.py gateway/src/alkera_gateway
    # -> snowflake

`alkera_gateway.backends.BACKENDS` is the authority for which client sets exist
(a row's `clients`: requirements-<clients>.lock, the `-<clients>` image-tag
suffix). Two readers need that list without the gateway's runtime:
build_episode.py, which runs on a plain `python`, and the Dockerfile's client
loop, which runs before the gateway is installed in the image. Importing
`alkera_gateway.backends` would execute the package `__init__` (pydantic,
duckdb, ...), so the two files the registry is - backends.py and the plugs.py it
imports - are loaded by path into a private package that has no `__init__`.
"""
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

#: The private package the registry files are loaded under: not
#: `alkera_gateway`, so a gateway imported later in the same process is the
#: real one.
PACKAGE = "_alkera_backends_registry"


def load(src: Path):
    """`backends.py` (and the `plugs.py` beside it) from the directory `src`,
    without running `src/__init__.py`."""
    src = Path(src).resolve()
    package = sys.modules.get(PACKAGE)
    if package is None or list(package.__path__) != [str(src)]:
        for name in [n for n in sys.modules if n == PACKAGE or n.startswith(PACKAGE + ".")]:
            del sys.modules[name]
        package = types.ModuleType(PACKAGE)
        package.__path__ = [str(src)]
        package.__package__ = PACKAGE
        sys.modules[PACKAGE] = package
    return importlib.import_module(f"{PACKAGE}.backends")


def names(src: Path) -> tuple[str, ...]:
    """Every client set a registry row names, sorted."""
    return tuple(sorted({b.clients for b in load(src).BACKENDS.values() if b.clients}))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: python warehouse_clients.py <dir holding backends.py>")
    print(" ".join(names(Path(sys.argv[1]))))
