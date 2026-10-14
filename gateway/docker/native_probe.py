"""Containment P6, measured from inside the episode container.

`gateway/NATIVE.md`: in native mode the agent has bash and a network
instead of a tool list, and the boundary moves from the tool surface to the
episode network. P6 is the claim that moved boundary makes:

    only the fake services are reachable.

Three questions, each answered by trying, never by reading compose.native.yml:

  A. every `<name>.internal` resolves, and answers `GET /health` with
     `{"ok": true, "service": NAME, "items": >0}` (an HTTP service) or accepts
     a TCP connection (postgres, and anything else that does not speak HTTP);
     `items > 0` and not merely a 200, because a health check over an empty
     world is the runner-up finding in docs/implementation_plan/harness/NATIVE_DRIVER.md;
  B. a control name does not resolve - `example.internal`, which looks exactly
     like a service and is not one; the backends the overlay detaches the
     episode from; and a public host, which is the egress control P2 already
     owns and is repeated here because a DNS answer for it would mean the
     network changed under us. The compose service names of the containers the
     episode DOES share a network with are a measured exception, recorded under
     `--compose-name` and pinned to this stack's own addresses;
  C. TCP to a port that is not on the list is refused, on the very addresses
     that answered in A - so "the service is reachable" cannot be confused with
     "the host is reachable".
  D. (`--sweep-cidr`) EVERY address of the on-link subnet - the bridge gateway,
     which is the host, included - and on each live one EVERY port, not six.
     Each open port that is not an expected service's is a `not_audited` row,
     with whatever it says first. C only ever looked at the addresses that
     answered, so the host's sshd at `<episode_net>.1:22`, MinIO's console on a
     random port and MinIO's audit receiver went unseen until a sweep found
     them (gateway/docker/evidence/CONTAINMENT_2026-10-07.md). `passed` stays
     the claim A-C make; the sweep's rows are reported beside it
     (`every_open_port_is_expected`), because which of those doors are closed
     is decided elsewhere (the host rule, the Postgres role, MinIO's console).

Standard library only, so it runs under the episode image's interpreter with
nothing installed. `gateway/docker/containment.py --native` runs it with
`compose exec` and folds the JSON on stdout into evidence/containment-native
.json; it is also runnable by hand inside a live stack::

    docker compose ... exec -T episode python /opt/stack/native_probe.py

A probe that could not detect a hole reports a clean stack, so `--self-test`
runs B and C against a name and a port the probe knows are open - it is the
same positive control P2's egress probe carries.
"""
from __future__ import annotations

import argparse
import errno
import ipaddress
import json
import selectors
import socket
import sys
import time
import urllib.error
import urllib.request
from collections import Counter

#: Names that must NOT resolve from the episode container.
#:   *.internal that is not a service  - the shape of a real alias, and not one
#:   backends off episode_net          - `s3` and the fake Snowflake account
#:                                       hostname live on backend_net only, so
#:                                       detaching the episode from it must
#:                                       have taken them with it
#:   public hosts                      - the egress control, restated
#:
#: NOT here, and measured separately: the compose service name of a container
#: the episode IS on a network with. See `--compose-name`.
CONTROL_NAMES = ("example.internal", "warehouse.internal",
                 "s3", "snowflake", "acme.snowflake.local",
                 "example.com", "pypi.org")
#: Ports probed on every address that answered. None is a service port in the
#: native stack, and each is something a real box would have had open.
CLOSED_PORTS = (22, 80, 443, 3306, 6379, 8888)
HEALTH_TIMEOUT = 5
CONNECT_TIMEOUT = 3
#: The sweep (question D). An address is live when any of these ports - plus
#: the closed-port list - answers at all, CONNECTED or refused; 9 (discard) is
#: one nothing in the stack serves, so a refusal there is the cheapest "a host
#: is here". Every port of a live address is then tried with SWEEP_TIMEOUT, at
#: most SWEEP_WINDOW connections in flight (the episode's `ulimit -n` is 1024).
LIVENESS_PORTS = (9,)
SWEEP_TIMEOUT = 1.0
SWEEP_WINDOW = 512
BANNER_BYTES = 300


def controls(on_episode_net=(), must_not_resolve=(),
             base: tuple[str, ...] = CONTROL_NAMES) -> tuple[str, ...]:
    """The control list for one stack: `base` minus every compose service name
    that is ON episode_net (Docker registers it there and nothing unregisters
    it, so it is pinned with `--compose-name` instead - `snowflake` on a box
    that plugs Snowflake), plus `must_not_resolve` (the names of running
    services that are NOT on episode_net, the hosts a profile excludes)."""
    on = set(on_episode_net)
    names = [n for n in base if n not in on]
    names += [n for n in must_not_resolve if n not in on]
    return tuple(dict.fromkeys(names))


def registry_expectation() -> list[dict]:
    """The nine services and the ports they are served on, from the registry.

    A module that imported declares its own `PORT`; one that has not landed
    falls back to `serve.STUB_PORTS`, which is the port its stub is served on
    and the port the module is expected to take. The two disagreeing is a
    finding, not a fallback: the alias would answer on one port and this probe
    would look at the other, and it would say so.
    """
    from alkera_gateway.native import serve, services

    loaded, _ = services.load()
    rows = []
    for name in services.NAMES:
        module = loaded.get(name)
        host = getattr(module, "HOST", None) or f"{name}.internal"
        port = int(getattr(module, "PORT", 0) or serve.STUB_PORTS[name])
        rows.append({"host": host, "port": port, "probe": "health",
                     "source": "module" if module is not None else "STUB_PORTS"})
    return rows


def normalise(spec: object) -> list[dict]:
    """Accept `{"jira.internal": 8081}` or the full list of row objects."""
    if isinstance(spec, dict):
        return [{"host": host, "port": int(port), "probe": "health",
                 "source": "--expect"}
                for host, port in sorted(spec.items())]
    rows = []
    for row in spec:
        rows.append({"host": row["host"], "port": int(row["port"]),
                     "probe": row.get("probe", "health"),
                     "source": row.get("source", "--expect")})
    return rows


def resolve(name: str) -> dict:
    try:
        return {"name": name,
                "addresses": sorted({info[4][0] for info in socket.getaddrinfo(name, None)})}
    except OSError as exc:
        return {"name": name, "error": type(exc).__name__}


def connect(host: str, port: int) -> str:
    try:
        socket.create_connection((host, port), timeout=CONNECT_TIMEOUT).close()
        return "CONNECTED"
    except OSError as exc:
        return type(exc).__name__


def health(host: str, port: int) -> dict:
    """`GET /health`, and whether the world behind it has anything in it.

    `docs/implementation_plan/harness/NATIVE_DRIVER_REQUESTS.md` items 1-2 fix the body as
    `{"ok": true, "service": NAME, "items": int}`. `items` is checked here and
    not only `status == 200`, because a 200 from a service with an empty world
    is exactly the failure NATIVE_DRIVER.md names as its runner-up: the surface
    gate passes, the rollout is scored, and the world was half absent.
    """
    url = f"http://{host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=HEALTH_TIMEOUT) as response:
            raw = response.read(4096).decode("utf-8", "replace")
            out = {"url": url, "status": response.status, "body": raw.strip()[:300]}
    except urllib.error.HTTPError as exc:
        return {"url": url, "status": exc.code, "body": None, "items": None}
    except OSError as exc:
        return {"url": url, "status": None, "items": None,
                "error": f"{type(exc).__name__}: {exc}"[:200]}
    try:
        payload = json.loads(raw)
    except ValueError:
        out["items"] = None
        out["error"] = "body is not JSON"
        return out
    out["ok"] = payload.get("ok")
    out["service"] = payload.get("service")
    out["items"] = payload.get("items")
    return out


def _result(code: int) -> str:
    """An errno as the exception name `connect` would have recorded for it."""
    if code == 0:
        return "CONNECTED"
    return type(OSError(code, "")).__name__


def scan(targets, timeout: float = SWEEP_TIMEOUT,
         window: int = SWEEP_WINDOW) -> dict[tuple[str, int], str]:
    """Non-blocking TCP connects to every `(address, port)`, at most `window` in
    flight: CONNECTED, the refusal's exception name, or "timeout". One thread,
    so a full sweep of a /24's live hosts is seconds, not the hour a blocking
    connect per port would take."""
    results: dict[tuple[str, int], str] = {}
    pending = iter(targets)
    selector = selectors.DefaultSelector()
    exhausted = False
    try:
        while True:
            while not exhausted and len(selector.get_map()) < window:
                target = next(pending, None)
                if target is None:
                    exhausted = True
                    break
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setblocking(False)
                code = sock.connect_ex(target)
                if code in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
                    selector.register(sock, selectors.EVENT_WRITE,
                                      (target, time.monotonic() + timeout))
                else:
                    results[target] = _result(code)
                    sock.close()
            if not selector.get_map():
                break
            for key, _ in selector.select(timeout=0.05):
                sock = key.fileobj
                results[key.data[0]] = _result(
                    sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR))
                selector.unregister(sock)
                sock.close()
            now = time.monotonic()
            for key in list(selector.get_map().values()):
                if key.data[1] <= now:
                    results[key.data[0]] = "timeout"
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
    finally:
        for key in list(selector.get_map().values()):
            key.fileobj.close()
        selector.close()
    return results


def banner(host: str, port: int) -> dict:
    """What an open port says: first unprompted, then to `GET /`. Read-only -
    a request line and nothing else - and recorded as text, cut short."""
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT) as sock:
            sock.settimeout(1.5)
            try:
                first = sock.recv(BANNER_BYTES)
            except TimeoutError:
                first = b""
            if first:
                return {"spoke_first": first.decode("utf-8", "replace")}
            sock.sendall(b"GET / HTTP/1.0\r\nHost: " + host.encode() + b"\r\n\r\n")
            try:
                reply = sock.recv(BANNER_BYTES)
            except TimeoutError:
                reply = b""
            return {"http_get_reply": reply.decode("utf-8", "replace")} if reply \
                else {"silent": True}
    except OSError as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:200]}


def _ports_label(ports: range) -> str:
    return f"{ports.start}-{ports.stop - 1}" if len(ports) else "none"


def sweep(cidr: str, ports: range, *, skip=(), extra_ports=()) -> dict:
    """Question D: liveness of every host address of `cidr` (the gateway too),
    then every port in `ports` on each live one. `skip` is the vantage's own
    addresses - reaching yourself is not a door."""
    started = time.monotonic()
    network = ipaddress.ip_network(cidr, strict=False)
    hosts = [str(a) for a in (network.hosts() if network.num_addresses > 2
                              else network)]
    own = sorted(set(skip) & set(hosts))
    candidates = [a for a in hosts if a not in own]
    liveness_ports = tuple(dict.fromkeys((*LIVENESS_PORTS, *extra_ports)))
    first = scan([(a, p) for a in candidates for p in liveness_ports])
    live = [a for a in candidates
            if any(first[(a, p)] in ("CONNECTED", "ConnectionRefusedError")
                   for p in liveness_ports)]
    full = scan([(a, p) for a in live for p in ports if (a, p) not in first])
    results = {**first, **full}
    counts = {a: dict(Counter(r for (addr, _), r in results.items() if addr == a))
              for a in live}
    odd = sorted(f"{a}:{p}" for (a, p), r in full.items()
                 if r not in ("CONNECTED", "ConnectionRefusedError"))
    return {
        "cidr": str(network),
        "addresses_in_cidr": len(hosts),
        "own_addresses_skipped": own,
        "liveness_ports": list(liveness_ports),
        "liveness": {a: first[(a, liveness_ports[0])] for a in candidates},
        "live_addresses": live,
        "ports": _ports_label(ports),
        "timeout_s": SWEEP_TIMEOUT,
        "result_counts": counts,
        "neither_open_nor_refused_first_200": odd[:200],
        "open": sorted((f"{a}:{p}" for (a, p), r in results.items() if r == "CONNECTED"),
                       key=_target_key),
        "seconds": round(time.monotonic() - started, 1),
    }


def _target_key(target: str) -> tuple:
    address, _, port = target.rpartition(":")
    return (tuple(int(x) for x in address.split(".")), int(port))


def interfaces() -> dict:
    """The container's own network namespace, from /proc.

    compose.native.yml claims it adds no interface and no route out. That is a
    property of the running container, so it is read here rather than asserted
    there; P2 makes the same read for every service in the non-native stack.
    """
    names, routes = [], []
    try:
        for line in open("/proc/net/dev", encoding="utf-8").read().splitlines()[2:]:
            name = line.split(":")[0].strip()
            if name:
                names.append(name)
        for line in open("/proc/net/route", encoding="utf-8").read().splitlines()[1:]:
            fields = line.split()
            if len(fields) > 2:
                dest = ".".join(str(int(fields[1][i:i + 2], 16)) for i in (6, 4, 2, 0))
                routes.append({"iface": fields[0], "dest": dest})
    except OSError as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"interfaces": names, "routes": routes,
            "default_route": any(r["dest"] == "0.0.0.0" for r in routes)}


def probe(expected: list[dict], controls: tuple[str, ...],
          closed_ports: tuple[int, ...],
          compose_names: tuple[str, ...] = (), *,
          sweep_cidr: str | None = None,
          sweep_ports: range = range(1, 65536)) -> dict:
    services_out, addresses = [], set()
    #: (address, port) of every expected door, and who is at each address -
    #: what the sweep (D) holds its open ports against.
    expected_targets, names_at = set(), {}
    for row in expected:
        found = resolve(row["host"])
        entry = {**row, "resolution": found}
        if found.get("addresses"):
            addresses.update(found["addresses"])
            for address in found["addresses"]:
                expected_targets.add((address, row["port"]))
                names_at.setdefault(address, set()).add(row["host"])
            entry["tcp"] = connect(row["host"], row["port"])
            if row["probe"] == "health":
                answer = health(row["host"], row["port"])
                entry["health"] = answer
                entry["reachable"] = (
                    entry["tcp"] == "CONNECTED"
                    and answer.get("status") == 200
                    and answer.get("ok") is True
                    and isinstance(answer.get("items"), int)
                    and answer["items"] > 0)
            else:
                entry["reachable"] = entry["tcp"] == "CONNECTED"
        else:
            entry["reachable"] = False
        services_out.append(entry)

    control_rows = [resolve(name) for name in controls]
    # Docker's embedded resolver registers a container's COMPOSE SERVICE NAME on
    # every network it joins, and compose offers no way to unregister it. So
    # `postgres` resolves from the episode for the same reason `postgres
    # .internal` does, and NATIVE.md's "nothing else resolves" is not
    # literally true. Recorded rather than asserted away, and pinned: each of
    # these must resolve to an address that is already one of OUR services, so
    # a name that resolves to something else is still a failure.
    # The episode's own container is one of the compose services, and it
    # resolves its own name to itself, so its address counts as known too.
    own = set(resolve(socket.gethostname()).get("addresses") or [])
    compose_rows = []
    for name in compose_names:
        found = resolve(name)
        found["points_at_a_known_service"] = bool(
            found.get("addresses")) and set(found["addresses"]) <= (addresses | own)
        compose_rows.append(found)
        for address in found.get("addresses") or []:
            names_at.setdefault(address, set()).add(name)

    open_ports = {row["port"] for row in expected}
    refused = []
    for address in sorted(addresses):
        for port in closed_ports:
            if port in open_ports:
                continue
            refused.append({"target": f"{address}:{port}", "result": connect(address, port)})

    out = {
        "vantage": "episode container, episode_net only",
        "own_addresses": sorted(own),
        "netns": interfaces(),
        "expected": expected,
        "services": services_out,
        "controls": control_rows,
        "compose_service_names": compose_rows,
        "dns_deviation": (
            "Docker registers a container's compose service name on every "
            "network it joins and gives no way to unregister it, so the names "
            "below resolve in addition to the aliases. Each is pinned to an "
            "address that is already one of this stack's own services; a name "
            "resolving anywhere else fails this property. Removing them needs "
            "a resolver of our own in front of 127.0.0.11 - see "
            "gateway/docker/NATIVE_STACK.md."),
        "closed_ports_probed": sorted(set(closed_ports) - open_ports),
        "addresses_probed": sorted(addresses),
        "unexpected_open": [row["target"] for row in refused if row["result"] == "CONNECTED"],
        "closed_port_attempts": refused,
    }
    out["services_unreachable"] = [row["host"] for row in services_out
                                   if not row["reachable"]]
    out["controls_that_resolved"] = [row["name"] for row in control_rows
                                     if row.get("addresses")]
    out["compose_names_pointing_elsewhere"] = [
        row["name"] for row in compose_rows if not row["points_at_a_known_service"]]
    out["passed"] = bool(
        services_out
        and not out["services_unreachable"]
        and not out["controls_that_resolved"]
        and not out["compose_names_pointing_elsewhere"]
        and not out["unexpected_open"]
        and out["closed_ports_probed"]          # a probe with nothing to refuse proves nothing
        and out["netns"].get("default_route") is False
    )
    out["not_audited"] = []
    if sweep_cidr:
        found = sweep(sweep_cidr, sweep_ports, skip=own, extra_ports=closed_ports)
        found["saw_every_expected_port_open"] = all(
            f"{a}:{p}" in found["open"] for a, p in expected_targets
            if p in sweep_ports and a not in own)
        out["sweep"] = found
        for target in found["open"]:
            address, _, port = target.rpartition(":")
            if (address, int(port)) in expected_targets:
                continue
            out["not_audited"].append({
                "target": target, "address": address, "port": int(port),
                "names": sorted(names_at.get(address, ())),
                "banner": banner(address, int(port))})
        out["every_open_port_is_expected"] = not out["not_audited"]
    return out


def self_test(expected: list[dict]) -> dict:
    """Show the probe can fail. Resolve a name that IS served and connect to a
    port that IS open, through the same two functions the properties use."""
    if not expected:
        return {"ran": False, "why": "nothing expected to test against"}
    row = expected[0]
    found = resolve(row["host"])
    return {
        "ran": True,
        "name_that_should_resolve": found,
        "port_that_should_answer": {"target": f"{row['host']}:{row['port']}",
                                    "result": connect(row["host"], row["port"])},
        "probe_can_see_an_open_name_and_port": bool(found.get("addresses"))
        and connect(row["host"], row["port"]) == "CONNECTED",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="native_probe")
    parser.add_argument("--expect", default=None,
                        help="JSON: {alias: port} or [{host, port, probe}]; "
                             "default is the service registry")
    parser.add_argument("--backend", action="append", default=[], metavar="HOST:PORT",
                        help="a real backend (postgres, minio, airflow, fakesnow) "
                             "that must also be reachable; repeatable. TCP only - "
                             "none of them serves /health. Added to the registry "
                             "expectation, so a stack that is not running one does "
                             "not have to claim it.")
    parser.add_argument("--control", action="append", default=[],
                        help="a name that must NOT resolve; repeatable, "
                             "replaces the default list")
    parser.add_argument("--compose-name", action="append", default=[],
                        help="a compose service name the episode shares a "
                             "network with; repeatable. Recorded, and required "
                             "to resolve to one of this stack's own services.")
    parser.add_argument("--closed-port", action="append", type=int, default=[],
                        help="a port that must be refused; repeatable, "
                             "replaces the default list")
    parser.add_argument("--self-test", action="store_true",
                        help="also record the positive control")
    parser.add_argument("--sweep-cidr", default=None, metavar="CIDR",
                        help="question D: every address of this subnet (the "
                             "episode network's, gateway included), and every "
                             "port of each live one")
    parser.add_argument("--sweep-ports", default="1-65535", metavar="A-B",
                        help="the ports D tries on each live address")
    args = parser.parse_args(argv)
    low, _, high = args.sweep_ports.partition("-")
    sweep_ports = range(int(low), int(high or low) + 1)

    expected = normalise(json.loads(args.expect)) if args.expect else registry_expectation()
    for item in args.backend:
        host, _, port = item.rpartition(":")
        expected.append({"host": host, "port": int(port), "probe": "tcp",
                         "source": "--backend"})
    result = probe(expected,
                   tuple(args.control) or CONTROL_NAMES,
                   tuple(args.closed_port) or CLOSED_PORTS,
                   tuple(args.compose_name),
                   sweep_cidr=args.sweep_cidr, sweep_ports=sweep_ports)
    if args.self_test:
        result["self_test"] = self_test(expected)
        result["passed"] = result["passed"] and result["self_test"].get(
            "probe_can_see_an_open_name_and_port", False)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
