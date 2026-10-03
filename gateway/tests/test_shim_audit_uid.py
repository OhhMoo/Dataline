"""`ALKERA_SHIM_AUDIT_UID`: only the audited user's calls become action rows.

The Harbor image runs the agent as an unprivileged user while Harbor installs
the agent as root through the same PATH, so root's `python -m pip ...` would
otherwise land in the action log as something the agent did. With the variable
set, a shim invoked under any other effective uid runs the real binary exactly
as before - same output, same exit code, same side effects - and writes no row.
Unset, malformed, or on a platform without uids, every call is audited, as it
was before the variable existed.

Every behavioural test drives the installed wrapper as a subprocess, the way
the agent's shell does.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from alkera_gateway.audit import read_rows
from alkera_gateway.native.shims import install as shim_install
from alkera_gateway.native.shims import run as shim_run

pytestmark = pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX uids")

ME = os.geteuid() if hasattr(os, "geteuid") else 0
SOMEONE_ELSE = ME + 4242


@pytest.fixture
def shims(tmp_path, monkeypatch):
    """`python` and `duckdb` wrappers, a private log, ALKERA_SHIM_AUDIT_UID unset."""
    bin_dir = tmp_path / "shims"
    shim_install.install(bin_dir, ("python", "duckdb"))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    log = tmp_path / "audit" / "actions.jsonl"
    monkeypatch.delenv("ALKERA_AUDIT_DIR", raising=False)
    monkeypatch.delenv(shim_run.AUDIT_UID_ENV, raising=False)
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(log))
    monkeypatch.setenv("ALKERA_VTS", "2026-03-05T09:00:00Z")
    monkeypatch.setenv("ALKERA_SHIM_BIN", str(bin_dir))
    monkeypatch.setenv("ALKERA_SHIM_WORKSPACE", str(workspace))
    # The interpreter's own directory behind the shims, so `python` resolves;
    # no `duckdb` binary anywhere on it, so the duckdb shim uses the library.
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), str(Path(sys.executable).parent)]))

    class Shims:
        workspace_dir = workspace

        def rows(self):
            return read_rows(log) if log.exists() else []

        def run(self, name, *args, uid=None, stdin=None):
            env = dict(os.environ)
            if uid is not None:
                env[shim_run.AUDIT_UID_ENV] = str(uid)
            return subprocess.run(
                [str(bin_dir / name), *args], cwd=workspace, env=env, input=stdin,
                capture_output=True, text=True, timeout=120)

    return Shims()


WRITE_NOTE = "import pathlib, sys; pathlib.Path('note.txt').write_text('x'); sys.exit(3)"


def test_unset_audits_every_call(shims):
    done = shims.run("python", "-c", WRITE_NOTE)
    assert done.returncode == 3
    row, = shims.rows()
    assert row["tool"] == "shim.python"
    assert row["targets"] == ["note.txt"]
    assert row["exit"] == "error"


def test_the_audited_uid_is_audited(shims):
    done = shims.run("python", "-c", WRITE_NOTE, uid=ME)
    assert done.returncode == 3
    row, = shims.rows()
    assert row["tool"] == "shim.python" and row["targets"] == ["note.txt"]


def test_another_uid_runs_the_binary_and_leaves_no_row(shims):
    done = shims.run("python", "-c", "print('hello'); " + WRITE_NOTE, uid=SOMEONE_ELSE)
    assert done.returncode == 3
    assert done.stdout == "hello\n"
    assert (shims.workspace_dir / "note.txt").read_text() == "x"
    assert shims.rows() == []


def test_another_uid_missing_binary_is_still_127_and_no_row(shims, monkeypatch):
    monkeypatch.setenv(shim_run.AUDIT_UID_ENV, str(SOMEONE_ELSE))
    assert shim_run.main(["absent-binary-for-uid-test"]) == 127
    assert shims.rows() == []


def test_malformed_value_fails_toward_auditing(shims):
    done = shims.run("python", "-c", "pass", uid="engineer")
    assert done.returncode == 0
    assert [row["tool"] for row in shims.rows()] == ["shim.python"]


@pytest.mark.parametrize("uid, rows", [(None, 1), (ME, 1), (SOMEONE_ELSE, 0)])
def test_duckdb_shim_honours_the_uid(shims, uid, rows):
    done = shims.run("duckdb", "warehouse.duckdb", "-c",
                     "CREATE TABLE t AS SELECT 42 AS x; SELECT x FROM t", uid=uid)
    assert done.returncode == 0, done.stderr
    # The library engine prints every statement's result; the CTAS's row count first.
    assert done.stdout.split() == ["1", "42"]
    written = shims.rows()
    assert len(written) == rows, json.dumps(written, indent=1)
    if rows:
        assert written[0]["tool"] == "duckdb.statement"


def test_duckdb_shim_forwards_stdin_for_another_uid(shims):
    done = shims.run("duckdb", "warehouse.duckdb", uid=SOMEONE_ELSE,
                     stdin="CREATE TABLE s AS SELECT 7 AS y;\nSELECT y FROM s;\n")
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["1", "7"]
    assert shims.rows() == []


@pytest.mark.parametrize("value, euid, expected", [
    (None, 1000, True),
    ("", 1000, True),
    ("1000", 1000, True),
    (" 1000 ", 1000, True),
    ("1000", 0, False),
    ("0", 1000, False),
    ("not-a-uid", 0, True),
])
def test_audit_enabled(monkeypatch, value, euid, expected):
    if value is None:
        monkeypatch.delenv(shim_run.AUDIT_UID_ENV, raising=False)
    else:
        monkeypatch.setenv(shim_run.AUDIT_UID_ENV, value)
    monkeypatch.setattr(os, "geteuid", lambda: euid)
    assert shim_run.audit_enabled() is expected


def test_no_uids_on_this_platform_means_audited(monkeypatch):
    monkeypatch.setenv(shim_run.AUDIT_UID_ENV, "1000")
    monkeypatch.delattr(os, "geteuid")
    assert shim_run.audit_enabled() is True
