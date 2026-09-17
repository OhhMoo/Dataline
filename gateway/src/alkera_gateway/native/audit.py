"""The single action-log row writer for native mode (gateway/NATIVE_SURFACES.md).

Every tailer, shim and fake service calls `record` and nothing else, so the
native transport produces the same agent.md 9.2.2 row the MCP gateway does.

WHY THIS FILE DOES NOT BUILD ITS OWN ROW SHAPE
----------------------------------------------
`Gateway._call` assembles the row the grader, `rollout.py --reclassify` and the
blindness detector read. There are 23 keys in it and two of them are hashes of
canonical JSON. A second hand-written dict would agree with it on the day it
was written and drift on the first key anyone adds. So the hashing, the JSON
coercion and the summary all come from `alkera_gateway.records`, the module the
gateway itself uses, and `ROW_KEYS` below is asserted against a recorded MCP
row in `tests/test_native_audit.py`.

SEQUENCE AND THE WRITER LOCK
----------------------------
9.2.2 rows carry a contiguous `seq`, and `audit.AuditLog` refuses to open a
directory whose sequence has a hole. Native writers are many processes (a
tailer, four shims, eight services), so `seq` cannot be an in-process counter:
it is allocated under `<audit_dir>/writer.lock` from the final complete row in
`actions.jsonl`. This avoids rereading the whole log for every request.

That is deliberately the *same* lock file a live `AuditLog` holds for the whole
episode. An episode serving MCP and native into one directory would interleave
two sequence allocators and corrupt the log; instead the second writer blocks
and then says so. Native mode has no gateway, so the lock is always free.

CONFIGURATION
-------------
ALKERA_NATIVE_AUDIT  the rows file; else `<ALKERA_AUDIT_DIR>/actions.jsonl`;
                     when neither is set `record` writes nowhere and says so,
                     returning the row with `"flags": ["unrecorded"]`.
ALKERA_VTS           the virtual timestamp; else `vnow` from the scheduler
                     state named by ALKERA_SCHEDULER; else ALKERA_VIRTUAL_TIME;
                     else the `GatewayConfig.virtual_time` default.
ALKERA_DISK_PATHS    os.pathsep-separated paths measured for `disk_mb`; unset
                     means `disk_mb: null`, which is the value the gateway
                     itself writes when it cannot stat its own paths.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import psutil
from filelock import FileLock, Timeout

from ..config import GatewayConfig
from ..records import canonical, digest, json_value

OP_CLASSES = ("read", "write", "ddl", "destructive", "exec")
EXITS = ("ok", "error")

# The 9.2.2 key set, exactly as `Gateway._call` writes it. Asserted against a
# recorded MCP row by tests/test_native_audit.py::test_row_keys_match_an_mcp_row.
ROW_KEYS = (
    "args",
    "args_hash",
    "args_summary",
    "disk_mb",
    "error",
    "exit",
    "flags",
    "latency_ms",
    "op_class",
    "read_targets",
    "result",
    "result_bytes",
    "result_hash",
    "rows_affected",
    "rss_mb",
    "schema_version",
    "seq",
    "target",
    "targets",
    "tool",
    "transport",
    "vts",
    "wts",
)

SUMMARY_CHARS = 240
LOCK_TIMEOUT_S = 10.0

_PROCESS_LOCK = threading.Lock()


def audit_path() -> Path | None:
    """The rows file, or None when this process was given no audit directory."""
    explicit = os.environ.get("ALKERA_NATIVE_AUDIT")
    if explicit:
        return Path(explicit)
    audit_dir = os.environ.get("ALKERA_AUDIT_DIR")
    return Path(audit_dir) / "actions.jsonl" if audit_dir else None


def virtual_time() -> str:
    """The episode's virtual clock, read the way `Gateway._virtual_time` reads it."""
    explicit = os.environ.get("ALKERA_VTS")
    if explicit:
        return explicit
    state = os.environ.get("ALKERA_SCHEDULER")
    if state:
        try:
            return json.loads(Path(state).read_text(encoding="utf-8"))["vnow"]
        except (OSError, ValueError, KeyError):
            pass
    return os.environ.get("ALKERA_VIRTUAL_TIME") or GatewayConfig().virtual_time


def disk_mb() -> float | None:
    """Megabytes under ALKERA_DISK_PATHS, or None - the gateway's own fallback."""
    raw = os.environ.get("ALKERA_DISK_PATHS")
    if not raw:
        return None
    total = 0
    try:
        for name in raw.split(os.pathsep):
            path = Path(name)
            if path.is_file():
                total += path.stat().st_size
            elif path.is_dir():
                total += sum(
                    item.stat().st_size
                    for item in path.rglob("*")
                    if item.is_file() and not item.is_symlink()
                )
        return round(total / 1_048_576, 3)
    except OSError:
        return None


def normalize_error(error) -> dict | None:
    """The gateway's error shape - `{"code", "message"}` - or None.

    Callers that have only a message get `native_error`; the alternative is a
    row whose `error` is a bare string, which nothing downstream can switch on.
    """
    if error is None:
        return None
    if isinstance(error, dict):
        return {
            "code": str(error.get("code", "native_error")),
            "message": str(error.get("message", "")),
        }
    return {"code": "native_error", "message": str(error)}


def count_rows(path: Path) -> int:
    """Rows already in the log. Called under the writer lock; never guesses."""
    if not path.exists():
        return 0
    with open(path, "rb") as stream:
        return sum(chunk.count(b"\n") for chunk in iter(lambda: stream.read(1 << 20), b""))


def last_sequence(path: Path) -> int:
    """Read the final row under writer.lock; refuse a torn append.

    Reading backwards bounds the work to the last row even across processes.
    The sequence in that row is authoritative; no separate counter can get
    ahead of a durable append after a crash.
    """
    try:
        stream = path.open("rb")
    except FileNotFoundError:
        return 0
    with stream:
        end = stream.seek(0, os.SEEK_END)
        if not end:
            return 0
        stream.seek(end - 1)
        if stream.read(1) != b"\n":
            raise ValueError("Incomplete native audit row; inspect the log before continuing")
        position = end - 1
        chunks = []
        while position:
            size = min(position, 8192)
            position -= size
            stream.seek(position)
            chunk = stream.read(size)
            boundary = chunk.rfind(b"\n")
            chunks.append(chunk[boundary + 1:])
            if boundary >= 0:
                break
        row = json.loads(b"".join(reversed(chunks)))
        seq = row.get("seq")
        if type(seq) is not int or seq < 1:
            raise ValueError("Invalid native audit sequence")
        return seq


def record(*, tool: str, args: dict, op_class: str, targets: list[str],
           read_targets: list[str] | None = None, exit: str = "ok",
           error: str | dict | None = None, latency_ms: float = 0.0,
           result: object = None, transport: str = "native", actor: str = "",
           rows_affected: int | None = None,
           flags: list[str] | None = None) -> dict:
    """Append one 9.2.2 row and return it.

    tool          "<service>.<operation>", e.g. "psql.statement", "minio.PutObject"
    args          what the agent sent, as the surface saw it (never secrets)
    op_class      one of OP_CLASSES
    targets       what the operation touched ("warehouse.fct_orders", "s3://b/k")
    read_targets  what it read; defaults to `targets` for reads, [] otherwise
    actor         which native writer produced the row; carried as the flag
                  `actor:<actor>`, because 9.2.2 has no `actor` key and an extra
                  key would break the one property this module exists to hold
    rows_affected / flags
                  the two `records.Observation` fields a tailer can know and the
                  original interface stub could not express; both optional, so
                  every call written against that stub still binds
    """
    if op_class not in OP_CLASSES:
        raise ValueError(f"op_class {op_class!r} not in {OP_CLASSES}")
    if exit not in EXITS:
        raise ValueError(f"exit {exit!r} not in {EXITS}")
    normalized_error = normalize_error(error)
    if exit == "error" and normalized_error is None:
        normalized_error = {
            "code": "native_error",
            "message": "The surface reported failure without a message",
        }
    # 9.2.2's invariant, restated: exit is "error" if and only if error is set.
    exit_code = "error" if normalized_error is not None else "ok"

    args = json_value(args)
    result = json_value(result)
    row_flags = list(flags or [])
    if actor:
        row_flags.append(f"actor:{actor}")
    encoded = canonical(result)
    row = {
        "args": args,
        "args_hash": digest(args),
        "args_summary": canonical(args).decode("utf-8")[:SUMMARY_CHARS],
        "disk_mb": disk_mb(),
        "error": normalized_error,
        "exit": exit_code,
        "flags": row_flags,
        "latency_ms": round(float(latency_ms), 3),
        "op_class": op_class,
        "read_targets": list(read_targets) if read_targets is not None
        else (list(targets) if op_class == "read" else []),
        "result": result,
        "result_bytes": len(encoded),
        "result_hash": digest(result),
        "rows_affected": rows_affected,
        "rss_mb": round(psutil.Process().memory_info().rss / 1_048_576, 3),
        "schema_version": 1,
        "seq": 0,
        "target": targets[0] if targets else None,
        "targets": list(targets),
        "tool": tool,
        "transport": transport,
        "vts": virtual_time(),
        "wts": time.time_ns() // 1_000_000,
    }
    path = audit_path()
    if path is None:
        row["flags"].append("unrecorded")
        return row
    with _PROCESS_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(path.parent / "writer.lock")
        try:
            lock.acquire(timeout=LOCK_TIMEOUT_S)
        except Timeout:
            raise RuntimeError(
                f"{path.parent / 'writer.lock'} is held by another writer - an "
                f"MCP gateway is open on this audit directory, so a native row "
                f"would break the 9.2.2 sequence rather than extend it"
            ) from None
        try:
            row["seq"] = last_sequence(path) + 1
            with open(path, "ab") as stream:
                stream.write(canonical(row) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            lock.release()
    return row
