"""The Jupyter plug's kernel side: notebook work cannot escape the action log.

In native mode a world change with no audited row is a HARD violation
(gateway/NATIVE.md), and a notebook kernel is a long-lived Python process the
`python` PATH shim never sees: the shim wraps a process, and the kernel is
started once and then runs every cell in-process. So the kernel records its
own rows, one per executed cell, through `native.audit.record`.

Everything here runs a REAL kernel - the `alkera-python` kernelspec installed
into a private JUPYTER_PATH, started by jupyter_client exactly as
jupyter-server starts it - because the property under test is "the kernelspec
the plug serves records cells", and a test that called the hooks by hand would
pass with a kernelspec that never loads them.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

import nbformat
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from jupyter_client.manager import KernelManager
from nbclient import NotebookClient

from alkera_gateway.audit import read_rows, validate_trace
from alkera_gateway.native import audit
from alkera_gateway.native import notebook as plug

KERNEL = "alkera-python"
TIMEOUT_S = 120
CHECKED_IN = Path(__file__).resolve().parents[1] / "docker" / "jupyter" / "kernels" / KERNEL


@pytest.fixture
def episode(tmp_path, monkeypatch):
    """A private JUPYTER_PATH, action log and workspace; the kernel inherits them."""
    jupyter = tmp_path / "jupyter"
    plug.install_kernelspec(jupyter)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log = tmp_path / "audit" / "actions.jsonl"
    monkeypatch.setenv("JUPYTER_PATH", str(jupyter))
    monkeypatch.setenv("JUPYTER_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.delenv("ALKERA_AUDIT_DIR", raising=False)
    monkeypatch.delenv("ALKERA_SHIM_WATCH", raising=False)
    monkeypatch.delenv("ALKERA_WORKSPACE", raising=False)
    monkeypatch.setenv("ALKERA_NATIVE_AUDIT", str(log))
    monkeypatch.setenv("ALKERA_VTS", "2026-03-05T09:00:00Z")
    monkeypatch.setenv("ALKERA_SHIM_WORKSPACE", str(workspace))
    return {"workspace": workspace, "log": log, "jupyter": jupyter}


def run_notebook(workspace: Path, sources: list[str], kernel: str = KERNEL):
    nb = nbformat.v4.new_notebook()
    nb.cells = [nbformat.v4.new_code_cell(source) for source in sources]
    NotebookClient(
        nb, kernel_name=kernel, timeout=TIMEOUT_S, allow_errors=True,
        resources={"metadata": {"path": str(workspace)}},
    ).execute()
    return nb


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_four_cells_four_rows(episode):
    """Compute, write, raise, read: exactly one valid 9.2.2 row per cell, in order."""
    workspace = episode["workspace"]
    pq.write_table(pa.table({"n": [1, 2, 3]}), workspace / "in.parquet")
    sources = [
        "x = sum(range(10))\nx",
        "import pandas as pd\npd.DataFrame({'a': [1, 2]}).to_csv('out.csv', index=False)",
        "1 / 0",
        "import pyarrow.parquet as pq\npq.read_table('in.parquet').num_rows",
    ]
    nb = run_notebook(workspace, sources)
    assert nb.cells[2].outputs[0]["ename"] == "ZeroDivisionError"
    assert (workspace / "out.csv").exists()

    rows = validate_trace(episode["log"])
    assert len(rows) == 4
    assert [row["seq"] for row in rows] == [1, 2, 3, 4]
    for row, source in zip(rows, sources, strict=True):
        assert set(row) == set(audit.ROW_KEYS)
        assert row["tool"] == "jupyter.cell"
        assert row["op_class"] == "exec"
        assert row["transport"] == "native"
        assert "actor:notebook" in row["flags"]
        assert row["args"]["source_sha256"] == sha(source)
        assert row["args"]["source_excerpt"] == source
        assert row["args"]["kernel_id"]
        assert row["latency_ms"] > 0
    assert [row["args"]["execution_count"] for row in rows] == [1, 2, 3, 4]

    compute, write, raised, read = rows
    assert compute["exit"] == "ok" and compute["targets"] == []
    assert write["exit"] == "ok" and write["targets"] == ["out.csv"]
    assert raised["exit"] == "error"
    assert raised["error"]["code"] == "cell_error"
    assert raised["error"]["message"].startswith("ZeroDivisionError")
    assert raised["result"]["ename"] == "ZeroDivisionError"
    assert raised["result"]["evalue"] == "division by zero"
    assert read["exit"] == "ok" and read["targets"] == []


def test_loading_the_extension_again_does_not_double_record(episode):
    """Preloaded, then loaded three more ways from cells: still one row a cell."""
    sources = [
        "from alkera_gateway.native import notebook\n"
        "notebook.load_ipython_extension(get_ipython())",
        "%load_ext alkera_gateway.native.notebook",
        "%reload_ext alkera_gateway.native.notebook",
        "open('after.txt', 'w').write('y')",
    ]
    run_notebook(episode["workspace"], sources)
    rows = validate_trace(episode["log"])
    assert [row["args"]["source_sha256"] for row in rows] == [sha(s) for s in sources]
    # The reload swaps recorders mid-cell, so that cell's row cannot have a
    # before-snapshot and says so instead of claiming nothing changed.
    assert "workspace_unscanned" in rows[2]["flags"]
    assert rows[3]["targets"] == ["after.txt"]


def test_shell_escape_and_cell_magic_are_the_cell(episode):
    """`!cmd` and `%%bash` run in a subprocess; the cell row still names the file."""
    sources = ["!echo hi > bang.txt", "%%bash\necho hi > bash.txt"]
    run_notebook(episode["workspace"], sources)
    rows = read_rows(episode["log"])
    assert [row["targets"] for row in rows] == [["bang.txt"], ["bash.txt"]]


@pytest.fixture
def client(episode):
    manager = KernelManager(kernel_name=KERNEL)
    manager.start_kernel(cwd=str(episode["workspace"]))
    kc = manager.client()
    kc.start_channels()
    kc.wait_for_ready(timeout=TIMEOUT_S)
    try:
        yield kc
    finally:
        kc.stop_channels()
        manager.shutdown_kernel(now=True)


def test_silent_execution_is_still_recorded(client, episode):
    """silent=True skips IPython's run_cell events; it must not skip the log."""
    code = "open('quiet.txt', 'w').write('x')"
    reply = client.execute_interactive(code, silent=True, timeout=TIMEOUT_S)
    assert reply["content"]["status"] == "ok"
    (row,) = read_rows(episode["log"])
    assert row["tool"] == "jupyter.cell"
    assert row["targets"] == ["quiet.txt"]
    assert row["args"]["silent"] is True
    assert row["args"]["source_sha256"] == sha(code)


def test_user_expressions_are_recorded(client, episode):
    """user_expressions run arbitrary code after the cell; they get their own row."""
    expression = "open('expr.txt', 'w').write('y')"
    reply = client.execute_interactive(
        "pass", user_expressions={"e": expression}, timeout=TIMEOUT_S
    )
    assert reply["content"]["user_expressions"]["e"]["status"] == "ok"
    rows = validate_trace(episode["log"])
    assert [row["tool"] for row in rows] == ["jupyter.cell", "jupyter.user_expressions"]
    assert rows[0]["targets"] == []
    assert rows[1]["targets"] == ["expr.txt"]
    assert rows[1]["op_class"] == "exec"


def test_the_debugger_is_refused(client, episode):
    """debugpy `evaluate` would run code no hook sees, so the kernel refuses it."""
    msg = client.session.msg("debug_request", {
        "type": "request", "seq": 1, "command": "initialize",
        "arguments": {"adapterID": "alkera", "clientID": "test"},
    })
    client.control_channel.send(msg)
    reply = client.control_channel.get_msg(timeout=TIMEOUT_S)
    assert reply["content"]["success"] is False
    assert "audit" in reply["content"]["message"]
    assert not episode["log"].exists() or read_rows(episode["log"]) == []


def test_a_kernel_whose_extension_fails_does_not_start(episode):
    """Fail closed: a kernel that cannot record must not run cells at all."""
    spec_dir = episode["jupyter"] / "kernels" / "broken-python"
    spec_dir.mkdir(parents=True)
    spec = json.loads((episode["jupyter"] / "kernels" / KERNEL / "kernel.json").read_text())
    spec["argv"] = [arg.replace(plug.EXTENSION, "alkera_gateway.native.no_such_module")
                    for arg in spec["argv"]]
    (spec_dir / "kernel.json").write_text(json.dumps(spec))
    manager = KernelManager(kernel_name="broken-python")
    manager.start_kernel(cwd=str(episode["workspace"]))
    try:
        deadline = time.monotonic() + 60
        while manager.is_alive() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert not manager.is_alive()
    finally:
        manager.shutdown_kernel(now=True)


def test_kernelspec_matches_the_checked_in_one(tmp_path):
    """docker/jupyter/kernels/alkera-python/kernel.json is generated, not hand-kept."""
    written = plug.install_kernelspec(tmp_path, interpreter="/usr/local/bin/python")
    assert written == tmp_path / "kernels" / KERNEL / "kernel.json"
    assert written.read_bytes() == (CHECKED_IN / "kernel.json").read_bytes()
    spec = json.loads(written.read_text())
    # Absolute: a bare `python` resolves to the PATH shim in the episode image,
    # which would wrap the whole kernel lifetime in one shim.python row.
    assert spec["argv"][0] == "/usr/local/bin/python"
    assert spec["metadata"]["debugger"] is False


def test_install_defaults_to_this_interpreter(tmp_path):
    spec = json.loads(plug.install_kernelspec(tmp_path).read_text())
    assert spec["argv"][0] == sys.executable


def test_install_audited_kernelspecs_writes_both_names_with_identical_bytes(tmp_path):
    """`python3` is the name every client defaults to (nbclient, `jupyter execute`,
    a notebook with no kernelspec metadata), so it is installed as the audited
    spec too, byte for byte, over ipykernel's own."""
    assert plug.AUDITED_KERNEL_NAMES == ("alkera-python", "python3")
    written = plug.install_audited_kernelspecs(tmp_path, interpreter="/usr/local/bin/python")
    assert written == [tmp_path / "kernels" / name / "kernel.json"
                       for name in ("alkera-python", "python3")]
    for path in written:
        assert path.read_bytes() == (CHECKED_IN / "kernel.json").read_bytes()


def test_default_kernel_name_resolves_to_the_audited_spec(tmp_path):
    """jupyter_client resolves `python3` from the data dirs before falling back to
    ipykernel's built-in spec, so the installed file is the one a client starts."""
    from jupyter_client.kernelspec import KernelSpecManager

    plug.install_audited_kernelspecs(tmp_path)
    manager = KernelSpecManager(kernel_dirs=[str(tmp_path / "kernels")])
    assert f"--IPKernelApp.extra_extensions={plug.EXTENSION}" in \
        manager.get_kernel_spec("python3").argv


def test_nbclient_on_python3_writes_one_cell_row(episode):
    """The tour's miss: `NotebookClient(kernel_name="python3")` ran ipykernel's
    own spec and left no row. With the audited pair installed it leaves one."""
    plug.install_audited_kernelspecs(episode["jupyter"])
    nb = run_notebook(episode["workspace"], ["print(6 * 7)"], kernel="python3")
    assert nb.cells[0].outputs[0]["text"] == "42\n"
    rows = validate_trace(episode["log"])
    assert [row["tool"] for row in rows] == ["jupyter.cell"]
    assert rows[0]["args"]["source_sha256"] == sha("print(6 * 7)")


def test_the_server_can_start_no_other_kernel(tmp_path, monkeypatch):
    """`allowed_kernelspecs` only filters the LISTING; jupyter_client still starts
    ipykernel's built-in `python3` by name (measured against jupyter-server 2.21),
    and a cell can write a kernel.json into any data dir. The server's spec
    manager therefore knows one name and builds that spec in memory."""
    from jupyter_client.kernelspec import NoSuchKernel

    monkeypatch.setenv("JUPYTER_PATH", str(tmp_path))
    written = plug.install_kernelspec(tmp_path)
    rogue = tmp_path / "kernels" / "rogue"
    rogue.mkdir()
    (rogue / "kernel.json").write_text(json.dumps(
        {"argv": [sys.executable, "-m", "ipykernel_launcher", "-f", "{connection_file}"],
         "display_name": "rogue", "language": "python"}))
    # Tampering with the installed file changes nothing the server launches.
    written.write_text(written.read_text().replace(plug.EXTENSION, "nothing"))

    manager = plug.AuditedKernelSpecManager()
    assert set(manager.find_kernel_specs()) == {KERNEL}
    assert set(manager.get_all_specs()) == {KERNEL}
    for name in ("python3", "rogue"):
        with pytest.raises(NoSuchKernel):
            manager.get_kernel_spec(name)
    spec = manager.get_kernel_spec(KERNEL)
    assert spec.argv == plug.kernelspec()["argv"]
    assert spec.metadata == {"debugger": False}


def _launcher():
    import importlib.util

    path = Path(__file__).resolve().parent.parent / "docker" / "jupyter" / "launch.py"
    spec = importlib.util.spec_from_file_location("jupyter_launch", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launcher_strips_what_the_episode_strips(tmp_path):
    # The jupyter container is a second container from the episode image; the
    # harness strips /opt/stack and /opt/gateway/tests only from the episode's,
    # so the launcher removes them itself before a cell can run.
    launch = _launcher()
    keep, gone_dir, gone_file = tmp_path / "keep", tmp_path / "stack", tmp_path / "f"
    for d in (keep, gone_dir / "jupyter"):
        d.mkdir(parents=True)
    gone_file.write_text("x")
    removed = launch.strip([str(gone_dir), str(gone_file), str(tmp_path / "absent")])
    assert removed == [str(gone_dir), str(gone_file)]
    assert keep.exists() and not gone_dir.exists() and not gone_file.exists()


def test_launcher_parses_strip_flags():
    launch = _launcher()
    assert launch.parse(["--strip", "/opt/stack", "--strip", "/opt/gateway/tests"]) \
        == ["/opt/stack", "/opt/gateway/tests"]
    assert launch.parse([]) == []
