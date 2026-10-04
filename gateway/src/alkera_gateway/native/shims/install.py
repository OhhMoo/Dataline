"""Materialise the PATH-front shims into a bin directory.

    python -m alkera_gateway.native.shims.install --bin /opt/alkera/shims
    export PATH=/opt/alkera/shims:$PATH   ALKERA_SHIM_BIN=/opt/alkera/shims

TWO FILES PER BINARY, AND WHY NOT A `.py` PAIR
----------------------------------------------
The brief allows `.cmd` + `.py` pairs on Windows. A `.py` file would be a third
copy of `run.py`, so what is written instead is:

    <name>.cmd   picked up by cmd.exe and PowerShell through PATHEXT
    <name>       an extensionless `#!/bin/sh` script, which is what Git Bash,
                 WSL and every POSIX shell look for

Both exec `python -m alkera_gateway.native.shims.run <name>`, so the logic lives
in one module and the generated files stay three lines long. On POSIX only the
extensionless file is written and it is made executable.

The interpreter is baked in as the absolute path of the Python running the
install, not as the word `python`: the shim must not depend on the agent's PATH,
which is the thing it is being inserted into.

`install_cli` writes the other kind of native surface: a CLI the WORKSPACE
carries, `./ops/sched` and `./ops/deploy`, invoked by path from the repo with no
real binary behind it. Same two files, and `PYTHONPATH` baked in beside the
interpreter, because such a CLI may need a package the interpreter does not
install (`world/`, for the scheduler) and must not depend on the agent's
environment for its own imports.
"""
from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

# `duckdb`, `dbt`, `python` and `git` are the binaries NATIVE.md leaves
# as real binaries touching the world directly. `psql` and `aws` are
# deliberately absent: their rows come from the server-side tailers, and
# shimming them too would double every statement in the action log.
#
# `python3` is the same interpreter under the name Debian, the episode image
# and the worlds' own scripts use (`PY="${PYTHON:-python3}"` in a theme's
# run.sh). Unshimmed it was a second, unaudited python on PATH: measured
# 2026-10-04 (tasks/harness/bi_e2e_check.py), `python3 -c "...duckdb...
# CREATE TABLE..."` rewrote the warehouse and wrote no row at all. Its rows
# are filed as `shim.python` (run.TOOL_NAMES).
#
# `duckdb` is on this list and not on that one because nothing serves it: the
# 2026-09-16 amendment keeps the warehouse a DuckDB FILE for the existing
# tasks, so there is no statement log to tail and the command line is the only
# record. `shims/duckdb_cli.py` is what reads it.
#
# `tabcmd` (the toolbelt's Tableau CLI) talks to tableau.internal, whose rows
# record what it asked for - but tabcmd 2 also hard-codes a
# RotatingFileHandler("tabcmd.log") in the CURRENT DIRECTORY, so every run from
# the repo writes a file into the workspace. Measured 2026-10-04
# (bi_e2e_check.py): unshimmed, that was a world change no row named. The shim
# row names the log; the service rows stay the record of the Tableau calls.
DEFAULT_BINARIES = ("duckdb", "dbt", "python", "python3", "git", "tabcmd")

CMD_TEMPLATE = """@echo off
"{interpreter}" -m alkera_gateway.native.shims.run {name} %*
"""
SH_TEMPLATE = """#!/bin/sh
exec "{interpreter}" -m alkera_gateway.native.shims.run {name} "$@"
"""

# A CLI the workspace carries rather than a PATH-front wrapper: `./ops/sched`
# and `./ops/deploy` are tools a team writes and commits, so they are invoked by
# path and there is no real binary behind them. Same two files for the same
# reason, and PYTHONPATH is baked in because the module may need a package - the
# world's - that is not installed into the interpreter running it, and a CLI
# that depended on the agent's environment for its own imports would break the
# moment the agent exported something.
CLI_CMD_TEMPLATE = """@echo off
set "PYTHONPATH={pythonpath}"
"{interpreter}" -m {module} %*
"""
CLI_SH_TEMPLATE = """#!/bin/sh
PYTHONPATH="{pythonpath}" exec "{interpreter}" -m {module} "$@"
"""


def write(path: Path, content: str, executable: bool) -> Path:
    """Write one generated wrapper with LF endings, as the repo requires."""
    with open(path, "w", encoding="utf-8", newline="") as stream:
        stream.write(content)
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def install(bin_dir: Path, binaries=DEFAULT_BINARIES, interpreter: str | None = None) -> list[Path]:
    """Create the wrappers and return every file written."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    interpreter = interpreter or sys.executable
    written = []
    for name in binaries:
        written.append(
            write(bin_dir / name, SH_TEMPLATE.format(interpreter=interpreter, name=name), True)
        )
        if os.name == "nt":
            written.append(
                write(
                    bin_dir / f"{name}.cmd",
                    CMD_TEMPLATE.format(interpreter=interpreter, name=name),
                    False,
                )
            )
    return written


def install_cli(path: Path, module: str, interpreter: str | None = None,
                pythonpath: str = "") -> list[Path]:
    """Materialise one workspace CLI at `path`, returning every file written.

    `path` is the extensionless name the repo refers to (`ops/sched`); on
    Windows a `.cmd` beside it is what cmd.exe and PowerShell find through
    PATHEXT. `pythonpath` is the caller's to supply: this package does not know
    where `world/` is and must not learn (agent.md 10.2).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    interpreter = interpreter or sys.executable
    written = [write(path, CLI_SH_TEMPLATE.format(
        interpreter=interpreter, module=module, pythonpath=pythonpath), True)]
    if os.name == "nt":
        written.append(write(path.with_suffix(".cmd"), CLI_CMD_TEMPLATE.format(
            interpreter=interpreter, module=module, pythonpath=pythonpath),
            False))
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bin", required=True, type=Path, help="directory to materialise into")
    parser.add_argument(
        "--binary",
        action="append",
        dest="binaries",
        help="binary to wrap; repeatable, defaults to " + " ".join(DEFAULT_BINARIES),
    )
    arguments = parser.parse_args(argv)
    written = install(arguments.bin, tuple(arguments.binaries or DEFAULT_BINARIES))
    for path in written:
        print(path)
    print(
        f"prepend {arguments.bin} to PATH and set ALKERA_SHIM_BIN={arguments.bin}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
