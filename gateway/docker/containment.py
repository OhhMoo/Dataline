"""Containment evidence for the Track B compose stack.

Runs the full lifecycle and writes ``evidence/containment.json``. Every claim in that
file is produced by a command this script ran; nothing is asserted from the compose
file alone, because ``internal: true`` is a property of a *network*, not proof that a
given service is attached to the network you think it is, nor that it has no second
interface.

Five properties, one section each:

P1  the grading zone does not exist during the episode, and when it does exist it is
    joined to no network the episode can reach and cannot write the world (rule 1).
P2  no service in the episode zone can reach the internet - proven from inside the
    network namespace of every running service, not from one of them - and the probe
    is shown to be capable of detecting egress by running it somewhere it is allowed.
P3  the gateway is the only channel: the agent's vantage reaches the gateway port and
    nothing else, and the gateway's own tool surface exposes no socket primitive.
P4  no service perturbs the fingerprinted world tree, idle or under load.
P5  the publication ledger (world/README.md#publication-ledger) is world evidence and not
    an agent surface: a receipt planted in the world volume is unreadable and
    unwritable from the episode shell, and unreachable through every gateway tool.

P6  native mode only (gateway/NATIVE.md): only the fake services are
    reachable. Every `<name>.internal` resolves and answers, a control name does
    not, and a port that is not a service port is refused - measured from inside
    the episode container by gateway/docker/native_probe.py.
P7  native mode only: the episode container's FILESYSTEM holds the working
    tree, the warehouse FILES and the audit directory, and no other plane of
    the world, no rollout directory, no canary and no grading zone - measured
    from inside by gateway/docker/native_episode_probe.py. P6 is about the
    network and P7 is about the mounts, and the two came apart in practice: a
    native episode on the HOST topology passes nothing like P7, which is why
    three of five GT001 rollouts quarantined on a backup and an `ls`.

Usage::

    python containment.py                       # episode stack
    python containment.py --profile snowflake   # + the fakesnow socket
    python containment.py --native \\
        --out evidence/containment-native.json  # the native stack, P6 alone
    python containment.py --native-episode --project <live project> \\
        --world <the rollout's world root> \\
        --out evidence/containment-native-episode.json          # P7 alone
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import native_probe                       # the nine services' own port table

HERE = Path(__file__).resolve().parent
COMPOSE_FILE = HERE / "compose.yml"
# The native-mode stack is compose.yml plus BOTH overlays, in this order
# (compose.native.yml layers on the episode overlay, never instead of it).
EPISODE_OVERLAY = HERE / "compose.episode.yml"
NATIVE_OVERLAY = HERE / "compose.native.yml"
NATIVE_PROBE = "/opt/stack/native_probe.py"
# The real backends compose.native.yml gives an episode_net alias, by the
# compose service that carries it. Only the ones actually running are probed:
# `airflow` and `snowflake` are behind profiles, and a P6 that claimed an alias
# nobody was serving would fail for the wrong reason.
NATIVE_BACKENDS = {
    "postgres": ("postgres.internal", 5432),
    "minio": ("minio.internal", 9000),
    "airflow": ("airflow.internal", 8080),
    "snowflake": ("snowflake.internal", 8443),
}
PROBE_IMAGE = "alkera-stack:track-b"
ALL_PROFILES = ("harness", "grading", "snowflake")

# The episode zone. Anything here must fail every egress probe.
EPISODE_ZONE = {"episode", "harness"}
# Ports a backend listens on, probed by IP from the agent's vantage.
BACKEND_PORTS = (5432, 9090, 8443, 8080, 80, 443)

# Enumerates the shared network namespace from /proc, which is per-netns, so this
# reports the interfaces the *probed container* actually has - including a second
# interface nobody declared - and then tries to leave.
NETNS_PROBE = r"""
import json, socket
ifaces = []
for line in open("/proc/net/dev").read().splitlines()[2:]:
    name = line.split(":")[0].strip()
    if name:
        ifaces.append(name)
routes = []
for line in open("/proc/net/route").read().splitlines()[1:]:
    f = line.split()
    if len(f) > 2:
        dest = ".".join(str(int(f[1][i:i+2], 16)) for i in (6, 4, 2, 0))
        gw = ".".join(str(int(f[2][i:i+2], 16)) for i in (6, 4, 2, 0))
        routes.append({"iface": f[0], "dest": dest, "gateway": gw})
addrs = {}
for name in ifaces:
    try:
        import fcntl, struct
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        addrs[name] = socket.inet_ntoa(fcntl.ioctl(
            s.fileno(), 0x8915, struct.pack("256s", name[:15].encode()))[20:24])
    except Exception:
        addrs[name] = None
egress = []
for host, port in [("8.8.8.8", 53), ("1.1.1.1", 443), ("93.184.215.14", 80)]:
    try:
        socket.create_connection((host, port), timeout=3).close()
        egress.append({"target": f"{host}:{port}", "result": "CONNECTED"})
    except OSError as exc:
        egress.append({"target": f"{host}:{port}", "result": type(exc).__name__})
dns = []
for name in ["example.com", "google.com", "pypi.org"]:
    try:
        socket.getaddrinfo(name, 443)
        dns.append({"name": name, "result": "RESOLVED"})
    except OSError as exc:
        dns.append({"name": name, "result": type(exc).__name__})
print(json.dumps({
    "interfaces": ifaces, "addresses": addrs, "routes": routes,
    "egress_tcp": egress, "egress_dns": dns,
    "default_route": any(r["dest"] == "0.0.0.0" for r in routes),
    "egress_blocked": all(e["result"] != "CONNECTED" for e in egress)
                      and all(d["result"] != "RESOLVED" for d in dns),
}))
"""

# Run from a given vantage: names and addresses that must (not) be reachable.
REACH_PROBE = r"""
import json, socket, sys
spec = json.loads(sys.argv[1])
out = {"names": [], "addresses": []}
for name in spec["names"]:
    try:
        out["names"].append({"name": name,
                             "result": sorted({i[4][0] for i in socket.getaddrinfo(name, 80)})})
    except OSError as exc:
        out["names"].append({"name": name, "result": type(exc).__name__})
for host, port in spec["addresses"]:
    try:
        socket.create_connection((host, port), timeout=2).close()
        out["addresses"].append({"target": f"{host}:{port}", "result": "CONNECTED"})
    except OSError as exc:
        out["addresses"].append({"target": f"{host}:{port}", "result": type(exc).__name__})
print(json.dumps(out))
"""

# Hash the fingerprinted tree the way world/world/fingerprint.py treats files:
# path -> sha256 of contents. .git churns on its own and is not semantic.
TREE_HASH = r"""
import hashlib, json
from pathlib import Path
root = Path("/world")
files = {}
for p in sorted(root.rglob("*")):
    if p.is_file() and ".git" not in p.parts:
        files[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
print(json.dumps({"count": len(files),
                  "digest": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(),
                  "files": files}))
"""

# Exercise every backend through the gateway, so P4 measures a tree under load and
# not a tree nobody touched.
LOAD_PROBE = r"""
import json, re, urllib.request
import httpx
client = httpx.Client(base_url="http://127.0.0.1:8080", timeout=60)
def call(tool, **args):
    response = client.post("/call", json={"tool": tool, "args": args}).json()
    assert response["ok"], (tool, response)
    return response["result"]
out = {}
call("sql_execute", sql="CREATE TABLE warehouse.containment_probe(id INTEGER)")
call("sql_execute", sql="INSERT INTO warehouse.containment_probe VALUES (1), (2)")
rows = call("sql_query", sql="SELECT * FROM warehouse.containment_probe")["rows"]
out["warehouse_rows"] = len(rows)
call("sql_execute", sql="DROP TABLE warehouse.containment_probe")
# The pinned adobe/s3mock digest ignores `initialBuckets`, so read the bucket list off
# the wire from inside the container rather than trusting the compose declaration.
listing = urllib.request.urlopen("http://s3:9090/", timeout=10).read().decode()
out["s3_buckets_on_the_wire"] = re.findall(r"<Name>([^<]+)</Name>", listing)
out["s3_roundtrip"] = (
    bool(call("s3_put", bucket="landing", key="containment.bin", body_base64="AAEC")["etag"])
    and call("s3_get", bucket="landing", key="containment.bin")["body_base64"] == "AAEC"
    and any(o["key"] == "containment.bin"
            for o in call("s3_list", bucket="landing", prefix="containment")["objects"])
)
call("s3_delete", bucket="landing", key="containment.bin")
out["bi_panels"] = bool(call("bi_refresh", dashboard="revenue")["panels"])
out["docs_lines"] = bool(call("docs_get", path="docs/metrics.md")["lines"])
# A second warehouse, when the stack declares one (ALKERA_WAREHOUSES): every
# declared name answers by name, the attached peers join in one statement, and
# an ATTACH the world did not declare is refused by the engine - measured
# through the tool surface, on the running stack, not inferred from the
# unit tests. Absent from the evidence of a one-warehouse stack.
cfg = json.load(open("/tmp/gateway.json"))
if cfg.get("warehouses"):
    from alkera_gateway.config import GatewayConfig
    from alkera_gateway.warehouses import create_registry
    registry = create_registry(GatewayConfig.model_validate(cfg))
    dual = {"declared": registry.names, "attachments": registry.attachments,
            "by_name": {}, "cross_join": None, "undeclared_attach": None}
    for name in registry.names:
        rows = call("sql_query", sql="SELECT id, amount FROM warehouse.items ORDER BY id",
                    warehouse=name)["rows"]
        dual["by_name"][name] = rows
    attached = [n for n, note in registry.attachments.items() if note.startswith("attached")]
    if attached:
        peer = attached[0]
        dual["cross_join"] = call(
            "sql_query",
            sql=f"SELECT p.id, p.amount AS primary_amount, l.amount AS {peer}_amount "
                f"FROM wh.warehouse.items p FULL OUTER JOIN {peer}.warehouse.items l USING (id) "
                "WHERE p.amount IS DISTINCT FROM l.amount ORDER BY 1")["rows"]
    refused = client.post("/call", json={"tool": "sql_execute", "args": {
        "sql": "ATTACH '/tmp/undeclared.duckdb' AS sneaky"}}).json()
    dual["undeclared_attach"] = {"ok": refused.get("ok"),
                                 "error": (refused.get("error") or {}).get("code")}
    out["dual"] = dual
print(json.dumps(out))
"""

# The tool surface is the agent's only channel. A tool that accepted a host, port,
# URL or DSN would be a second one. Walked over the table the running gateway
# SERVES (build_tools over its own config), which is `tools.TOOLS` itself for one
# warehouse and gains a `warehouse` name argument for two - a logical name, which
# is the whole point of the check below.
TOOL_SURFACE = r"""
import json
from alkera_gateway.tools import TOOLS
served, source = TOOLS, "alkera_gateway.tools.TOOLS"
try:
    from alkera_gateway.config import GatewayConfig
    from alkera_gateway.warehouses import build_tools
    served = build_tools(GatewayConfig.load(__import__("pathlib").Path("/tmp/gateway.json")))
    source = ("alkera_gateway.tools.TOOLS (build_tools returned the table itself)"
              if served is TOOLS else "alkera_gateway.warehouses.build_tools(/tmp/gateway.json)")
except ImportError:
    pass
socketish = {"host", "hostname", "port", "url", "uri", "endpoint", "dsn", "server",
             "address", "addr", "proxy", "connection", "conn", "account", "network"}
fields, offenders = {}, []
for name, (model, _) in served.items():
    names = sorted(model.model_fields)
    fields[name] = names
    for field in names:
        if field.lower() in socketish:
            offenders.append(f"{name}.{field}")
print(json.dumps({"tool_count": len(served), "source": source, "fields": fields,
                  "socket_arguments": offenders, "no_socket_primitive": not offenders}))
"""

# "By trying": the episode container holds the gateway's own Postgres credentials.
# Record what that buys, rather than claiming a network boundary that is not there.
DIRECT_BACKEND = r"""
import json, os
out = {"dsn_present": bool(os.environ.get("ALKERA_POSTGRES_DSN"))}
try:
    import psycopg
    with psycopg.connect(os.environ["ALKERA_POSTGRES_DSN"], connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("select current_user, current_database()")
            out["identity"] = [str(v) for v in cur.fetchone()]
            cur.execute("select rolsuper from pg_roles where rolname = current_user")
            out["superuser"] = bool(cur.fetchone()[0])
    out["connected"] = True
except Exception as exc:
    out["connected"] = False
    out["error"] = f"{type(exc).__name__}: {exc}"[:200]
print(json.dumps(out))
"""

GRADER_MOUNTS = r"""
import json
from pathlib import Path
world = Path("/world")
out = {"world_exists": world.is_dir(),
       "world_file_count": sum(1 for p in world.rglob("*") if p.is_file()),
       "grading_dir_present": Path("/grading").exists()}
try:
    (world / ".containment-write-probe").write_text("x")
    out["world_writable"] = True
except OSError as exc:
    out["world_writable"] = False
    out["world_write_error"] = f"{type(exc).__name__}: {exc}"[:140]
print(json.dumps(out))
"""


def run(argv, check=True):
    result = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if check and result.returncode:
        raise RuntimeError(f"command failed: {argv}\n{result.stdout}\n{result.stderr}")
    return result


def out(argv):
    return run(argv).stdout.strip()


class Stack:
    def __init__(self, project, profiles, overlays=()):
        self.project = project
        self.profiles = list(profiles)
        # `overlays` is empty for every stack P0-P5 describe, so `self.files` is
        # `[compose.yml]` and every command below is the one it always was.
        self.overlays = [Path(p) for p in overlays]
        self.files = [COMPOSE_FILE, *self.overlays]
        self.base = ["docker", "compose", "-p", project]
        for path in self.files:
            self.base += ["-f", str(path)]
        for profile in self.profiles:
            self.base += ["--profile", profile]

    def compose(self, *args, check=True):
        return run([*self.base, *args], check=check)

    def declared(self):
        """Every service, including profile-gated ones. `config` omits services whose
        profile is not enabled, which silently empties any check written against it."""
        argv = ["docker", "compose", "-p", self.project]
        for path in self.files:
            argv += ["-f", str(path)]
        for profile in ALL_PROFILES:
            argv += ["--profile", profile]
        return json.loads(out([*argv, "config", "--format", "json"]))

    def containers(self):
        ids = out([*self.base, "ps", "-q"]).splitlines()
        return [json.loads(out(["docker", "inspect", cid]))[0] for cid in ids if cid]

    def netns_probe(self, container_id, code=NETNS_PROBE, args=()):
        return json.loads(
            out(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    f"container:{container_id}",
                    "--entrypoint",
                    "python",
                    PROBE_IMAGE,
                    "-c",
                    code,
                    *args,
                ]
            )
        )

    def exec_json(self, service, code, args=()):
        return json.loads(
            self.compose("exec", "-T", service, "python", "-c", code, *args)
            .stdout.strip()
            .splitlines()[-1]
        )

    def run_json(self, service, code, args=()):
        return json.loads(
            self.compose("run", "--rm", "--entrypoint", "python", service, "-c", code, *args)
            .stdout.strip()
            .splitlines()[-1]
        )


def service_facts(spec):
    labels = spec["Config"]["Labels"]
    networks = spec["NetworkSettings"]["Networks"]
    return {
        "service": labels["com.docker.compose.service"],
        "container": spec["Name"].lstrip("/"),
        "image": spec["Config"]["Image"],
        "network_mode": spec["HostConfig"]["NetworkMode"],
        "networks": {name: net["IPAddress"] for name, net in networks.items()},
        "aliases": {name: net.get("Aliases") or [] for name, net in networks.items()},
        "published_ports": {p: b for p, b in (spec["NetworkSettings"]["Ports"] or {}).items() if b},
        "mounts": [
            {"source": m.get("Name") or m.get("Source"), "target": m["Destination"], "rw": m["RW"]}
            for m in spec["Mounts"]
        ],
    }


def declared_mounts(declared, service):
    return [
        {
            "source": v.get("source"),
            "target": v.get("target"),
            "read_only": bool(v.get("read_only")),
        }
        for v in (declared["services"].get(service, {}).get("volumes") or [])
    ]


def property_2_egress(stack, evidence):
    """Every running service, probed from inside its own network namespace."""
    # Positive control first: the same probe, somewhere egress is allowed. Without it
    # a probe that can never connect reports "contained" for a stack with a hole in it.
    control = json.loads(
        out(["docker", "run", "--rm", "--entrypoint", "python", PROBE_IMAGE, "-c", NETNS_PROBE])
    )
    rows = []
    for spec in evidence["_specs"]:
        probe = stack.netns_probe(spec["Id"])
        rows.append(
            {**service_facts(spec), "netns": probe, "egress_blocked": probe["egress_blocked"]}
        )
    # The agent's own vantage has no long-lived container, so raise one.
    harness = stack.run_json("harness", NETNS_PROBE)
    rows.append(
        {
            "service": "harness",
            "container": "(compose run)",
            "netns": harness,
            "egress_blocked": harness["egress_blocked"],
            "mounts": [],
            "networks": {"episode_net": harness["addresses"].get("eth0")},
        }
    )
    evidence["services"] = rows
    declared = set(evidence["_declared"]["services"])
    probed = {r["service"] for r in rows}
    evidence["p2_egress"] = {
        "probe_control": {
            "vantage": "default bridge network, same image and probe",
            "egress_blocked": control["egress_blocked"],
            "egress_tcp": control["egress_tcp"],
            "egress_dns": control["egress_dns"],
            "probe_can_detect_egress": not control["egress_blocked"],
        },
        "probed": sorted(probed),
        "declared_but_not_probed": sorted(declared - probed),
        "extra_interfaces": {
            r["service"]: r["netns"]["interfaces"]
            for r in rows
            if set(r["netns"]["interfaces"]) - {"lo", "eth0", "eth1"}
        },
        "passed": (
            all(r["egress_blocked"] for r in rows)
            and not control["egress_blocked"]
            and not (declared - probed - {"grading"})
        ),
    }
    return evidence["p2_egress"]


def property_3_channel(stack, evidence):
    """The gateway is the only channel the agent has."""
    names = {"postgres", "s3", "snowflake", "acme.snowflake.local", "episode"}
    evidence["_names"] = names
    role = {
        spec["Config"]["Labels"]["com.docker.compose.service"]: spec for spec in evidence["_specs"]
    }
    backend_ips = sorted(
        {
            net["IPAddress"]
            for service, spec in role.items()
            if service not in EPISODE_ZONE
            for net in spec["NetworkSettings"]["Networks"].values()
            if net["IPAddress"]
        }
    )
    episode_ips = sorted(
        {
            net["IPAddress"]
            for net in role["episode"]["NetworkSettings"]["Networks"].values()
            if net["IPAddress"]
        }
    )
    evidence["_backend_ips"] = backend_ips
    spec = json.dumps(
        {
            "names": sorted(names),
            "addresses": [[ip, port] for ip in backend_ips for port in BACKEND_PORTS]
            + [[ip, 8080] for ip in episode_ips],
        }
    )
    agent = stack.run_json("harness", REACH_PROBE, [spec])
    reachable = [a["target"] for a in agent["addresses"] if a["result"] == "CONNECTED"]
    resolved = [n["name"] for n in agent["names"] if isinstance(n["result"], list)]
    gateway_targets = {f"{ip}:8080" for ip in episode_ips}
    inside = stack.exec_json("episode", REACH_PROBE, [spec])
    section = {
        "agent_vantage": "harness container, episode_net only",
        "agent_name_resolution": agent["names"],
        "agent_reachable": reachable,
        "agent_resolved": resolved,
        "gateway_port_reachable": sorted(gateway_targets & set(reachable)),
        "backends_reachable_from_agent": sorted(set(reachable) - gateway_targets),
        # The episode container is the gateway. It must reach the backends; the
        # boundary there is the tool surface, not the network, and this records both.
        "episode_container_reachability": inside["addresses"],
        "episode_container_resolution": inside["names"],
        "episode_direct_backend_connection": stack.exec_json("episode", DIRECT_BACKEND),
        "tool_surface": stack.exec_json("episode", TOOL_SURFACE),
        "published_host_ports": {
            s["service"]: s["published_ports"]
            for s in evidence["services"]
            if s.get("published_ports")
        },
    }
    section["passed"] = (
        not section["backends_reachable_from_agent"]
        and resolved == ["episode"]
        and bool(section["gateway_port_reachable"])
        and section["tool_surface"]["no_socket_primitive"]
        and not section["published_host_ports"]
        # the gateway's own database role must not be a superuser, or a single SQL
        # tool call is a second channel regardless of the network
        and section["episode_direct_backend_connection"].get("superuser") is False
    )
    evidence["p3_only_channel"] = section
    return section


# --------------------------------------------------------------------------
# P5 - the publication ledger. world/README.md#publication-ledger.
# --------------------------------------------------------------------------

# The ledger's path in the world tree, and the canary receipt planted in it.
# The canary is planted from a container that is NOT the episode - it reaches
# the world volume by a mount of its own - so the plant proves the file really
# is in the volume the episode container mounts, which is the only thing that
# makes "cannot read it" mean anything. A probe that looked for a file nobody
# wrote would pass against an empty directory.
LEDGER_DIR = "/world/var/ledger"
LEDGER_FILE = LEDGER_DIR + "/receipts.jsonl"
LEDGER_CANARY = "ledger-canary-8f41c2d6e5b0"

LEDGER_PLANT = r"""
import hashlib, json, pathlib, sys
canary = sys.argv[1]
d = pathlib.Path("/w/var/ledger")
d.mkdir(parents=True, exist_ok=True)
f = d / "receipts.jsonl"
line = json.dumps({"seq": 1, "vnow": "2026-03-03T01:01:30Z", "day": "2026-03-02",
                   "job": "dbt_build", "target": "warehouse.warehouse.fct_revenue",
                   "kind": "table", "canonical_hash": canary, "row_count": 2,
                   "sample": [[canary]], "consumer": "finance_daily"},
                  sort_keys=True, separators=(",", ":"))
f.write_text(line + "\n", encoding="utf-8")
print(json.dumps({"path": str(f), "bytes": f.stat().st_size,
                  "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}))
"""

# Read the planted receipt back after the episode has had its turn at it.
LEDGER_VERIFY = r"""
import hashlib, json, pathlib
f = pathlib.Path("/w/var/ledger/receipts.jsonl")
d = f.parent
print(json.dumps({
    "exists": f.is_file(),
    "sha256": hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else None,
    "lines": len(f.read_text(encoding="utf-8").splitlines()) if f.is_file() else 0,
    "listing": sorted(p.name for p in d.iterdir()) if d.is_dir() else []}))
"""

# Inside the episode container: read the kernel's own mount table, then try it -
# list, read, write, and sweep the whole world mount for the canary string.
# Every claim here is the result of an attempt, never of reading compose.yml.
LEDGER_PROBE = r"""
import json, os, sys
spec = json.loads(sys.argv[1])
out = {}
mounts = []
try:
    for line in open("/proc/self/mountinfo"):
        f = line.split()
        sep = f.index("-")
        mounts.append({"target": f[4], "options": f[5], "fstype": f[sep + 1]})
except OSError as exc:
    mounts = [{"error": str(exc)}]
out["ledger_mount"] = [m for m in mounts if m.get("target") == spec["dir"]]
out["listing"] = None
try:
    out["listing"] = sorted(os.listdir(spec["dir"]))
except OSError as exc:
    out["listing_error"] = "%s: %s" % (type(exc).__name__, exc)
try:
    with open(spec["file"], "rb") as fh:
        out["read"] = {"result": "READ", "bytes": len(fh.read(4096))}
except OSError as exc:
    out["read"] = {"result": "%s: %s" % (type(exc).__name__, exc)}
try:
    with open(spec["dir"] + "/forged.jsonl", "wb") as fh:
        fh.write(b"{}\n")
    out["write"] = {"result": "WROTE"}
except OSError as exc:
    out["write"] = {"result": "%s: %s" % (type(exc).__name__, exc)}
hits, scanned = [], 0
for base in ("/world", "/workspace"):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__")]
        for name in filenames:
            scanned += 1
            if scanned > spec["sweep_limit"]:
                break
            path = os.path.join(dirpath, name)
            try:
                if os.path.getsize(path) > 2000000:
                    continue
                blob = open(path, "rb").read()
            except OSError:
                continue
            if spec["canary"].encode() in blob:
                hits.append(path)
        if scanned > spec["sweep_limit"]:
            break
out["canary_sweep"] = {"files_scanned": scanned, "hits": hits}
print(json.dumps(out))
"""

# The other half: the tool surface. Go through the gateway exactly as the agent
# does, and record what every plausible spelling of the ledger path returns. The
# vantage is the episode container because the boundary under test here is the
# tool surface, not the network - P3 is what establishes the network vantage.
LEDGER_TOOLS = r"""
import json, sys
import httpx
spec = json.loads(sys.argv[1])
client = httpx.Client(base_url="http://127.0.0.1:8080", timeout=30)
def call(tool, **args):
    r = client.post("/call", json={"tool": tool, "args": args}).json()
    if r.get("ok"):
        body = json.dumps(r.get("result"))[:400]
        return {"ok": True, "result": body, "leaked": spec["canary"] in body}
    err = r.get("error") or r
    return {"ok": False, "error": json.dumps(err)[:300], "leaked": False}
escapes, inside = {}, {}
for path in spec["escape_paths"]:
    escapes["fs_read " + path] = call("fs_read", path=path)
    escapes["fs_list " + path] = call("fs_list", path=path)
    escapes["fs_write " + path] = call("fs_write", path=path, content="forged")
# NOT an escape: "var/ledger/receipts.jsonl" with no leading slash resolves
# inside the agent's own repository. It is allowed to succeed - the agent may
# write any file it likes in its workspace - and the claim under test is that
# what it wrote is not the ledger. The property re-reads the planted receipt
# afterwards to say so.
for path in spec["workspace_paths"]:
    inside["fs_read " + path] = call("fs_read", path=path)
    inside["fs_write " + path] = call("fs_write", path=path, content="forged")
inside["docs_search canary"] = call("docs_search", query=spec["canary"])
try:
    cfg = json.loads(open(spec["config"], "r", encoding="utf-8").read())
except OSError as exc:
    cfg = {"_error": str(exc)}
both = dict(escapes)
both.update(inside)
print(json.dumps({
    "escape_attempts": escapes,
    "workspace_attempts": inside,
    "config_keys_naming_ledger": sorted(
        k for k, v in cfg.items()
        if isinstance(v, str) and "ledger" in v.lower()),
    "escapes_that_worked": [k for k, v in escapes.items() if v.get("ok")],
    "any_leak": [k for k, v in both.items() if v.get("leaked")]}))
"""


def property_5_ledger(stack, evidence):
    """The publication ledger is written by the world and readable by nobody inside.

    Ground rule 1 says the grader does not exist during the episode. The ledger
    is the same species of object one layer down: it is the record the grader
    reads, so an agent that could read it would know exactly which intermediate
    publication it has to explain, and one that could write it could erase the
    bad one. Neither is hypothetical - /world is a single bind mount of the
    whole world root and the episode has a real shell, so the ledger is out of
    reach only because a mount shadows it. That is a claim about the running
    kernel, which is why it is measured here and not asserted in a comment.

    Runs AFTER P4, and must: it plants a file in the world volume and lets the
    gateway write one into the workspace, which is exactly the perturbation P4
    measures the absence of. Reordering the two would make P4 fail on this
    probe's own footprints.
    """
    volume = f"{stack.project}_worlddata"
    # Plant through a second container mounting the same world volume. `docker
    # run` rather than `compose run`: no declared service mounts the world
    # volume anywhere the episode's shadow does not also cover.
    plant = json.loads(out([
        "docker", "run", "--rm", "--network", "none",
        "-v", f"{volume}:/w", "--entrypoint", "python", PROBE_IMAGE,
        "-c", LEDGER_PLANT, LEDGER_CANARY]))
    shell = stack.exec_json("episode", LEDGER_PROBE, [json.dumps(
        {"dir": LEDGER_DIR, "file": LEDGER_FILE,
         "canary": LEDGER_CANARY, "sweep_limit": 20000})])
    tools = stack.exec_json("episode", LEDGER_TOOLS, [json.dumps(
        {"canary": LEDGER_CANARY,
         "config": "/tmp/gateway.json",
         "escape_paths": ["../var/ledger/receipts.jsonl",
                          "../../var/ledger/receipts.jsonl",
                          "/world/var/ledger/receipts.jsonl",
                          "/workspace/../var/ledger/receipts.jsonl"],
         "workspace_paths": ["var/ledger/receipts.jsonl"]})])
    # Read the planted receipt back through the volume, AFTER every attempt
    # above. Unchanged bytes is the claim the attempts are evidence for: the
    # agent could not reach the ledger, and the one write it was allowed went
    # somewhere else.
    after = json.loads(out([
        "docker", "run", "--rm", "--network", "none",
        "-v", f"{volume}:/w", "--entrypoint", "python", PROBE_IMAGE,
        "-c", LEDGER_VERIFY]))
    section = {
        "planted": plant,
        "planted_from": "a separate container mounting the world volume",
        "ledger_path_in_container": LEDGER_FILE,
        "shadow_mount": shell["ledger_mount"],
        "shell": shell,
        "tools": tools,
        "ledger_after_the_attempts": after,
    }
    section["passed"] = bool(
        plant.get("bytes")
        # the kernel reports a mount AT the ledger path that is not the world bind
        and any(m.get("fstype") == "tmpfs" for m in shell["ledger_mount"])
        # nothing readable there, by trying
        and "receipts.jsonl" not in (shell.get("listing") or [])
        and shell["read"]["result"] != "READ"
        and shell["write"]["result"] != "WROTE"
        and not shell["canary_sweep"]["hits"]
        # no gateway tool call escapes the workspace toward it, and nothing the
        # agent could call returned the canary
        and not tools["escapes_that_worked"]
        and not tools["any_leak"]
        and not tools["config_keys_naming_ledger"]
        # and the planted receipt is still exactly what was planted
        and after.get("sha256") == plant["sha256"]
        and after.get("lines") == 1)
    evidence["p5_ledger_protected"] = section
    return section


def property_4_determinism(stack, evidence, settle_seconds):
    """Nothing but the gateway writes into the fingerprinted tree."""
    worlddata, writers, readers = None, [], []
    for row in evidence["services"]:
        for mount in row.get("mounts", []):
            if mount["source"] and mount["source"].endswith("worlddata"):
                worlddata = mount["source"]
                (writers if mount["rw"] else readers).append(row["service"])
    declared = evidence["_declared"]
    declared_world = {
        service: [
            m for m in declared_mounts(declared, service) if (m["source"] or "") == "worlddata"
        ]
        for service in declared["services"]
    }
    probe = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{worlddata}:/world:ro",
        "--entrypoint",
        "python",
        PROBE_IMAGE,
        "-c",
        TREE_HASH,
    ]
    first = json.loads(out(probe))
    run(["docker", "run", "--rm", "--entrypoint", "sleep", PROBE_IMAGE, str(settle_seconds)])
    idle = json.loads(out(probe))
    load = stack.exec_json("episode", LOAD_PROBE)
    after = json.loads(out(probe))

    def delta(a, b):
        return sorted(
            k for k in set(a["files"]) | set(b["files"]) if a["files"].get(k) != b["files"].get(k)
        )

    # With a DuckDB PRIMARY (ALKERA_BACKEND=duckdb) the warehouse the load probe
    # writes through the gateway IS a file in the tree, so the probe's own CREATE
    # TABLE / DROP TABLE move `var/warehouse/warehouse.duckdb` by design. Measured:
    # a one-warehouse DuckDB-primary stack and a DuckDB+Postgres stack fail the
    # raw comparison on exactly that one file (evidence/containment-duckdb-primary
    # .json, evidence/containment-dual.json, 2026-09-15). The property is that no
    # *service* perturbs the tree and nothing writes it but the gateway, so the
    # gateway's own write to its own warehouse file is expected and recorded
    # separately; anything else that moved under load still fails.
    expected_under_load = set()
    if os.environ.get("ALKERA_BACKEND", "postgres") == "duckdb":
        expected_under_load = {"var/warehouse/warehouse.duckdb", "var/warehouse/warehouse.duckdb.wal"}
    unexpected_under_load = sorted(set(delta(first, after)) - expected_under_load)

    section = {
        "worlddata_volume": worlddata,
        "writable_in_running": sorted(writers),
        "read_only_in_running": sorted(readers),
        "declared_worlddata_mounts": {s: m for s, m in declared_world.items() if m},
        "idle_seconds": settle_seconds,
        "file_count": first["count"],
        "tree_digest_start": first["digest"],
        "tree_digest_after_idle": idle["digest"],
        "tree_digest_after_load": after["digest"],
        "files_changed_while_idle": delta(first, idle),
        "files_changed_under_load": delta(first, after),
        "gateway_writes_expected_under_load": sorted(expected_under_load),
        "files_changed_under_load_unexpected": unexpected_under_load,
        "load_exercised": load,
        "writable_state_outside_world": {
            row["service"]: sorted(
                m["target"]
                for m in row.get("mounts", [])
                if m["rw"] and not m["target"].startswith("/world")
            )
            for row in evidence["services"]
        },
    }
    section["passed"] = (
        writers == ["episode"]
        and not delta(first, idle)
        and not unexpected_under_load
        and all(
            m["read_only"]
            for service, mounts in declared_world.items()
            for m in mounts
            if service != "episode"
        )
    )
    evidence["p4_determinism"] = section
    return section


def property_1_grading(stack, evidence):
    """First half: the grader does not exist while the episode does."""
    declared = evidence["_declared"]["services"].get("grading", {})
    live = sorted(c["service"] for c in evidence["services"] if c["container"] != "(compose run)")
    evidence["p1_grading_zone"] = {
        "declared_network_mode": declared.get("network_mode"),
        "declared_networks": sorted(declared.get("networks") or {}),
        "declared_profiles": declared.get("profiles", []),
        "declared_mounts": declared_mounts(evidence["_declared"], "grading"),
        "services_live_during_episode": live,
        "absent_during_episode": "grading" not in live,
        "episode_sees_grading_path": stack.compose(
            "exec",
            "-T",
            "episode",
            "python",
            "-c",
            "import os,json;print(json.dumps(os.path.exists('/grading')))",
            check=False,
        ).stdout.strip(),
    }
    return evidence["p1_grading_zone"]


def property_1_grading_after_teardown(stack, evidence):
    """Second half: start the grader only after the episode is gone, then probe it."""
    section = evidence["p1_grading_zone"]
    running = sorted(
        {c["Config"]["Labels"]["com.docker.compose.service"] for c in stack.containers()}
    )
    if running:
        stack.compose("stop", "-t", "5", *running, check=False)
    section["stopped_before_grader_started"] = running
    section["still_running_when_grader_started"] = sorted(
        {c["Config"]["Labels"]["com.docker.compose.service"] for c in stack.containers()}
    )
    probe = stack.run_json("grading", NETNS_PROBE)
    reach = stack.run_json(
        "grading",
        REACH_PROBE,
        [
            json.dumps(
                {
                    "names": sorted(evidence["_names"]),
                    "addresses": [
                        [ip, port] for ip in evidence["_backend_ips"] for port in BACKEND_PORTS
                    ],
                }
            )
        ],
    )
    mounts = stack.run_json("grading", GRADER_MOUNTS)
    section["grader_netns"] = probe
    section["grader_reachability"] = reach
    section["grader_mounts"] = mounts
    section["grader_isolated"] = (
        probe["interfaces"] == ["lo"]
        and not probe["default_route"]
        and all(a["result"] != "CONNECTED" for a in reach["addresses"])
        and all(isinstance(n["result"], str) for n in reach["names"])
    )
    section["passed"] = bool(
        section["absent_during_episode"]
        and not section["still_running_when_grader_started"]
        and section["grader_isolated"]
        and section["declared_network_mode"] == "none"
        and mounts["world_exists"]
        and not mounts["world_writable"]
    )
    return section


# --------------------------------------------------------------------------
# P6 - native mode. gateway/NATIVE.md, gateway/docker/NATIVE_STACK.md.
# --------------------------------------------------------------------------


def property_6_native(stack, evidence):
    """Only the fake services are reachable from the episode container.

    In native mode the agent has bash and no tool list, so P3's boundary - the
    tool surface - is gone and the network is the whole of it. The measurement
    is `native_probe.py`, run from inside the episode container: it resolves
    every `<name>.internal`, asks each for `/health`, resolves a set of control
    names that must fail, and tries ports that are not service ports on the
    very addresses that answered.

    Two things are checked here rather than in the probe, because they are
    properties of the stack and not of the vantage: the aliases compose
    declared are the aliases the running containers actually carry (an alias in
    a YAML file is not an alias on a container), and nothing publishes a host
    port.
    """
    rows = [service_facts(spec) for spec in evidence["_specs"]]
    evidence["services"] = rows
    declared = evidence["_declared"]["services"]
    declared_aliases = {
        service: sorted(
            alias
            for net in (spec.get("networks") or {}).values()
            for alias in ((net or {}).get("aliases") or [])
        )
        for service, spec in declared.items()
    }
    running_aliases = {
        row["service"]: sorted({a for names in row["aliases"].values() for a in names})
        for row in rows
    }
    # A container always carries its own short id and service name as aliases;
    # the claim is about the declared ones, so compare only those.
    missing_aliases = {
        service: sorted(set(names) - set(running_aliases.get(service, [])))
        for service, names in declared_aliases.items()
        if names and service in running_aliases
        and set(names) - set(running_aliases.get(service, []))
    }
    running = {row["service"] for row in rows}
    backends = [f"{host}:{port}" for service, (host, port) in sorted(NATIVE_BACKENDS.items())
                if service in running]
    backend_args = [arg for target in backends for arg in ("--backend", target)]
    # Every compose service name the episode shares a network with. Docker
    # registers each of them in the embedded resolver whether we want it or
    # not, so they are named to the probe and pinned there rather than left to
    # fail as controls - the deviation is real and is recorded, not excused.
    compose_args = [arg for service in sorted(running)
                    for arg in ("--compose-name", service)]
    result = stack.compose("exec", "-T", "episode", "python", NATIVE_PROBE,
                           "--self-test", *backend_args, *compose_args, check=False)
    probe = json.loads(result.stdout.strip().splitlines()[-1])
    section = {
        "compose_files": [str(p) for p in stack.files],
        "backends_probed": backends,
        "backends_declared_but_not_running": sorted(
            service for service in NATIVE_BACKENDS if service not in running),
        "stub_services": os.environ.get("ALKERA_NATIVE_STUBS", ""),
        "native_services_startup": stack.compose(
            "logs", "--no-log-prefix", "native-services", check=False).stdout.strip(),
        "declared_aliases": {s: a for s, a in declared_aliases.items() if a},
        "running_aliases": running_aliases,
        "declared_aliases_missing_on_the_container": missing_aliases,
        "published_host_ports": {
            row["service"]: row["published_ports"] for row in rows if row["published_ports"]
        },
        "probe": probe,
    }
    section["passed"] = bool(
        probe["passed"]
        and not missing_aliases
        and not section["published_host_ports"]
    )
    evidence["p6_only_fake_services_reachable"] = section
    return section


# --------------------------------------------------------------------------
# P7 - the native EPISODE container's filesystem. gateway/docker/
# native_episode_probe.py, docs/implementation_plan/harness/NATIVE_CONTAINER.md.
# --------------------------------------------------------------------------


def property_7_native_episode(stack, evidence, warehouses, services):
    """The episode container holds the workspace and the warehouses, and no
    other part of the world.

    P6 is a claim about the network. P7 is the claim the container topology was
    built for, and it is the one the host topology cannot make: a shell in the
    episode reaches the working tree, the warehouse FILES and the audit
    directory, and reaches no other plane of the world, no rollout directory,
    no canary and no grading zone. Measured by trying, from inside, by
    `native_episode_probe.PROBE` over `docker exec -i` with the source on
    stdin - so nothing describing the harness is ever written to the disk the
    subject can read.

    Two facts are checked here rather than in the probe, because they are
    properties of the stack rather than of the vantage: the episode container
    publishes no host port (the services may, and which ones is recorded), and
    the bind mounts docker actually created are the ones the mount table
    promises.
    """
    import native_episode_probe

    rows = [service_facts(spec) for spec in evidence["_specs"]]
    evidence["services"] = rows
    episode = next((row for row in rows if row["service"] == "episode"), None)
    if episode is None:
        raise RuntimeError("no `episode` container in this project; P7 has no vantage")
    container = next(spec["Id"] for spec in evidence["_specs"]
                     if spec["Config"]["Labels"]["com.docker.compose.service"] == "episode")
    probe = native_episode_probe.run(
        container,
        native_episode_probe.spec(warehouses=warehouses, services=services))
    # Every bind the episode carries, by target, read off the running container
    # rather than off the compose file. `/world/...` binds must all be files
    # the caller named; a `/world` bind itself is the failure this property
    # exists to detect.
    # HOST BINDS ONLY. The ledger shadow at /world/var/ledger is a read-only
    # tmpfs and carries no source, so it is not a path out of the container and
    # must not be read as one; `service_facts` leaves its `source` empty, which
    # is the difference.
    binds = {mount["target"]: mount for mount in episode["mounts"]
             if mount.get("source")}
    world_binds = sorted(target for target in binds
                         if target == "/world" or target.startswith("/world/"))
    unexpected_world_binds = [
        target for target in world_binds
        if target.removeprefix("/world/") not in set(warehouses)]
    shadowed = [mount["target"] for mount in episode["mounts"]
                if not mount.get("source")]
    section = {
        "compose_files": [str(p) for p in stack.files],
        "vantage": "episode container, native topology",
        "warehouses_declared": sorted(warehouses),
        "episode_mounts": sorted(binds),
        "world_binds_on_the_episode": world_binds,
        "shadowed_paths_with_no_host_source": sorted(shadowed),
        "world_binds_that_are_not_declared_warehouses": unexpected_world_binds,
        "published_host_ports": {
            row["service"]: row["published_ports"] for row in rows
            if row["published_ports"]},
        "probe": probe,
    }
    section["episode_publishes_no_host_port"] = not episode["published_ports"]
    section["passed"] = bool(
        probe["contained"]
        and not unexpected_world_binds
        and section["episode_publishes_no_host_port"])
    evidence["p7_native_episode_filesystem"] = section
    return section


def native_episode_main(args):
    """P7 against a live native episode project. No `up`, no `down`.

    The vantage has to be a container a rollout built: its mounts ARE the
    property, and they come from the world that rollout's builder produced. So
    this attaches to `--project` by name, measures, writes the artifact and
    leaves the stack exactly as it found it.
    """
    import native_episode_probe

    from alkera_gateway.native import serve

    stack = Stack(args.project, args.profile,
                  overlays=(EPISODE_OVERLAY, NATIVE_OVERLAY))
    warehouses = list(args.warehouse)
    if args.world:
        warehouses += sorted(
            path.relative_to(args.world).as_posix()
            for path in (args.world / "var" / "warehouse").glob("*.duckdb"))
    services = {row["host"].split(".")[0]: f"http://{row['host']}:{row['port']}"
                for row in native_probe.registry_expectation()}
    # The scheduler plane, which the registry does not list because it is not
    # one of the nine vendor-shaped services - it is the backend of the agent's
    # own `./ops/sched`. P7 requires it to answer because the mount set puts
    # `var/sched` out of the episode's reach on purpose.
    services[native_episode_probe.SCHEDULER_SERVICE] = (
        f"http://{native_episode_probe.SCHEDULER_SERVICE}.internal:"
        f"{serve.SCHED_PORT}")
    evidence = {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "project": args.project,
        "mode": "native-episode",
        "compose_file": str(COMPOSE_FILE),
        "compose_overlays": [str(EPISODE_OVERLAY), str(NATIVE_OVERLAY),
                             "+ the rollout's own compose.native-mounts.yml"],
        "world_root_on_the_host": str(args.world) if args.world else None,
        "host": {
            "platform": platform.platform(),
            "docker": out(["docker", "version", "--format", "{{.Server.Version}}"]),
        },
        # By compose LABEL and not by `compose ps`: this script is attaching to
        # someone else's project, and `compose ps` would have to interpolate
        # that project's own ALKERA_EPISODE_* variables, which belong to the
        # rollout and not to us. The label is what compose itself stamps.
        "_specs": [
            json.loads(out(["docker", "inspect", cid]))[0]
            for cid in out(["docker", "ps", "-q", "--filter",
                            f"label=com.docker.compose.project={args.project}"]
                           ).split()],
    }
    if not evidence["_specs"]:
        raise SystemExit(
            f"no running containers in compose project {args.project!r}; P7 "
            f"measures a stack that is up, and this one is not")
    p7 = property_7_native_episode(stack, evidence, sorted(set(warehouses)),
                                   services)
    for key in [k for k in evidence if k.startswith("_")]:
        evidence.pop(key)
    evidence["summary"] = {"p7_native_episode_filesystem": p7["passed"]}
    evidence["not_measured_here"] = (
        "P0-P5 describe the MCP-transport stack (evidence/containment.json) and "
        "P6 the native network (evidence/containment-native.json); this run "
        "measures P7 only.")
    evidence["passed"] = p7["passed"]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, indent=2, sort_keys=True),
                        encoding="utf-8")
    print(json.dumps({
        "artifact": str(args.out),
        "checks": p7["probe"]["checks"],
        "world_binds_on_the_episode": p7["world_binds_on_the_episode"],
        "episode_publishes_no_host_port": p7["episode_publishes_no_host_port"],
        "published_host_ports": p7["published_host_ports"],
        "passed": evidence["passed"],
    }, indent=2))
    return 0 if evidence["passed"] else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="alkera-b2b4")
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--settle", type=int, default=20)
    parser.add_argument("--out", type=Path, default=HERE / "evidence" / "containment.json")
    parser.add_argument("--keep-up", action="store_true")
    parser.add_argument(
        "--backend", default=None,
        help="the primary warehouse backend for the fixture stack (ALKERA_BACKEND; "
             "compose.yml defaults to postgres)",
    )
    parser.add_argument(
        "--warehouses", default=None,
        help="JSON list of additional WarehouseSpec objects for the fixture stack "
             "(ALKERA_WAREHOUSES), e.g. '[{\"name\": \"pg\", \"backend\": \"postgres\"}]' "
             "with --backend duckdb for a DuckDB primary attaching the Postgres peer",
    )
    parser.add_argument(
        "--native", action="store_true",
        help="native mode (gateway/NATIVE.md): layer compose.episode.yml "
             "and compose.native.yml, and measure P6 alone. P0-P5 are properties of "
             "the MCP-transport stack and are NOT re-run here - the overlay parks the "
             "gateway, swaps the object store and detaches the episode from "
             "backend_net, so running them would record five properties about a stack "
             "nobody runs that way. Use `python containment.py` for those.",
    )
    parser.add_argument(
        "--native-episode", action="store_true",
        help="measure P7 alone against a native episode stack that is ALREADY "
             "UP (`--project` names it). Lifecycle is deliberately not this "
             "script's: the stack P7 describes is one a ROLLOUT built, with "
             "that rollout's world and that rollout's warehouse mounts, and a "
             "stack stood up here would be a stack nobody measured a number "
             "in. docs/implementation_plan/harness/NATIVE_CONTAINER.md has the two commands.",
    )
    parser.add_argument(
        "--world", type=Path, default=None,
        help="with --native-episode: the rollout's world root on the HOST. The "
             "warehouses P7 requires to be mounted are globbed from it by "
             "world/world/fingerprint.py's own rule (var/warehouse/*.duckdb), "
             "so the expectation is read off the world rather than typed.",
    )
    parser.add_argument(
        "--warehouse", action="append", default=[], metavar="REL",
        help="with --native-episode: a world-relative warehouse path that must "
             "be mounted; repeatable. Use instead of --world when the world is "
             "not readable from here.",
    )
    args = parser.parse_args()
    # Into the environment rather than onto the command line: every compose
    # invocation below - up, exec, run, down - must see the same declaration,
    # and compose reads these from the environment of the process that calls it.
    if args.backend:
        os.environ["ALKERA_BACKEND"] = args.backend
    if args.warehouses:
        json.loads(args.warehouses)  # fail here, not inside the container
        os.environ["ALKERA_WAREHOUSES"] = args.warehouses

    if args.native_episode:
        return native_episode_main(args)

    overlays = (EPISODE_OVERLAY, NATIVE_OVERLAY) if args.native else ()
    stack = Stack(args.project, args.profile, overlays=overlays)
    evidence = {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "project": args.project,
        "profiles": args.profile,
        "backend": os.environ.get("ALKERA_BACKEND") or "postgres (compose default)",
        "warehouses": json.loads(args.warehouses) if args.warehouses else [],
        "compose_file": str(COMPOSE_FILE),
        "compose_overlays": [str(p) for p in overlays],
        "mode": "native" if args.native else "mcp",
        "host": {
            "platform": platform.platform(),
            "docker": out(["docker", "version", "--format", "{{.Server.Version}}"]),
        },
    }
    if args.native:
        try:
            stack.compose("up", "-d", "--wait")
            evidence["_declared"] = stack.declared()
            evidence["_specs"] = stack.containers()
            p6 = property_6_native(stack, evidence)
        finally:
            if not args.keep_up:
                stack.compose("down", "-v", "-t", "5", check=False)
        for key in [k for k in evidence if k.startswith("_")]:
            evidence.pop(key)
        evidence["summary"] = {"p6_only_fake_services_reachable": p6["passed"]}
        evidence["not_measured_here"] = (
            "P0-P5 describe the MCP-transport stack (evidence/containment.json); "
            "this run measures P6 only.")
        evidence["passed"] = p6["passed"]
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
        print(json.dumps({
            "artifact": str(args.out),
            **evidence["summary"],
            "services_unreachable": p6["probe"]["services_unreachable"],
            "controls_that_resolved": p6["probe"]["controls_that_resolved"],
            "compose_names_that_also_resolve": [
                row["name"] for row in p6["probe"]["compose_service_names"]
                if row.get("addresses")],
            "unexpected_open": p6["probe"]["unexpected_open"],
            "probe_can_see_an_open_name_and_port":
                p6["probe"].get("self_test", {}).get("probe_can_see_an_open_name_and_port"),
            "passed": evidence["passed"],
        }, indent=2))
        return 0 if evidence["passed"] else 1

    try:
        stack.compose("up", "-d", "--wait")
        evidence["_declared"] = stack.declared()
        evidence["_specs"] = stack.containers()
        p2 = property_2_egress(stack, evidence)
        p3 = property_3_channel(stack, evidence)
        p4 = property_4_determinism(stack, evidence, args.settle)
        p5 = property_5_ledger(stack, evidence)
        property_1_grading(stack, evidence)
        p1 = property_1_grading_after_teardown(stack, evidence)
    finally:
        if not args.keep_up:
            stack.compose("down", "-v", "-t", "5", check=False)

    for key in [k for k in evidence if k.startswith("_")]:
        evidence.pop(key)
    # P0 is not one of the four properties; it is the precondition all four assume.
    # B3-FINISH found the `landing` bucket was never created on the pinned s3mock
    # digest, so the storage half of acceptance_inside.py had never passed. Read the
    # bucket list off the wire from inside the container so that cannot recur quietly.
    load = p4["load_exercised"]
    p0 = {
        "s3_buckets_on_the_wire": load["s3_buckets_on_the_wire"],
        "s3_roundtrip_through_gateway": load["s3_roundtrip"],
        "warehouse_rows": load["warehouse_rows"],
        "bi_panels": load["bi_panels"],
        "docs_lines": load["docs_lines"],
        "passed": (
            "landing" in load["s3_buckets_on_the_wire"]
            and load["s3_roundtrip"]
            and load["warehouse_rows"] == 2
            and load["bi_panels"]
            and load["docs_lines"]
        ),
    }
    if evidence["warehouses"]:
        # The dual case is a precondition too: every declared warehouse must
        # answer by name, the attached peer must join, and the engine must
        # refuse an ATTACH nobody declared - or the stack is not the dual
        # stack the evidence claims to describe.
        dual = load.get("dual") or {}
        declared = ["primary"] + [w["name"] for w in evidence["warehouses"]]
        p0["dual"] = dual
        p0["dual_passed"] = bool(
            dual
            and sorted(dual.get("by_name") or {}) == sorted(declared)
            and all(len(rows) == 2 for rows in dual["by_name"].values())
            and (dual.get("cross_join") is not None
                 or not any(n.startswith("attached")
                            for n in (dual.get("attachments") or {}).values()))
            and (dual.get("undeclared_attach") or {}).get("ok") is False
        )
        p0["passed"] = p0["passed"] and p0["dual_passed"]
    evidence["p0_world_provisioned"] = p0
    evidence["summary"] = {
        "p0_world_actually_provisioned": p0["passed"],
        "p1_grading_zone_isolated": p1["passed"],
        "p2_no_egress_from_any_service": p2["passed"],
        "p3_gateway_is_only_channel": p3["passed"],
        "p4_no_service_perturbs_world_tree": p4["passed"],
        "p5_ledger_protected": p5["passed"],
    }
    evidence["passed"] = all(evidence["summary"].values())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    print(
        json.dumps(
            {
                "artifact": str(args.out),
                **evidence["summary"],
                "probe_can_detect_egress": p2["probe_control"]["probe_can_detect_egress"],
                "services_probed": [s["service"] for s in evidence["services"]],
                "passed": evidence["passed"],
            },
            indent=2,
        )
    )
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
