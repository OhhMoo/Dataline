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

Role-aware. The image's toolbelt role (de/ds/da) is read from `ALKERA_TOOLBELT`, then
`/opt/toolbelt/ROLE`; an image built before roles has neither and is a de image.
ds and da install their libraries into the SYSTEM interpreter (/usr/local/bin/python:
the agent's `python` and the Jupyter kernel's), so that is where they are imported -
together with `alkera_gateway.native.notebook`, the kernel's audit extension. Every
package another role has and this one does not is checked ABSENT - not off PATH,
absent: not importable in either interpreter, no script in /opt/toolbelt/bin or
/usr/local/bin. A PATH mask would pass a `which` check and fail this one, which is
the point.
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
    ("tabcmd", ("tabcmd", "--version"), (0,)),
    # MetricFlow over the dbt project's semantic layer (dbt-metricflow); prints
    # "mf, version 0.15.0" without a project, a profile or a warehouse
    ("mf", ("mf", "--version"), (0,)),
    # the gateway's own environment
    ("python", ("python", "--version"), (0,)),
    ("pip", ("pip", "--version"), (0,)),
    ("dbt", ("dbt", "--version"), (0,)),
    ("alkera-gateway", ("alkera-gateway", "--help"), (0,)),
)

# Per role, on top of PROBES. ds/da libraries are in the system interpreter (the
# Dockerfile's ALKERA_TOOLBELT step), which is also the kernelspec's argv[0].
ROLE_PYTHON = "/usr/local/bin/python"
TOOLBELT_PYTHON = "/opt/toolbelt/bin/python"
KERNEL_NAME = "alkera-python"
_NOTEBOOK = ("pandas", "pyarrow", "matplotlib", "ipykernel", "jupyter_client",
             "jupyter_server", "nbclient", "nbformat", "alkera_gateway.native.notebook")
ROLE_IMPORTS: dict[str, tuple[str, ...]] = {
    "de": (),
    "da": _NOTEBOOK,
    "ds": (*_NOTEBOOK, "polars", "sklearn"),
}
# Console scripts each role's lock installs into /usr/local/bin. `jupyter` is
# jupyter_core's dispatcher: `jupyter server` and `jupyter kernelspec` resolve to the
# jupyter-server / jupyter-kernelspec scripts beside it.
ROLE_BINARIES: dict[str, tuple[str, ...]] = {
    "de": (),
    "da": ("jupyter", "jupyter-server", "jupyter-kernelspec", "jupyter-execute", "ipython"),
    "ds": ("jupyter", "jupyter-server", "jupyter-kernelspec", "jupyter-execute", "ipython"),
}


def _role_probes(imports: tuple[str, ...]):
    return (
        (ROLE_PYTHON, (ROLE_PYTHON, "-c",
                       f"import {', '.join(imports)}; print(pandas.__version__)"), (0,)),
        ("jupyter", ("jupyter", "kernelspec", "list"), (0,)),
        ("jupyter", ("jupyter", "server", "--version"), (0,)),
        ("jupyter-execute", ("jupyter-execute", "--version"), (0,)),
        ("ipython", ("ipython", "--version"), (0,)),
    )


# Libraries a role's SYSTEM interpreter gets that /opt/toolbelt ALSO carries, so they
# are checked against ROLE_PYTHON only. openpyxl is the one: csvkit brings it into the
# venv for `in2csv` on every role, and ds/da put it in the system interpreter for
# `pandas.read_excel`. In ROLE_IMPORTS it would land in `forbidden_imports("de")`,
# which checks BOTH interpreters, and fail every de image on the venv's own copy.
SYSTEM_IMPORTS: dict[str, tuple[str, ...]] = {
    "de": (),
    "da": ("openpyxl",),
    "ds": ("openpyxl",),
}
# The Excel round trip the role's own python must make: write a workbook with
# openpyxl, read it back with pandas.read_excel, and fail (non-zero exit) unless the
# value read back is the value written. Prints it.
EXCEL_ROUNDTRIP = (
    "import io, openpyxl, pandas; wb = openpyxl.Workbook(); ws = wb.active; "
    "ws.append(['metric', 'value']); ws.append(['net_revenue', 2361224.0]); "
    "buf = io.BytesIO(); wb.save(buf); buf.seek(0); "
    "got = pandas.read_excel(buf, engine='openpyxl')['value'].iloc[0]; "
    "assert float(got) == 2361224.0, f'read back {got!r}, wrote 2361224.0'; print(got)")
_EXCEL_PROBE = (ROLE_PYTHON, (ROLE_PYTHON, "-c", EXCEL_ROUNDTRIP), (0,))

ROLE_PROBES: dict[str, tuple[tuple[str, tuple[str, ...], tuple[int, ...]], ...]] = {
    "de": (),
    "da": (*_role_probes(ROLE_IMPORTS["da"]), _EXCEL_PROBE),
    "ds": (*_role_probes(ROLE_IMPORTS["ds"]), _EXCEL_PROBE),
}
ROLES = tuple(ROLE_IMPORTS)


def role_imports(role: str) -> tuple[str, ...]:
    return ROLE_IMPORTS[role]


def forbidden_imports(role: str) -> tuple[str, ...]:
    """Everything any other role imports that this one must not have installed."""
    every = {m for mods in ROLE_IMPORTS.values() for m in mods}
    return tuple(sorted(every - set(ROLE_IMPORTS[role])))


def role_probes(role: str):
    return ROLE_PROBES[role]


def system_imports(role: str) -> tuple[str, ...]:
    return SYSTEM_IMPORTS[role]


def system_forbidden(role: str) -> tuple[str, ...]:
    """SYSTEM_IMPORTS another role has that this role's system interpreter must not:
    absent from ROLE_PYTHON only (the venv's copy is every role's)."""
    every = {m for mods in SYSTEM_IMPORTS.values() for m in mods}
    return tuple(sorted(every - set(SYSTEM_IMPORTS[role])))


def forbidden_binaries(role: str) -> tuple[str, ...]:
    every = {b for bins in ROLE_BINARIES.values() for b in bins}
    return tuple(sorted(every - set(ROLE_BINARIES[role])))


def kernelspecs() -> dict:
    """`jupyter kernelspec list --json`, or the reason it could not be read."""
    try:
        done = subprocess.run(["jupyter", "kernelspec", "list", "--json"],
                              capture_output=True, text=True, timeout=120, errors="replace")
        return {"names": sorted(json.loads(done.stdout)["kernelspecs"]), "reason": None}
    except Exception as exc:  # noqa: BLE001 - the reason is the finding
        return {"names": [], "reason": f"{type(exc).__name__}: {exc}"[:200]}


def toolbelt_role() -> str:
    role = os.environ.get("ALKERA_TOOLBELT", "").strip()
    if not role and os.path.isfile("/opt/toolbelt/ROLE"):
        role = open("/opt/toolbelt/ROLE").read().strip()
    return role or "de"


def importable(interpreter: str, module: str) -> dict:
    """Import, not find_spec: a module that is present and broken is still present
    for the absence check, and still a failure for the presence check."""
    if not os.path.exists(interpreter):
        return {"interpreter": interpreter, "ok": False, "reason": "no such interpreter"}
    done = subprocess.run([interpreter, "-c", f"import {module}"],
                          capture_output=True, text=True, timeout=120, errors="replace")
    err = (done.stderr or "").strip().splitlines()
    return {"interpreter": interpreter, "ok": done.returncode == 0,
            "reason": None if done.returncode == 0 else (err[-1] if err else "")[:200]}


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


def duckdb_extensions(names) -> dict:
    out = {}
    try:
        import hashlib

        import duckdb
    except Exception as exc:  # noqa: BLE001
        return {name: {"loaded": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}
                for name in names}
    for name in names:
        row = {"loaded": False, "installed": False, "install_path": None,
               "version": None, "sha256": None, "reason": None}
        try:
            conn = duckdb.connect(
                ":memory:",
                config={"autoinstall_known_extensions": False,
                        "autoload_known_extensions": False},
            )
            conn.execute(f"LOAD {name}")
            # `LOAD postgres` loads the extension duckdb_extensions() lists as
            # `postgres_scanner` (its canonical name; `postgres` is the alias),
            # so match on the alias list rather than the name asked for.
            found = conn.execute(
                "select extension_name, installed, loaded, install_path, extension_version "
                "from duckdb_extensions() where loaded and (extension_name = ? "
                "or list_contains(aliases, ?))", [name, name]
            ).fetchone()
            conn.close()
            if found:
                row.update(canonical_name=found[0], installed=bool(found[1]),
                           loaded=bool(found[2]), install_path=found[3], version=found[4])
                if found[3] and os.path.isfile(found[3]):
                    with open(found[3], "rb") as fh:
                        row["sha256"] = hashlib.sha256(fh.read()).hexdigest()
        except Exception as exc:  # noqa: BLE001 - the reason is the finding
            row["reason"] = f"{type(exc).__name__}: {exc}"[:200]
        out[name] = row
    return out


def main() -> int:
    role = toolbelt_role()
    if role not in ROLES:
        print(json.dumps({"passed": False, "toolbelt_role": role,
                          "reason": f"unknown toolbelt role {role!r}"}))
        return 1
    binaries = [probe(*spec) for spec in (*PROBES, *role_probes(role))]
    imports = {m: importable(ROLE_PYTHON, m)
               for m in (*role_imports(role), *system_imports(role))}
    # Absent from BOTH interpreters, and no script left behind on disk.
    forbidden_present = sorted(
        {f"import {m} ({py})" for m in forbidden_imports(role)
         for py in (ROLE_PYTHON, TOOLBELT_PYTHON) if importable(py, m)["ok"]}
        | {f"binary {path}" for b in forbidden_binaries(role)
           for path in (shutil.which(b), f"/opt/toolbelt/bin/{b}", f"/usr/local/bin/{b}")
           if path and os.path.exists(path)}
        # The system interpreter only: /opt/toolbelt carries these for every role.
        | {f"import {m} ({ROLE_PYTHON})" for m in system_forbidden(role)
           if importable(ROLE_PYTHON, m)["ok"]}
    )
    report = {
        "toolbelt_role": role,
        "role_imports": imports,
        "role_imports_failed": sorted(m for m, r in imports.items() if not r["ok"]),
        "role_forbidden_present": forbidden_present,
        # ds/da: the Jupyter plug's audited kernel must be the one installed.
        "kernelspecs": kernelspecs() if role_imports(role) else None,
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
    # The DuckDB extensions the image ships, LOADED rather than listed: a
    # dual-warehouse world attaches a Postgres peer with `LOAD postgres` and the
    # episode zone cannot INSTALL anything (no egress), so an extension that is
    # not here at build time is not there at all. Loaded on a connection with
    # autoinstall/autoload off, the way the gateway opens its own.
    report["duckdb_extensions"] = duckdb_extensions(("postgres",))
    report["passed"] = (
        not report["binaries_failed"]
        and not report["role_imports_failed"]
        and not report["role_forbidden_present"]
        and (not role_imports(role) or KERNEL_NAME in report["kernelspecs"]["names"])
        and report["python_not_shadowed"]
        and report["tool_count"] > 0
        and all(row["loaded"] for row in report["duckdb_extensions"].values())
    )
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
