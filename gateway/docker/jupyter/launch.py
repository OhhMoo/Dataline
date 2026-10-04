"""Serve the Jupyter plug: install the audited kernelspec, then exec jupyter-server.

    /usr/local/bin/python /opt/stack/jupyter/launch.py

Run with the ABSOLUTE interpreter (README.md): `install_kernelspec` bakes in
`sys.executable`, and a bare `python` in the episode image is the PATH shim.

Every flag below closes a way to run code or reach a kernel that the cell
audit (`alkera_gateway.native.notebook`) would not see:

- `kernel_spec_manager_class`: `alkera-python` is the ONLY kernel the server
  will start, and its spec is built in memory, not read from a file a cell
  could rewrite. `allowed_kernelspecs` was tried first and is not enough: it
  filters the listing, and `POST /api/kernels {"name": "python3"}` still
  started ipykernel's built-in, unaudited kernel (measured; see
  `AuditedKernelSpecManager`).
- `terminals_enabled=False`: a jupyter terminal is a second bash, with none of
  the harness's BASH_ENV redirect behind it.
- token-less and xsrf-off, because the only route here is episode_net
  (`internal: true`, no published port); a token would be a secret the agent
  has to be handed, and nothing outside the episode can reach the port anyway.
- `root_dir`: the contents API serves the workspace and nothing above it, so
  the warehouse files bound under /world are not writable through it.

ALKERA_JUPYTER_{ROOT,PORT,IP,DATA} override the container defaults; they exist
so this exact launcher can be smoke-tested off-container.

`--strip PATH` (repeatable; compose.native.yml passes the harness's
container_episode.STRIP_PATHS): removed before the server starts. This is a
second container from the episode image, and the harness strips the compose
and containment scripts and the gateway's test suite from the EPISODE container
only, so without this a cell could read them. Passed as arguments rather than
built in, so a smoke run on a developer's host deletes nothing.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

from alkera_gateway.native.notebook import KERNEL_NAME, install_kernelspec


def flags(root: str, port: str, ip: str) -> list[str]:
    return [
        f"--ServerApp.ip={ip}",
        f"--ServerApp.port={port}",
        "--ServerApp.port_retries=0",
        f"--ServerApp.root_dir={root}",
        "--ServerApp.open_browser=False",
        "--ServerApp.allow_root=True",
        "--ServerApp.allow_remote_access=True",
        "--ServerApp.disable_check_xsrf=True",
        "--ServerApp.terminals_enabled=False",
        "--IdentityProvider.token=",
        "--ServerApp.kernel_spec_manager_class="
        "alkera_gateway.native.notebook.AuditedKernelSpecManager",
        f"--MappingKernelManager.default_kernel_name={KERNEL_NAME}",
    ]


def parse(argv: list[str]) -> list[str]:
    parser = argparse.ArgumentParser(prog="launch.py")
    parser.add_argument("--strip", action="append", default=[], metavar="PATH")
    return parser.parse_args(argv).strip


def strip(paths: list[str]) -> list[str]:
    """Remove each path that exists; -> the ones removed, in order."""
    removed = []
    for path in paths:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        elif os.path.lexists(path):
            os.remove(path)
        else:
            continue
        removed.append(path)
    return removed


def main() -> None:
    # This file is already in memory and the server imports nothing from
    # /opt/stack, so the directory holding it can go first.
    for path in strip(parse(sys.argv[1:])):
        print(f"launch.py: stripped {path}", file=sys.stderr, flush=True)
    data = os.environ.get("ALKERA_JUPYTER_DATA") or os.path.join(sys.prefix, "share", "jupyter")
    install_kernelspec(data)
    argv = [sys.executable, "-m", "jupyter_server", *flags(
        os.environ.get("ALKERA_JUPYTER_ROOT", "/workspace"),
        os.environ.get("ALKERA_JUPYTER_PORT", "8888"),
        os.environ.get("ALKERA_JUPYTER_IP", "0.0.0.0"),
    )]
    os.execv(sys.executable, argv)


if __name__ == "__main__":
    main()
