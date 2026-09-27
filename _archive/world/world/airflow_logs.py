"""Make `dag.test()` leave task logs where Airflow's own log reader finds them.

WHY THIS FILE EXISTS (R004 section 7.5, measured again here)
------------------------------------------------------------
`DAG.test()` runs tasks in-process. It never installs the per-task file handler
that a real worker installs, so it writes **no task log files at all** -
measured, both majors:

    --- task log files: 0 []

after a complete, green `vendor_feeds` run. The api-server then falls back to
*served logs*: it asks the worker named in `task_instance.hostname` for the log
over HTTP on port 8793, and on a `--network none` container that host does not
resolve, so `GET /api/v2/.../logs/1` returns a `NameResolutionError` string
instead of a log. "Read the failed task's log" is #3 on R004's frequency list;
returning a DNS error there makes the whole surface a demo.

The fix is entirely on our side and needs no Airflow patch: write the output we
already captured to the path the shipped `log_filename_template` names.
Measured, both majors, identical shape:

    dag_id={dag_id}/run_id={run_id}/task_id={task_id}/attempt={n}.log

under `[logging] base_log_folder` (default `$AIRFLOW_HOME/logs`). The only
difference between 2.10.5 and 3.0.2 is the Jinja default on `try_number`, which
does not affect the rendered path.

HOW THE SPLIT WORKS
-------------------
`dag.test()` brackets every task with two markers on the root logger, and both
majors emit them (2.10.5 `dag.py:4410`, 3.0.2 `dag.py:1276`):

    [DAG TEST] starting task_id=settle_window map_index=-1
    [DAG TEST] end task task_id=settle_window map_index=-1

So `capture()` tees everything written during the run and `split()` cuts it on
those markers. A line before the first marker (DagBag parsing, run creation)
belongs to no task and is dropped rather than guessed at.

THE ATTEMPT NUMBER IS READ BACK, NEVER COUNTED
----------------------------------------------
A task that retried has more than one attempt and the marker stream does not
say which. `write()` therefore takes the try_number from the `task_instance`
rows the run just wrote, which is the metadata DB agreeing with itself. When a
task appears more than once in the marker stream, the segments are matched to
attempts in order, oldest first.

NOTHING HERE IS A SOURCE OF TRUTH
---------------------------------
These files are part of the derived catalogue (`airflow_bridge`'s module
docstring). `var/sched/state.json` remains the single record of what executed;
if a log file and the ledger disagree, the ledger is right and the log is
rebuilt. Nothing here is fingerprinted, and nothing here is read back by the
world.
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import sys
from pathlib import Path

__all__ = ["capture", "split", "write", "log_dir", "log_path"]

_START = re.compile(r"\[DAG TEST\] starting task_id=(\S+?)(?:\s+map_index=(-?\d+))?\s*$")
_END = re.compile(r"\[DAG TEST\] end task task_id=(\S+?)(?:\s+map_index=(-?\d+))?\s*$")


class _Tee:
    """Write to the real stream and to a buffer. Not a replacement for it."""

    def __init__(self, stream, buffer):
        self._stream, self._buffer = stream, buffer

    def write(self, s):
        self._buffer.write(s)
        return self._stream.write(s)

    def flush(self):
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


@contextlib.contextmanager
def capture():
    """Tee stdout and stderr into a buffer for the duration of the block.

    Yields the buffer.

    Most Airflow handlers follow `sys.stdout` dynamically - `RedirectStdHandler`
    resolves it per record and its `stream` is a **read-only property**
    (measured: `AttributeError: property 'stream' of 'RedirectStdHandler'
    object has no setter`), so swapping `sys.stdout` is enough for them and
    assigning to the handler is both unnecessary and fatal. A handler that
    captured a stream object at construction time is rebound instead, and one
    that refuses the assignment is left alone rather than crashing the run.
    """
    import logging

    buf = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    tee_out, tee_err = _Tee(old_out, buf), _Tee(old_err, buf)
    rebound = []
    for h in list(logging.getLogger().handlers):
        try:
            stream = getattr(h, "stream", None)
            if stream is old_out:
                h.stream = tee_out
                rebound.append((h, old_out))
            elif stream is old_err:
                h.stream = tee_err
                rebound.append((h, old_err))
        except (AttributeError, TypeError):
            continue          # read-only property: it follows sys.stdout anyway
    sys.stdout, sys.stderr = tee_out, tee_err
    try:
        yield buf
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        for h, stream in rebound:
            try:
                h.stream = stream
            except (AttributeError, TypeError):
                pass


def _strip_ansi(text):
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def split(text):
    """-> {task_id: [segment, ...]} in the order the segments appeared.

    A task that Airflow started but never ended (a hard crash inside the task
    runner) still yields its segment, running to the next `starting` marker.
    Losing a crashed task's log is exactly the case the log surface exists for.
    """
    out, current, lines = {}, None, []
    for line in _strip_ansi(text).splitlines():
        m = _START.search(line)
        if m:
            if current is not None:
                out.setdefault(current, []).append("\n".join(lines))
            current, lines = m.group(1), []
            continue
        if current is None:
            continue
        lines.append(line)
        m = _END.search(line)
        if m and m.group(1) == current:
            out.setdefault(current, []).append("\n".join(lines))
            current, lines = None, []
    if current is not None:
        out.setdefault(current, []).append("\n".join(lines))
    return out


def log_dir():
    """`[logging] base_log_folder`, or `$AIRFLOW_HOME/logs` if Airflow is not
    importable (so this module can be unit-tested without Airflow)."""
    try:
        from airflow.configuration import conf
        return Path(conf.get("logging", "base_log_folder"))
    except Exception:
        return Path(os.environ.get("AIRFLOW_HOME", "~/airflow")).expanduser() / "logs"


def log_path(dag_id, run_id, task_id, try_number, map_index=-1, base=None):
    """The path the shipped `log_filename_template` renders to.

    Hard-coded rather than rendered through Jinja on purpose: rendering the
    template needs a real TaskInstance, and this runs after the run is over.
    A theme that overrides the template would need this updated, which is why
    `verify()` exists.
    """
    base = Path(base) if base is not None else log_dir()
    parts = [f"dag_id={dag_id}", f"run_id={run_id}", f"task_id={task_id}"]
    if map_index is not None and map_index >= 0:
        parts.append(f"map_index={map_index}")
    return base.joinpath(*parts, f"attempt={try_number}.log")


def verify(base=None):
    """Assert the shipped template still matches `log_path`'s shape.

    Returns the configured template. Raises if the segment names moved, which
    is the one way this module goes quietly wrong across an Airflow upgrade.
    """
    from airflow.configuration import conf
    tpl = conf.get("logging", "log_filename_template")
    for needle in ("dag_id={{ ti.dag_id }}", "run_id={{ ti.run_id }}",
                   "task_id={{ ti.task_id }}", "attempt="):
        if needle not in tpl:
            raise AssertionError(
                "log_filename_template no longer matches airflow_logs.log_path: "
                "%r is missing from %r" % (needle, tpl))
    return tpl


def write(text, dag_id, run_id, attempts, base=None):
    """Write captured output to the per-attempt log files. -> [Path, ...]

    `attempts` is `[(task_id, try_number, map_index), ...]`, read back from the
    `task_instance` table - see the module docstring on why it is not counted
    here. Tasks with no segment (never started: `upstream_failed`, `skipped`)
    are skipped rather than given an empty file, because an empty log file and
    a task that never ran are different things and the API should say so.
    """
    segments = split(text)
    cursor = {}
    written = []
    for task_id, try_number, map_index in sorted(attempts,
                                                 key=lambda a: (a[0], a[1])):
        got = segments.get(task_id) or []
        i = cursor.get(task_id, 0)
        if i >= len(got):
            continue
        cursor[task_id] = i + 1
        path = log_path(dag_id, run_id, task_id, try_number, map_index, base)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(got[i] + "\n", encoding="utf-8", newline="\n")
        written.append(path)
    return written
