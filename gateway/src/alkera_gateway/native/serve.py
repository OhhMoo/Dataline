"""Serve every native fake service from one process, one port per service.

`gateway/NATIVE.md` gives each service module a `NAME`, a `HOST` (its
DNS alias in the episode network), a `PORT` and an `app`. This module mounts
whatever `services.load()` returns - one `uvicorn.Server` per service inside a
single asyncio loop - prints which services loaded and which did not, and keeps
the network-destination audit the harness reads in native mode. One extra
socket, `native-services.internal:8099`, answers an aggregate `/health` that
lists the whole set, so "what is this process serving" is one request rather
than nine. `--profile` (with `--plug`) narrows the set to one environment
profile's services and drops the scheduler plane when it plugs no `ops_sched`;
it is also the only way a BI vendor (`services.PLUG_NAMES`: powerbi, tableau,
sigma; ports in PLUG_PORTS) is served at all, and the only way a hosted backend
(`alkera_gateway.backends`, `hosted=True`; port and alias from its registry
row, `hosted_ports()`) is.

Ports, not host-header routing
------------------------------
Both designs work: every client these services are imitated for (`jira`,
`slack_sdk`, `atlassian-python-api`, `boto3`) is configured with a base URL, so
one socket plus `Host:` routing would need less compose plumbing. It was
rejected for three reasons.

* The registry already declares `PORT` per service. A single-socket router
  makes that field dead while leaving it in the contract the other builders
  program against, and a dead field in a shared interface is how two builders
  end up disagreeing about what a service is.
* Containment P6 claims "TCP to anything but the listed ports is refused". On
  one socket that claim degenerates to "one port is open", which says nothing
  about whether the agent can reach a service it should not have found.
* A `Host` header is the client's claim. Routing on it means any client that
  connects by IP, or that sets its own header (`curl -H`, a proxy, a library
  with path-style addressing), lands on whichever service is the fallback.
  Routing on the accepted socket's own local port cannot be talked out of.

The network-destination audit
-----------------------------
Native mode drops `unknown_tool` and `shell_outside_gateway` - bash *is* the
interface - and replaces them with a record of every destination the episode
opened a connection to, written on the server side. `<audit>/destinations.jsonl`
carries one row per destination::

    {"host": "jira.internal", "port": 8080, "count": 3,
     "first_ts": "2026-09-16T11:02:04Z", "last_ts": "2026-09-16T11:09:41Z"}

`count` is **TCP connections accepted**, counted in `connection_made` on
uvicorn's HTTP protocol - the accept, not the request - so a keep-alive
connection carrying six API calls is one bump here and six rows in
`actions.jsonl`. The two files answer different questions on purpose: this one
says what the agent talked to, `actions.jsonl` says what it did there.

`host` is the service's declared alias, not the `Host` header, because the
header is the client's claim and the local port is the server's observation.
The file is rewritten whole on every change - at most one row per service -
rather than appended to, because a reader of an append-only file would have to
fold duplicate keys before it could answer the question the file exists for.

Usage::

    python -m alkera_gateway.native.serve --audit /audit --world /world
    python -m alkera_gateway.native.serve --audit /audit --world /world --stub jira
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from uvicorn.protocols.http.h11_impl import H11Protocol

from ..plugs import ProfileError, parse_plug_args, resolve
from . import services

#: The port each registry service listens on when its own module does not say.
#:
#: NOT a free choice. `tasks/harness/agent_under_test.NATIVE_SERVICES` is what
#: the subject env carries and `docs/implementation_plan/harness/NATIVE_DRIVER_REQUESTS.md` item 3
#: fixes these nine numbers; vendor defaults wherever the vendor has one, Slack
#: and deploy have none. A module PORT still wins at mount time - it is the
#: thing that listens - but a module that disagrees means the subject JIRA_URL
#: points at nothing and the native surface gate reports a dead world, so the
#: startup table prints the disagreement instead of letting it through.
STUB_PORTS = {
    "jira": 8080,
    "slack": 8081,
    "airbyte": 8001,
    "deploy": 8085,
    "confluence": 8090,
    "metabase": 3000,
    "prometheus": 9090,
    "alertmanager": 9093,
    "loki": 3100,
}
#: The explicit-only BI vendors (`services.PLUG_NAMES`): served only when an
#: environment profile plugs them (`--profile ... --plug bi=powerbi`, or a
#: stack whose default is one, `snowflake` -> `sigma`).
#:
#: A SEPARATE TABLE, NOT THREE MORE ROWS OF STUB_PORTS, because STUB_PORTS is
#: the unprofiled stack's contract and two readers take it as "every port an
#: unprofiled serve.py binds": `tasks/harness/native_stack.service_ports(None)`
#: waits for all of them to be bound before the host-topology rollout starts
#: (three ports nobody serves would be a start-up timeout), and world/tests/
#: test_native_workspace.py pins it row for row to the nine-service discovery
#: table. Vendor ports have no self-hosted default here (Power BI, Tableau Cloud
#: and Sigma are SaaS), so these are the next free numbers after deploy's 8085
#: block: 8095, 8096, 8097. `tasks/harness/agent_under_test.PLUG_SERVICES` and
#: `world/world/native_workspace.PLUG_SERVICES` carry the same numbers.
PLUG_PORTS = {
    "powerbi": 8095,
    "tableau": 8096,
    "sigma": 8097,
}
def hosted_ports() -> dict[str, int]:
    """The registry's hosted backends (`alkera_gateway.backends`, `hosted=True`)
    and the port each row names: where serve.py mounts its module, only when a
    profile plugs it (`selected_services`). Read from the registry at call
    time, never copied into a table here."""
    return {name: b.port for name, b in services.hosted().items()}


def service_ports() -> dict[str, int]:
    """Every port a named service is known to listen on: the nine, the BI
    vendors, the registry's hosted backends."""
    return {**STUB_PORTS, **PLUG_PORTS, **hosted_ports()}


#: Every port a named service is known to listen on (`service_ports()` as the
#: registry stood at import).
SERVICE_PORTS = service_ports()
#: Every service answers this path. A service that defines its own route for it
#: keeps it; otherwise `HealthWrapper` answers.
#:
#: Body shape, fixed by NATIVE_DRIVER_REQUESTS.md items 1-2 and read by
#: `surface_audit.probe_native`: {"ok": true, "service": NAME, "items": int}.
#: `items` is how much world that service is actually serving, which is the
#: difference between "the process is up" and "the world behind it has
#: content" - the runner-up finding in NATIVE_DRIVER.md.
HEALTH_PATH = "/health"
#: The aggregate, served under `native-services.internal`. One request answers
#: "what is this process serving", which is otherwise nine health checks.
AGGREGATE_PORT = 8099
#: MinIO's audit webhook lands here, on this same process.
#:
#: `compose.native.yml` points `MINIO_AUDIT_WEBHOOK_ENDPOINT_alkera` at
#: `http://native-services.internal:8110/minio/audit`
#: (docs/implementation_plan/gateway/COMPOSE_REQUESTS.md item 3). The receiver is AUDIT's
#: `tailers.minio.build_app()`; it is hosted here rather than in a container of
#: its own because it writes into the same `actions.jsonl` this process's
#: services write, and `native.audit` allocates `seq` under one lock per audit
#: directory - a second container would be a second writer racing for it.
#: 8110 and not 8099: 8099 is the aggregate /health.
MINIO_AUDIT_PORT = 8110
#: The name that receiver is mounted under. Not one of the nine: the episode
#: never dials it (MinIO does, from inside the stack), so it is excluded from
#: the aggregate's service list the way any non-agent surface would be.
MINIO_AUDIT_NAME = "minio-audit"
#: The SCHEDULER plane, under its own alias `sched.internal`.
#:
#: It is not one of the nine and it is not a vendor: `sched_*` has no fake
#: vendor because `world/world/scheduler.py` is a state file plus an executor,
#: and the native surface for it is `./ops/sched`, a CLI in the agent's own
#: repo (`shims/sched_cli.py`). What this mount adds is the CLI's BACKEND for
#: the container topology, where the files that CLI drives - `var/sched`, its
#: run logs, `var/obs`, `build/sched` - are deliberately not mounted into the
#: episode container (docs/implementation_plan/harness/NATIVE_CONTAINER.md). Without it the
#: scheduler plane is unreachable from the episode and a task that needs
#: `sched_*` cannot be measured under that topology at all.
#:
#: Its own port and its own alias, for the reason every service has one: P6's
#: "TCP to anything but the listed ports is refused" is vacuous on a shared
#: socket, and the accepted socket's local port is the server's own observation
#: of who was dialled while a `Host:` header is the client's claim.
#:
#: 8120 and not 8110: 8110 is MinIO's audit receiver. Fixed here and in
#: `tasks/harness/agent_under_test.NATIVE_WORKSPACE_SERVICES`, which is what
#: the episode's `SCHED_URL` carries.
SCHED_NAME = "sched"
SCHED_PORT = 8120
#: The destination audit, under the directory `--audit` names.
#:
#: Row shape, fixed by NATIVE_DRIVER_REQUESTS.md item 5: the harness reads
#: `host`, `port` and `count`, reads `count: 0` as "seen, never dialled", and
#: reads an ABSENT FILE as `unobserved_network` - hard, and it quarantines the
#: rollout. So the file is written before the first socket opens, with every
#: mounted service at zero. `first_ts`/`last_ts` are ours; the harness ignores
#: what it does not read.
DESTINATIONS = "destinations.jsonl"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class DestinationLog:
    """Accepted-connection counts per (host, port), persisted as JSONL.

    Thread-safe because uvicorn's protocol objects are constructed by the event
    loop while `flush` may also run from the signal path; the lock is cheap and
    the file is at most one row per service.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._by_port: dict[int, dict[str, Any]] = {}

    def declare(self, host: str, port: int) -> None:
        """Register a destination before anything has connected to it.

        A row with `count: 0` is a real observation - the service was served
        and nobody dialled it - and the harness needs the difference between
        "never offered" and "offered, never used".
        """
        with self._lock:
            self._by_port[port] = {"host": host, "port": port, "count": 0,
                                   "first_ts": None, "last_ts": None}

    def accepted(self, port: int) -> None:
        with self._lock:
            row = self._by_port.get(port)
            if row is None:  # a socket this process did not open cannot happen
                return
            stamp = _now()
            row["count"] += 1
            row["first_ts"] = row["first_ts"] or stamp
            row["last_ts"] = stamp
        self.flush()

    def rows(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(row) for _, row in sorted(self._by_port.items())]

    def flush(self) -> Path:
        """Rewrite the whole file, atomically. Returns the path written."""
        with self._lock:
            body = "".join(
                json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
                for row in self.rows()
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            with open(tmp, "w", encoding="utf-8", newline="") as fh:
                fh.write(body)
            # os.replace can lose transiently on Windows while an indexer or
            # scanner holds the destination; the content is identical per retry.
            for attempt in range(5):
                try:
                    os.replace(tmp, self.path)
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.1 * (attempt + 1))
        return self.path


class AuditedH11Protocol(H11Protocol):
    """uvicorn's HTTP/1.1 protocol plus one destination count per accept.

    `destinations` is a class attribute rather than a constructor argument
    because uvicorn instantiates the protocol class itself
    (`Config.http_protocol_class`) and passes it only what it knows about.
    `serve_all` sets it once, for the whole process.
    """

    destinations: DestinationLog | None = None

    def connection_made(self, transport) -> None:
        super().connection_made(transport)
        log = AuditedH11Protocol.destinations
        sockname = transport.get_extra_info("sockname")
        if log is not None and sockname:
            log.accepted(int(sockname[1]))


class JSONResponder:
    """The one-route ASGI app the health and aggregate endpoints are built from.

    Plain ASGI rather than a FastAPI route: this module has to run inside the
    episode image, which carries uvicorn and starlette (through `mcp`) but not
    fastapi, and it must mount a service app without knowing which framework
    built it.
    """

    def __init__(self, payload: dict) -> None:
        self.body = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
        self.status = 503 if payload.get("ok") is False else 200

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        await send({
            "type": "http.response.start",
            "status": self.status,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(self.body)).encode())],
        })
        await send({"type": "http.response.body", "body": self.body})


class HealthWrapper:
    """ASGI middleware answering `GET /health` for a service with no such route.

    `items` is what the service reports for its own world. A module that serves
    no `/health` of its own is wrapped with `items: 0`, which the native surface
    gate reads as a dead surface - fail-closed, and the right answer:
    NATIVE_DRIVER_REQUESTS.md item 1 makes `/health` the module owner's
    obligation, and a wrapper cannot know how much world is behind an app it
    did not write.
    """

    def __init__(self, app: Callable, service: str, items: int) -> None:
        self.app = app
        self.health = JSONResponder({"ok": items > 0, "service": service,
                                     "items": items})

    async def __call__(self, scope, receive, send) -> None:
        if (scope["type"] == "http" and scope.get("path") == HEALTH_PATH
                and scope.get("method") == "GET"):
            await self.health(scope, receive, send)
            return
        await self.app(scope, receive, send)


def serves_health(app: object) -> bool:
    """Does this app route `/health` itself? True for starlette and FastAPI."""
    return any(getattr(route, "path", None) == HEALTH_PATH
               for route in getattr(app, "routes", ()) or ())


#: What a stub serves: one item, so its `/health` reports `items: 1` and P6's
#: "every alias answers" is a claim about something rather than about nothing.
STUB_ITEMS = 1


def stub_app(name: str) -> Callable:
    """A minimal ASGI app standing in for a service module that has not landed.

    Used by `--stub`: containment P6 and `world/tests/test_native_workspace.py`
    have to measure the DNS and port surface where a service module cannot be
    imported, and measuring it against nothing would make "every alias answers
    /health" vacuously true.
    """
    return JSONResponder({"stub": name, "items": STUB_ITEMS})


@dataclass(frozen=True)
class Mounted:
    """One service, ready to be served."""

    name: str
    host: str
    port: int
    app: Callable
    origin: str  # "module", "stub", "aggregate" or "receiver"
    own_health: bool
    #: Is a connection to this port something the EPISODE did?
    #:
    #: False for the MinIO audit receiver: MinIO dials it, from inside the
    #: stack, once per API call it served. Counted, that port would appear in
    #: `destinations.jsonl` with `count > 0` and no matching entry in the
    #: episode's endpoint map, and `blindness.network_violations` would
    #: quarantine every rollout that touched object storage for a connection
    #: the subject never made. So the receiver is neither declared nor counted.
    destination: bool = True
    #: A hosted backend whose registry row says `tls=True` is served over HTTPS:
    #: `certificate` is its module's own (`() -> (certfile, keyfile)`, minting with
    #: `native.tls.mint` and publishing the bundle the episode trusts), called by
    #: `serve_all` before anything binds. A TLS row whose module has none is a
    #: refusal there, never a plain-HTTP fallback the clients would fail against.
    tls: bool = False
    certificate: Callable | None = None


def mount(loaded: dict, stubs: dict[str, int] | None = None,
          offset: int = 0) -> list[Mounted]:
    """Wrap each loaded service (and each stub) for serving, ordered by port.

    `loaded` is `services.load()[0]`, or any mapping of name -> object carrying
    `HOST`, `PORT` and `app`; the tests pass their own so they do not depend on
    another builder's module existing.

    `offset` shifts every port by a constant. It is 0 inside the episode
    network, where the ports ARE the contract - jira 8080 and nothing else -
    and non-zero only on the host topology, where `tasks/harness/native_stack
    .py` runs K rollouts side by side on one loopback interface and a fixed
    8080 would make the second rollout fail to bind. The offset is constant
    across the set on purpose: the endpoint map the subject is handed is then
    the alias table plus one number, so a reader can still check a port against
    NATIVE_DRIVER_REQUESTS.md item 3 by subtracting it.
    """
    mounted: list[Mounted] = []
    hosted = services.hosted()
    for name, module in sorted(loaded.items()):
        host, port, app = module.HOST, int(module.PORT) + offset, module.app
        tls = False
        if name in hosted:
            # a hosted backend answers where its registry row says, and as it says
            host, port = hosted[name].host, hosted[name].port + offset
            tls = hosted[name].tls
        own = serves_health(app)
        mounted.append(Mounted(name, host, port,
                               app if own else HealthWrapper(app, name, 0),
                               "module", own, tls=tls,
                               certificate=getattr(module, "certificate", None)
                               if tls else None))
    for name, port in sorted((stubs or {}).items()):
        mounted.append(Mounted(name, f"{name}.internal", port + offset,
                               HealthWrapper(stub_app(name), name, STUB_ITEMS),
                               "stub", False))
    claimed: dict[int, str] = {}
    for item in mounted:
        owner = claimed.setdefault(item.port, item.name)
        if owner != item.name:
            raise SystemExit(
                f"port {item.port} is claimed by both {owner!r} and {item.name!r}; "
                "one service per port is what makes the destination audit and "
                "containment P6 mean anything")
    return sorted(mounted, key=lambda item: item.port)


def world_gateway_config(world: Path, audit: Path) -> str:
    """A `GatewayConfig` over this world, as JSON, on the agent.md 9.2.1 layout.

    `native/services/_ops.py` and the three human-plane modules build their
    `Operations` from `$ALKERA_GATEWAY_CONFIG` and fall back to
    `GatewayConfig()` - whose defaults are `/workspace`, `/var/warehouse/...`
    and so on, absolute and NOT under this episode's world. Unset, every
    service would serve a world that is not the episode's, so a config is
    derived here whenever the caller did not name one (SERVICES_A_REQUESTS.md
    item 5).

    The derivation is the contract layout and nothing cleverer: the world
    builder writes exactly those paths under `<root>` (world/world/build.py's
    docstring). `virtual_time` is left at the model default because it is only
    ever a fallback - `native.audit.virtual_time()` reads the scheduler state
    named by `ALKERA_SCHEDULER` first, which is the world's own `vnow`.
    """
    from ..config import GatewayConfig

    return GatewayConfig(
        workspace=world / "workspace",
        warehouse=world / "var" / "warehouse" / "warehouse.duckdb",
        scheduler=world / "var" / "sched" / "state.json",
        ingest=world / "var" / "ingest" / "state.json",
        observability=world / "var" / "obs" / "observability.yml",
        workflow_dir=world / "var" / "workflow",
        dashboards=world / "var" / "bi" / "dashboards",
        profiles_dir=world / "build",
        audit_dir=audit,
        world_root=world,
    ).model_dump_json()


def service_environment(audit: Path, world: Path,
                        gateway_config: Path | None) -> dict[str, str]:
    """The three variables every service resolves its world and its rows from.

    SERVICES_A_REQUESTS.md item 5, and the reason it is a request rather than a
    default: all nine service modules resolve the world PER REQUEST from the
    environment, so what this process exports is what a row ends up carrying.

    * `ALKERA_GATEWAY_CONFIG` - which world the services serve.
    * `ALKERA_NATIVE_AUDIT`   - the 9.2.2 file every row is appended to. Named
      explicitly rather than left to `ALKERA_AUDIT_DIR`, because a PATH shim
      the agent runs is a separate process reading the same variable, and the
      two must land in one file.
    * `ALKERA_SCHEDULER`      - the world's scheduler state, whose `vnow` is
      the `vts` on every row. Unset, `native.audit` falls back to
      `GatewayConfig().virtual_time`, a fixed literal, and every row in every
      episode would carry the same wrong stamp.

    `setdefault`, not assignment: a caller that already exported one of these
    (the harness, a test, a mixed stack) meant it.
    """
    env = {
        "ALKERA_NATIVE_AUDIT": str(audit / "actions.jsonl"),
        "ALKERA_SCHEDULER": str(world / "var" / "sched" / "state.json"),
    }
    if gateway_config is not None:
        env["ALKERA_GATEWAY_CONFIG"] = str(gateway_config)
    for name, value in env.items():
        os.environ.setdefault(name, value)
    return {name: os.environ[name] for name in env}


def minio_receiver(port: int) -> tuple[Mounted | None, str | None]:
    """AUDIT's MinIO audit-webhook receiver, mounted on this process.

    Returns `(mounted, None)` or `(None, why)`. It is not fatal: MinIO's
    `MINIO_AUDIT_WEBHOOK_QUEUE_DIR` spools every event on disk until something
    answers, so a receiver that failed to build costs the stack latency rather
    than rows - but the reason is carried into the startup table and the
    aggregate's `missing`, because a storage plane whose rows are all still in
    a spool directory must not look like a storage plane nobody used.
    """
    try:
        from .tailers.minio import build_app
    except ImportError as exc:                         # the `native` extra
        return None, f"{type(exc).__name__}: {exc}"
    return Mounted(MINIO_AUDIT_NAME, "native-services.internal", port,
                   build_app(), "receiver", False, destination=False), None


def sched_plane(port: int) -> tuple[Mounted | None, str | None]:
    """The scheduler plane, mounted on this process. See `native/sched_plane.py`.

    Returns `(mounted, None)` or `(None, why)`, the shape `minio_receiver` has
    and for the same reason: fastapi arrives with the `native` extra, and a
    process that cannot import it must still serve the nine and SAY what it
    could not serve rather than failing to start. The routes live in their own
    module because this one carries `from __future__ import annotations`, which
    stops FastAPI resolving a handler's `Request` annotation - that module's
    docstring has the measurement.
    """
    try:
        from .sched_plane import NAME, build_app
    except ImportError as exc:                         # the `native` extra
        return None, f"{type(exc).__name__}: {exc}"
    return Mounted(NAME, f"{NAME}.internal", port, build_app(),
                   "scheduler", True), None


def port_disagreements(mounted: list[Mounted], offset: int = 0) -> dict[str, str]:
    """Services whose declared port is not the one the subject is told about.

    `STUB_PORTS` is the harness's table (NATIVE_DRIVER_REQUESTS.md item 3). A
    module is free to listen wherever it likes and this process will serve it
    there, but the subject's `JIRA_URL` carries the number below - so a
    disagreement is a dead surface with a live process behind it, which is the
    one failure that looks like the agent's fault.
    """
    known = service_ports()
    return {
        item.name: f"serving {item.port}, the subject is told "
                   f"{known[item.name] + offset}"
        for item in mounted
        if item.origin == "module" and item.name in known
        and item.port != known[item.name] + offset
    }


def aggregate_app(mounted: list[Mounted], missing: dict[str, str],
                  environment: dict[str, str] | None = None,
                  offset: int = 0) -> Callable:
    """`GET /health` on the aggregate port: everything this process serves.

    `environment` is the three variables `serve.py` propagates to the services
    (SERVICES_A_REQUESTS.md item 5). They are published here because a row
    stamped with the wrong `vts` is the failure they exist to prevent, and
    "which scheduler state is this process reading" should be one request
    rather than a look inside a container.
    """
    return JSONResponder({
        "ok": bool(mounted) and not missing
              and not port_disagreements(mounted, offset),
        "service": "native-services",
        "port_offset": offset,
        "items": len(mounted),
        "environment": dict(sorted((environment or {}).items())),
        "services": [
            {"service": item.name, "host": item.host, "port": item.port,
             "url": f"{'https' if item.tls else 'http'}://{item.host}:{item.port}",
             "origin": item.origin,
             "own_health": item.own_health}
            for item in mounted
        ],
        "missing": dict(sorted(missing.items())),
        "port_disagreements": port_disagreements(mounted, offset),
    })


def startup_table(mounted: list[Mounted], missing: dict[str, str],
                  bind: str, audit: Path, world: Path, offset: int = 0) -> str:
    """The table printed at startup: what is being served, and what is not."""
    lines = [
        f"alkera native services  bind={bind}  audit={audit}  world={world}"
        + (f"  port_offset={offset}" if offset else ""),
        f"{'service':<14}{'alias':<24}{'port':>6}  state",
    ]
    disagree = port_disagreements(mounted, offset)
    for item in mounted:
        # The health note applies to the surfaces the gate probes. The MinIO
        # receiver is not one: nothing asks it for /health, and claiming
        # `items=0` for it would put a dead surface in a table the gate reads.
        state = item.origin
        if item.origin in ("module", "stub") and not item.own_health:
            state += " (+/health items=0)"
        if not item.destination:
            state += " (not a destination: the stack dials it, not the episode)"
        if item.name in disagree:
            state += f"  PORT DISAGREES: {disagree[item.name]}"
        lines.append(f"{item.name:<14}{item.host:<24}{item.port:>6}  {state}")
    for name, why in sorted(missing.items()):
        lines.append(f"{name:<14}{'-':<24}{'-':>6}  MISSING: {why}")
    lines.append(f"loaded={len(mounted)} missing={len(missing)} "
                 f"port_disagreements={len(disagree)}")
    return "\n".join(lines)


class _Server(uvicorn.Server):
    """uvicorn's server with its own signal capture disabled.

    `uvicorn.Server.serve` installs `handle_exit` for the duration of the run.
    N servers share one loop here, they start one after another, and each
    install replaces the last - so a SIGTERM would stop only the server that
    started last and the process would hang on the other eight. `serve_all`
    installs one handler for the whole set instead.
    """

    @contextlib.contextmanager
    def capture_signals(self):
        yield


def tls_options(item: Mounted) -> dict[str, str]:
    """uvicorn's `ssl_certfile`/`ssl_keyfile` for a TLS-serving item, {} otherwise."""
    if not item.tls:
        return {}
    if not callable(item.certificate):
        raise SystemExit(f"{item.name} is a TLS backend (its registry row says tls=True) "
                         f"but its module defines no certificate(); refusing to serve it "
                         f"over plain HTTP")
    certfile, keyfile = item.certificate()
    return {"ssl_certfile": str(certfile), "ssl_keyfile": str(keyfile)}


async def serve_all(mounted: list[Mounted], bind: str,
                    log: DestinationLog) -> None:
    """Run every mounted service in this process, on one event loop."""
    AuditedH11Protocol.destinations = log
    servers = [
        _Server(uvicorn.Config(
            item.app, host=bind, port=item.port, http=AuditedH11Protocol,
            log_level="warning", access_log=False, lifespan="auto",
            timeout_graceful_shutdown=5, **tls_options(item),
        ))
        for item in mounted
    ]

    def stop(*_: object) -> None:
        for server in servers:
            server.should_exit = True

    installed = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):
            installed[sig] = signal.signal(sig, stop)
    sockets = []
    tasks = []

    async def run(server, listener):
        try:
            await server.serve(sockets=[listener])
        except SystemExit as exc:
            raise RuntimeError("Native listener failed to start") from exc

    try:
        # Bind the complete set before publishing any HTTP surface. One busy
        # port must not leave the remaining services running as a partial stack.
        for item in mounted:
            listener = socket.create_server(
                (bind, item.port), family=socket.AF_INET6 if ":" in bind else socket.AF_INET
            )
            listener.setblocking(False)
            sockets.append(listener)
        tasks = [asyncio.create_task(run(server, listener))
                 for server, listener in zip(servers, sockets, strict=True)]
        if tasks:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            expected = all(server.should_exit for server in servers)
            stop()
            for task in done:
                task.result()
            if not expected:
                raise RuntimeError("Native listener stopped unexpectedly")
    finally:
        stop()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=10)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for listener in sockets:
            listener.close()
        for sig, previous in installed.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, previous)
        log.flush()
        AuditedH11Protocol.destinations = None


def parse_stub(value: str) -> tuple[str, int]:
    """`NAME` or `NAME:PORT` -> (name, port). `NAME` alone needs a known port."""
    name, _, port = value.partition(":")
    if not name:
        raise argparse.ArgumentTypeError(f"--stub {value!r} has no name")
    if port:
        return name, int(port)
    known = service_ports()
    if name not in known:
        raise argparse.ArgumentTypeError(
            f"--stub {name!r} is not one of {sorted(known)}; give it a "
            f"port as {name}:PORT")
    return name, known[name]


def _profile(profile: str | None, plug_args: Sequence[str]):
    if profile is None:
        if plug_args:
            raise ProfileError("--plug requires --profile")
        return None
    return resolve(profile, parse_plug_args(plug_args))


def selected_services(profile: str | None,
                      plug_args: Sequence[str]) -> tuple[str, ...] | None:
    """The profile's native services, or None (the nine) with no profile.

    A profile's list may name an explicit-only BI vendor (`services.PLUG_NAMES`)
    - `bi=powerbi` mounts powerbi at 8095 in place of metabase - and `load`
    accepts it because it is named; None never does. A hosted backend the
    profile plugs (`alkera_gateway.backends.plugged`, `hosted=True`) is named
    after them, from the registry, and mounted on its row's port; a profile
    that plugs two TLS backends is a ProfileError here."""
    from ..backends import plugged

    resolved = _profile(profile, plug_args)
    if resolved is None:
        return None
    hosted = tuple(b.name for b in plugged(resolved.slots)
                   if b.hosted and b.name not in resolved.services)
    return resolved.services + hosted


def sched_enabled(profile: str | None, plug_args: Sequence[str]) -> bool:
    """True with no profile, or when the profile's orchestrator plugs ops_sched."""
    resolved = _profile(profile, plug_args)
    return resolved is None or "ops_sched" in resolved.slots.get("orchestrator", ())


def object_store_enabled(profile: str | None, plug_args: Sequence[str]) -> bool:
    """True with no profile, or when the profile plugs the object_store slot.
    Gates the object store's audit receiver: no store, nothing to receive."""
    resolved = _profile(profile, plug_args)
    return resolved is None or "object_store" in resolved.slots


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="alkera_gateway.native.serve")
    parser.add_argument("--audit", type=Path, required=True,
                        help="audit directory: actions.jsonl and destinations.jsonl")
    parser.add_argument("--world", type=Path, required=True,
                        help="world root; the services read and mutate its fixtures")
    parser.add_argument("--bind", default="127.0.0.1",
                        help="listen address (0.0.0.0 inside the episode network)")
    parser.add_argument("--stub", action="append", type=parse_stub, default=[],
                        metavar="NAME[:PORT]",
                        help="serve a stub under NAME; repeatable. Used by "
                             "containment P6 and the tests while a service "
                             "module has not landed.")
    parser.add_argument("--aggregate-port", type=int, default=AGGREGATE_PORT,
                        help="port for the aggregate /health on "
                             "native-services.internal")
    parser.add_argument("--gateway-config", type=Path, default=None,
                        help="the gateway config JSON the services resolve the "
                             "world through (ALKERA_GATEWAY_CONFIG). Derived "
                             "from --world and written beside the audit log "
                             "when this is not given.")
    parser.add_argument("--sched-port", type=int, default=SCHED_PORT,
                        help="the scheduler plane's port, under the alias "
                             "sched.internal; 0 serves it not at all, which "
                             "leaves the container topology with no scheduler "
                             "surface (docs/implementation_plan/harness/NATIVE_CONTAINER.md)")
    parser.add_argument("--destination-host", default=None,
                        help="the host to record in destinations.jsonl, when "
                             "it is not the service's own DNS alias. The "
                             "harness compares that file against the endpoint "
                             "map the episode was handed "
                             "(`agent_under_test.allowed_destinations`), and "
                             "off the episode network the episode reaches "
                             "these services at a loopback address, not at "
                             "`<name>.internal` - so a row naming the alias "
                             "would be a destination outside the episode's own "
                             "surfaces and would quarantine every host-topology "
                             "rollout.")
    parser.add_argument("--port-offset", type=int, default=0,
                        help="add this to every port, including the aggregate "
                             "and the audit receiver. 0 inside the episode "
                             "network, where the ports are the contract; "
                             "non-zero on the host topology, where K rollouts "
                             "share one loopback interface.")
    parser.add_argument("--minio-audit-port", type=int, default=MINIO_AUDIT_PORT,
                        help="port for MinIO's audit webhook receiver; 0 to "
                             "serve none")
    parser.add_argument("--print-only", action="store_true",
                        help="print the startup table and exit; no socket is opened")
    parser.add_argument("--profile", default=None, metavar="ROLE/STACK",
                        help="serve only this environment profile's services "
                             "(alkera_gateway.plugs); the scheduler plane is "
                             "off unless it plugs ops_sched. Default: everything.")
    parser.add_argument("--plug", action="append", default=[],
                        metavar="SLOT=PLUG[,PLUG]",
                        help="override one slot of --profile; SLOT= removes it. "
                             "Repeatable; needs --profile.")
    args = parser.parse_args(argv)

    # Resolve before touching the world or the audit directory: a profile
    # that cannot be served stops here with nothing written.
    try:
        names = selected_services(args.profile, args.plug)
        with_sched = sched_enabled(args.profile, args.plug)
        with_store = object_store_enabled(args.profile, args.plug)
    except ProfileError as exc:
        print(exc, file=sys.stderr, flush=True)
        return 2

    # The services read the world and write the action log through
    # `native.audit.record`, which takes both from the environment. Set here
    # rather than passed, because a PATH shim the agent runs is a separate
    # process that has to land in the same two files.
    os.environ.setdefault("ALKERA_AUDIT_DIR", str(args.audit))
    os.environ.setdefault("ALKERA_WORLD_ROOT", str(args.world))
    config_path = args.gateway_config
    if config_path is None:
        config_path = args.audit / "native_gateway.json"
        args.audit.mkdir(parents=True, exist_ok=True)
        config_path.write_text(world_gateway_config(args.world, args.audit),
                               encoding="utf-8", newline="")
    environment = service_environment(args.audit, args.world, config_path)

    loaded, missing = services.load(names)
    mounted = mount(loaded, dict(args.stub), args.port_offset)
    aggregate_port = args.aggregate_port + args.port_offset
    audit_port = (args.minio_audit_port + args.port_offset
                  if args.minio_audit_port and with_store else 0)
    if audit_port:
        if any(item.port == audit_port for item in mounted):
            print(f"--minio-audit-port {audit_port} is a service port",
                  file=sys.stderr, flush=True)
            return 1
        receiver, why = minio_receiver(audit_port)
        if receiver is not None:
            mounted.append(receiver)
        else:
            missing = {**missing, MINIO_AUDIT_NAME: why}
    sched_port = (args.sched_port + args.port_offset
                  if args.sched_port and with_sched else 0)
    if sched_port:
        if any(item.port == sched_port for item in mounted):
            print(f"--sched-port {sched_port} is a service port",
                  file=sys.stderr, flush=True)
            return 1
        plane, why = sched_plane(sched_port)
        if plane is not None:
            mounted.append(plane)
        else:
            # Not fatal, and the reason travels: a stack whose scheduler plane
            # failed to build is a stack where `./ops/sched` answers nothing in
            # the container topology, and the startup table is where a reader
            # finds that out rather than by watching an episode fail.
            missing = {**missing, SCHED_NAME: why}
    print(startup_table(mounted, missing, args.bind, args.audit, args.world,
                        args.port_offset),
          flush=True)
    for name, value in sorted(environment.items()):
        print(f"{name}={value}", flush=True)
    if not mounted:
        print("nothing to serve: no service module imported and no --stub given",
              file=sys.stderr, flush=True)
        return 1
    # The aggregate is mounted last and on its own port, so it can report the
    # set it is part of. It is a `Mounted` like any other, which means it is
    # also a row in the destination log: a connection to `native-services
    # .internal` is a destination the episode reached like any other.
    if any(item.port == aggregate_port for item in mounted):
        print(f"--aggregate-port {aggregate_port} is a service port",
              file=sys.stderr, flush=True)
        return 1
    mounted.append(Mounted("native-services", "native-services.internal",
                           aggregate_port,
                           aggregate_app(mounted, missing, environment,
                                         args.port_offset),
                           "aggregate", True))
    log = DestinationLog(args.audit / DESTINATIONS)
    for item in mounted:
        if item.destination:
            log.declare(args.destination_host or item.host, item.port)
    log.flush()
    if args.print_only:
        return 0
    asyncio.run(serve_all(mounted, args.bind, log))
    return 0


if __name__ == "__main__":
    sys.exit(main())
