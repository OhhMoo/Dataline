"""The Jupyter plug's kernel side: one 9.2.2 row per executed cell.

    %load_ext alkera_gateway.native.notebook        (or, as served, preloaded
                                                      by the `alkera-python`
                                                      kernelspec below)

WHY THE KERNEL RECORDS ITSELF
-----------------------------
The `python` PATH shim (shims/run.py) wraps a PROCESS: argv, exit, the files it
changed. A notebook kernel is one process that lives for the whole episode and
runs every cell in-process, so behind the shim it would be one row written at
shutdown naming everything the notebook ever touched - and in native mode a
world change with no audited row is a HARD violation (gateway/NATIVE.md). So the
row is written where the cell runs: IPython's execution events, around every
cell, calling `native.audit.record` and nothing else.

WHAT A ROW SAYS
---------------
tool       "jupyter.cell" ("jupyter.user_expressions", see below)
args       source_sha256 (the whole source), source_excerpt (first
           SOURCE_EXCERPT_CHARS), source_chars, source_truncated,
           execution_count, silent, cell_id, msg_id, kernel_id, notebook.
           `notebook` is JPY_SESSION_NAME, which jupyter-server sets per kernel
           to the notebook's path; null under a bare client.
op_class   "exec", as the python shim classifies a script: a cell is arbitrary
           code, and only the diff below says what it did.
targets    the shim's own answer - sha256 snapshot of the workspace before and
           after, `shims.run.snapshot` / `changed`, the same root
           (ALKERA_SHIM_WORKSPACE, else ALKERA_WORKSPACE, else the cwd), the same
           ALKERA_SHIM_MAX_FILES ceiling and `workspace_unscanned` flag, and the
           same ALKERA_SHIM_WATCH warehouse files. Reused rather than copied, so
           a file a cell edits and one `python fix.py` edits name the same target.
exit       "error" when the cell raised (or failed to compile); the error is
           `{"code": "cell_error", "message": "<ename>: <evalue>"}` and `result`
           carries `ename` / `evalue` separately.
result     execution_count, changed, watched, ename/evalue, and `rows_during`:
           the seq range other writers appended while the cell ran (below).

THE EVENT PROTOCOL, AND THE FOUR WAYS IT IS NOT ONE-TO-ONE
----------------------------------------------------------
IPython fires `pre_execute` / `post_execute` around EVERY run and
`pre_run_cell` / `post_run_cell` only around non-silent ones, and ipykernel fires
the post pair itself on its async path. The recorder keys on those, per thread
(ipykernel 7 subshells run cells concurrently on separate threads):

- silent=True (an execute_request any client may send) fires no `*_run_cell`
  at all. A recorder keyed on `post_run_cell` alone would let a client run
  arbitrary code with no row, so the span opens on `pre_execute` and a span
  that never saw `pre_run_cell` is written on `post_execute`, its source read
  from the request ipykernel is handling.
- a cell that calls `get_ipython().run_cell(...)` nests a second run. Only the
  outermost run writes: the inner one is part of the cell, and its changes are
  inside the outer snapshot pair.
- an empty cell fires the post pair with no pre pair. Nothing ran; no row.
- a cell during which the extension was (re)loaded has a `post_run_cell` and
  no span. It still ran, so it still gets its row, with `targets: []` and
  `workspace_unscanned` - the shim's way of refusing to claim "nothing changed".

`!cmd`, `%%bash`, `%run`: RECORDED AS THE CELL, AND A SHIMMED BINARY TWICE
--------------------------------------------------------------------------
A shell escape is part of the cell; its file changes are in the cell's diff.
When it runs a shimmed binary - `!duckdb -c "update ..."`, `!dbt run`,
`!python fix.py` - the PATH shim (the kernel inherits the server's PATH) writes
its own row as well. TWO ROWS, BY DESIGN: the shim row is the only one with the
binary's resolution (duckdb's table targets through `sql.inspect_sql`), which
the cell row cannot have, and the cell row is the only one that says the
notebook did it. Dropping either loses information; deduplicating would need a
cross-process handshake in the hot path of every shim. The same holds for a
cell that calls a fake service over HTTP (`requests.get(JIRA_URL)`): the service
writes its row, the cell writes its own. So the cell row's `result.rows_during`
is the `[first, last]` seq range appended to the log while the cell ran, which
lets a reader fold those rows under their cell. It is a range, not a claim of
causation: the agent's bash may write rows concurrently and they land in it too.

TWO OTHER WAYS CODE RUNS IN A KERNEL, CLOSED HERE
-------------------------------------------------
- `user_expressions` in an execute_request are `eval`ed after the cell, outside
  every IPython event. Wrapped: a non-empty set gets one
  "jupyter.user_expressions" row with the same snapshot diff.
- the debugger (debug_request over the control channel) `evaluate`s arbitrary
  code through debugpy, also outside every event. Refused, and the kernelspec
  advertises `"debugger": false` so a frontend never offers it.

KNOWN GAP: a thread or subprocess a cell starts and leaves running can write
after the cell's after-snapshot. Those writes land between cells and no row
names them. Attributing between-cell drift to the kernel would be worse - the
agent's bash writes the same workspace, and its shimmed rows would be doubled.

THE KERNELSPEC FAILS CLOSED. IPython's default on a failing extension is to log
a warning and start the kernel anyway - which here is a kernel running cells
with no row. `install_kernelspec` writes `reraise_ipython_extension_failures`,
so a kernel that cannot record does not start.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

from jupyter_client.kernelspec import KernelSpecManager, NoSuchKernel

from .audit import audit_path, last_sequence, record
from .shims.run import (
    DEFAULT_MAX_FILES,
    changed,
    snapshot,
    watch_signature,
    watched_changes,
    watched_paths,
    workspace_root,
)

EXTENSION = __name__
ACTOR = "notebook"
KERNEL_NAME = "alkera-python"
# Every name an image installs the audited spec under (install_audited_kernelspecs):
# ours, and `python3`, the name a client starts when it is not told otherwise.
AUDITED_KERNEL_NAMES: tuple[str, ...] = (KERNEL_NAME, "python3")
DISPLAY_NAME = "Python 3 (alkera)"
SOURCE_EXCERPT_CHARS = 2000
# The attribute that marks a shell as already recording. On the shell, not in
# this module: `%reload_ext` re-executes this module, and a module-level flag
# would be reset while the old recorder's callbacks were still registered.
SHELL_ATTRIBUTE = "_alkera_cell_recorder"
DEBUGGER_REFUSAL = (
    "The debugger is disabled in this kernel: debugpy evaluates code outside the "
    "cell audit, so it would change the world with no action-log row."
)


def source_args(source: str) -> dict:
    return {
        "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "source_excerpt": source[:SOURCE_EXCERPT_CHARS],
        "source_chars": len(source),
        "source_truncated": len(source) > SOURCE_EXCERPT_CHARS,
    }


def kernel_id() -> str | None:
    """The id jupyter-server knows this kernel by: its connection file's stem."""
    try:
        from ipykernel.connect import get_connection_file

        name = Path(get_connection_file()).name
    except Exception:  # noqa: BLE001 - outside an ipykernel there is no id
        return None
    return name.removeprefix("kernel-").removesuffix(".json")


def log_sequence() -> int | None:
    """The last seq in the action log, read without the lock; None if unreadable.

    Only for `rows_during`, which is advisory, so a torn final row (a writer
    mid-append) reads as unknown rather than failing the cell's own row.
    """
    path = audit_path()
    if path is None:
        return None
    try:
        return last_sequence(path)
    except (OSError, ValueError):
        return None


class Span:
    """One cell's before-state: snapshot, watched signatures, clock, log position."""

    def __init__(self):
        self.root = workspace_root()
        self.max_files = int(os.environ.get("ALKERA_SHIM_MAX_FILES") or DEFAULT_MAX_FILES)
        self.watch = watched_paths()
        self.before = snapshot(self.root, self.max_files)
        self.watch_before = watch_signature(self.watch)
        self.seq_before = log_sequence()
        self.started = time.monotonic()
        self.info = None  # IPython's ExecutionInfo, once pre_run_cell names it

    def finish(self) -> tuple[list[str], list[str], dict]:
        """targets, flags and the result fields every row of this span carries."""
        latency = (time.monotonic() - self.started) * 1000
        after = snapshot(self.root, self.max_files) if self.before is not None else None
        moved = watched_changes(self.watch_before, watch_signature(self.watch))
        flags: list[str] = []
        if self.before is None or after is None:
            targets: list[str] = []
            flags.append("workspace_unscanned")
        else:
            targets = changed(self.before, after)
        targets = targets + [path for path in moved if path not in targets]
        seq_after = log_sequence()
        rows_during = None
        if self.seq_before is not None and seq_after is not None and seq_after > self.seq_before:
            rows_during = [self.seq_before + 1, seq_after]
        return targets, flags, {
            "changed": targets, "watched": self.watch, "rows_during": rows_during,
            "latency_ms": latency,
        }


class CellRecorder:
    """The four IPython callbacks, plus the two non-event code paths."""

    def __init__(self, shell):
        self.shell = shell
        self.local = threading.local()
        self.kernel_id = kernel_id()
        self.saved_debug = None

    # -- per-thread state ------------------------------------------------------

    @property
    def depth(self) -> int:
        return getattr(self.local, "depth", 0)

    @depth.setter
    def depth(self, value: int):
        self.local.depth = value

    @property
    def span(self) -> Span | None:
        return getattr(self.local, "span", None)

    @span.setter
    def span(self, value: Span | None):
        self.local.span = value

    # -- the request ipykernel is handling -------------------------------------

    def request(self) -> dict:
        parent = getattr(self.shell, "parent_header", None)
        return parent if isinstance(parent, dict) else {}

    def identity(self, cell_id=None) -> dict:
        parent = self.request()
        header = parent.get("header") or {}
        metadata = parent.get("metadata") or {}
        return {
            "cell_id": cell_id or metadata.get("cellId"),
            "msg_id": header.get("msg_id"),
            "kernel_id": self.kernel_id,
            "notebook": os.environ.get("JPY_SESSION_NAME"),
        }

    # -- IPython events --------------------------------------------------------

    def pre_execute(self):
        self.depth += 1
        if self.depth == 1:
            self.span = Span()

    def pre_run_cell(self, info):
        if self.depth == 1 and self.span is not None and self.span.info is None:
            self.span.info = info

    def post_execute(self):
        if self.depth == 0:
            return  # an empty cell: IPython returned before pre_execute
        self.depth -= 1
        span = self.span
        if self.depth == 0 and span is not None and span.info is None:
            # silent=True: no post_run_cell is coming, so this is the only
            # chance to write the row. The source is the request's own code.
            self.span = None
            code = (self.request().get("content") or {}).get("code") or ""
            self.write_cell(span, code, execution_count=None, silent=True,
                            cell_id=None, error=None)

    def post_run_cell(self, result):
        if self.depth != 0:
            return  # a nested run_cell; the outer cell's row covers it
        span, self.span = self.span, None
        info = getattr(result, "info", None)
        source = getattr(info, "raw_cell", None) or ""
        if span is None and not source.strip():
            return  # empty cell, nothing ran
        error = getattr(result, "error_before_exec", None) or getattr(
            result, "error_in_exec", None)
        self.write_cell(span, source, execution_count=getattr(result, "execution_count", None),
                        silent=False, cell_id=getattr(info, "cell_id", None), error=error)

    def write_cell(self, span: Span | None, source: str, *, execution_count, silent,
                   cell_id, error):
        if span is None:
            # Loaded mid-cell: no before-state exists. Say so, never "nothing".
            targets, flags, extra = [], ["workspace_unscanned"], {
                "changed": [], "watched": watched_paths(), "rows_during": None,
                "latency_ms": 0.0}
        else:
            targets, flags, extra = span.finish()
        args = {**source_args(source), "execution_count": execution_count,
                "silent": silent, **self.identity(cell_id)}
        latency = extra.pop("latency_ms")
        result = {"execution_count": execution_count, **extra}
        row_error = None
        if error is not None:
            ename, evalue = type(error).__name__, str(error)
            result.update(ename=ename, evalue=evalue)
            row_error = {"code": "cell_error", "message": f"{ename}: {evalue}"}
        record(tool="jupyter.cell", args=args, op_class="exec", targets=targets,
               exit="error" if row_error else "ok", error=row_error,
               latency_ms=latency, result=result, flags=flags, actor=ACTOR)

    # -- user_expressions ------------------------------------------------------

    def user_expressions(self, expressions):
        original = type(self.shell).user_expressions
        if not expressions:
            return original(self.shell, expressions)
        span = Span()
        out = original(self.shell, expressions)
        targets, flags, extra = span.finish()
        latency = extra.pop("latency_ms")
        failed = sorted(name for name, value in out.items()
                        if isinstance(value, dict) and value.get("status") == "error")
        source = json.dumps(expressions, sort_keys=True)
        record(tool="jupyter.user_expressions",
               args={**source_args(source), "names": sorted(expressions),
                     **self.identity()},
               op_class="exec", targets=targets,
               exit="error" if failed else "ok",
               error={"code": "expression_error",
                      "message": f"user_expressions failed: {', '.join(failed)}"}
               if failed else None,
               latency_ms=latency, result={"failed": failed, **extra},
               flags=flags, actor=ACTOR)
        return out

    # -- registration ----------------------------------------------------------

    CALLBACKS = ("pre_execute", "pre_run_cell", "post_execute", "post_run_cell")

    def register(self):
        for event in self.CALLBACKS:
            self.shell.events.register(event, getattr(self, event))
        # An instance attribute shadows the class method for ipykernel's call.
        self.shell.user_expressions = self.user_expressions
        kernel = getattr(self.shell, "kernel", None)
        if kernel is not None and hasattr(kernel, "do_debug_request"):
            self.saved_debug = kernel.__dict__.get("do_debug_request")
            kernel.do_debug_request = refuse_debug_request

    def unregister(self):
        for event in self.CALLBACKS:
            try:
                self.shell.events.unregister(event, getattr(self, event))
            except ValueError:
                pass
        self.shell.__dict__.pop("user_expressions", None)
        kernel = getattr(self.shell, "kernel", None)
        if kernel is not None and kernel.__dict__.get("do_debug_request") is refuse_debug_request:
            if self.saved_debug is None:
                del kernel.do_debug_request
            else:
                kernel.do_debug_request = self.saved_debug


async def refuse_debug_request(msg):
    return {
        "type": "response",
        "request_seq": msg.get("seq"),
        "success": False,
        "command": msg.get("command"),
        "message": DEBUGGER_REFUSAL,
    }


def load_ipython_extension(ip):
    """Register the recorder once per shell, however many times this is called."""
    if getattr(ip, SHELL_ATTRIBUTE, None) is not None:
        return
    recorder = CellRecorder(ip)
    recorder.register()
    setattr(ip, SHELL_ATTRIBUTE, recorder)


def unload_ipython_extension(ip):
    recorder = getattr(ip, SHELL_ATTRIBUTE, None)
    if recorder is None:
        return
    recorder.unregister()
    setattr(ip, SHELL_ATTRIBUTE, None)


# ---------------------------------------------------------------------------
# the kernelspec


def kernelspec(interpreter: str | None = None) -> dict:
    """The `alkera-python` kernel.json.

    The interpreter is ABSOLUTE, for the reason compose.native.yml's healthcheck
    is: in the episode image `/opt/alkera/shims` is first on PATH and `python`
    there is the audited shim, which would wrap the kernel's whole lifetime in a
    single `shim.python` row.

    `extra_extensions` rather than `extensions`, so a site config that sets
    `extensions` is added to rather than replaced; and
    `reraise_ipython_extension_failures` so the kernel fails closed (module
    docstring). Both are plain traitlets CLI assignments, which traitlets 5
    parses a single-string value of into a one-element List.
    """
    return {
        "argv": [
            interpreter or sys.executable,
            "-m", "ipykernel_launcher",
            "-f", "{connection_file}",
            f"--IPKernelApp.extra_extensions={EXTENSION}",
            "--IPKernelApp.reraise_ipython_extension_failures=True",
        ],
        "display_name": DISPLAY_NAME,
        "language": "python",
        "interrupt_mode": "signal",
        "metadata": {"debugger": False},
    }


def install_kernelspec(directory, *, interpreter: str | None = None,
                       name: str = KERNEL_NAME) -> Path:
    """Write `<directory>/kernels/<name>/kernel.json` and return its path.

    `directory` is a Jupyter data directory - an entry of JUPYTER_PATH, or
    `<sys.prefix>/share/jupyter` - which is where jupyter_client looks for
    `kernels/<name>`. Deterministic bytes (sorted keys, LF), so the checked-in
    docker/jupyter/kernels/alkera-python/kernel.json can be asserted equal to it.
    """
    target = Path(directory) / "kernels" / name
    target.mkdir(parents=True, exist_ok=True)
    path = target / "kernel.json"
    with open(path, "w", encoding="utf-8", newline="") as stream:
        stream.write(json.dumps(kernelspec(interpreter), indent=1, sort_keys=True) + "\n")
    return path


def install_audited_kernelspecs(directory, *, interpreter: str | None = None) -> list[Path]:
    """`install_kernelspec` once per AUDITED_KERNEL_NAMES entry, identical bytes.

    `python3` is jupyter_client's NATIVE_KERNEL_NAME and the default of nbclient,
    `jupyter execute` and a notebook with no kernelspec metadata. Unless it is
    installed here, a client gets ipykernel's own spec - the
    `share/jupyter/kernels/python3` its wheel ships, or with none on disk its
    built-in fallback - and neither preloads anything. Measured in
    alkera-episode:head-da (the wheel's file, /usr/local/share/jupyter):
    nbclient on `python3` wrote 0 `jupyter.cell` rows where `alkera-python`
    wrote 1. Installed into `<sys.prefix>/share/jupyter`, this replaces that file.

    This covers the data dirs it is given, nothing more. A kernel.json the agent
    writes into `~/.local/share/jupyter/kernels/` is searched first and is
    honoured by any client in the box: the in-box kernel is best-effort, and the
    server's AuditedKernelSpecManager (below) is the guarantee.
    """
    return [install_kernelspec(directory, interpreter=interpreter, name=name)
            for name in AUDITED_KERNEL_NAMES]


class AuditedKernelSpecManager(KernelSpecManager):
    """The server's spec manager: one kernel, built from code, never from disk.

    `KernelSpecManager.allowed_kernelspecs` filters the LISTING only. Measured
    against jupyter-server 2.21: with it set to `alkera-python`, `POST
    /api/kernels {"name": "python3"}` still started ipykernel's built-in spec,
    a kernel with no extension and so no rows. And any kernel.json a cell writes
    into a Jupyter data dir (`~/.local/share/jupyter/kernels/x`, or over ours)
    would be honoured by the next kernel start. So this manager knows exactly
    one name, and builds that spec from `kernelspec()` in memory; the installed
    file is only where the resource dir (logos) lives.
    """

    def find_kernel_specs(self) -> dict[str, str]:
        return {KERNEL_NAME: self.resource_dir()}

    def resource_dir(self) -> str:
        found = self._find_spec_directory(KERNEL_NAME)
        return found or ""

    def get_kernel_spec(self, kernel_name: str):
        if kernel_name.lower() != KERNEL_NAME:
            raise NoSuchKernel(kernel_name)
        return self.kernel_spec_class(resource_dir=self.resource_dir(), **kernelspec())
