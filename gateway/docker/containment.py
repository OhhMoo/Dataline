"""Containment evidence for the Track B compose stack.

Runs the full lifecycle and writes ``evidence/containment.json``. Every claim in that
file is produced by a command this script ran; nothing is asserted from the compose
file alone, because ``internal: true`` is a property of a *network*, not proof that a
given service is attached to the network you think it is, nor that it has no second
interface.

Four properties, one section each:

P1  the grading zone does not exist during the episode, and when it does exist it is
    joined to no network the episode can reach and cannot write the world (rule 1).
P2  no service in the episode zone can reach the internet - proven from inside the
    network namespace of every running service, not from one of them - and the probe
    is shown to be capable of detecting egress by running it somewhere it is allowed.
P3  the gateway is the only channel: the agent's vantage reaches the gateway port and
    nothing else, and the gateway's own tool surface exposes no socket primitive.
P4  no service perturbs the fingerprinted world tree, idle or under load.

Usage::

    python containment.py                       # episode stack
    python containment.py --profile snowflake   # + the fakesnow socket
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
COMPOSE_FILE = HERE / "compose.yml"
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
print(json.dumps(out))
"""

# The tool surface is the agent's only channel. A tool that accepted a host, port,
# URL or DSN would be a second one.
TOOL_SURFACE = r"""
import json
from alkera_gateway.tools import TOOLS
socketish = {"host", "hostname", "port", "url", "uri", "endpoint", "dsn", "server",
             "address", "addr", "proxy", "connection", "conn", "account", "network"}
fields, offenders = {}, []
for name, (model, _) in TOOLS.items():
    names = sorted(model.model_fields)
    fields[name] = names
    for field in names:
        if field.lower() in socketish:
            offenders.append(f"{name}.{field}")
print(json.dumps({"tool_count": len(TOOLS), "fields": fields,
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
    def __init__(self, project, profiles):
        self.project = project
        self.profiles = list(profiles)
        self.base = ["docker", "compose", "-p", project, "-f", str(COMPOSE_FILE)]
        for profile in self.profiles:
            self.base += ["--profile", profile]

    def compose(self, *args, check=True):
        return run([*self.base, *args], check=check)

    def declared(self):
        """Every service, including profile-gated ones. `config` omits services whose
        profile is not enabled, which silently empties any check written against it."""
        argv = ["docker", "compose", "-p", self.project, "-f", str(COMPOSE_FILE)]
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
        and not delta(first, after)
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="alkera-b2b4")
    parser.add_argument("--profile", action="append", default=[])
    parser.add_argument("--settle", type=int, default=20)
    parser.add_argument("--out", type=Path, default=HERE / "evidence" / "containment.json")
    parser.add_argument("--keep-up", action="store_true")
    args = parser.parse_args()

    stack = Stack(args.project, args.profile)
    evidence = {
        "generated": datetime.now(UTC).isoformat(timespec="seconds"),
        "project": args.project,
        "profiles": args.profile,
        "compose_file": str(COMPOSE_FILE),
        "host": {
            "platform": platform.platform(),
            "docker": out(["docker", "version", "--format", "{{.Server.Version}}"]),
        },
    }
    try:
        stack.compose("up", "-d", "--wait")
        evidence["_declared"] = stack.declared()
        evidence["_specs"] = stack.containers()
        p2 = property_2_egress(stack, evidence)
        p3 = property_3_channel(stack, evidence)
        p4 = property_4_determinism(stack, evidence, args.settle)
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
    evidence["p0_world_provisioned"] = p0
    evidence["summary"] = {
        "p0_world_actually_provisioned": p0["passed"],
        "p1_grading_zone_isolated": p1["passed"],
        "p2_no_egress_from_any_service": p2["passed"],
        "p3_gateway_is_only_channel": p3["passed"],
        "p4_no_service_perturbs_world_tree": p4["passed"],
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
