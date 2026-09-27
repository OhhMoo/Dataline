"""The Airflow api-server access log -> one 9.2.2 row per control-plane call.

WHY THE ACCESS LOG AND NOT THE AIRFLOW DB
-----------------------------------------
`_archive/world/world/airflow_bridge.py` (archived, never wired) is explicit that the Airflow metadata DB is a
derived catalogue: it can be rebuilt from the ledger and is not hashed by the
fingerprint. Reading rows out of it would therefore audit the world's own state,
not the agent's actions, and it would miss every call that read something or
failed. The api-server logs one line per HTTP request it served, which is one
line per thing the agent asked the control plane to do - including the ones it
was refused - so that is what this module reads.

`airflow dags trigger`, a `curl` at the REST API and the Airflow UI all arrive
as the same request, so all three are audited identically and none of them needs
a client wrapper.

TARGET NAMES ARE THE MCP SCHEDULER'S, NOT AIRFLOW'S
---------------------------------------------------
`Operations.sched_trigger` writes `["scheduler:<job_id>", "dbt_project"]` and
`sched_state` writes `["scheduler:<run_id>"]`. This module writes those same
strings with the dag_id and run_id in them rather than the more natural
`dag:<id>`, because target-set parity between an MCP episode and a native one is
the Phase 1 acceptance and it is a set comparison over exactly these strings.

TWO ACCESS-LOG FORMATS
----------------------
Airflow 3's api-server is FastAPI under uvicorn and Airflow 2's webserver is
Flask under gunicorn, so both are parsed: uvicorn's `INFO:  host - "METHOD path
HTTP/1.1" code` and the NCSA combined line gunicorn writes. Both carry method,
path and status, which is all a row needs.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from ..audit import record
from .postgres import follow

ACTOR = "airflow-tailer"

UVICORN = re.compile(
    r'"(?P<method>[A-Z]+) (?P<path>\S+) HTTP/[\d.]+"\s+(?P<status>\d{3})'
)
COMBINED = re.compile(
    r'"(?P<method>[A-Z]+) (?P<path>\S+) HTTP/[\d.]+"\s+(?P<status>\d{3})\s+\S+'
)
METHOD_OP_CLASS = {
    "GET": "read",
    "HEAD": "read",
    "OPTIONS": "read",
    "POST": "write",
    "PUT": "write",
    "PATCH": "write",
    "DELETE": "destructive",
}
FALLBACK_OP_CLASS = "exec"

# The REST surface an agent actually drives, in the order it must be matched:
# the longest path first, so `/dagRuns/{run}` never swallows `/dagRuns`.
# `{dag}` and `{run}` name the capture groups the target template reads.
API = r"/api/v\d+"
ROUTES = (
    ("GET", rf"^{API}/dags/(?P<dag>[^/]+)/dagRuns/(?P<run>[^/]+)/taskInstances/"
             rf"(?P<task>[^/]+)/logs/\d+$", "task_logs", "read", ("scheduler:{run}",)),
    ("GET", rf"^{API}/dags/(?P<dag>[^/]+)/dagRuns/(?P<run>[^/]+)/taskInstances/(?P<task>[^/]+)$",
     "task_get", "read", ("scheduler:{run}",)),
    ("GET", rf"^{API}/dags/(?P<dag>[^/]+)/dagRuns/(?P<run>[^/]+)/taskInstances$",
     "task_list", "read", ("scheduler:{run}",)),
    ("PATCH", rf"^{API}/dags/(?P<dag>[^/]+)/dagRuns/(?P<run>[^/]+)$",
     "dagrun_update", "write", ("scheduler:{run}",)),
    ("DELETE", rf"^{API}/dags/(?P<dag>[^/]+)/dagRuns/(?P<run>[^/]+)$",
     "dagrun_delete", "destructive", ("scheduler:{run}",)),
    ("GET", rf"^{API}/dags/(?P<dag>[^/]+)/dagRuns/(?P<run>[^/]+)$",
     "dagrun_get", "read", ("scheduler:{run}",)),
    ("POST", rf"^{API}/dags/(?P<dag>[^/]+)/dagRuns$",
     "dag_trigger", "exec", ("scheduler:{dag}", "dbt_project")),
    ("GET", rf"^{API}/dags/(?P<dag>[^/]+)/dagRuns$",
     "dagrun_list", "read", ("scheduler:{dag}",)),
    ("POST", rf"^{API}/dags/(?P<dag>[^/]+)/clearTaskInstances$",
     "task_clear", "destructive", ("scheduler:{dag}",)),
    ("PATCH", rf"^{API}/dags/(?P<dag>[^/]+)$", "dag_update", "write", ("scheduler:{dag}",)),
    ("DELETE", rf"^{API}/dags/(?P<dag>[^/]+)$", "dag_delete", "destructive",
     ("scheduler:{dag}",)),
    ("GET", rf"^{API}/dags/(?P<dag>[^/]+)$", "dag_get", "read", ("scheduler:{dag}",)),
    ("GET", rf"^{API}/dags$", "dag_list", "read", ("scheduler",)),
    ("GET", rf"^{API}/dagSources/.*$", "dag_source", "read", ("scheduler",)),
    ("GET", rf"^{API}/eventLogs.*$", "event_log", "read", ("scheduler",)),
)
COMPILED = tuple(
    (method, re.compile(pattern), operation, op_class, targets)
    for method, pattern, operation, op_class, targets in ROUTES
)


@dataclass
class Request:
    """One served HTTP request: what the api-server was asked, and what it answered."""

    method: str
    path: str
    status: int


def iter_requests(lines: Iterable[str]) -> Iterator[Request]:
    """Access-log lines -> requests, in either of the two server formats."""
    for raw in lines:
        match = UVICORN.search(raw) or COMBINED.search(raw)
        if match is None:
            continue
        yield Request(match.group("method"), match.group("path"), int(match.group("status")))


def route(request: Request) -> tuple[str, str, list[str], list[str]]:
    """A request -> (operation, op_class, targets, flags)."""
    path = request.path.split("?", 1)[0].rstrip("/") or "/"
    for method, pattern, operation, op_class, templates in COMPILED:
        if method != request.method:
            continue
        found = pattern.match(path)
        if found is None:
            continue
        values = found.groupdict()
        return operation, op_class, [name.format(**values) for name in templates], []
    return (
        "request",
        METHOD_OP_CLASS.get(request.method, FALLBACK_OP_CLASS),
        ["scheduler"],
        ["unrouted_path"],
    )


def emit(request: Request) -> dict | None:
    """One served request -> one 9.2.2 row, or None for traffic outside the API."""
    if not re.match(rf"^{API}(/|$)", request.path):
        return None
    operation, op_class, targets, flags = route(request)
    failed = not (200 <= request.status < 400)
    return record(
        tool=f"airflow.{operation}",
        args={"method": request.method, "path": request.path},
        op_class=op_class,
        targets=targets,
        read_targets=targets if op_class == "read" else [],
        exit="error" if failed else "ok",
        error={"code": f"http_{request.status}", "message": "Airflow refused the request"}
        if failed
        else None,
        result={"status": request.status},
        flags=flags,
        actor=ACTOR,
    )


def run(lines: Iterable[str]) -> list[dict]:
    """Drain a stream of access-log lines, returning every row written."""
    return [row for row in (emit(request) for request in iter_requests(lines)) if row is not None]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "log",
        nargs="?",
        help="access log to follow; omitted means read stdin to EOF "
        "(`docker logs -f airflow 2>&1 | python -m ...`)",
    )
    arguments = parser.parse_args(argv)
    if arguments.log:
        for request in iter_requests(follow(Path(arguments.log))):
            emit(request)
        return 0
    rows = run(sys.stdin)
    print(f"{len(rows)} rows", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
