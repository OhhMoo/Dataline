"""The `duckdb` shim reads SQL piped or heredoc'd into it, as the CLI does.

Regression for SQL on stdin being silently dropped: `echo "CREATE TABLE ..." |
duckdb warehouse.duckdb` printed nothing, wrote nothing, exited 0 and left a
`no_statements` row, because `duckdb_cli` never read stdin - the library engine
ran an empty command list and the audit had no statement to classify.

Every test drives the installed wrapper as a subprocess, so stdin really is a
pipe (or a heredoc, or a pty) and nothing is simulated inside the process.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import duckdb
import pytest

from alkera_gateway.audit import read_rows
from alkera_gateway.native.shims import install as shim_install

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX wrappers, pipes and ptys")


@pytest.fixture
def shim(tmp_path, monkeypatch):
    """An installed `duckdb` wrapper, a private log, and no real binary on PATH.

    PATH is the shim directory alone (plus `extra_path`), so `real_binary()`
    finds nothing and the library engine runs unless a test puts a fake
    `duckdb` on it. The wrapper's shebang and interpreter are absolute paths.
    """
    bin_dir = tmp_path / "shims"
    shim_install.install(bin_dir, ("duckdb",))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    log = tmp_path / "audit" / "actions.jsonl"
    monkeypatch.delenv("ALKERA_AUDIT_DIR", raising=False)
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(log))
    monkeypatch.setenv("ALKERA_VTS", "2026-03-05T09:00:00Z")
    monkeypatch.setenv("ALKERA_SHIM_BIN", str(bin_dir))
    monkeypatch.setenv("ALKERA_SHIM_WORKSPACE", str(workspace))
    monkeypatch.setenv("PATH", str(bin_dir))

    class Shim:
        wrapper = bin_dir / "duckdb"
        cwd = workspace
        db = workspace / "warehouse.duckdb"

        def rows(self):
            return read_rows(log) if log.exists() else []

        def run(self, *args, stdin=None):
            return subprocess.run(
                [str(self.wrapper), *args], cwd=workspace, input=stdin,
                capture_output=True, text=True, timeout=120,
            )

        def bash(self, script):
            return subprocess.run(
                ["/bin/bash", "-c", script], cwd=workspace,
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=120,
            )

    return Shim()


def tables(db: Path) -> dict[str, list]:
    connection = duckdb.connect(str(db), read_only=True)
    try:
        names = [r[0] for r in connection.execute("SHOW TABLES").fetchall()]
        return {n: connection.execute(f"SELECT * FROM {n} ORDER BY ALL").fetchall()
                for n in names}
    finally:
        connection.close()


def only_row(shim):
    rows = shim.rows()
    assert len(rows) == 1, json.dumps(rows, indent=1)
    return rows[0]


def assert_statement_row(row, *, op_class, targets, sql_contains):
    assert row["tool"] == "duckdb.statement"
    assert row["op_class"] == op_class
    assert row["targets"] == targets
    assert row["args"]["source"] == "stdin"
    assert row["args"]["database"] == "warehouse.duckdb"
    assert sql_contains in row["args"]["sql"]
    assert row["exit"] == "ok"
    assert "no_statements" not in row["flags"]


def test_piped_write_executes_and_is_audited(shim):
    done = shim.run("warehouse.duckdb", stdin="CREATE TABLE t AS SELECT 1 AS x\n")
    assert done.returncode == 0, done.stderr
    assert tables(shim.db) == {"t": [(1,)]}
    assert_statement_row(only_row(shim), op_class="ddl", targets=["t"],
                         sql_contains="CREATE TABLE t")


def test_piped_select_prints_its_result(shim):
    done = shim.run("warehouse.duckdb", stdin="SELECT 42;\n")
    assert done.returncode == 0, done.stderr
    assert "42" in done.stdout
    assert_statement_row(only_row(shim), op_class="read", targets=[],
                         sql_contains="SELECT 42")


def test_echo_pipe_through_a_shell(shim):
    done = shim.bash('echo "CREATE TABLE e AS SELECT 7 AS v" | duckdb warehouse.duckdb'
                     ' && echo "SELECT v * 6 FROM e" | duckdb warehouse.duckdb')
    assert done.returncode == 0, done.stderr
    assert "42" in done.stdout
    rows = shim.rows()
    assert [r["op_class"] for r in rows] == ["ddl", "read"]
    assert rows[0]["targets"] == ["e"]
    assert rows[1]["read_targets"] == ["e"]
    assert all(r["args"]["source"] == "stdin" for r in rows)


def test_heredoc_write_and_select(shim):
    script = textwrap.dedent("""\
        duckdb warehouse.duckdb <<'SQL'
        CREATE TABLE h (id INTEGER);
        INSERT INTO h VALUES (1), (2);
        SQL
        duckdb warehouse.duckdb <<'SQL'
        SELECT 'first', count(*) FROM h;
        SELECT 'second', sum(id) FROM h;
        SQL
        """)
    done = shim.bash(script)
    assert done.returncode == 0, done.stderr
    assert tables(shim.db) == {"h": [(1,), (2,)]}
    # Both SELECTs' results, not only the last statement's.
    assert "first\t2" in done.stdout and "second\t3" in done.stdout
    write, read = shim.rows()
    assert_statement_row(write, op_class="ddl", targets=["h"],
                         sql_contains="INSERT INTO h")
    assert_statement_row(read, op_class="read", targets=["h"],
                         sql_contains="sum(id)")
    assert read["read_targets"] == ["h"]


def test_piped_error_is_reported_and_audited(shim):
    done = shim.run("warehouse.duckdb", stdin="SELECT * FROM missing_table;\n")
    assert done.returncode != 0
    assert "missing_table" in done.stderr
    row = only_row(shim)
    assert row["tool"] == "duckdb.statement" and row["exit"] == "error"
    assert row["args"]["source"] == "stdin"


def test_dash_c_wins_over_stdin(shim):
    """`-c` runs and exits: the CLI never reads stdin, so neither does the audit."""
    done = shim.run("warehouse.duckdb", "-c", "CREATE TABLE c AS SELECT 1 AS x",
                    stdin="CREATE TABLE ignored AS SELECT 1 AS x\n")
    assert done.returncode == 0, done.stderr
    assert set(tables(shim.db)) == {"c"}
    row = only_row(shim)
    assert row["args"]["source"] == "-c" and row["targets"] == ["c"]


def test_dash_cmd_runs_before_stdin(shim):
    """`-cmd` does not exit, so stdin's script runs after it and is audited."""
    done = shim.run("warehouse.duckdb", "-cmd", "CREATE TABLE a AS SELECT 1 AS x",
                    stdin="CREATE TABLE b AS SELECT x + 1 AS x FROM a\n")
    assert done.returncode == 0, done.stderr
    assert tables(shim.db) == {"a": [(1,)], "b": [(2,)]}
    rows = shim.rows()
    assert [(r["args"]["source"], r["targets"]) for r in rows] == [
        ("-c", ["a"]), ("stdin", ["a", "b"])]


def test_empty_stdin_still_leaves_one_row(shim):
    done = shim.run("warehouse.duckdb", stdin="")
    assert done.returncode == 0, done.stderr
    row = only_row(shim)
    assert row["tool"] == "shim.duckdb" and "no_statements" in row["flags"]


def test_stdin_is_forwarded_to_a_real_binary(shim, tmp_path, monkeypatch):
    """With a real `duckdb` on PATH, the bytes read for the audit reach it."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    received = tmp_path / "received.sql"
    fake = real_dir / "duckdb"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"open({str(received)!r}, 'wb').write(sys.stdin.buffer.read())\n"
        "print('fake-duckdb', *sys.argv[1:])\n"
        "sys.exit(3)\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(shim.wrapper.parent), str(real_dir)]))
    sql = "INSERT INTO t VALUES (1);\n"
    done = shim.run("warehouse.duckdb", stdin=sql)
    assert done.returncode == 3
    assert "fake-duckdb warehouse.duckdb" in done.stdout
    assert received.read_text(encoding="utf-8") == sql
    row = only_row(shim)
    assert row["tool"] == "duckdb.statement" and row["args"]["source"] == "stdin"
    assert row["targets"] == ["t"] and row["op_class"] == "write"
    assert row["result"] == {"engine": "binary", "returncode": 3}
    assert "duckdb_library" not in row["flags"]


def test_a_terminal_stdin_is_not_read(shim):
    """Interactive use is unchanged: a tty is left to the CLI, never drained."""
    import pty

    parent, child = pty.openpty()
    try:
        done = subprocess.run([str(shim.wrapper), "warehouse.duckdb"],
                              cwd=shim.cwd, stdin=child, capture_output=True,
                              text=True, timeout=60)
    finally:
        os.close(child)
        os.close(parent)
    assert done.returncode == 0, done.stderr
    row = only_row(shim)
    assert row["tool"] == "shim.duckdb" and "no_statements" in row["flags"]
