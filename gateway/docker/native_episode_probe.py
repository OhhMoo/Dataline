"""Containment P7, measured from inside the NATIVE episode container.

P6 (`native_probe.py`) asks what the episode's network reaches. P7 asks what
its FILESYSTEM reaches, and it exists because the two questions came apart the
day `--driver native` was first run against real models. Under the host
topology the subject's bash is a host process: it can `cp
var/warehouse/warehouse.duckdb warehouse.duckdb.bak` - a backup, good practice,
audited by nothing - and it can `ls` the rollout directory the canaries live
in. Three of five GT001 rollouts quarantined on exactly those two actions
(`out_of_band_world_change` twice, `canary_leak` once). Neither is possible by
construction when the episode container mounts the workspace and the warehouse
FILES and nothing else, and P7 is the measurement of "by construction".

Eight questions, each answered by trying:

  A. `/world` holds the mounted warehouse file(s) AND NOTHING ELSE from the
     world tree. Every other plane the world has - `var/sched`, `var/obs`,
     `var/workflow`, `var/bi`, `var/world`, `build` - must be absent, and the
     probe is told their names by the caller so it cannot pass by not looking.
  B. the rollout directory and the grading zone have no path here: not denied,
     absent, and the names are the caller's real host paths, spelled the way
     Docker Desktop would spell them if a mount leaked them.
  C. the ledger path answers, is empty, and refuses a write.
  D. the workspace is read-write and the warehouse file is read-write, because
     a topology that contained the episode by making its own job impossible
     would pass every question above and be useless.
  E. the nine services AND the scheduler plane answer `/health` from this
     vantage. The scheduler is asked by name because it is the one plane whose
     files this topology deliberately does not mount: `var/sched`, its run
     logs, `var/obs` and `build/sched` are absent by question A, so if
     `sched.internal` did not answer the episode would have no scheduler
     surface at all and a task needing `sched_*` could not be measured here.
     What the probe does NOT do is trigger a job: P7 runs before and after
     every episode and a containment probe that moved the world would be
     evidence about itself. That half - a `trigger` writing a
     `sched.sched_trigger` row and the run log appearing in the HOST world - is
     asserted by `tasks/harness/test_native_container.py` and by the GT004
     acceptance, where moving the world is the point.
     A service whose URL is `https://` (a TLS backend hosted in native-
     services: Databricks on 443) is asked by TCP connect first and then over
     https, trusting the box's own bundle - the one its clients are given
     (`tls_bundles`, else the episode's REQUESTS_CA_BUNDLE). Asking a TLS port
     in plain HTTP gets no answer at all, which is how P7 on de/databricks
     failed a box that was fine (CONTAINMENT_2026-10-07.md).
     The DOORS that are not HTTP services - postgres, minio when the store is
     plugged, a container backend (snowflake, redshift), jupyter - are asked by
     TCP connect (`doors`), so every door the box plugs is asked about.
  F. egress is refused - TCP and DNS, with the interface and route list read
     from `/proc` so a second interface nobody declared cannot hide.
  G. what the environment profile EXCLUDES does not even resolve: an excluded
     one of the nine, an unplugged BI vendor (none resolves on an unprofiled
     box), `sched.internal` without `ops_sched`, `minio.internal` without an
     object store. A profile with no orchestrator is served no scheduler plane
     BY DESIGN, so for it question E's scheduler half is recorded under
     `checks_not_applicable` with its reason - never silently dropped - and
     this question asserts the stronger negative in its place. An excluded
     service that is a COMPOSE service (minio) is also asked by its compose
     name and, when the caller knows the project, its container name: with
     only its alias gone, `minio` still resolved on every da/* box and G
     passed (finding 1 of CONTAINMENT_2026-10-07.md).
  H. the native services' OWN state (`/service-state` in native-services: the
     BI vendors' published content, the config the services read) is not
     mounted here under any name, and its host path does not exist here. It
     used to live in `/audit`, which this container mounts read-write, and was
     forgeable from the subject's shell (tasks/harness/bistate.py).

Standard library only: it runs under the episode image's interpreter with
nothing installed, and it is fed to `python -` on stdin so it is never written
to the container's disk and never appears in a command line the subject can
read. THE SPEC TRAVELS THE SAME WAY, as one assignment prepended to the source,
and that is a fix rather than a preference: passed as an argv it went through
`/opt/alkera/shims/python`, which is the audited shim, and the shim wrote the
whole spec - canary tokens, the host rollout path, the marker names - into
`/audit/actions.jsonl`, a file the episode container mounts read-write. The
probe found its own leak on the first real rollout. For the same reason every
harness `docker exec` names the interpreter by absolute path: a harness command
must never become a row in the episode's action log.

    docker exec -i <episode> /usr/local/bin/python -   < SPEC + this PROBE

`PROBE` is the source both callers use: `gateway/docker/containment.py
--native-episode` (which writes `evidence/containment-native-episode.json`) and
`tasks/harness/container_episode.NativeContainerEpisode.containment_probe`
(which puts the same document on every rollout record, before and after the
episode). One definition, two callers, so a rollout and the evidence file can
never be measuring two different things.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

#: The probe body. Read as text and piped to `python -`; never imported.
PROBE = r"""
import json, os, socket, ssl, urllib.error, urllib.parse, urllib.request

spec = json.loads(SPEC)
out = {}

# --- A. what is mounted, from the kernel rather than from a compose file ----
mounts = []
try:
    for line in open("/proc/self/mountinfo"):
        f = line.split()
        sep = f.index("-")
        mounts.append({"root": f[3], "target": f[4], "options": f[5],
                       "fstype": f[sep + 1]})
except OSError as exc:
    mounts = [{"error": str(exc)}]
out["mounts"] = [m for m in mounts
                 if m.get("target", "").startswith(("/world", "/workspace",
                                                    "/audit"))]
out["bind_targets"] = sorted(m["target"] for m in out["mounts"]
                             if m.get("fstype") not in ("tmpfs",))

# The world tree, one plane at a time. `world_planes` is every world-relative
# path the world manifest named that is NOT a mounted warehouse; every one of
# them must be absent, and the listing of /world is recorded whole so a reader
# can see what IS there instead of trusting the list.
def tree(path, depth=2):
    rows = []
    for current, dirs, files in os.walk(path):
        rel = os.path.relpath(current, path)
        if rel != "." and rel.count(os.sep) >= depth:
            dirs[:] = []
            continue
        for name in sorted(files):
            rows.append(os.path.join(rel, name).replace("\\", "/").lstrip("./"))
        if len(rows) > 200:
            rows.append("... truncated")
            return rows
    return sorted(rows)

out["world_listing"] = tree("/world") if os.path.isdir("/world") else None
out["world_planes_present"] = [p for p in spec["world_planes"]
                               if os.path.exists("/world/" + p)]
out["warehouses_expected"] = spec["warehouses"]
out["warehouses_present"] = [p for p in spec["warehouses"]
                             if os.path.isfile("/world/" + p)]

# --- B. the zone, the rollout directory, the host paths --------------------
zone = []
for path in spec["forbidden_dirs"]:
    row = {"path": path, "exists": os.path.exists(path)}
    try:
        row["listing"] = sorted(os.listdir(path))[:20]
    except OSError as exc:
        row["listing_error"] = "%s: %s" % (type(exc).__name__, exc)
    zone.append(row)
out["forbidden_dirs"] = zone
reads = []
for path in spec["forbidden_files"]:
    row = {"path": path}
    try:
        with open(path, "rb") as fh:
            row["read_bytes"] = len(fh.read(64))
        row["result"] = "READ"
    except OSError as exc:
        row["result"] = "%s: %s" % (type(exc).__name__, exc)
    reads.append(row)
out["forbidden_reads"] = reads
out["host_paths_present"] = [p for p in spec["host_paths"] if os.path.exists(p)]
out["mount_roots_naming_repo"] = [
    m["root"] for m in out["mounts"]
    if any(n and n.lower() in m.get("root", "").lower()
           for n in spec["repo_path_leaf_forms"])]
out["root_listing"] = sorted(os.listdir("/"))

# The canary sweep, phrased as the subject would phrase it. `grep -r` over the
# whole filesystem and a `find` for json: the two commands the r3 subject ran
# when it read the rollout directory. Here they are run in python so the answer
# is a list rather than an exit code.
hits, scanned, truncated = [], 0, False
needles = [n for n in spec["canaries"] if n]
names = set(spec["marker_names"])
for base in spec["sweep_roots"]:
    if truncated:
        break
    for current, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__",
                                                "node_modules", "site-packages")]
        for name in files:
            scanned += 1
            if scanned > spec["sweep_limit"]:
                truncated = True
                break
            path = os.path.join(current, name)
            if name in names:
                hits.append({"kind": "marker_filename", "path": path})
                continue
            if any(n in name for n in needles):
                hits.append({"kind": "canary_filename", "path": path})
                continue
            if not needles:
                continue
            try:
                if os.path.getsize(path) > 2_000_000:
                    continue
                blob = open(path, "r", encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            for needle in needles:
                if needle in blob:
                    hits.append({"kind": "canary_in_file", "path": path})
                    break
        if truncated:
            break
out["sweep"] = {"files_scanned": scanned, "truncated": truncated, "hits": hits,
                "roots": spec["sweep_roots"]}

# --- C. the ledger ---------------------------------------------------------
ledger = {"path": spec["ledger"], "exists": os.path.isdir(spec["ledger"])}
try:
    ledger["listing"] = sorted(os.listdir(spec["ledger"]))
except OSError as exc:
    ledger["listing"] = None
    ledger["listing_error"] = "%s: %s" % (type(exc).__name__, exc)
try:
    with open(os.path.join(spec["ledger"], "probe-write"), "w") as fh:
        fh.write("x")
    ledger["write"] = "WROTE"
except OSError as exc:
    ledger["write"] = type(exc).__name__
out["ledger"] = ledger

# --- D. the episode can still do its job -----------------------------------
work = {"workspace_writable": bool(os.access("/workspace", os.W_OK)),
        "audit_writable": bool(os.access("/audit", os.W_OK)),
        "warehouses_writable": {p: bool(os.access("/world/" + p, os.W_OK))
                                for p in out["warehouses_present"]}}
out["work"] = work

# --- E. the nine services, from this vantage -------------------------------
def tcp(host, port):
    try:
        socket.create_connection((host, port), timeout=5).close()
        return "CONNECTED"
    except OSError as exc:
        return type(exc).__name__

bundles = spec.get("tls_bundles") or {}
services = []
for name, url in sorted(spec["services"].items()):
    row = {"service": name, "url": url}
    parts = urllib.parse.urlsplit(url)
    opener = {}
    if parts.scheme == "https":
        # TCP first, then TLS trusting the bundle the box's clients are given.
        row["tcp"] = tcp(parts.hostname, parts.port or 443)
        row["bundle"] = (bundles.get(name) or os.environ.get("REQUESTS_CA_BUNDLE")
                         or os.environ.get("SSL_CERT_FILE"))
        try:
            opener["context"] = ssl.create_default_context(cafile=row["bundle"])
        except (OSError, ssl.SSLError) as exc:
            row["error"] = "%s: %s" % (type(exc).__name__, exc)
    try:
        if "error" in row:
            raise OSError(row["error"])
        with urllib.request.urlopen(url + "/health", timeout=5, **opener) as response:
            body = response.read(4096).decode("utf-8", "replace")
            row["status"] = response.status
            payload = json.loads(body)
            row["ok"] = payload.get("ok")
            row["items"] = payload.get("items")
    except urllib.error.HTTPError as exc:
        row["status"] = exc.code
    except Exception as exc:
        row.setdefault("error", "%s: %s" % (type(exc).__name__, exc))
    # REACHABLE, not populated. A 503 carrying a health document is a service
    # answering about a world plane this task's seed does not have
    # (`native/context.service_health`), and six of GT001's nine answer exactly
    # that - so asking for content here would make a containment property fail
    # for a fact about the world. Whether a surface has anything in it is the
    # surface gate's question and it refuses to score a world that does not.
    row["reachable"] = row.get("status") is not None
    row["populated"] = row.get("ok") is True and isinstance(row.get("items"), int) \
        and row["items"] > 0
    services.append(row)
out["services"] = services
out["services_unreachable"] = [r["service"] for r in services
                               if not r["reachable"]]
out["services_with_no_content"] = [r["service"] for r in services
                                   if r["reachable"] and not r["populated"]]
# The scheduler plane is named separately because its answer means something
# different: the nine may legitimately be empty in a world whose seed does not
# fill their plane, while a scheduler with no jobs is a world the episode
# cannot be asked to schedule anything in. `items` is the job count.
out["scheduler"] = next((r for r in services if r["service"] == spec["scheduler"]),
                        None)
# The doors that are not HTTP services, by TCP connect.
doors = []
for name, target in sorted((spec.get("doors") or {}).items()):
    host, _, port = target.rpartition(":")
    doors.append({"door": name, "target": target, "tcp": tcp(host, int(port))})
out["doors"] = doors
out["doors_unreachable"] = [d["door"] for d in doors if d["tcp"] != "CONNECTED"]

# --- F. egress -------------------------------------------------------------
ifaces, routes = [], []
try:
    for line in open("/proc/net/dev").read().splitlines()[2:]:
        name = line.split(":")[0].strip()
        if name:
            ifaces.append(name)
    for line in open("/proc/net/route").read().splitlines()[1:]:
        f = line.split()
        if len(f) > 2:
            routes.append({"iface": f[0],
                           "dest": ".".join(str(int(f[1][i:i+2], 16))
                                            for i in (6, 4, 2, 0))})
except OSError:
    pass
tcp = []
for host, port in [("8.8.8.8", 53), ("1.1.1.1", 443), ("93.184.215.14", 80)]:
    try:
        socket.create_connection((host, port), timeout=3).close()
        tcp.append({"target": "%s:%d" % (host, port), "result": "CONNECTED"})
    except OSError as exc:
        tcp.append({"target": "%s:%d" % (host, port), "result": type(exc).__name__})
dns = []
for name in ["example.com", "pypi.org", "api.anthropic.com"]:
    try:
        socket.getaddrinfo(name, 443)
        dns.append({"name": name, "result": "RESOLVED"})
    except OSError as exc:
        dns.append({"name": name, "result": type(exc).__name__})
out["network"] = {"interfaces": ifaces, "routes": routes, "egress_tcp": tcp,
                  "egress_dns": dns,
                  "default_route": any(r["dest"] == "0.0.0.0" for r in routes)}
out["egress_blocked"] = (all(r["result"] != "CONNECTED" for r in tcp)
                         and all(d["result"] != "RESOLVED" for d in dns))

# --- G. what the profile excludes does not resolve ---------------------------
excluded = []
for name, hosts in sorted((spec.get("excluded") or {}).items()):
    # one host, or the alias plus the compose and container names
    hosts = [hosts] if isinstance(hosts, str) else list(hosts)
    row = {"service": name, "host": hosts[0], "names": {}}
    found = set()
    for host in hosts:
        try:
            row["names"][host] = sorted({a[4][0] for a in socket.getaddrinfo(host, None)})
        except OSError as exc:
            row["names"][host] = []
            row.setdefault("error", type(exc).__name__)
        found.update(row["names"][host])
    row["addresses"] = sorted(found)
    excluded.append(row)
out["excluded_services"] = excluded
out["excluded_that_resolve"] = [r["service"] for r in excluded if r["addresses"]]

# --- H. the services' own state is not mounted here -------------------------
def under(path, base):
    base = base.rstrip("/") or "/"
    return path == base or path.startswith(base + "/")

state = spec.get("service_state") or {}
state_mounts = [m for m in mounts
                if any(under(m.get("target", ""), t) for t in state.get("targets", []))
                or any(under(m.get("root", ""), r) for r in state.get("roots", []))]
out["service_state"] = {
    "targets": state.get("targets", []), "roots": state.get("roots", []),
    "mounts": state_mounts,
    "roots_present": [r for r in state.get("roots", []) if os.path.exists(r)],
    "targets_present_as_plain_paths": [t for t in state.get("targets", [])
                                       if os.path.exists(t)]}

out["identity"] = {"uid": os.getuid(), "gid": os.getgid(), "cwd": os.getcwd()}
out["checks"] = {
    "world_holds_only_the_mounted_warehouses": not out["world_planes_present"],
    "every_declared_warehouse_is_mounted":
        out["warehouses_present"] == out["warehouses_expected"],
    "no_grading_or_rollout_path_exists":
        not any(z["exists"] for z in out["forbidden_dirs"])
        and all(r["result"] != "READ" for r in out["forbidden_reads"])
        and not out["host_paths_present"],
    "no_canary_and_no_marker_is_readable": not out["sweep"]["hits"],
    "no_mount_root_names_the_repository": not out["mount_roots_naming_repo"],
    "ledger_is_present_empty_and_read_only":
        ledger["exists"] and ledger["listing"] == [] and ledger["write"] != "WROTE",
    "the_episode_can_still_work":
        work["workspace_writable"] and work["audit_writable"]
        and bool(work["warehouses_writable"])
        and all(work["warehouses_writable"].values()),
    "every_service_is_reachable_from_the_episode":
        bool(services) and not out["services_unreachable"],
    "egress_refused": out["egress_blocked"] and not out["network"]["default_route"],
}
if spec["scheduler"]:
    out["checks"]["the_scheduler_plane_answers_with_jobs"] = (
        bool(out["scheduler"]) and out["scheduler"].get("populated") is True)
if spec.get("excluded"):
    out["checks"]["no_excluded_service_resolves"] = not out["excluded_that_resolve"]
if spec.get("doors"):
    out["checks"]["every_plugged_door_accepts_tcp"] = not out["doors_unreachable"]
if state.get("targets") or state.get("roots"):
    # A plain directory the subject made at that path is its own container's
    # and reaches nothing; a MOUNT is the leak, and so is the host path.
    out["checks"]["the_services_state_is_out_of_reach"] = (
        not state_mounts and not out["service_state"]["roots_present"])
out["checks_not_applicable"] = dict(spec.get("not_applicable") or {})
out["contained"] = all(out["checks"].values())
print(json.dumps(out, sort_keys=True))
"""

#: The world-relative planes a native episode container must NOT hold. Every
#: one is a directory `world/world/build.py` creates under the world root, so a
#: mount that leaked the tree would light one of them up. `var/warehouse` is
#: deliberately absent from the list: the warehouse FILES are mounted, so that
#: directory exists by construction and is checked by name instead.
WORLD_PLANES = ("var/sched", "var/obs", "var/workflow", "var/bi", "var/world",
                "var/ingest", "var/dbt", "build", "home")
#: Filename-shaped things that must not be readable, kept in step with
#: `tasks/harness/blindness.MARKERS` and `container_episode.MARKER_NAMES`.
MARKER_NAMES = ("SEED_SPEC.md", "expected.duckdb", "manifest.yaml", "grade.py",
                "solution.sh", "run_baselines.py", "fixture_contract.py",
                "make_fixture_world.py", "task.toml", "test_integration.py",
                "world.duckdb", "3_track.md", "agent.md")
#: Where the grading zone would be if any path led to it.
FORBIDDEN_DIRS = ("/grading", "/world/../grading", "/workspace/../grading",
                  "/grading-zone", "/tests")
LEDGER_DIR = "/world/var/ledger"
#: The one served plane that is not a vendor: the backend of the workspace's own
#: `./ops/sched`. Named to the probe so question E can single it out.
SCHEDULER_SERVICE = "sched"
#: Question E's scheduler half, by the name it carries in `checks`.
SCHEDULER_CHECK = "the_scheduler_plane_answers_with_jobs"
#: native-services' own state directory (compose.native.yml,
#: native_services.py), by the path it has in THAT container.
SERVICE_STATE_TARGET = "/service-state"
#: The compose service that, alone, mounts it.
SERVICE_STATE_OWNER = "native-services"
#: The episode image's own interpreter. Named absolutely on purpose: the
#: episode container's PATH puts `/opt/alkera/shims` first, and `python` there
#: is the audited shim - a harness probe run through it writes a row into the
#: episode's action log, and the first one wrote this probe's own spec.
CONTAINER_PYTHON = "/usr/local/bin/python"
SWEEP_ROOTS = ("/world", "/workspace", "/audit", "/root", "/opt/episode", "/tmp")
SWEEP_LIMIT = 40_000
#: Excluded services that are compose services of their own rather than
#: modules inside native-services, by compose service name: question G asks
#: their compose name (and container name) too, not only the alias.
COMPOSE_EXCLUDABLE = {"minio": "minio"}
#: The doors every native box serves that are not HTTP services of serve.py,
#: by the static stack (compose.native.yml): postgres always, minio when the
#: object store is plugged; and the notebook plug's (container_episode's
#: JUPYTER_URL). Container backends come from `alkera_gateway.backends`.
POSTGRES_DOOR = ("postgres.internal", 5432)
MINIO_DOOR = ("minio.internal", 9000)
JUPYTER_DOOR = ("jupyter.internal", 8888)

Scheme = Literal["http", "https", "tcp"]


@dataclass(frozen=True)
class Expect:
    """One door the episode must reach, and how it is asked.

    `http`: `GET /health` (question E). `https`: TCP connect, then `GET
    /health` over TLS trusting `bundle` - the bundle the box's clients are
    given - so a TLS port is never asked in plain HTTP. `tcp`: a connect, for
    the doors that are not HTTP services (postgres, minio, a container
    backend, jupyter).
    """

    name: str
    host: str
    port: int
    scheme: Scheme
    bundle: str | None = None
    source: str = ""

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def target(self) -> str:
        return f"{self.host}:{self.port}"


def spec(*, warehouses, services, forbidden_files=(), host_paths=(),
         canaries=(), repo_root="", world_planes=WORLD_PLANES,
         sweep_roots=SWEEP_ROOTS, sweep_limit=SWEEP_LIMIT,
         scheduler=SCHEDULER_SERVICE, excluded=None, not_applicable=None,
         service_state=None, tls_bundles=None, doors=None) -> dict:
    """The probe's one JSON argument, built in one place.

    `warehouses` are world-relative posix paths (`var/warehouse/x.duckdb`) and
    `services` is `{name: base_url}` as the EPISODE addresses them, never as the
    harness does: the question P7 answers is what the episode's own vantage can
    reach. An `https://` service is asked over TLS trusting
    `tls_bundles[name]` (else the episode's REQUESTS_CA_BUNDLE). `doors` is
    `{name: "host:port"}` asked by TCP connect. `excluded` is `{name: host}`
    or `{name: [host, compose name, ...]}` that must NOT resolve (question G);
    `expectations()` builds `services`, `tls_bundles`, `doors`, `excluded`,
    `scheduler` and `not_applicable` together from an environment profile.
    `service_state` is `{"targets": [...], "roots": [<host path>]}` (question H).

    `scheduler=None` means "this box is served no scheduler plane by design",
    and it is refused unless the reason is recorded (`not_applicable`) and
    `sched.internal` is in `excluded` - a containment check may be replaced by
    a stronger one, never just switched off.
    """
    excluded = dict(excluded or {})
    not_applicable = dict(not_applicable or {})
    if scheduler is None and (SCHEDULER_CHECK not in not_applicable
                              or SCHEDULER_SERVICE not in excluded):
        raise ValueError(
            "scheduler=None needs not_applicable[SCHEDULER_CHECK] (why) and "
            f"{SCHEDULER_SERVICE!r} in excluded (so its absence is asserted)")
    if scheduler is not None and SCHEDULER_CHECK in not_applicable:
        raise ValueError("the scheduler check cannot be both asked and "
                         "not applicable")
    leaf = str(repo_root).replace("\\", "/").split(":", 1)[-1]
    return {
        "warehouses": sorted(warehouses),
        "world_planes": list(world_planes),
        "services": dict(services),
        "tls_bundles": dict(tls_bundles or {}),
        "doors": dict(doors or {}),
        "excluded": excluded,
        "not_applicable": not_applicable,
        "service_state": dict(service_state or {}),
        "forbidden_dirs": list(FORBIDDEN_DIRS),
        "forbidden_files": list(forbidden_files),
        "host_paths": list(host_paths),
        "canaries": list(canaries),
        "marker_names": list(MARKER_NAMES),
        "repo_path_leaf_forms": [leaf] if leaf else [],
        "ledger": LEDGER_DIR,
        "scheduler": scheduler,
        "sweep_roots": list(sweep_roots),
        "sweep_limit": int(sweep_limit),
    }


def plug_args(plugs: dict | None) -> list[str]:
    """`{slot: [plug, ...]}` -> serve.py's `--plug` values (`slot=a,b`)."""
    return [f"{slot}={','.join(names)}" for slot, names in (plugs or {}).items()]


def _plug_values(plugs) -> list[str]:
    return plug_args(plugs) if isinstance(plugs, dict) else list(plugs or ())


def expects(profile: str | None = None, plugs=()) -> list[Expect]:
    """Every door a box served for `profile` (None: the unprofiled box) must
    answer from the episode, each with the way it is asked - by serve.py's own
    rules for what native-services serves, by `alkera_gateway.backends` for the
    backends, and by the static stack for postgres, minio and jupyter.

    A hosted backend whose registry row says `tls` (Databricks, 443) is an
    `https` row with that row's bundle; a hosted backend whose module serves
    its own socket (`serve_socket`: Spark Connect's gRPC, which no HTTP/1.1
    GET can ask) is `tcp`, as serve.mount serves it; every other served
    service is `http`; the doors that are not HTTP services are `tcp`.
    """
    import importlib

    from alkera_gateway import backends as registry_backends
    from alkera_gateway.native import serve
    from alkera_gateway.native import services as registry
    from alkera_gateway.plugs import parse_plug_args, resolve

    args = _plug_values(plugs)
    profile = profile or None
    names = serve.selected_services(profile, args)
    rows = []
    for n in (registry.NAMES if names is None else names):
        if n not in serve.SERVICE_PORTS:
            continue
        b = registry_backends.BACKENDS.get(n)
        if b is not None and b.hosted and hasattr(
                importlib.import_module(f"{registry.__name__}.{n}"), "serve_socket"):
            rows.append(Expect(n, f"{n}.internal", serve.SERVICE_PORTS[n], "tcp",
                               source="alkera_gateway.backends (hosted, serve_socket)"))
        elif b is not None and b.tls:
            rows.append(Expect(n, f"{n}.internal", serve.SERVICE_PORTS[n], "https",
                               bundle=registry_backends.bundle(b),
                               source="alkera_gateway.backends (hosted, tls)"))
        else:
            rows.append(Expect(n, f"{n}.internal", serve.SERVICE_PORTS[n], "http",
                               source="serve.selected_services"))
    if serve.sched_enabled(profile, args):
        rows.append(Expect(SCHEDULER_SERVICE, f"{SCHEDULER_SERVICE}.internal",
                           serve.SCHED_PORT, "http", source="serve.sched_enabled"))
    rows.append(Expect("postgres", *POSTGRES_DOOR, "tcp",
                       source="compose.native.yml (every native box)"))
    if serve.object_store_enabled(profile, args):
        rows.append(Expect("minio", *MINIO_DOOR, "tcp",
                           source="serve.object_store_enabled"))
    resolved = None if profile is None else resolve(profile, parse_plug_args(args))
    if resolved is not None:
        rows += [Expect(b.name, b.host, b.port, "tcp",
                        source="alkera_gateway.backends.plugged (own container)")
                 for b in registry_backends.plugged(resolved.slots) if not b.hosted]
        if "notebook" in resolved.slots:
            rows.append(Expect("jupyter", *JUPYTER_DOOR, "tcp",
                               source="the notebook plug"))
    return rows


def expectations(profile: str | None = None, plugs=()) -> dict:
    """What the episode must reach, and what must not even resolve, on a box
    served for `profile` (None: the unprofiled box), by serve.py's own rules.

    `plugs` is serve.py's `--plug` values (`["bi=powerbi,tableau"]`) or the
    `{slot: [plug]}` mapping a task declares. The answer is computed by the
    functions `native/serve.py --profile` itself serves by -
    `selected_services`, `sched_enabled`, `object_store_enabled` - so P7 asks
    about exactly the set the native-services process mounted (`expects`):

      services        every served vendor service (the nine, or the profile's
                      own, plugged BI vendors included) plus the scheduler
                      plane when `ops_sched` is plugged, as `{name: url}`;
                      `https://` for a TLS backend
      tls_bundles     `{name: bundle}` for the `https://` ones
      doors           `{name: "host:port"}`, the doors that are not HTTP
                      services, asked by TCP
      excluded        every other one of the nine and of the BI vendors,
                      `sched` without `ops_sched`, `minio` without an object
                      store (by alias AND compose name), as `{name: host}` or
                      `{name: [host, ...]}` - question G
      scheduler       SCHEDULER_SERVICE, or None when not served
      not_applicable  `{SCHEDULER_CHECK: reason}` when not served

    Imports the gateway, so call it where the gateway is installed (the
    harness venv, containment.py); the PROBE itself stays standard library.
    """
    from alkera_gateway.native import serve
    from alkera_gateway.native import services as registry

    args = _plug_values(plugs)
    profile = profile or None
    rows = expects(profile, args)
    services = {e.name: e.url for e in rows if e.scheme != "tcp"}
    tls_bundles = {e.name: e.bundle for e in rows if e.scheme == "https" and e.bundle}
    doors = {e.name: e.target for e in rows if e.scheme == "tcp"}
    excluded = {n: f"{n}.internal" for n in (*registry.NAMES, *registry.PLUG_NAMES)
                if n not in services}
    not_applicable = {}
    if SCHEDULER_SERVICE in services:
        scheduler = SCHEDULER_SERVICE
    else:
        excluded[SCHEDULER_SERVICE] = f"{SCHEDULER_SERVICE}.internal"
        scheduler = None
        not_applicable[SCHEDULER_CHECK] = (
            f"profile {profile!r} plugs no ops_sched orchestrator: serve.py "
            f"--profile serves no scheduler plane by design, so there is none to "
            f"answer; `{SCHEDULER_SERVICE}.internal` must instead NOT resolve "
            f"(no_excluded_service_resolves)")
    if not serve.object_store_enabled(profile, args):
        excluded["minio"] = ["minio.internal", COMPOSE_EXCLUDABLE["minio"]]
    return {"profile": profile, "plugs": args,
            "services": dict(sorted(services.items())),
            "tls_bundles": tls_bundles,
            "doors": dict(sorted(doors.items())),
            "excluded": dict(sorted(excluded.items())),
            "scheduler": scheduler, "not_applicable": not_applicable}


def with_container_names(excluded: dict, project: str) -> dict:
    """`excluded` plus the container name compose gives each excluded compose
    service in `project` (`<project>-<service>-1`): question G then asks the
    alias, the compose name and the container name."""
    out = {}
    for name, hosts in excluded.items():
        hosts = [hosts] if isinstance(hosts, str) else list(hosts)
        if name in COMPOSE_EXCLUDABLE:
            hosts.append(f"{project}-{COMPOSE_EXCLUDABLE[name]}-1")
        out[name] = hosts if len(hosts) > 1 else hosts[0]
    return out


def _under(path: str, base: str) -> bool:
    base = base.rstrip("/") or "/"
    return path == base or path.startswith(base + "/")


def service_state_isolation(container_specs: list[dict], host_dir,
                            target: str = SERVICE_STATE_TARGET,
                            owner: str = SERVICE_STATE_OWNER) -> dict:
    """Who mounts the native services' own state, from `docker inspect`.

    The stack half of question H: native-services must mount `host_dir` at
    `target` read-write (the positive control - the state is SOMEWHERE, and
    somewhere the grader can stage it from), and no other container of the
    project - episode, jupyter, subject, egress-proxy, postgres, minio - may
    mount that host path or anything at `target`, under any name.
    `container_specs` are `docker inspect` documents.
    """
    host = Path(host_dir).resolve().as_posix() if host_dir else ""
    rows = []
    for spec_ in container_specs:
        labels = (spec_.get("Config") or {}).get("Labels") or {}
        service = labels.get("com.docker.compose.service") or spec_.get("Name", "?")
        for mount in spec_.get("Mounts") or []:
            source = mount.get("Source") or ""
            destination = mount.get("Destination") or ""
            if _under(destination, target) or (host and _under(source, host)):
                rows.append({"service": service, "source": source,
                             "destination": destination, "rw": bool(mount.get("RW"))})
    mine = [r for r in rows if r["service"] == owner]
    others = [r for r in rows if r["service"] != owner]
    owned = any(r["destination"] == target and r["rw"]
                and (not host or r["source"] == host) for r in mine)
    return {"host_dir": host, "target": target, "owner": owner, "mounts": rows,
            "mounted_read_write_in_owner": owned, "mounted_elsewhere": others,
            "isolated": owned and not others}


def run(container: str, probe_spec: dict, *, timeout: int = 600) -> dict:
    """Run the probe inside `container` over `docker exec -i`, return its JSON.

    Stdin, so the source never lands on the container's disk; one JSON argument,
    so nothing about the episode's own machinery is written into a file the
    subject could read.
    """
    program = f"SPEC = {json.dumps(json.dumps(probe_spec))}\n{PROBE}"
    result = subprocess.run(
        ["docker", "exec", "-i", container, CONTAINER_PYTHON, "-"],
        input=program, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout)
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError(
            f"the P7 probe printed nothing in {container[:12]}: "
            f"{result.stderr[-2000:]}")
    return json.loads(lines[-1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="native_episode_probe")
    parser.add_argument("--container", required=True,
                        help="the episode container id or name")
    parser.add_argument("--warehouse", action="append", default=[],
                        metavar="REL",
                        help="a world-relative warehouse path that must be "
                             "mounted, e.g. var/warehouse/warehouse.duckdb; "
                             "repeatable")
    parser.add_argument("--service", action="append", default=[],
                        metavar="NAME=URL",
                        help="a service the episode must reach, e.g. "
                             "jira=http://jira.internal:8080; repeatable. The "
                             "scheduler plane is one of them and must be named "
                             f"{SCHEDULER_SERVICE!r}")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    services = {}
    for item in args.service:
        name, _, url = item.partition("=")
        services[name] = url
    evidence = run(args.container,
                   spec(warehouses=args.warehouse, services=services))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(evidence, indent=2, sort_keys=True),
                            encoding="utf-8")
    print(json.dumps({"checks": evidence["checks"],
                      "contained": evidence["contained"]}, indent=2))
    return 0 if evidence["contained"] else 1


if __name__ == "__main__":
    sys.exit(main())
