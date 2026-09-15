"""Run an episode inside the `episode` service of gateway/docker/compose.yml.

Why this file exists, in one paragraph. The 2026-09-12 meeting asked for
terminal access rather than a wall of custom tools. The harness could not give
it: `agent_under_test.py` launches the gateway as a host subprocess, so the
agent, the gateway and the world all live in one process on the host with the
grading zone one `..` away on the same filesystem. A shell there is a hole -
`cat ../grading/manifest.yaml` and ground rule 1 is gone, along with the meaning
of every Pass@1 number measured afterwards. So containment has to come from the
container, and the terminal has to come second. That ordering is the whole
design: this module puts the world, the gateway and every tool the agent runs
inside the containment boundary first, and only then is bash safe to hand over.

## What runs where

The subject process itself stays on the host, and it has to: it is a model CLI
and it needs to reach an inference API, while both compose networks are
`internal: true` and have no egress by construction. What moves into the
container is everything the subject can *touch* - the world, the gateway, the
audit log, and the shell. The agent loop is outside; the workspace is inside.

    host                                  | container `episode`
    --------------------------------------+------------------------------------
    claude / kimi CLI (needs the API)      | the world at /world  (bind mount)
      |                                    | the gateway  (container_shim serve)
      | MCP over stdio, via `docker exec`  | the audit log at /audit (bind mount)
      v                                    | a DE's toolbelt: bash, python, git,
    <rollout>/world   <- bind mounted      | dbt, psql/pg_dump, aws, curl, jq,
    <rollout>/audit   <- bind mounted      | sqlfluff, csvkit, less, vim, nano,
    <rollout>/grading <- NOT mounted       | make, unzip, tree, file, ps, nc,
                                           | dig, ping, ssh/sftp, openssl
                                           |
                                           | NO /grading. NO egress. No host fs.

Until 2026-09-14 the right-hand column read "bash, git, dbt, psql, python" while the
image installed `git` and `openssl` and nothing else - `psql` was never in it. The
list above is the ledger in gateway/docker/TOOLBELT.md, and it is not maintained by
hand: gateway/docker/toolbelt_check.py executes every one of those binaries inside
the built image, gateway/docker/toolbelt_acceptance.py drives them against the live
backends, and gateway/tests/test_image_parity.py fails if a binary this diagram
names does not run. A comment describing a box we do not ship is exactly the class
of defect that keeps costing this project rounds.

The grading zone is a sibling directory of the world root on the host. A bind
mount exposes one directory and not its parent, so there is no path from inside
the container to the zone at all - not a denied path, an absent one. That is
what makes the shell safe, and it is measured per rollout rather than asserted
from this comment (see `containment_probe`).

## Why stdio over `docker exec` and not a port

`published_host_ports: {}` is a pass condition of the containment evidence, and
a port published to the host would be a second way into the episode zone. stdio
needs no port. It also keeps the gateway to exactly one process, which is not
optional: the audit log asserts a contiguous sequence and holds an exclusive
writer lock, so a second gateway on the same audit directory fails to start.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCKER_DIR = REPO / "gateway" / "docker"
COMPOSE_BASE = DOCKER_DIR / "compose.yml"
COMPOSE_OVERLAY = DOCKER_DIR / "compose.episode.yml"
SHIM = Path(__file__).resolve().parent / "container_shim.py"

DEFAULT_IMAGE = "alkera-episode:head"
SHIM_IN_CONTAINER = "/opt/episode/shim.py"
CONFIG_IN_CONTAINER = "/opt/episode/gateway.json"
PROBES_IN_CONTAINER = "/opt/episode/probes.json"
WORLD_IN_CONTAINER = "/world"
WORKSPACE_IN_CONTAINER = "/workspace"
AUDIT_IN_CONTAINER = "/audit"

# Baked into the image and not needed to run an episode: the compose/containment
# scripts and the gateway's own test suite. They are removed from the episode
# container because the agent has a shell and they are exactly the kind of
# machinery it must not be able to read. What remains is swept and recorded
# rather than assumed clean - see MARKER_NAMES.
STRIP_PATHS = ("/opt/stack", "/opt/gateway/tests")

# Filename-shaped things that must not be readable from inside the episode. Kept
# in step with blindness.MARKERS; passed to the probe as data so no marker string
# is ever written into a file that lives inside the container.
MARKER_NAMES = (
    "SEED_SPEC.md", "expected.duckdb", "manifest.yaml", "grade.py",
    "solution.sh", "run_baselines.py", "fixture_contract.py",
    "make_fixture_world.py", "task.toml", "test_integration.py",
    "world.duckdb", "3_track.md", "agent.md",
)

# Where the probe looks for the grading zone, phrased as paths rather than as a
# concept, because the claim under test is "no path leads there".
FORBIDDEN_DIRS = ("/grading", "/world/../grading", "/grading-zone", "/tests")


class ContainerError(Exception):
    """Could not stand up or verify the episode container. Never the agent's fault."""


def _run(argv: list[str], *, check: bool = True, timeout: int = 300,
         stdin_text: str | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
        input=stdin_text, timeout=timeout)
    if check and result.returncode != 0:
        raise ContainerError(
            f"command failed ({result.returncode}): {' '.join(argv[:6])}...\n"
            f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}")
    return result


def docker_available() -> tuple[bool, str]:
    try:
        r = _run(["docker", "version", "--format", "{{.Server.Version}}"],
                 check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return (r.returncode == 0), (r.stdout.strip() or r.stderr.strip())


def container_world(world: dict) -> dict:
    """Translate the host world layout onto the container's /world mount.

    The grading zone is deliberately dropped: it is not mounted, so there is no
    container path for it and nothing downstream may invent one.
    """
    root = Path(world["world_root"]).resolve()
    out = {}
    for key, value in world.items():
        if key == "grading" or not isinstance(value, str):
            out[key] = value
            continue
        try:
            rel = Path(value).resolve().relative_to(root)
        except (ValueError, OSError):
            out[key] = value
            continue
        out[key] = (WORLD_IN_CONTAINER if rel == Path(".")
                    else f"{WORLD_IN_CONTAINER}/{rel.as_posix()}")
    out["world_root"] = WORLD_IN_CONTAINER
    return out


def container_gateway_config(world: dict, episode_id: str, virtual_time: str) -> dict:
    """The gateway config as the container sees it.

    Note what is not here, exactly as on the host: any path under the grading
    zone. The difference is that on the host the gateway merely does not name
    it, and here the path does not exist.
    """
    inside = container_world(world)
    return {
        # the second mount of the same host directory, at the 9.2.1 path
        "workspace": WORKSPACE_IN_CONTAINER,
        "warehouse": inside["warehouse"],
        "scheduler": inside["scheduler"],
        "dashboards": inside["dashboards"],
        "profiles_dir": inside["profiles_dir"],
        "world_root": inside["world_root"],
        "audit_dir": AUDIT_IN_CONTAINER,
        "episode_id": episode_id,
        "initial_world_fp": None,
        "virtual_time": (virtual_time or world.get("virtual_time")
                         or "1970-01-01T00:00:00Z"),
    }


# --------------------------------------------------------------- the probe
# Fed to `python -` on stdin so it is never written to the container's disk and
# never appears in a command line the subject could read. Its parameters arrive
# as one JSON argument. It answers one question - can anything in this container
# see outside it - and it answers by trying.
CONTAINMENT_PROBE = r"""
import json, os, socket, sys, stat

spec = json.loads(sys.argv[1])
out = {}

# 1. what is mounted, from the kernel rather than from the compose file
mounts = []
try:
    for line in open("/proc/self/mountinfo"):
        f = line.split()
        sep = f.index("-")
        mounts.append({"root": f[3], "target": f[4], "options": f[5],
                       "fstype": f[sep + 1], "source": f[sep + 2]})
except OSError as exc:
    mounts = [{"error": str(exc)}]
out["mounts"] = [m for m in mounts
                 if m.get("fstype") not in ("proc", "sysfs", "cgroup", "cgroup2",
                                            "devpts", "mqueue", "tmpfs", "shm")]
# A bind mount publishes the HOST path it came from in mountinfo's root field,
# and that is readable by anyone in the container. It is disclosure, not access -
# every attempt to follow it fails, and `host_paths_present` below is the check
# that says so - but a mount root that spelled out the source repository would
# hand the agent the one thing it must not have. Recorded, and gated.
out["mount_roots"] = sorted({m["root"] for m in out["mounts"] if m.get("root")})
out["mount_roots_naming_repo"] = [
    r for r in out["mount_roots"]
    if any(n and n.lower() in r.lower() for n in spec["repo_path_leaf_forms"])]

# 2. the zone. Not "is it denied" - is there any path at all.
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

# 3. the world mount's parent. `/world/..` must be the container root, not the
#    host directory the world was built in.
try:
    out["world_parent_listing"] = sorted(os.listdir("/world/.."))
except OSError as exc:
    out["world_parent_listing"] = ["ERROR %s" % exc]
out["root_listing"] = sorted(os.listdir("/"))
out["host_paths_present"] = [p for p in spec["host_paths"] if os.path.exists(p)]

# 4. egress, by trying, from inside this container's own network namespace
ifaces = []
try:
    for line in open("/proc/net/dev").read().splitlines()[2:]:
        name = line.split(":")[0].strip()
        if name:
            ifaces.append(name)
except OSError:
    pass
routes = []
try:
    for line in open("/proc/net/route").read().splitlines()[1:]:
        f = line.split()
        if len(f) > 2:
            routes.append({"iface": f[0],
                           "dest": ".".join(str(int(f[1][i:i+2], 16)) for i in (6, 4, 2, 0))})
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
out["network"] = {"interfaces": ifaces, "routes": routes,
                  "egress_tcp": tcp, "egress_dns": dns,
                  "default_route": any(r["dest"] == "0.0.0.0" for r in routes)}
out["egress_blocked"] = (all(r["result"] != "CONNECTED" for r in tcp)
                         and all(r["result"] != "RESOLVED" for r in dns))

# 5. sweep the filesystem the shell can read for anything marker-shaped, and for
#    the host repository path written inside a file. Bounded, and it reports what
#    it found rather than asserting there is nothing to find.
hits, scanned, truncated = [], 0, False
needles = [n.lower() for n in spec["repo_path_forms"]]
for base in spec["sweep_roots"]:
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__",
                                                        "node_modules", "site-packages")]
        for name in filenames:
            scanned += 1
            if scanned > spec["sweep_limit"]:
                truncated = True
                break
            path = os.path.join(dirpath, name)
            if name in spec["marker_names"]:
                hits.append({"kind": "marker_filename", "path": path, "name": name})
                continue
            try:
                if os.path.getsize(path) > 2_000_000 or not name.endswith(
                        (".py", ".md", ".txt", ".yml", ".yaml", ".json", ".toml", ".sql", ".cfg")):
                    continue
                blob = open(path, "r", encoding="utf-8", errors="replace").read().lower()
            except OSError:
                continue
            for needle in needles:
                if needle and needle in blob:
                    hits.append({"kind": "repo_path_in_file", "path": path})
                    break
            for marker in spec["marker_names"]:
                if marker.lower() in blob:
                    hits.append({"kind": "marker_in_file", "path": path, "name": marker})
                    break
        if truncated:
            break
    if truncated:
        break
out["marker_sweep"] = {"files_scanned": scanned, "truncated": truncated, "hits": hits}

# 6. who the shell is, and what it inherits
out["identity"] = {"uid": os.getuid(), "gid": os.getgid(), "cwd": os.getcwd()}
out["env_keys"] = sorted(os.environ)
try:
    out["world_writable"] = bool(os.access("/world", os.W_OK))
    out["audit_writable"] = bool(os.access("/audit", os.W_OK))
except OSError:
    pass

out["contained"] = bool(
    out["egress_blocked"]
    and not out["network"]["default_route"]
    and not any(z["exists"] for z in out["forbidden_dirs"])
    and all(r["result"] != "READ" for r in out["forbidden_reads"])
    and not out["host_paths_present"]
    and not out["mount_roots_naming_repo"]
    and not [h for h in out["marker_sweep"]["hits"] if h["kind"] == "marker_filename"]
)
print(json.dumps(out))
"""


@dataclass
class ContainerEpisode:
    project: str
    world_root: Path
    audit_dir: Path
    image: str = DEFAULT_IMAGE
    container_id: str = ""
    image_digest: str = ""
    services: list = field(default_factory=list)
    _up: bool = False

    # ------------------------------------------------------------- lifecycle
    def _compose(self, *args, check: bool = True, timeout: int = 600):
        env = {
            **os.environ,
            "ALKERA_EPISODE_WORLD": Path(self.world_root).resolve().as_posix(),
            "ALKERA_EPISODE_AUDIT": Path(self.audit_dir).resolve().as_posix(),
            "ALKERA_EPISODE_IMAGE": self.image,
        }
        argv = ["docker", "compose", "-p", self.project,
                "-f", str(COMPOSE_BASE), "-f", str(COMPOSE_OVERLAY), *args]
        result = subprocess.run(argv, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", env=env,
                                timeout=timeout)
        if check and result.returncode != 0:
            raise ContainerError(
                f"docker compose {' '.join(args[:3])} failed ({result.returncode})\n"
                f"stdout: {result.stdout[-3000:]}\nstderr: {result.stderr[-3000:]}")
        return result

    def up(self) -> str:
        Path(self.world_root).mkdir(parents=True, exist_ok=True)
        Path(self.audit_dir).mkdir(parents=True, exist_ok=True)
        self._up = True
        self._compose("up", "-d", "--wait", timeout=900)
        ids = self._compose("ps", "-q").stdout.split()
        if not ids:
            raise ContainerError("compose reported no containers after `up --wait`")
        specs = [json.loads(_run(["docker", "inspect", cid]).stdout)[0] for cid in ids]
        self.services = sorted(
            s["Config"]["Labels"]["com.docker.compose.service"] for s in specs)
        episode = [s for s in specs
                   if s["Config"]["Labels"]["com.docker.compose.service"] == "episode"]
        if not episode:
            raise ContainerError(f"no `episode` container in {self.services}")
        self.container_id = episode[0]["Id"]
        self.image_digest = episode[0]["Image"]
        return self.container_id

    def down(self) -> None:
        if not self._up:
            return
        self._compose("down", "-v", "-t", "2", "--remove-orphans",
                      check=False, timeout=300)
        self._up = False

    # ------------------------------------------------------------- exec
    def exec_(self, argv: list[str], *, check: bool = True, timeout: int = 300,
              stdin_text: str | None = None) -> subprocess.CompletedProcess:
        flags = ["-i"] if stdin_text is not None else []
        return _run(["docker", "exec", *flags, self.container_id, *argv],
                    check=check, timeout=timeout, stdin_text=stdin_text)

    def exec_json(self, argv: list[str], *, timeout: int = 300,
                  stdin_text: str | None = None) -> dict:
        result = self.exec_(argv, timeout=timeout, stdin_text=stdin_text)
        lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
        if not lines:
            raise ContainerError(f"no output from {argv[:4]}: {result.stderr[-2000:]}")
        try:
            return json.loads(lines[-1])
        except ValueError as exc:
            raise ContainerError(
                f"non-json from {argv[:4]}: {lines[-1][:400]}") from exc

    # ------------------------------------------------------------- prepare
    def prepare(self, gateway_config: dict, probes: list[dict],
                scratch: Path) -> None:
        """Put the shim and the episode's config in, and strip what must not be read."""
        removed = self.exec_(
            ["sh", "-c",
             "for p in " + " ".join(STRIP_PATHS) +
             "; do if [ -e \"$p\" ]; then rm -rf \"$p\" && echo \"$p\"; fi; done"],
            check=False)
        self.stripped = [p for p in removed.stdout.split() if p]
        self.exec_(["mkdir", "-p", "/opt/episode"])
        _run(["docker", "cp", str(SHIM), f"{self.container_id}:{SHIM_IN_CONTAINER}"])
        scratch.mkdir(parents=True, exist_ok=True)
        cfg = scratch / "container_gateway.json"
        cfg.write_text(json.dumps(gateway_config, indent=2), encoding="utf-8")
        pr = scratch / "container_probes.json"
        pr.write_text(json.dumps(probes), encoding="utf-8")
        _run(["docker", "cp", str(cfg), f"{self.container_id}:{CONFIG_IN_CONTAINER}"])
        _run(["docker", "cp", str(pr), f"{self.container_id}:{PROBES_IN_CONTAINER}"])

    # ------------------------------------------------------------- evidence
    def containment_probe(self, *, forbidden_files: list[str],
                          host_paths: list[str], repo_root: Path) -> dict:
        spec = {
            "forbidden_dirs": list(FORBIDDEN_DIRS),
            "forbidden_files": forbidden_files,
            "host_paths": host_paths,
            "repo_path_forms": [str(repo_root), str(repo_root).replace("\\", "/")],
            # the drive letter is gone from a Docker Desktop mount root, so the
            # form that would actually appear there is the drive-less one
            "repo_path_leaf_forms": [
                str(repo_root).replace("\\", "/").split(":", 1)[-1]],
            "marker_names": list(MARKER_NAMES),
            "sweep_roots": ["/opt", "/srv", "/root", "/home", "/etc/alkera", "/usr/local/lib/python3.11/site-packages/alkera_gateway"],
            "sweep_limit": 40000,
        }
        evidence = self.exec_json(["python", "-", json.dumps(spec)],
                                  stdin_text=CONTAINMENT_PROBE, timeout=600)
        evidence["probe_source"] = "stdin to `python -`; never written to the container disk"
        evidence["stripped_paths"] = getattr(self, "stripped", [])
        evidence["image"] = self.image
        evidence["image_digest"] = self.image_digest
        evidence["services_live"] = self.services
        evidence["container_id"] = self.container_id[:12]
        return evidence

    def toolbelt(self) -> dict:
        return self.exec_json(["python", SHIM_IN_CONTAINER, "tools"])

    def probe_surfaces(self) -> dict:
        return self.exec_json(
            ["python", SHIM_IN_CONTAINER, "probe",
             "--config", CONFIG_IN_CONTAINER, "--probes", PROBES_IN_CONTAINER,
             "--world", WORLD_IN_CONTAINER],
            timeout=900)

    # ------------------------------------------------------------- transport
    def mcp_argv(self) -> list[str]:
        """The subject's one channel: the gateway, over stdio, inside the container."""
        return ["docker", "exec", "-i",
                "-e", "PYTHONUTF8=1", "-e", "PYTHONUNBUFFERED=1",
                "-w", "/workspace", self.container_id,
                "python", SHIM_IN_CONTAINER, "serve", "--config", CONFIG_IN_CONTAINER]


def prune_projects(prefix: str) -> list[str]:
    """Tear down leftover episode projects. Used by the checker, not by rollouts."""
    result = _run(["docker", "ps", "-a", "--format", "{{.Label \"com.docker.compose.project\"}}"],
                  check=False)
    projects = sorted({p for p in result.stdout.split() if p.startswith(prefix)})
    for project in projects:
        subprocess.run(["docker", "compose", "-p", project, "-f", str(COMPOSE_BASE),
                        "-f", str(COMPOSE_OVERLAY), "down", "-v", "-t", "2",
                        "--remove-orphans"],
                       capture_output=True, text=True,
                       env={**os.environ, "ALKERA_EPISODE_WORLD": "/tmp",
                            "ALKERA_EPISODE_AUDIT": "/tmp",
                            "ALKERA_EPISODE_IMAGE": DEFAULT_IMAGE})
    return projects
