"""Alertmanager-shaped alarm service over `alkera_gateway.observability`.

WHAT AN ALERTMANAGER CAN AND CANNOT SAY. It holds the alerts something sent it,
which here means the alarms that are *firing*. An alarm that is `ok`, `pending`
or `insufficient_data` is not an alert and does not appear - so this surface,
faithfully imitated, is blinder than `alarms_list`, which reports all four
states. That is not a defect of the imitation, it is the real difference between
reading an alert queue and reading the rule set, and the rule set is served by
`prometheus.py` (`/api/v1/rules`) exactly as a real operator would read it.

SILENCES ARE WORLD STATE NOW. They used to live in a dict in this process: a
silence shaped `/api/v2/alerts` for as long as the service ran, wrote its 9.2.2
row, and then vanished on restart without the world ever recording it - an
action the log claimed and the world denied, which is the exact asymmetry native
mode exists to remove (SERVICES_B_REQUESTS.md item 2). `ObservabilityStore`
now serves the write: `alarms_ack` appends to `var/obs/silences.json`, inside
the world tree and inside the fingerprint, and `alarms_silences` reads it back.
This service holds no state at all.

Silencing is per ALARM, not per matcher expression. The matchers on a POST are
resolved against `alarms_list` here and the alarm ids are what is stored, so the
row's targets are `alarm:<id>` - the vocabulary `alarms_get` uses and the one a
grader can join on - and a silence written by one process is read identically by
the next. A matcher that selects no declared alarm is a 404, never a silence
that quietly covers nothing.
"""
from __future__ import annotations

import hashlib
from datetime import timedelta

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ...observability import iso, parse_vts
from ...records import ToolError, canonical
from ..context import Ledger, ServiceError, Span, audit_unrouted, context_of, served, service_health
from ..http import JSONBody

NAME = "alertmanager"
HOST = "alertmanager.internal"
PORT = 9093

# Alertmanager's own default: an alert whose source stops re-sending it is
# resolved this long after the last update.
RESOLVE_TIMEOUT_S = 300
RECEIVER = "alkera-oncall"
# A silence with no `endsAt` runs for this long, which is Alertmanager's own
# default in its UI. Measured on the virtual clock, like everything else here.
DEFAULT_SILENCE_H = 1
# The one label a stored silence can select on: a silence names alarms, and the
# alarm id is what `alertname` carries.
SILENCE_LABEL = "alertname"

app = FastAPI(title="Alkera Alertmanager (fake)", version="0.1.0")
# One row per request, including the ones no handler ran for (context.py).
audit_unrouted(app, NAME)


@app.exception_handler(ServiceError)
async def _service_error(request: Request, exc: ServiceError) -> JSONResponse:
    """Alertmanager's v2 API answers with a bare string body."""
    return JSONResponse(status_code=exc.status, content=f"{exc.code}: {exc.message}")


def fingerprint(labels: dict) -> str:
    """Alertmanager identifies an alert by a 16-hex-digit hash of its labels."""
    return hashlib.sha256(canonical(labels)).hexdigest()[:16]


def labels_of(alarm: dict) -> dict:
    return {
        "alertname": alarm["alarm"],
        "severity": alarm["severity"],
        "metric": alarm["metric"],
    }


def annotations_of(alarm: dict) -> dict:
    annotations = {"summary": alarm["title"] or alarm["alarm"]}
    if alarm.get("description"):
        annotations["description"] = alarm["description"]
    if alarm.get("runbook"):
        annotations["runbook_url"] = alarm["runbook"]
    if alarm.get("value") is not None:
        annotations["value"] = repr(float(alarm["value"]))
    return annotations


def matches(matchers: list[dict], labels: dict) -> bool:
    return all(labels.get(item["name"], "") == item["value"] for item in matchers)


def in_force(row: Span) -> dict[str, dict]:
    """alarm id -> the silence covering it now, read through `alarms_silences`.

    A header read on the way to rendering an alert's status, so it goes through
    `aside` and costs no row: "is this alarm silenced" is part of reporting the
    alert, not a question of its own, and charging for it would make
    `GET /api/v2/alerts` cost two rows where `alarms_list` costs one.
    """
    covering: dict[str, dict] = {}
    for entry in row.aside(lambda ops, obs: ops.alarms_silences(obs))["silences"]:
        if entry["state"] == "active":
            for alarm in entry["alarms"]:
                covering[alarm] = entry
    return covering


def silence_object(entry: dict) -> dict:
    """One stored silence -> the Alertmanager silence shape.

    One matcher per alarm the entry covers, on `alertname`, which is the only
    label a stored silence can be expressed in - see the module docstring.
    """
    return {
        "id": entry["id"],
        "matchers": [{"name": SILENCE_LABEL, "value": alarm, "isRegex": False, "isEqual": True}
                     for alarm in entry["alarms"]],
        "startsAt": entry["at"],
        "endsAt": entry["until"],
        "updatedAt": entry["at"],
        "createdBy": entry["actor"],
        "comment": entry["comment"],
        "status": {"state": entry["state"]},
    }


def alert_of(alarm: dict, now: str, covering: dict[str, dict]) -> dict:
    labels = labels_of(alarm)
    silence = covering.get(alarm["alarm"])
    silenced = [silence["id"]] if silence else []
    ends = iso(parse_vts(alarm["evaluated_at"]) + timedelta(seconds=RESOLVE_TIMEOUT_S))
    return {
        "labels": labels,
        "annotations": annotations_of(alarm),
        "startsAt": alarm.get("since") or alarm["evaluated_at"],
        "endsAt": ends,
        "updatedAt": alarm["evaluated_at"],
        "generatorURL": f"http://{HOST}:{PORT}/#/alerts?filter=alertname={alarm['alarm']}",
        "fingerprint": fingerprint(labels),
        "receivers": [{"name": RECEIVER}],
        "status": {
            "state": "suppressed" if silenced else "active",
            "silencedBy": silenced,
            "inhibitedBy": [],
        },
    }


def parse_filters(values: list[str]) -> dict:
    """Alertmanager's `filter=` is `label="value"`; only equality is served."""
    parsed = {}
    for value in values:
        if "=" not in value or any(token in value for token in ("=~", "!=", "!~")):
            raise ServiceError(
                400, "bad_filter", f'only label="value" filters are served: {value}'
            )
        label, raw = value.split("=", 1)
        parsed[label.strip()] = raw.strip().strip('"')
    return parsed


def firing_alerts(request: Request) -> list[dict]:
    """Every firing alarm, as an Alertmanager alert.

    `alarms_list(state="firing")` is the MCP call this is: the same filter, the
    same `alarms` target, the same evaluation. One row, plus the silence log
    read as a header (`in_force`).
    """
    with Ledger(request, NAME).span("alarms_list", state="firing") as row:
        listing = row.run(lambda ops, obs: ops.alarms_list("firing", obs))
        covering = in_force(row)
    now = listing["evaluated_at"]
    return [alert_of(alarm, now, covering) for alarm in listing["alarms"]]


@app.get("/health")
def health(request: Request) -> object:
    """The alarms this service evaluates - NOT the alerts currently firing.

    A healthy world with nothing wrong has no firing alert, and a probe that
    reported 0 there would tell the surface gate the service is dead when it is
    working perfectly. The firing subset is what `/api/v2/alerts` serves.
    """
    return service_health(
        request, NAME, lambda ops, obs: len(ops.alarms_list(None, obs)["alarms"])
    )


@app.get("/api/v2/alerts")
def get_alerts(request: Request, active: bool = True, silenced: bool = True) -> list[dict]:
    """`inhibited=` and `unprocessed=` are accepted and ignored rather than
    rejected: nothing here is ever inhibited or unprocessed, so both are
    already true of every alert served."""
    wanted = parse_filters(request.query_params.getlist("filter"))
    alerts = []
    for alert in firing_alerts(request):
        suppressed = alert["status"]["state"] == "suppressed"
        if suppressed and not silenced:
            continue
        if not suppressed and not active:
            continue
        if all(alert["labels"].get(key, "") == value for key, value in wanted.items()):
            alerts.append(alert)
    return alerts


@app.get("/api/v2/alerts/groups")
def get_groups(request: Request, active: bool = True, silenced: bool = True) -> list[dict]:
    """Grouped the way a real route groups them: by severity."""
    alerts = get_alerts(request, active=active, silenced=silenced)
    groups: dict[str, list[dict]] = {}
    for alert in alerts:
        groups.setdefault(alert["labels"]["severity"], []).append(alert)
    return [
        {
            "labels": {"severity": severity},
            "receiver": {"name": RECEIVER},
            "alerts": members,
        }
        for severity, members in sorted(groups.items())
    ]


@app.get("/api/v2/silences")
def get_silences(request: Request) -> list[dict]:
    """Every silence this world holds. The equivalent of `alarms_silences`."""
    listing = served(request, NAME, "alarms_silences",
                     lambda ops, obs: ops.alarms_silences(obs))
    return [silence_object(entry) for entry in listing["silences"]]


@app.post("/api/v2/silences")
def post_silence(request: Request, body: JSONBody) -> dict:
    """Silence every alarm the matchers select, for as long as they say.

    The matchers are resolved against `alarms_list` first - a resolution read,
    so no row of its own - and the alarm ids are what `alarms_ack` stores, so
    this row's targets are `alarm:<id>` rather than a matcher expression no
    grader can join on. One row: `alertmanager.alarms_ack`.
    """
    matchers = body.get("matchers") or []
    if not isinstance(matchers, list) or not matchers:
        raise ServiceError(400, "bad_silence", "a silence needs at least one matcher")
    for matcher in matchers:
        if not isinstance(matcher, dict) or "name" not in matcher or "value" not in matcher:
            raise ServiceError(400, "bad_silence", "each matcher needs a name and a value")
        if matcher.get("isRegex"):
            raise ServiceError(400, "bad_silence", "regex matchers are not served")
    now = context_of(request.app).clock()
    starts = body.get("startsAt") or now
    if parse_vts(starts) > parse_vts(now):
        # Refused by name rather than silently started now: a silence that says
        # it begins later and does not is the kind of quiet difference this
        # benchmark measures.
        raise ServiceError(400, "bad_silence",
                           f"a silence starts when it is posted; startsAt {starts} is "
                           f"later than the world clock ({now})")
    ends = body.get("endsAt") or iso(parse_vts(now) + timedelta(hours=DEFAULT_SILENCE_H))
    comment = body.get("comment") or ""
    creator = body.get("createdBy") or "agent"
    try:
        with Ledger(request, NAME).span("alarms_ack", matchers=matchers, endsAt=ends,
                                        comment=comment, createdBy=creator) as row:
            listing = row.aside(lambda ops, obs: ops.alarms_list(None, obs))
            matched = sorted(alarm["alarm"] for alarm in listing["alarms"]
                             if matches(matchers, labels_of(alarm)))
            if not matched:
                raise ServiceError(404, "no_such_alarm",
                                   "no declared alarm matches those matchers")
            entry = row.run(
                lambda ops, obs: ops.alarms_ack(matched, ends, comment, creator, obs))
    except ToolError as exc:
        raise ServiceError.from_tool_error(exc) from None
    return {"silenceID": entry["id"]}


@app.delete("/api/v2/silence/{silence_id}")
def expire_silence(silence_id: str, request: Request) -> JSONResponse:
    """Expire a silence now. Without this a silence written too wide is
    permanent, which is now a real problem rather than a process-local one."""
    served(request, NAME, "alarms_unack",
           lambda ops, obs: ops.alarms_unack(silence_id, obs),
           {"silence_id": silence_id})
    return JSONResponse(status_code=200, content=None)
