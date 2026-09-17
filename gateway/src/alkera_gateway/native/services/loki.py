"""Loki-shaped log service over `alkera_gateway.runlogs` (the `logs_*` tools).

WHAT THE LOGS ACTUALLY ARE. Not a log stream: the world records a per-run
output blob, and `RunLogs` turns it into numbered lines and names where each one
came from (`run_output`, `run_result`, `scheduler_state`, `none`). There are no
per-line timestamps anywhere in the world.

SO THE TIMESTAMPS ARE SYNTHESISED, AND SAY SO. Every line of a run is stamped
`started_at` plus the line number in **nanoseconds** - so ordering inside a run
is the file's ordering, ordering between runs is the world's, the line number is
recoverable from the stamp, and no line is ever attributed to a second the world
did not record. A client that treats these as observed instants is reading more
than the world knows; OPS_PLANE.md says so, and nothing else in this package
invents a value.

THE LOGQL SERVED:

    query    := selector [ "|=" '"' text '"' ]
    selector := "{" matcher { "," matcher } "}"
    matcher  := label "=" '"' value '"'
    label    := "job" | "run_id"

NOT parsed, and refused by name: `!=`, `=~`, `!~`, `|~`, `| json`, `| logfmt`,
line formatting, label filters, `unwrap`, metric queries (`rate`, `count_over_time`
and friends - a LogQL metric query returns a matrix and this surface has none).
"""
from __future__ import annotations

import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ...observability import moment
from ..context import ServiceError, served, service_health

NAME = "loki"
HOST = "loki.internal"
PORT = 3100

# The labels a run carries. `status` is deliberately not one: it is known only
# from the run list, never from a line, and a label you cannot filter a line by
# is a label that lies.
LABELS = ("job", "run_id", "source")
SELECTOR_LABELS = ("job", "run_id")
DEFAULT_LIMIT = 100
# The bounds the MCP schema serves (`tools.LogList`, `tools.LogSearch`). The
# native surface stays inside them: a native path that read more than the MCP
# agent can ask for would make the two transports incomparable.
MAX_RUNS = 500
MAX_SEARCH = 100

_QUERY = re.compile(r'^\s*\{(?P<matchers>[^}]*)\}\s*(?:\|=\s*"(?P<filter>[^"]*)"\s*)?$')
_MATCHER = re.compile(r'^\s*(?P<label>[a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"(?P<value>[^"]*)"\s*$')

app = FastAPI(title="Alkera Loki (fake)", version="0.1.0")


@app.exception_handler(ServiceError)
async def _service_error(request: Request, exc: ServiceError) -> JSONResponse:
    """Loki answers a bad query with a plain-text body."""
    return JSONResponse(status_code=exc.status, content=f"{exc.code}: {exc.message}")


def parse(query: str) -> tuple[dict, str]:
    """-> (stream matchers, line filter). Raises `ServiceError`."""
    text = (query or "").strip()
    for token, why in (
        ("=~", "regex matchers"), ("!~", "negative matchers"), ("!=", "negative matchers"),
        ("|~", "regex line filters"), ("| json", "parsers"),
        ("| logfmt", "parsers"), ("| line_format", "line formatting"), ("unwrap", "`unwrap`"),
    ):
        if token in text:
            raise ServiceError(400, "bad_query", f"{why} are not supported by this endpoint")
    matched = _QUERY.match(text)
    if not matched:
        raise ServiceError(
            400, "bad_query",
            f'cannot parse {text!r}; this surface serves {{label="value"}} |= "text" only',
        )
    matchers = {}
    for part in (matched.group("matchers") or "").split(","):
        if not part.strip():
            continue
        matcher = _MATCHER.match(part)
        if not matcher:
            raise ServiceError(400, "bad_query", f"cannot parse matcher {part.strip()!r}")
        if matcher.group("label") not in SELECTOR_LABELS:
            raise ServiceError(
                400, "bad_query",
                f"{matcher.group('label')!r} is not a stream label; "
                f"this surface selects by {' or '.join(SELECTOR_LABELS)}",
            )
        matchers[matcher.group("label")] = matcher.group("value")
    if not matchers:
        raise ServiceError(400, "bad_query", "a stream selector needs at least one matcher")
    return matchers, matched.group("filter") or ""


def stamp(started_at: str | None, line: int) -> str:
    """`started_at` + the line number, in nanoseconds. A run with no start time
    stamps at 0, which reads as 1970 and is unmistakably not an observation."""
    at = moment(started_at) if started_at else None
    base = int(at.timestamp()) * 1_000_000_000 if at else 0
    return str(base + line)


def _runs(request: Request) -> dict:
    """The run list, for the start times a line stamp needs (and for labels).

    An extra audited read rather than a cached one: the service really does have
    to list the runs to stamp a search hit, and hiding that read would put a
    line in a response that no row accounts for.
    """
    listing = served(
        request, NAME, "logs_list",
        lambda ops, obs: ops.logs_list(None, None, MAX_RUNS, obs),
        {"job": None, "status": None, "limit": MAX_RUNS},
    )
    return {run["run_id"]: run for run in listing["runs"]}


def _stream(run: dict, source: str) -> dict:
    labels = {"run_id": run["run_id"], "source": source}
    if run.get("job"):
        labels["job"] = run["job"]
    return labels


def _bounds(start: str | None, end: str | None) -> tuple[int, int]:
    def edge(value: str | None, default: int) -> int:
        if value is None:
            return default
        if value.isdigit():
            return int(value)
        at = moment(value)
        if at is None:
            raise ServiceError(400, "bad_query", f"cannot parse time {value!r}")
        return int(at.timestamp()) * 1_000_000_000

    return edge(start, 0), edge(end, 2**63 - 1)


@app.get("/health")
def health(request: Request) -> object:
    """The log streams this service can serve: one per run that captured
    output. A run with `output_source == "none"` has no lines and therefore no
    stream, so counting it would overstate what can be read."""
    return service_health(
        request, NAME,
        lambda ops, obs: sum(
            1 for run in ops.logs_list(None, None, MAX_RUNS, obs)["runs"]
            if run["output_source"] != "none"
        ),
    )


@app.get("/loki/api/v1/query_range")
def query_range(request: Request, query: str, limit: int = DEFAULT_LIMIT,
                start: str | None = None, end: str | None = None,
                direction: str = "backward") -> dict:
    """One selector, one optional line filter, audited `logs_*` calls.

    `{run_id="..."}`             -> logs_get     (that run's numbered lines)
    `{job="..."} |= "text"`      -> logs_list + logs_search
    `{job="..."}`                -> logs_list + logs_get per run, until `limit`

    The run list is read whenever a hit has to be stamped, because a line's
    timestamp is its run's start time (see the module docstring).
    """
    matchers, needle = parse(query)
    low, high = _bounds(start, end)
    stats: dict = {}
    entries: list[tuple[str, str, dict]] = []
    if "run_id" in matchers:
        run_id = matchers["run_id"]
        result = served(
            request, NAME, "logs_get",
            lambda ops, obs: ops.logs_get(run_id, obs), {"run_id": run_id},
        )
        if matchers.get("job") not in (None, result["job"]):
            return {"status": "success", "data": {"resultType": "streams", "result": [],
                                                  "stats": stats}}
        stream = _stream(result, result["source"])
        for line in result["lines"]:
            if needle.casefold() in line["text"].casefold():
                entries.append((stamp(result["started_at"], line["line"]), line["text"], stream))
        if result["truncated"]:
            stats["alkera_truncated"] = True
        if result["note"]:
            stats["alkera_note"] = result["note"]
    elif needle:
        job = matchers["job"]
        runs = _runs(request)
        cap = max(1, min(limit, MAX_SEARCH))
        found = served(
            request, NAME, "logs_search",
            lambda ops, obs: ops.logs_search(needle, job, cap, obs),
            {"query": needle, "job": job, "limit": cap},
        )
        for match in found["matches"]:
            run = runs.get(match["run_id"], {"run_id": match["run_id"], "job": match["job"]})
            entries.append(
                (stamp(run.get("started_at"), match["line"]), match["text"],
                 _stream(run, match["source"]))
            )
        if found["skipped"]:
            # Not a Loki field. The alternative is dropping the accounting of
            # runs that could not be read, which is how evidence disappears.
            stats["alkera_skipped"] = found["skipped"]
        if found["limit_reached"]:
            stats["alkera_limit_reached"] = True
    else:
        # A selector with no line filter is every line of every run of that job.
        # It is NOT `logs_search("")`: the served MCP schema requires a non-empty
        # query (`tools.LogSearch`), so an empty search would be a native-only
        # capability. Reading the runs is what an MCP agent does here, call for
        # call, and it stops as soon as the client's limit is filled.
        job = matchers["job"]
        for run in _runs(request).values():
            if run["job"] != job:
                continue
            if len(entries) >= limit:
                stats["alkera_limit_reached"] = True
                break
            identifier = run["run_id"]
            result = served(
                request, NAME, "logs_get",
                lambda ops, obs, run=identifier: ops.logs_get(run, obs),
                {"run_id": identifier},
            )
            stream = _stream(result, result["source"])
            entries += [
                (stamp(result["started_at"], line["line"]), line["text"], stream)
                for line in result["lines"]
            ]
    entries = [item for item in entries if low <= int(item[0]) <= high]
    entries.sort(key=lambda item: int(item[0]), reverse=direction != "forward")
    streams: dict[tuple, dict] = {}
    for at, text, labels in entries[:limit]:
        key = tuple(sorted(labels.items()))
        streams.setdefault(key, {"stream": labels, "values": []})["values"].append([at, text])
    return {
        "status": "success",
        "data": {"resultType": "streams", "result": list(streams.values()), "stats": stats},
    }


def values_for(runs, label: str) -> list[str]:
    key = "output_source" if label == "source" else label
    return sorted({run.get(key) for run in runs} - {None})


@app.get("/loki/api/v1/labels")
def labels(request: Request) -> dict:
    """Only the labels some run actually carries - Loki lists a label when a
    stream has it, and reading the run list is what makes that knowable (so the
    request is a real, audited read rather than a constant served for free)."""
    runs = _runs(request).values()
    return {"status": "success", "data": [name for name in LABELS if values_for(runs, name)]}


@app.get("/loki/api/v1/label/{name}/values")
def label_values(name: str, request: Request) -> dict:
    if name not in LABELS:
        return {"status": "success", "data": []}
    return {"status": "success", "data": values_for(_runs(request).values(), name)}
