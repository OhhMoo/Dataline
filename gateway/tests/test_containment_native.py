"""Containment instruments that see every door (P6, P7 and the box sweep).

    cd gateway && .venv/bin/python -m pytest tests/test_containment_native.py -q

gateway/docker/evidence/CONTAINMENT_2026-10-07.md found three `passed: false`
files that were the instruments' fault, not the stack's:

- P6 on da/snowflake: its control list pinned `snowflake`, which is the compose
  name of a service that is ON episode_net there;
- P6 on de/gcp: the BigQuery engine is correctly unreachable, but P6 picked
  backends by RUNNING compose service and passed every running service as a
  compose name, so the engine's absence read as a failure;
- P7 on de/databricks: it asked a TLS port for `/health` in plain HTTP.

and two doors no instrument looked at: the bridge gateway (the host's sshd)
and every port that is not one of six. These tests pin the fixes. No Docker:
P6 runs against a fake stack whose `docker inspect` documents are written
here, the sweep and the P7 probe body run for real on loopback.
"""
from __future__ import annotations

import json
import socket
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

DOCKER = Path(__file__).resolve().parents[1] / "docker"
sys.path.insert(0, str(DOCKER))

import containment  # noqa: E402
import native_episode_probe as nep  # noqa: E402
import native_probe  # noqa: E402

from alkera_gateway import backends  # noqa: E402

PROJECT = "alkera-t4-unit"
EPISODE_NET = f"{PROJECT}_episode_net"
BACKEND_NET = f"{PROJECT}_backend_net"


# ------------------------------------------------------------------ fakes
def container(service: str, episode: str | None = None, backend: str | None = None, *,
              aliases=(), backend_aliases=(), cmd=None) -> dict:
    """One `docker inspect` document, as compose stamps it."""
    name = f"{PROJECT}-{service}-1"
    networks = {}
    if episode:
        networks[EPISODE_NET] = {"IPAddress": episode,
                                 "Aliases": [name, service, *aliases]}
    if backend:
        networks[BACKEND_NET] = {"IPAddress": backend,
                                 "Aliases": [name, service, *backend_aliases]}
    return {"Id": f"{service}-0123456789ab", "Name": f"/{name}",
            "Config": {"Labels": {"com.docker.compose.service": service,
                                  "com.docker.compose.project": PROJECT},
                       "Image": "alkera-episode:t4-unit", "Cmd": cmd or [], "Env": []},
            "HostConfig": {"NetworkMode": EPISODE_NET},
            "NetworkSettings": {"Networks": networks, "Ports": {}},
            "Mounts": []}


def native_services(episode: str, profile: str | None, plugs=(), *,
                    backend: str | None = None) -> dict:
    """native-services carrying exactly the aliases `profile` is served."""
    served = nep.expectations(profile, list(plugs))["services"]
    hosted = [b.host for b in backends.BACKENDS.values() if b.hosted
              and b.name in served]
    aliases = ["native-services.internal",
               *(f"{name}.internal" for name in served if name not in backends.BACKENDS),
               *hosted]
    words = ("exec python /opt/stack/native_services.py --state-dir /service-state "
             "-- --audit /audit --world /world"
             + (f" --profile {profile}" if profile else "")
             + "".join(f" --plug {p}" for p in plugs))
    return container("native-services", episode, backend, aliases=aliases,
                     cmd=["sh", "-c", words])


def postgres() -> dict:
    return container("postgres", "10.210.9.4", "10.210.8.4", aliases=["postgres.internal"])


def minio() -> dict:
    return container("minio", "10.210.9.5", "10.210.8.5", aliases=["minio.internal"])


#: What `native_probe.probe` prints when nothing is wrong; P6 folds it in.
CLEAN_PROBE = {"passed": True, "services_unreachable": [], "controls_that_resolved": [],
               "compose_service_names": [], "compose_names_pointing_elsewhere": [],
               "unexpected_open": [], "not_audited": [],
               "self_test": {"probe_can_see_an_open_name_and_port": True}}


class FakeStack:
    """The four things P6 asks of a stack, answered from the documents above."""

    files = ()
    rollout_built = True

    def __init__(self, probe: dict | None = None):
        self.argv: list[str] | None = None
        self.probe = probe or CLEAN_PROBE

    def episode_network(self) -> dict:
        return {"name": EPISODE_NET, "subnet": "10.210.9.0/24", "gateway": "10.210.9.1"}

    def native_probe(self, argv):
        self.argv = list(argv)
        return self.probe

    def service_logs(self, service: str) -> str:
        return ""


def values(argv: list[str], flag: str) -> list[str]:
    return [b for a, b in zip(argv, argv[1:], strict=False) if a == flag]


def p6(specs, profile, plugs=None, probe=None):
    stack = FakeStack(probe)
    section = containment.property_6_native(stack, {"_specs": specs},
                                            profile=profile, plugs=plugs or {})
    return stack, section


# ------------------------------------------------------------------- P6
def test_p6_selects_backends_by_alias_carrier_not_running_service():
    """de/databricks: Databricks is HOSTED - a module inside native-services -
    so no compose service is called `databricks` and P6 as written never dialed
    it. The alias is carried by native-services, and that is what selects it."""
    specs = [container("episode", "10.210.9.7"),
             native_services("10.210.9.3", "de/databricks"),
             postgres(), minio()]
    running = {s["Config"]["Labels"]["com.docker.compose.service"] for s in specs}
    assert "databricks" not in running
    stack, section = p6(specs, "de/databricks")
    assert "databricks.internal:443" in section["backends_probed"]
    assert "databricks.internal:443" in values(stack.argv, "--backend")
    # airflow.internal names two rows; nobody carries either here
    assert not {"airflow.internal:8080", "airflow.internal:8100"} & set(
        section["backends_probed"])
    assert section["plugged_backends_not_carried"] == []


def test_p6_control_list_does_not_pin_a_plugged_backend():
    """da/snowflake: `snowflake` is a control name (it must not resolve on a
    box with no Snowflake) AND the compose name of the snowflake service, which
    this profile puts on episode_net. It is pinned as a compose name there."""
    specs = [container("episode", "10.210.9.7"),
             native_services("10.210.9.3", "da/snowflake"),
             postgres(),
             container("snowflake", "10.210.9.2", "10.210.8.2",
                       aliases=["snowflake.internal"],
                       backend_aliases=["acme.snowflake.local"]),
             container("jupyter", "10.210.9.6", aliases=["jupyter.internal"])]
    stack, section = p6(specs, "da/snowflake")
    controls = values(stack.argv, "--control")
    assert "snowflake" not in controls
    assert "snowflake" in values(stack.argv, "--compose-name")
    assert "snowflake" in section["controls_removed_because_on_episode_net"]
    assert {"example.internal", "acme.snowflake.local", "minio.internal",
            "sched.internal"} <= set(controls)
    assert "snowflake.internal:8443" in section["backends_probed"]
    assert "jupyter.internal:8888" in section["backends_probed"]


def test_compose_names_only_for_episode_net_services():
    """de/gcp: the BigQuery engine runs on backend_net only. Its name is a
    must-not-resolve control, not a compose name pinned to a known address."""
    specs = [container("episode", "10.210.9.7"),
             native_services("10.210.9.3", "de/gcp", backend="10.210.8.3"),
             postgres(), minio(),
             container("bigquery", None, "10.210.8.2")]
    stack, section = p6(specs, "de/gcp")
    compose = values(stack.argv, "--compose-name")
    controls = values(stack.argv, "--control")
    assert sorted(compose) == ["episode", "minio", "native-services", "postgres"]
    assert "bigquery" in controls and f"{PROJECT}-bigquery-1" in controls
    assert section["must_not_resolve_off_episode_net"] == ["bigquery",
                                                           f"{PROJECT}-bigquery-1"]
    assert "bigquery.internal:9050" in section["backends_probed"]


def test_p6_names_the_gateway_row_as_the_host_and_keeps_it_out_of_passed():
    """A sweep row at the network's gateway is the HOST. It is reported, loudly,
    as `not_audited`; `passed` stays the claim P6 always made, so the doors
    H1/T7/T8 close are measured separately from what this task fixes."""
    probe = {**CLEAN_PROBE, "not_audited": [
        {"target": "10.210.9.1:22", "address": "10.210.9.1", "port": 22,
         "names": [], "banner": {"spoke_first": "SSH-2.0-OpenSSH_10.2p1\r\n"}},
        {"target": "10.210.9.3:8110", "address": "10.210.9.3", "port": 8110,
         "names": ["native-services"], "banner": {}}]}
    specs = [container("episode", "10.210.9.7"),
             native_services("10.210.9.3", None), postgres(), minio()]
    _, section = p6(specs, None, probe=probe)
    classes = {row["target"]: row["class"] for row in section["not_audited"]}
    assert "the HOST" in classes["10.210.9.1:22"]
    assert "MinIO audit receiver" in classes["10.210.9.3:8110"]
    assert section["every_open_port_is_an_expected_door"] is False
    assert section["passed"] is True


def test_native_backends_name_their_carrier_and_agree_with_the_registry():
    """Every registry backend has its row, keyed by the service that CARRIES its
    alias: native-services for a hosted one, its own container otherwise."""
    from alkera_gateway.native import serve

    rows = {target: containment.NATIVE_CARRIERS[key]
            for key, target in containment.NATIVE_BACKENDS.items()}
    for b in backends.BACKENDS.values():
        assert rows.get((b.host, b.port)) == (
            "native-services" if b.hosted else b.name), b.name
    assert set(containment.NATIVE_CARRIERS) == {*containment.NATIVE_BACKENDS,
                                                *containment.NATIVE_PLANES}
    assert containment.NATIVE_PLANES["aggregate"][1] == serve.AGGREGATE_PORT
    assert containment.NATIVE_PLANES["sched"][1] == serve.SCHED_PORT
    assert containment.MINIO_AUDIT_PORT == serve.MINIO_AUDIT_PORT


# ------------------------------------------------------------- the sweep
def _listener(address: str, port: int = 0) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((address, port))
    sock.listen(8)
    return sock


def test_sweep_reports_the_gateway_address_port_22():
    """The probe sweeps EVERY address of the on-link subnet, the gateway (.1)
    included, not only the addresses an alias resolved to - which is how the
    host's sshd at `<episode_net>.1:22` went unseen. A test cannot bind 22, so
    the fake gateway is 127.77.0.1 of a loopback /30 with a listener on a free
    port; a second listener on .2 is an EXPECTED door and is not reported."""
    gateway = _listener("127.77.0.1")
    port = gateway.getsockname()[1]
    served = _listener("127.77.0.2", port)
    try:
        out = native_probe.probe(
            [{"host": "127.77.0.2", "port": port, "probe": "tcp", "source": "test"}],
            ("alkera-t4.invalid",), (1,),
            sweep_cidr="127.77.0.0/30", sweep_ports=range(port, port + 1))
    finally:
        gateway.close()
        served.close()
    targets = [row["target"] for row in out["not_audited"]]
    assert f"127.77.0.1:{port}" in targets
    assert f"127.77.0.2:{port}" not in targets
    assert "127.77.0.1" in out["sweep"]["live_addresses"]
    assert out["sweep"]["ports"] == f"{port}-{port}"
    assert out["every_open_port_is_expected"] is False


def test_sweep_is_off_unless_asked_and_a_clean_sweep_says_so():
    served = _listener("127.77.0.2")
    port = served.getsockname()[1]
    try:
        quiet = native_probe.probe(
            [{"host": "127.77.0.2", "port": port, "probe": "tcp", "source": "test"}],
            ("alkera-t4.invalid",), (1,))
        clean = native_probe.probe(
            [{"host": "127.77.0.2", "port": port, "probe": "tcp", "source": "test"}],
            ("alkera-t4.invalid",), (1,),
            sweep_cidr="127.77.0.2/32", sweep_ports=range(port, port + 1))
    finally:
        served.close()
    assert "sweep" not in quiet and quiet["not_audited"] == []
    assert clean["not_audited"] == [] and clean["every_open_port_is_expected"] is True
    assert clean["sweep"]["saw_every_expected_port_open"] is True


# ------------------------------------------------------------------- P7
def test_p7_tls_rows_are_probed_by_tcp_or_https_not_plain_http():
    rows = {e.name: e for e in nep.expects("de/databricks", {})}
    assert rows["databricks"].scheme in ("tcp", "https")
    got = nep.expectations("de/databricks", {})
    assert not got["services"]["databricks"].startswith("http://")
    if rows["databricks"].scheme == "https":
        assert got["tls_bundles"]["databricks"] == backends.bundle(
            backends.BACKENDS["databricks"])
    # (b): every plugged door, not only the hosted ones, is asked about
    assert {"postgres", "minio"} <= set(got["doors"])
    assert "redshift" in nep.expectations("de/aws", {})["doors"]
    assert "minio" not in nep.expectations("da/snowflake", {})["doors"]


def test_p7_probes_a_socket_served_backend_by_tcp_not_http():
    """Spark Connect is gRPC over HTTP/2, served by its module's `serve_socket`
    (serve.Mounted), so an HTTP/1.1 `/health` gets a SETTINGS frame back and
    reads as unreachable. P7 asks such a door with a connect, as serve.py
    serves it."""
    rows = {e.name: e for e in nep.expects("ds/databricks", {})}
    assert rows["spark_connect"].scheme == "tcp"
    assert rows["databricks"].scheme in ("tcp", "https")
    got = nep.expectations("ds/databricks", {})
    assert "spark_connect" in got["doors"]
    assert "spark_connect" not in got["services"]


def _run_probe(spec: dict) -> dict:
    spec = {**spec, "sweep_roots": [], "world_planes": [], "warehouses": []}
    program = f"SPEC = {json.dumps(json.dumps(spec))}\n{nep.PROBE}"
    done = subprocess.run([sys.executable, "-"], input=program, capture_output=True,
                          text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.fixture
def tls_health(tmp_path):
    """`GET /health` over TLS on loopback, with a certificate minted the way a
    TLS backend mints its own (`native.tls.mint`), and the bundle it publishes."""
    from alkera_gateway.native import tls

    cert, key = tls.mint("localhost", tmp_path / "trust", private_dir=tmp_path / "private")

    class Health(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps({"ok": True, "service": "databricks", "items": 3}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Health)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], str(tmp_path / "trust" / "bundle.pem")
    finally:
        server.shutdown()
        server.server_close()


def test_p7_probe_speaks_https_with_the_box_bundle(tls_health):
    port, bundle = tls_health
    out = _run_probe(nep.spec(
        warehouses=[], services={"databricks": f"https://localhost:{port}"},
        tls_bundles={"databricks": bundle}))
    row = out["services"][0]
    assert row["tcp"] == "CONNECTED" and row["status"] == 200, row
    assert row["reachable"] is True and row["bundle"] == bundle
    # what P7 did before: plain HTTP to the TLS port - no answer at all
    plain = _run_probe(nep.spec(
        warehouses=[], services={"databricks": f"http://localhost:{port}"}))
    assert plain["services"][0]["reachable"] is False


def test_p7_doors_are_asked_by_tcp():
    served = _listener("127.0.0.1")
    port = served.getsockname()[1]
    closed = _listener("127.0.0.1")
    gone = closed.getsockname()[1]
    closed.close()
    try:
        out = _run_probe(nep.spec(warehouses=[], services={},
                                  doors={"postgres": f"127.0.0.1:{port}"}))
        bad = _run_probe(nep.spec(warehouses=[], services={},
                                  doors={"postgres": f"127.0.0.1:{gone}"}))
    finally:
        served.close()
    assert out["checks"]["every_plugged_door_accepts_tcp"] is True
    assert bad["checks"]["every_plugged_door_accepts_tcp"] is False
    assert bad["doors_unreachable"] == ["postgres"]


def test_p7_question_g_resolves_the_compose_name_of_an_excluded_service():
    """Finding 1 went unseen because G asked only `minio.internal`."""
    excluded = nep.expectations("da/snowflake", {})["excluded"]
    assert "minio" in excluded["minio"] and "minio.internal" in excluded["minio"]
    with_container = nep.with_container_names(excluded, PROJECT)
    assert f"{PROJECT}-minio-1" in with_container["minio"]
    out = _run_probe(nep.spec(
        warehouses=[], services={}, scheduler=None,
        excluded={"sched": "sched.alkera-t4.invalid",
                  "minio": ["minio.alkera-t4.invalid", "localhost"]},
        not_applicable={nep.SCHEDULER_CHECK: "why"}))
    assert out["excluded_that_resolve"] == ["minio"]
    assert out["checks"]["no_excluded_service_resolves"] is False


# ------------------------------------------------------------- box sweep
def test_box_sweep_classifies_every_open_port():
    import box_sweep

    rows = [containment.service_facts(s) for s in (
        container("episode", "10.210.9.7"),
        native_services("10.210.9.3", "de/postgres"),
        postgres(), minio())]
    destinations = [{"host": "jira.internal", "port": 8080, "count": 2,
                     "file": "destinations.jsonl"}]
    banners = {"10.210.9.1:22": {"spoke_first": "SSH-2.0-OpenSSH_10.2p1\r\n"}}
    out = box_sweep.classify(
        ["10.210.9.1:22", "10.210.9.3:8080", "10.210.9.3:8110", "10.210.9.4:5432",
         "10.210.9.5:9000", "10.210.9.5:40341"],
        containers=rows, network=EPISODE_NET, gateway="10.210.9.1",
        destinations=destinations, store_plugged=True, banners=banners)
    by = {row["target"]: row for row in out}
    assert by["10.210.9.3:8080"]["class"].startswith("audited door")
    assert by["10.210.9.4:5432"]["class"].startswith("audited door for role alkera")
    assert by["10.210.9.5:9000"]["class"].startswith("audited door (MinIO")
    assert "the HOST" in by["10.210.9.1:22"]["class"]
    assert by["10.210.9.1:22"]["banner"] == banners["10.210.9.1:22"]
    assert "console" in by["10.210.9.5:40341"]["class"]
    assert sorted(row["target"] for row in box_sweep.not_audited(out)) == [
        "10.210.9.1:22", "10.210.9.3:8110", "10.210.9.5:40341"]
