"""Measure the shipped box from inside it.

Run this INSIDE the episode image, never against the Dockerfile source::

    docker run --rm -i --entrypoint python alkera-episode:head - < toolbelt_check.py

It executes every binary the toolbelt claims to ship, imports the gateway's tool
table in the interpreter the gateway actually uses, and prints one JSON line. The
rule it exists for: a check that never ran against the thing it ships against
proves nothing. `adobe/s3mock` ignored `initialBuckets` on its pinned digest and the
S3 half of the acceptance script reported green for weeks without ever passing.

`ok` for a binary means all three of: it resolved on PATH, it ran, and it said
something. A binary that resolves but cannot execute (a broken venv shim, a missing
shared library) is a defect this would otherwise miss.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys

# (binary, probe argv, acceptable exit codes). Probes are chosen per tool because
# `--version` is not universal: nc and sftp print usage to stderr and exit non-zero.
PROBES: tuple[tuple[str, tuple[str, ...], tuple[int, ...]], ...] = (
    # shell and base userland (the base image already carries these; verified, not installed)
    ("bash", ("bash", "--version"), (0,)),
    ("sh", ("sh", "-c", "echo sh-ok"), (0,)),
    ("ls", ("ls", "--version"), (0,)),
    ("cat", ("cat", "--version"), (0,)),
    ("sort", ("sort", "--version"), (0,)),
    ("wc", ("wc", "--version"), (0,)),
    ("cut", ("cut", "--version"), (0,)),
    ("xargs", ("xargs", "--version"), (0,)),
    ("find", ("find", "--version"), (0,)),
    ("grep", ("grep", "--version"), (0,)),
    ("sed", ("sed", "--version"), (0,)),
    ("awk", ("awk", "--version"), (0, 2)),
    ("diff", ("diff", "--version"), (0,)),
    ("tar", ("tar", "--version"), (0,)),
    ("gzip", ("gzip", "--version"), (0,)),
    ("df", ("df", "--version"), (0,)),
    ("du", ("du", "--version"), (0,)),
    # installed by the toolbelt layer
    ("git", ("git", "--version"), (0,)),
    ("openssl", ("openssl", "version"), (0,)),
    ("curl", ("curl", "--version"), (0,)),
    ("jq", ("jq", "--version"), (0,)),
    ("less", ("less", "--version"), (0,)),
    ("make", ("make", "--version"), (0,)),
    ("unzip", ("unzip", "-v"), (0,)),
    ("ps", ("ps", "--version"), (0,)),
    ("top", ("top", "-v"), (0, 1)),
    ("tree", ("tree", "--version"), (0,)),
    ("file", ("file", "--version"), (0,)),
    ("vim", ("vim", "--version"), (0,)),
    ("nano", ("nano", "--version"), (0,)),
    ("nc", ("nc", "-h"), (0, 1, 2)),
    ("dig", ("dig", "-v"), (0,)),
    ("nslookup", ("nslookup", "-version"), (0, 1)),
    ("ping", ("ping", "-V"), (0,)),
    ("ssh", ("ssh", "-V"), (0,)),
    ("sftp", ("sftp", "-h"), (0, 1, 255)),
    ("ssh-keygen", ("ssh-keygen", "-h"), (0, 1, 255)),
    ("psql", ("psql", "--version"), (0,)),
    ("pg_dump", ("pg_dump", "--version"), (0,)),
    ("pg_isready", ("pg_isready", "--version"), (0,)),
    # python CLIs in /opt/toolbelt
    ("aws", ("aws", "--version"), (0,)),
    ("sqlfluff", ("sqlfluff", "--version"), (0,)),
    ("csvlook", ("csvlook", "--version"), (0,)),
    ("csvstat", ("csvstat", "--version"), (0,)),
    ("in2csv", ("in2csv", "--version"), (0,)),
    ("csvsql", ("csvsql", "--version"), (0,)),
    # the gateway's own environment
    ("python", ("python", "--version"), (0,)),
    ("pip", ("pip", "--version"), (0,)),
    ("dbt", ("dbt", "--version"), (0,)),
    ("alkera-gateway", ("alkera-gateway", "--help"), (0,)),
)

# The gateway's interpreter must stay the first `python` on PATH. /opt/toolbelt has
# its own, and a venv that shadowed this would break every tool in the image.
EXPECTED_PYTHON = "/usr/local/bin/python"


def probe(binary, argv, ok_codes):
    path = shutil.which(binary)
    row = {"binary": binary, "path": path, "argv": list(argv)}
    if not path:
        return {**row, "ok": False, "reason": "not on PATH"}
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=60, errors="replace"
        )
    except Exception as exc:  # noqa: BLE001 - any failure to execute is the finding
        return {**row, "ok": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}
    text = (done.stdout or "") + (done.stderr or "")
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return {
        **row,
        "exit": done.returncode,
        "first_line": first[:160],
        "ok": done.returncode in ok_codes and bool(first),
        "reason": None
        if done.returncode in ok_codes and first
        else f"exit={done.returncode} output={first[:80]!r}",
    }


def main() -> int:
    binaries = [probe(*spec) for spec in PROBES]
    report = {
        "source_revision": os.environ.get("ALKERA_SOURCE_REV") or None,
        "platform": platform.platform(),
        "os_release": {
            k: v.strip('"')
            for k, v in (
                line.split("=", 1)
                for line in open("/etc/os-release").read().splitlines()
                if "=" in line
            )
        }.get("PRETTY_NAME"),
        "interpreter": sys.executable,
        "python_on_path": shutil.which("python"),
        "python_not_shadowed": shutil.which("python") == EXPECTED_PYTHON,
        "path": os.environ.get("PATH"),
        "binaries": binaries,
        "binaries_ok": sorted(r["binary"] for r in binaries if r["ok"]),
        "binaries_failed": [r for r in binaries if not r["ok"]],
    }
    try:
        from alkera_gateway.tools import TOOLS

        report["tools"] = sorted(TOOLS)
        report["tool_count"] = len(TOOLS)
        report["tools_error"] = None
    except Exception as exc:  # noqa: BLE001
        report["tools"] = []
        report["tool_count"] = 0
        report["tools_error"] = f"{type(exc).__name__}: {exc}"[:300]
    report["passed"] = (
        not report["binaries_failed"]
        and report["python_not_shadowed"]
        and report["tool_count"] > 0
    )
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
