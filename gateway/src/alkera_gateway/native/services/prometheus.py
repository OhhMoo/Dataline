"""Prometheus-shaped metrics service over `alkera_gateway.observability`.

WHAT IS AND IS NOT A PROMQL ENGINE. This serves a deliberately small subset of
PromQL and refuses everything else by name rather than guessing:

    query       := aggregation | selector
    aggregation := ( "sum" | "rate" ) "(" selector [ "[" duration "]" ] ")"
    selector    := metric_name [ "{" matcher { "," matcher } "}" ]
                 | "{" matcher { "," matcher } "}"          (needs __name__)
    matcher     := label "=" '"' value '"'
    duration    := number ( "s" | "m" | "h" | "d" | "w" )

NOT parsed, and answered with `errorType: bad_data` naming the construct:
`!=`, `=~`, `!~`, `by`/`without`/`ignoring`/`on`, arithmetic and comparison
operators, `offset`, `@`, subqueries, nested aggregations, every other function
(`irate`, `increase`, `avg`, `histogram_quantile`, ...), and `[d]` on anything
but `rate`. A query this grammar cannot express is a refusal, never a silently
different answer - that failure mode is the one this benchmark measures.

WHY THE LABEL SET IS TINY. A metric here is a definition in the world's
`observability.yaml`, not a scraped time series, so the only labels that exist
are the ones the definition carries: `__name__`, `kind`, `unit` and, for job
metrics, `job`. There is no instance, no scrape job, no histogram.

ALERTING RULES LIVE HERE TOO. `/api/v1/rules` and `/api/v1/alerts` are served
because a real Alertmanager holds only *firing* alerts: an alarm that is `ok`,
`pending` or - the sharp one - `insufficient_data` is invisible there, and a
native agent that could not see those would be strictly blinder than the MCP
agent reading `alarms_list`. Prometheus is where a real operator reads rule
state, so that is where it is served. `insufficient_data` maps to Prometheus's
own `health: "unknown"` with `lastError`, which is exactly what it means.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ...observability import moment, parse_vts
from ..context import ServiceError, audit_unrouted, context_of, served, service_health

NAME = "prometheus"
HOST = "prometheus.internal"
PORT = 9090

# The labels a definition can carry. Anything else in a matcher can only match
# the empty string, which is Prometheus's own rule for an absent label.
LABELS = ("__name__", "kind", "unit", "job")
# `tools.MetricSeriesRef` caps `limit` at 1000. A native surface that read more
# than the MCP schema allows would be a capability the MCP agent does not have,
# and every difficulty number compared across the two would be measuring that.
MAX_SERIES = 1000
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
ERROR_TYPES = {400: "bad_data", 404: "not_found", 422: "execution", 503: "unavailable"}

_SELECTOR = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)?"
    r"(?:\{(?P<matchers>[^}]*)\})?"
    r"(?:\[(?P<duration>[0-9]+[smhdw])\])?$"
)
_MATCHER = re.compile(r'^\s*(?P<label>[a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"(?P<value>[^"]*)"\s*$')
_CALL = re.compile(r"^(?P<func>[a-zA-Z_][a-zA-Z0-9_]*)\((?P<arg>.*)\)$", re.DOTALL)

app = FastAPI(title="Alkera Prometheus (fake)", version="0.1.0")
# One row per request, including the ones no handler ran for (context.py).
audit_unrouted(app, NAME)


@app.exception_handler(ServiceError)
async def _service_error(request: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={
            "status": "error",
            "errorType": ERROR_TYPES.get(exc.status, "internal"),
            "error": f"{exc.code}: {exc.message}",
        },
    )


@dataclass
class Query:
    """One parsed query: an optional aggregation over one selector."""

    func: str | None
    name: str | None
    matchers: dict[str, str] = field(default_factory=dict)
    duration_s: float | None = None


def parse(expression: str) -> Query:
    """The whole PromQL this service understands. Raises `ServiceError`."""
    text = (expression or "").strip()
    if not text:
        raise ServiceError(400, "bad_data", "a query is required")
    for token, why in (
        ("=~", "regex matchers"), ("!~", "regex matchers"), ("!=", "negative matchers"),
        (" by ", "`by` grouping"), (" without ", "`without` grouping"),
        ("offset", "`offset`"), ("@", "the `@` modifier"),
    ):
        if token in text:
            raise ServiceError(400, "bad_data", f"{why} are not supported by this endpoint")
    func = None
    call = _CALL.match(text)
    if call:
        func = call.group("func")
        if func not in ("sum", "rate"):
            raise ServiceError(400, "bad_data", f"function {func!r} is not supported")
        text = call.group("arg").strip()
        if _CALL.match(text):
            raise ServiceError(400, "bad_data", "nested functions are not supported")
    selector = _SELECTOR.match(text)
    if not selector:
        raise ServiceError(400, "bad_data", f"cannot parse selector {text!r}")
    matchers = {}
    raw = (selector.group("matchers") or "").strip()
    if raw:
        for part in raw.split(","):
            matcher = _MATCHER.match(part)
            if not matcher:
                raise ServiceError(400, "bad_data", f"cannot parse matcher {part.strip()!r}")
            matchers[matcher.group("label")] = matcher.group("value")
    name = selector.group("name") or matchers.pop("__name__", None)
    duration = selector.group("duration")
    if duration and func != "rate":
        raise ServiceError(400, "bad_data", "a range selector is only supported inside rate()")
    if func == "rate" and not duration:
        raise ServiceError(400, "bad_data", "rate() needs a range selector, e.g. rate(x[1h])")
    if not name and not matchers:
        raise ServiceError(400, "bad_data", "a selector needs a metric name or a matcher")
    seconds = float(duration[:-1]) * UNITS[duration[-1]] if duration else None
    return Query(func=func, name=name, matchers=matchers, duration_s=seconds)


def labels_of(metric: dict) -> dict:
    """The labels one definition carries. `source.job` is the only one that
    comes from the definition body rather than its header."""
    labels = {"__name__": metric["id"], "kind": metric["kind"]}
    if metric.get("unit"):
        labels["unit"] = metric["unit"]
    job = (metric.get("source") or {}).get("job")
    if job:
        labels["job"] = job
    return labels


def matches(metric: dict, query: Query) -> bool:
    labels = labels_of(metric)
    if query.name and labels["__name__"] != query.name:
        return False
    # Prometheus: an absent label equals the empty string, so `{job=""}` selects
    # every series that has no job.
    return all(labels.get(key, "") == value for key, value in query.matchers.items())


def unix(value: str | datetime) -> float:
    return (parse_vts(value) if isinstance(value, str) else value).timestamp()


def sample(at: float, value: float) -> list:
    """Prometheus renders every value as a string, and clients parse it back."""
    return [at, repr(float(value))]


def instant(request: Request, query: Query) -> tuple[list[dict], list[str]]:
    """Read every series the selector matches, at the world clock.

    A selector that names a metric is one `metrics_get`; a selector that only
    filters by label is one `metrics_list`, because that is the question being
    asked and it is the one the MCP agent would ask too.
    """
    if query.name:
        metric = served(
            request, NAME, "metrics_get",
            lambda ops, obs: ops.metrics_get(query.name, obs), {"metric": query.name},
        )
        found = [metric] if matches(metric, query) else []
    else:
        listing = served(request, NAME, "metrics_list", lambda ops, obs: ops.metrics_list(obs))
        found = [metric for metric in listing["metrics"] if matches(metric, query)]
    series, warnings = [], []
    for metric in found:
        reading = metric["current"]
        if reading["status"] != "ok" or reading["value"] is None:
            # Never 0.0 for "could not measure": the series is absent and the
            # reason rides along where a client will print it.
            warnings.append(f"{metric['id']}: {reading['reason'] or reading['status']}")
            continue
        series.append(
            {
                "metric": labels_of(metric),
                "value": sample(unix(reading["as_of"]), reading["value"]),
            }
        )
    return series, warnings


def points(request: Request, query: Query, limit: int = MAX_SERIES) -> tuple[dict, list[dict]]:
    """One metric's recorded series as (metric definition, timestamped points).

    Only a named metric has a series: `metrics_series` takes one metric id, and
    inventing a fan-out over every definition would read the whole world for a
    query that did not ask for it.
    """
    if not query.name:
        raise ServiceError(
            400, "bad_data", "a range query needs a metric name; label-only selectors "
            "have no series on this surface"
        )
    result = served(
        request, NAME, "metrics_series",
        lambda ops, obs: ops.metrics_series(query.name, limit, obs),
        {"metric": query.name, "limit": limit},
    )
    stamped = []
    for point in result["points"]:
        at = moment(point["at"])
        if at is None or point["value"] is None:
            continue
        stamped.append({"at": at.timestamp(), "value": float(point["value"])})
    return result, sorted(stamped, key=lambda item: item["at"])


def rate(stamped: list[dict], window: tuple[float, float]) -> float | None:
    """Per-second change across the window, the way `rate()` is usually read.

    Counter resets are NOT detected (a real `rate()` corrects for them) and the
    result is a plain first-to-last slope rather than an extrapolated one, so
    this is `increase/seconds`, not Prometheus's estimator. Documented in
    OPS_PLANE.md; not approximated silently.
    """
    inside = [p for p in stamped if window[0] <= p["at"] <= window[1]]
    if len(inside) < 2 or inside[-1]["at"] == inside[0]["at"]:
        return None
    return (inside[-1]["value"] - inside[0]["value"]) / (inside[-1]["at"] - inside[0]["at"])


def _ok(data: dict, warnings: list[str]) -> dict:
    body = {"status": "success", "data": data}
    if warnings:
        body["warnings"] = warnings
    return body


def _now(request: Request) -> float:
    return unix(context_of(request.app).clock())


def _time(value: str | None, fallback: float) -> float:
    if value is None:
        return fallback
    try:
        return float(value)
    except ValueError:
        at = moment(value)
        if at is None:
            raise ServiceError(400, "bad_data", f"cannot parse timestamp {value!r}") from None
        return at.timestamp()


@app.get("/health")
def health(request: Request) -> object:
    """The metric names this service can serve, counted through `metrics_list`
    - the same call `/api/v1/label/__name__/values` makes."""
    return service_health(
        request, NAME, lambda ops, obs: len(ops.metrics_list(obs)["metrics"])
    )


@app.get("/api/v1/query")
def query_instant(request: Request, query: str, time: str | None = None) -> dict:
    now = _now(request)
    warnings = []
    if time is not None and abs(_time(time, now) - now) > 0.5:
        # Answering an as-of question with the current reading and stamping it
        # with the asked-for time is the silent-wrong failure; say so instead.
        warnings.append(
            "time= is not honoured: every reading is taken at the world clock "
            f"({context_of(request.app).clock()})"
        )
    parsed = parse(query)
    if parsed.func == "rate":
        definition, stamped = points(request, parsed)
        value = rate(stamped, (now - parsed.duration_s, now))
        if value is None:
            warnings.append(f"{parsed.name}: fewer than two usable points in the range")
            return _ok({"resultType": "vector", "result": []}, warnings)
        return _ok(
            {"resultType": "vector",
             "result": [{"metric": {"__name__": parsed.name}, "value": sample(now, value)}]},
            warnings + ([definition["note"]] if definition.get("note") else []),
        )
    series, found = instant(request, parsed)
    warnings += found
    if parsed.func == "sum":
        if not series:
            return _ok({"resultType": "vector", "result": []}, warnings)
        total = sum(float(item["value"][1]) for item in series)
        return _ok(
            {"resultType": "vector", "result": [{"metric": {}, "value": sample(now, total)}]},
            warnings,
        )
    return _ok({"resultType": "vector", "result": series}, warnings)


@app.get("/api/v1/query_range")
def query_range(request: Request, query: str, start: str | None = None,
                end: str | None = None, step: str | None = None) -> dict:
    """The recorded series, bounded by start/end.

    `step` is accepted and NOT applied: the points are the samples the world
    wrote down, at the times it wrote them, and resampling them onto a grid
    would manufacture readings that were never taken. A client that needs the
    grid can build it; it cannot un-invent a value.

    Prometheus requires `start` and `end`; here they are optional and their
    absence means the whole recorded series, because defaulting to a window
    would silently hide the samples outside it - and which samples a world still
    holds is exactly the question these tasks ask.
    """
    now = _now(request)
    parsed = parse(query)
    begin = _time(start, now) if start is not None else float("-inf")
    finish = _time(end, now) if end is not None else float("inf")
    definition, stamped = points(request, parsed)
    warnings = [] if step is None else ["step= is not applied; samples are returned as recorded"]
    if start is None or end is None:
        warnings.append("start=/end= were not both given; the whole recorded series is returned")
    if definition.get("note"):
        warnings.append(definition["note"])
    if parsed.func == "rate":
        # A rate always anchors at a real instant: the requested `end`, or the
        # world clock when the client did not name one.
        anchor = finish if end is not None else now
        value = rate(stamped, (anchor - parsed.duration_s, anchor))
        if value is None:
            warnings.append(f"{parsed.name}: fewer than two usable points in the range")
            return _ok({"resultType": "matrix", "result": []}, warnings)
        return _ok(
            {"resultType": "matrix",
             "result": [{"metric": {"__name__": parsed.name},
                         "values": [sample(anchor, value)]}]},
            warnings,
        )
    values = [sample(p["at"], p["value"]) for p in stamped if begin <= p["at"] <= finish]
    if not values:
        return _ok({"resultType": "matrix", "result": []}, warnings)
    labels = {"__name__": parsed.name, "kind": definition["kind"]}
    if definition.get("unit"):
        labels["unit"] = definition["unit"]
    return _ok({"resultType": "matrix", "result": [{"metric": labels, "values": values}]},
               warnings)


def _definitions(request: Request) -> list[dict]:
    return served(request, NAME, "metrics_list", lambda ops, obs: ops.metrics_list(obs))["metrics"]


@app.get("/api/v1/series")
def series_endpoint(request: Request) -> dict:
    """`match[]` is repeatable, so it is read off the raw query string."""
    selectors = request.query_params.getlist("match[]")
    if not selectors:
        raise ServiceError(400, "bad_data", "at least one match[] argument is required")
    parsed = [parse(selector) for selector in selectors]
    found = [
        labels_of(metric)
        for metric in _definitions(request)
        if any(matches(metric, query) for query in parsed)
    ]
    return _ok(found, [])


@app.get("/api/v1/labels")
def labels_endpoint(request: Request) -> dict:
    present = sorted({key for metric in _definitions(request) for key in labels_of(metric)})
    return _ok(present, [])


@app.get("/api/v1/label/{label}/values")
def label_values(label: str, request: Request) -> dict:
    if label not in LABELS:
        return _ok([], [f"{label} is not a label this surface carries"])
    values = sorted({labels_of(metric).get(label) for metric in _definitions(request)} - {None})
    return _ok(values, [])


def _rule(alarm: dict) -> dict:
    """One alarm as a Prometheus alerting rule.

    `insufficient_data` is a rule that could not be evaluated, which Prometheus
    spells `health: "unknown"` with `lastError` - not `inactive`, which would
    read as "evaluated, and fine".
    """
    firing = alarm["state"] == "firing"
    state = {"firing": "firing", "pending": "pending"}.get(alarm["state"], "inactive")
    labels = {"alertname": alarm["alarm"], "severity": alarm["severity"],
              "metric": alarm["metric"]}
    annotations = {key: alarm[key] for key in ("title", "description", "runbook")
                   if alarm.get(key)}
    return {
        "name": alarm["alarm"],
        "type": "alerting",
        "query": f"{alarm['metric']} {alarm['comparison']} {alarm['threshold']}",
        "duration": 0,
        "state": state,
        "health": "unknown" if alarm["state"] == "insufficient_data" else "ok",
        "lastError": alarm["reason"] or "",
        "labels": labels,
        "annotations": annotations,
        "evaluationTime": 0.0,
        "lastEvaluation": alarm["evaluated_at"],
        "alerts": [
            {
                "labels": labels,
                "annotations": annotations,
                "state": state,
                "activeAt": alarm.get("since") or alarm["evaluated_at"],
                "value": repr(float(alarm["value"])) if alarm["value"] is not None else "",
            }
        ]
        if firing or state == "pending"
        else [],
    }


def _alarms(request: Request) -> list[dict]:
    return served(request, NAME, "alarms_list",
                  lambda ops, obs: ops.alarms_list(None, obs), {"state": None})["alarms"]


@app.get("/api/v1/rules")
def rules(request: Request) -> dict:
    return _ok({"groups": [{"name": "alkera", "file": "observability.yaml", "interval": 0,
                            "rules": [_rule(alarm) for alarm in _alarms(request)]}]}, [])


@app.get("/api/v1/alerts")
def alerts(request: Request) -> dict:
    firing = [_rule(alarm) for alarm in _alarms(request) if alarm["state"] != "ok"]
    return _ok({"alerts": [alert for rule in firing for alert in rule["alerts"]]}, [])
