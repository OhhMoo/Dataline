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
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

import episode          # WORLD_PLANE_KEYS: one list, two topologies

REPO = Path(__file__).resolve().parents[2]
DOCKER_DIR = REPO / "gateway" / "docker"
COMPOSE_BASE = DOCKER_DIR / "compose.yml"
COMPOSE_OVERLAY = DOCKER_DIR / "compose.episode.yml"
SHIM = Path(__file__).resolve().parent / "container_shim.py"

DEFAULT_IMAGE = "alkera-episode:head"
SHIM_IN_CONTAINER = "/opt/episode/shim.py"
CONFIG_IN_CONTAINER = "/opt/episode/gateway.json"
PROBES_IN_CONTAINER = "/opt/episode/probes.json"
# Where a host-built world's dbt profiles land once their warehouse paths have
# been re-rooted onto /world (see reroot_profiles). Beside the config and the
# shim, not inside the world: the world tree is the subject's and the harness
# does not edit it behind the subject's back.
PROFILES_IN_CONTAINER = "/opt/episode/dbt"
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
    # A second warehouse's DuckDB file is a path NESTED in a list, which the
    # loop above cannot see. Re-rooted by the same rule, so the gateway inside
    # the container opens /world/var/warehouse/<name>.duckdb and not the host
    # path - which does not exist in there, and whose spelling would hand the
    # agent the host directory the world was built in.
    if world.get("warehouses"):
        out["warehouses"] = episode.reroot_warehouses(
            world["warehouses"], root, WORLD_IN_CONTAINER)
    return out


def reroot_profiles(text: str, host_root, container_root: str = WORLD_IN_CONTAINER):
    """A dbt profiles.yml with every DuckDB `path` under the host world root
    moved under the container's, and the list of outputs that moved.

    A world built on the host writes its profiles with ABSOLUTE host paths
    (world/world/executor.py:write_profiles, GT007's build_world.py), and every
    other world path is re-rooted for the container by container_world - but
    the profiles file is a file, not a config key, so its paths went in
    untouched. Measured 2026-09-15 (tasks/harness/evidence/dual-container-
    check.json, first run): `dbt run` inside the container failed with
    `Binder Error: Catalog "\\Users\\...\\target" does not exist!` - dbt-duckdb
    opened a stray file at the Windows path and the model bodies bound against
    nothing. Compile passes (it never binds), so the surface probe's
    `dbt_compile` reads live while `dbt_run` is broken; a rollout would file
    that as the agent's failure. An output whose path is not under the host
    root (a /tmp CI file, a remote type with no path) is left alone.
    """
    import yaml

    profiles = yaml.safe_load(text) or {}
    root = Path(host_root).resolve()
    moved = []
    for profile_name, profile in profiles.items():
        outputs = profile.get("outputs") if isinstance(profile, dict) else None
        if not isinstance(outputs, dict):
            continue
        for output_name, output in outputs.items():
            path = output.get("path") if isinstance(output, dict) else None
            if not isinstance(path, str) or not path:
                continue
            try:
                rel = Path(path).resolve().relative_to(root)
            except (ValueError, OSError):
                continue
            output["path"] = f"{container_root}/{rel.as_posix()}"
            moved.append(f"{profile_name}.{output_name}")
    return yaml.safe_dump(profiles, sort_keys=False), moved


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
        # The workflow / observability / ingest planes, re-rooted into the
        # container by container_world() like every other world path. Without
        # them the gateway falls back to GatewayConfig's ABSOLUTE defaults
        # (/var/workflow, /var/obs/observability.yml, /var/ingest/state.json),
        # which are outside the world root, and seven whole surfaces answer
        # "this world has no ..." however richly the world was seeded. See
        # episode.WORLD_PLANE_KEYS for the full reasoning; this run was measured
        # in a half-absent world for exactly that reason.
        **{k: inside[k] for k in episode.WORLD_PLANE_KEYS if inside.get(k)},
        "profiles_dir": inside["profiles_dir"],
        "world_root": inside["world_root"],
        "audit_dir": AUDIT_IN_CONTAINER,
        "episode_id": episode_id,
        "initial_world_fp": None,
        "virtual_time": (virtual_time or world.get("virtual_time")
                         or "1970-01-01T00:00:00Z"),
        # A second warehouse and, when one of them is remote, the fingerprint
        # the builder computed for it. Same pass-through as the plane keys
        # (episode.WORLD_WAREHOUSE_KEYS); paths are the re-rooted ones.
        **episode._warehouse_keys(inside),
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
        gateway_config = dict(gateway_config)
        self.profiles_rewritten = self._stage_profiles(gateway_config, scratch)
        cfg = scratch / "container_gateway.json"
        cfg.write_text(json.dumps(gateway_config, indent=2), encoding="utf-8")
        _run(["docker", "cp", str(cfg), f"{self.container_id}:{CONFIG_IN_CONTAINER}"])
        self.set_probes(probes, scratch)

    def _stage_profiles(self, gateway_config: dict, scratch: Path) -> list[str]:
        """Re-root the world's dbt profiles for the container (reroot_profiles).

        The config's `profiles_dir` is the re-rooted `/world/<rel>`; the file
        it names lives at `<host world root>/<rel>/profiles.yml`. When any of
        its outputs points into the host world tree, the rewritten file is
        copied to PROFILES_IN_CONTAINER and the config is pointed there, so
        dbt inside the container opens `/world/var/warehouse/<name>.duckdb`
        and not a Windows path. A world whose profiles already carry container
        paths (or none under the root) is left exactly as it was: same
        profiles_dir, no copy, an empty list back.
        """
        profiles_dir = str(gateway_config.get("profiles_dir") or "")
        prefix = WORLD_IN_CONTAINER + "/"
        if not profiles_dir.startswith(prefix):
            return []
        host_file = Path(self.world_root) / profiles_dir[len(prefix):] / "profiles.yml"
        if not host_file.is_file():
            return []
        rewritten, moved = reroot_profiles(
            host_file.read_text(encoding="utf-8"), self.world_root)
        if not moved:
            return []
        staged = scratch / "profiles.yml"
        staged.write_text(rewritten, encoding="utf-8")
        self.exec_(["mkdir", "-p", PROFILES_IN_CONTAINER])
        _run(["docker", "cp", str(staged),
              f"{self.container_id}:{PROFILES_IN_CONTAINER}/profiles.yml"])
        gateway_config["profiles_dir"] = PROFILES_IN_CONTAINER
        return moved

    def set_probes(self, probes: list[dict], scratch: Path) -> None:
        """Replace the probe list inside the container.

        Called a second time once `toolbelt()` is known - and the toolbelt can
        only be read after the shim is in, so the sweep over undeclared tools
        cannot be assembled at prepare() time. Rewriting one json file is
        cheaper than reordering the containment-first sequence above it.
        """
        scratch.mkdir(parents=True, exist_ok=True)
        pr = scratch / "container_probes.json"
        pr.write_text(json.dumps(probes), encoding="utf-8")
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
        evidence["dbt_profiles_rerooted"] = getattr(self, "profiles_rewritten", [])
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


# ==========================================================================
# NATIVE MODE. `--driver native --topology container`.
# ==========================================================================
# Everything above this line is the MCP topology and is unchanged: the gateway
# is the subject's only channel, so the whole world can be mounted at /world
# and the tool surface is the boundary. Native mode has no gateway and no tool
# list - bash IS the interface - so the boundary has to be the mount set, and
# that is the whole of what follows.
#
# WHAT THE HOST TOPOLOGY COULD NOT DO, MEASURED
# ---------------------------------------------
# GT001, `--driver native`, K=5, run-id native-20260916-k5: five of five
# subjects did the work and THREE quarantined.
#
#   r0  `cp var/warehouse/warehouse.duckdb warehouse.duckdb.pre-DATA-4471.bak`
#   r2  `mkdir -p var/warehouse/backup && cp ... backup/warehouse.pre-...duckdb`
#       -> a new file in the world tree that no row can explain, so
#          `out_of_band_world_change`, HARD. Both are BACKUPS TAKEN BEFORE A
#          DESTRUCTIVE STATEMENT, which is the behaviour the benchmark wants.
#   r3  `ls -la <rollout dir>` -> the planted canary filename came back in a
#       tool_result, so `canary_leak`, HARD.
#
# Under the host topology the subject's bash is a host process, so all three
# are reachable and no shim audits them. Under this topology none of them is:
# the warehouse is mounted as a FILE, so a `cp` beside it writes the
# container's own filesystem, and the rollout directory has no path here at
# all. That is measured per rollout by `containment_probe`, which is
# containment P7.
#
# WHERE THE SUBJECT'S BASH RUNS, AND WHAT THAT COSTS
# --------------------------------------------------
# The MCP topology keeps the model process on the host and moves the GATEWAY
# into the container (`container_shim serve` over `docker exec -i`), because
# the gateway is the only channel and there is nothing else to move. Native
# mode has no such chokepoint: the channel is the shell itself, so the SHELL is
# what has to move. The model process still has to stay on the host - it needs
# an inference API and both compose networks are `internal: true`.
#
# So the subject is given a `bash` that is not a shell: `write_bash_wrapper`
# materialises a launcher whose whole body is one `docker exec` into this
# rollout's episode container. `claude --print --tools Bash` runs it as its
# shell, the command text it recorded is the command text that ran, and the
# transcript is unchanged. Three costs, stated because they are real:
#
#   1. NO STATE BETWEEN COMMANDS. Each call is its own `docker exec`, so `cd`,
#      `export` and a background job do not survive to the next one. Every
#      command starts in /workspace. A persistent shell would need a state file
#      inside the container, which is one more thing the subject can read, and
#      the shape of that trade is why this one is refused rather than hidden.
#   2. THE WRAPPER IS FAIL-CLOSED. It accepts `-c <command>` (and the `-lc`
#      spelling) and nothing else. A wrapper that quietly fell back to running
#      an argv it did not understand ON THE HOST would reopen every hole this
#      topology closes, and it would do it silently.
#   3. The host path the model thinks it is in is not the container's. The
#      prompt names `/workspace` because that is true inside; `build_prompt`
#      gets the container path, not the host one.

#: Where the PATH-front shims (`duckdb`, `dbt`, `python`, `git`) are
#: materialised INSIDE the episode container. They are the subject's own
#: audited binaries, so they live on the box the subject is on - not on the
#: host, where `native_stack._install_shims` puts them for the host topology.
SHIMS_IN_CONTAINER = "/opt/alkera/shims"
#: The re-rooted dbt profile, at the path `compose.native.yml` already names
#: through `DBT_PROFILES_DIR=/root/.dbt`. Copied, never mounted: an edit to it
#: must stay inside the box like every other write outside the workspace.
DBT_DIR_IN_CONTAINER = "/root/.dbt"
HOME_IN_CONTAINER = "/root"
#: The world-relative directory `world/world/fingerprint.py` hashes warehouses
#: by, and therefore the directory `warehouse_mounts` globs.
WAREHOUSE_REL_DIR = "var/warehouse"
NATIVE_OVERLAY = DOCKER_DIR / "compose.native.yml"
#: The per-rollout overlay's name inside the rollout directory. Kept, not
#: deleted: it is the only record of which host files this episode's container
#: could see, and a reviewer reading a rollout should not have to re-derive it.
MOUNTS_OVERLAY_NAME = "compose.native-mounts.yml"
#: MinIO's port, published on loopback beside the nine so the surface gate's
#: signed bucket listing has somewhere to go.
MINIO_PORT = 9000


def warehouse_mounts(world: dict) -> list[str]:
    """Every warehouse FILE this world declares, world-relative, POSIX.

    The same three sources `blindness.declared_warehouses` reads, and for the
    same reason: the manifest's `warehouse`, any `warehouses` spec that names a
    path, and `var/warehouse/*.duckdb`, which is the rule
    `world/world/fingerprint.py` hashes by - so a file the world built but did
    not publish in its manifest is still mounted and still writable, rather
    than turning into a mysteriously read-only file half way through an
    episode.

    A declared warehouse OUTSIDE the world root is skipped: this topology
    mounts world files, and a path somewhere else is a different question that
    no task asks yet.
    """
    root = Path(world["world_root"]).resolve()
    found: list[str] = []
    candidates = [world.get("warehouse")]
    candidates += [spec.get("path") for spec in (world.get("warehouses") or [])
                   if isinstance(spec, dict)]
    candidates += sorted(str(p) for p in (root / "var" / "warehouse").glob("*.duckdb"))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            rel = Path(candidate).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            continue
        if rel not in found and (root / rel).is_file():
            found.append(rel)
    return sorted(found)


def image_path(image: str) -> str:
    """The PATH baked into `image`, read off the image rather than assumed.

    The shims have to go in FRONT of it and the rest has to survive: the
    episode image appends `/opt/toolbelt/bin`, where `aws`, `sqlfluff` and
    `csvkit` live, and a PATH written from memory here would silently take a
    third of the toolbelt away from the agent.
    """
    result = _run(["docker", "image", "inspect", image, "--format",
                   "{{range .Config.Env}}{{println .}}{{end}}"])
    for line in result.stdout.splitlines():
        if line.startswith("PATH="):
            return line[len("PATH="):].strip()
    raise ContainerError(f"image {image!r} declares no PATH")


# ------------------------------------------------------------ the bash wrapper
#: The wrapper body. A module and not an `sh` one-liner because it has to run
#: on the HOST, which is Windows here, and because "refuse what you do not
#: understand" is more than one line.
BASH_WRAPPER = '''#!/usr/bin/env python3
"""The subject\'s `bash`, in the episode container.

Generated per rollout by `tasks/harness/container_episode.write_bash_wrapper`.
`claude --print --tools Bash` (and the scripted acceptance subject) runs this
instead of a shell: every command is executed by `docker exec` inside this
rollout\'s episode container, with the same command text, and the transcript
records what it always recorded.

It accepts `-c COMMAND` and the `-lc` spelling, and REFUSES everything else.
The refusal is the point: a wrapper that fell through to a host shell for an
argv it did not recognise would put the subject back on the host filesystem,
which is the one thing this topology exists to prevent, and it would do it
without saying so.
"""
import json
import os
import subprocess
import sys

CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "container_bash.json")
#: Flags that only ever change how the shell greets the caller. Accepted and
#: dropped: `docker exec ... bash -c` is the one form this wrapper runs.
IGNORED_FLAGS = ("-l", "--login", "-i", "-s")


def parse(argv):
    """(command, None) or (None, why-this-argv-is-refused)."""
    rest = list(argv)
    command = None
    while rest:
        item = rest.pop(0)
        if item in IGNORED_FLAGS:
            continue
        combined = (item.startswith("-") and not item.startswith("--")
                    and item.endswith("c")
                    and set(item[1:-1]) <= set("lis"))
        if item == "-c" or combined:
            if not rest:
                return None, "%s was given no command" % item
            command = rest.pop(0)
            continue
        return None, "unsupported argument %r" % item
    if command is None:
        return None, "no -c COMMAND; this wrapper has no interactive mode"
    if rest:
        return None, "unexpected trailing arguments %r" % rest
    return command, None


def main(argv):
    with open(CONFIG, encoding="utf-8") as stream:
        config = json.load(stream)
    command, why = parse(argv)
    if command is None:
        sys.stderr.write(
            "container bash wrapper: %s.\\n"
            "This shell only runs `-c COMMAND` inside the episode container; "
            "it deliberately has no host fallback.\\n" % why)
        return 2
    exec_argv = ["docker", "exec", "-i", "-w", config["workdir"]]
    for name, value in sorted(config["env"].items()):
        exec_argv += ["-e", "%s=%s" % (name, value)]
    exec_argv += [config["container"], "bash", "-c", command]
    return subprocess.run(exec_argv).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''

LAUNCHER_CMD = """@echo off
"{interpreter}" "{wrapper}" %*
"""
LAUNCHER_SH = """#!/bin/sh
exec "{interpreter}" "{wrapper}" "$@"
"""


def write_bash_wrapper(bin_dir: Path, *, container: str, interpreter: str,
                       workdir: str = WORKSPACE_IN_CONTAINER,
                       env: dict | None = None) -> dict:
    """Materialise the wrapper and the `bash` launcher that fronts it.

    Two files for the launcher, the same pair `alkera_gateway.native.shims
    .install` writes and for the same reason: `bash.cmd` is what cmd.exe and
    PowerShell find through PATHEXT, the extensionless `bash` is what Git Bash
    and every POSIX shell look for. Both exec the one wrapper module, so the
    logic exists once.

    `env` is forwarded to every `docker exec`. The compose file already gives
    the container the endpoint variables; this carries the per-episode values
    only the harness knows - which file the subject edits, which issue key this
    world has - and, on the model path, whatever
    `agent_under_test.native_env` assembled.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "container_bash.py"
    with open(wrapper, "w", encoding="utf-8", newline="") as stream:
        stream.write(BASH_WRAPPER)
    config = bin_dir / "container_bash.json"
    with open(config, "w", encoding="utf-8", newline="") as stream:
        json.dump({"container": container, "workdir": workdir,
                   "env": dict(env or {})}, stream, indent=2, sort_keys=True)
    written = [wrapper, config]
    launcher = bin_dir / "bash"
    with open(launcher, "w", encoding="utf-8", newline="") as stream:
        stream.write(LAUNCHER_SH.format(interpreter=interpreter,
                                        wrapper=wrapper.as_posix()))
    launcher.chmod(launcher.stat().st_mode | 0o111)
    written.append(launcher)
    if os.name == "nt":
        cmd = bin_dir / "bash.cmd"
        with open(cmd, "w", encoding="utf-8", newline="") as stream:
            stream.write(LAUNCHER_CMD.format(interpreter=interpreter,
                                             wrapper=str(wrapper)))
        written.append(cmd)
    return {"bin": str(bin_dir), "wrapper": str(wrapper), "config": str(config),
            "launcher": str(bin_dir / ("bash.cmd" if os.name == "nt" else "bash")),
            "files": [str(p) for p in written]}


# ----------------------------------------------------- the surface gate's relay
#: One TCP connection, shuttled between the harness and a service, by a process
#: inside the stack. Fed to `python -c`, so it is never a file anywhere.
#:
#: It runs in the `native-services` container and dials the service by the same
#: `<name>.internal` alias the EPISODE uses - by alias and not by loopback,
#: because `minio.internal` is a different container, and because a relay that
#: reached the nine a second way would be a second way into them.
GATE_RELAY = """
import socket, sys, threading

sock = socket.create_connection((HOST, PORT), timeout=10)


def upstream():
    try:
        while True:
            chunk = sys.stdin.buffer.read1(65536)
            if not chunk:
                break
            sock.sendall(chunk)
    except Exception:
        pass
    finally:
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass


threading.Thread(target=upstream, daemon=True).start()
try:
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            break
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
finally:
    sock.close()
"""


class ServiceGate:
    """Loopback listeners that relay into the stack over `docker exec`.

    The surface gate (`surface_audit.probe_native`) runs in the HARNESS
    process, on the host, and asks each service for `/health` over HTTP. It
    cannot resolve `jira.internal`, the compose networks are `internal: true`
    so nothing can be published off them, and a probe file copied into the
    episode container is the one thing this topology may not have - the agent
    has a shell in there.

    So each service gets a loopback socket here and every byte is carried into
    the stack by one `docker exec` per connection. The relay is faithful at the
    byte level rather than at the HTTP level on purpose: the `s3` surface check
    is a SIGNED request, and a proxy that rewrote a header or a Host would
    break the signature and report a dead bucket that is not dead.

    One `docker exec` per connection is ~0.3s. The gate opens about one
    connection per surface, once, before the subject starts.
    """

    def __init__(self, container: str, targets: dict, interpreter: str) -> None:
        self.container = container
        #: {name: (alias, port)} as the EPISODE addresses each service
        self.targets = dict(targets)
        self.interpreter = interpreter
        self._servers: list = []
        self._threads: list = []
        self.endpoints: dict = {}

    def start(self) -> dict:
        """Bind one listener per service; return {name: http://127.0.0.1:port}."""
        for name, (host, port) in sorted(self.targets.items()):
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.bind(("127.0.0.1", 0))
            server.listen(8)
            bound = server.getsockname()[1]
            self._servers.append(server)
            thread = threading.Thread(target=self._serve,
                                      args=(server, host, port), daemon=True)
            thread.start()
            self._threads.append(thread)
            self.endpoints[name] = f"http://127.0.0.1:{bound}"
        return dict(self.endpoints)

    def _serve(self, server: socket.socket, host: str, port: int) -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return                      # stop() closed the listener
            threading.Thread(target=self._relay, args=(conn, host, port),
                             daemon=True).start()

    def _relay(self, conn: socket.socket, host: str, port: int) -> None:
        program = (GATE_RELAY.replace("HOST", json.dumps(host))
                   .replace("PORT", str(port)))
        proc = subprocess.Popen(
            ["docker", "exec", "-i", self.container, self.interpreter,
             "-c", program],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)

        def to_stack() -> None:
            try:
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    proc.stdin.write(chunk)
                    proc.stdin.flush()
            except OSError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass

        pump = threading.Thread(target=to_stack, daemon=True)
        pump.start()
        try:
            while True:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                conn.sendall(chunk)
        except OSError:
            pass
        finally:
            for closer in (conn.close, proc.stdout.close):
                try:
                    closer()
                except OSError:
                    pass
            proc.wait(timeout=30)

    def stop(self) -> None:
        for server in self._servers:
            try:
                server.close()
            except OSError:
                pass
        self._servers = []

@dataclass
class NativeContainerEpisode:
    """One native episode's compose project: `up`, `prepare`, `down`.

    The stack is `compose.yml + compose.episode.yml + compose.native.yml` plus
    ONE generated overlay per rollout, written into the rollout directory. The
    generated file exists because three of the things this topology needs
    cannot be spelled statically: the warehouse FILES, whose names and number
    come from the world the rollout built, and the shim PATH, which is the
    image's own PATH with the shim directory in front of it.
    """

    project: str
    world: dict
    audit_dir: Path
    rollout_dir: Path
    image: str = DEFAULT_IMAGE
    container_id: str = ""
    image_digest: str = ""
    services: list = field(default_factory=list)
    warehouses: list = field(default_factory=list)
    services_container: str = ""
    gate: object = None
    _up: bool = False

    # ---------------------------------------------------------------- paths
    @property
    def world_root(self) -> Path:
        return Path(self.world["world_root"]).resolve()

    @property
    def container_dir(self) -> Path:
        return Path(self.rollout_dir) / "container"

    @property
    def overlay(self) -> Path:
        return self.container_dir / MOUNTS_OVERLAY_NAME

    def compose_files(self) -> list[Path]:
        return [COMPOSE_BASE, COMPOSE_OVERLAY, NATIVE_OVERLAY, self.overlay]

    # -------------------------------------------------------------- overlay
    def write_overlay(self) -> Path:
        """The per-rollout overlay: warehouse binds, shim PATH, published ports.

        Written as text rather than through a YAML library so that what a
        reviewer reads in the rollout directory is what compose read, comments
        and all.
        """
        self.warehouses = warehouse_mounts(self.world)
        if not self.warehouses:
            raise ContainerError(
                f"the world at {self.world_root} declares no warehouse file "
                f"under {WAREHOUSE_REL_DIR}/; with nothing to mount the episode "
                f"container would have no warehouse at all")
        path_value = f"{SHIMS_IN_CONTAINER}:{image_path(self.image)}"
        binds = "\n".join(
            f'      - "{(self.world_root / rel).as_posix()}:'
            f'{WORLD_IN_CONTAINER}/{rel}"'
            for rel in self.warehouses)
        body = f"""# Generated per rollout by tasks/harness/container_episode.py. Do not edit:
# it is rewritten on every `up()` and is kept here as the record of which host
# files this episode's container could see.
#
# Two things compose.native.yml cannot say statically:
#
#   1. THE WAREHOUSE FILES. Their names and their number come from the world
#      this rollout built ({len(self.warehouses)} here), read by the same rule
#      world/world/fingerprint.py hashes by. Each is bound as a FILE at its own
#      world-relative path, so `/world/{WAREHOUSE_REL_DIR}` is a directory of
#      the CONTAINER's - and a `cp` beside the warehouse, which is what two of
#      five GT001 native subjects did before deleting rows, lands there instead
#      of in the world.
#   2. THE SHIM PATH. `{SHIMS_IN_CONTAINER}` goes in front of the image's own
#      PATH, which is read off the image so that /opt/toolbelt/bin survives.
#
# NOT here, and it was here for one afternoon: published host ports. Both
# compose networks are `internal: true`, and Docker cannot publish a host port
# off an internal network - the bindings appear in `HostConfig.PortBindings`
# and nothing ever listens. Measured: GT004's first container rollout failed
# its surface gate with `WinError 10061` on all nine services while
# `docker inspect` reported all nine mappings. The harness reaches the services
# through `container_episode.ServiceGate` instead, one `docker exec` per
# connection, and `published_host_ports: {{}}` stays true for every container
# in the project.
services:
  episode:
    environment:
      PATH: "{path_value}"
      ALKERA_SHIM_BIN: "{SHIMS_IN_CONTAINER}"
      ALKERA_SHIM_WORKSPACE: "{WORKSPACE_IN_CONTAINER}"
      ALKERA_WORKSPACE: "{WORKSPACE_IN_CONTAINER}"
    volumes:
{binds}
"""
        self.container_dir.mkdir(parents=True, exist_ok=True)
        with open(self.overlay, "w", encoding="utf-8", newline="") as stream:
            stream.write(body)
        return self.overlay

    # ------------------------------------------------------------- lifecycle
    def _compose(self, *args, check: bool = True, timeout: int = 900):
        env = {
            **os.environ,
            "ALKERA_EPISODE_WORLD": self.world_root.as_posix(),
            "ALKERA_EPISODE_AUDIT": Path(self.audit_dir).resolve().as_posix(),
            "ALKERA_EPISODE_IMAGE": self.image,
        }
        argv = ["docker", "compose", "-p", self.project]
        for path in self.compose_files():
            argv += ["-f", str(path)]
        result = subprocess.run([*argv, *args], capture_output=True, text=True,
                                encoding="utf-8", errors="replace", env=env,
                                timeout=timeout)
        if check and result.returncode != 0:
            raise ContainerError(
                f"docker compose {' '.join(args[:3])} failed "
                f"({result.returncode})\nstdout: {result.stdout[-3000:]}\n"
                f"stderr: {result.stderr[-3000:]}")
        return result

    def up(self) -> str:
        Path(self.audit_dir).mkdir(parents=True, exist_ok=True)
        self.write_overlay()
        self._up = True
        self._compose("up", "-d", "--wait", timeout=1200)
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
        # The relay runs in `native-services`, which is on episode_net and can
        # therefore reach every alias the episode can - including minio, which
        # it dials by name rather than on loopback.
        services = [s for s in specs
                    if s["Config"]["Labels"]["com.docker.compose.service"]
                    == "native-services"]
        if not services:
            raise ContainerError(
                f"no `native-services` container in {self.services}; the surface "
                f"gate has no vantage into the episode network")
        self.services_container = services[0]["Id"]
        return self.container_id

    def down(self) -> None:
        if self.gate is not None:
            self.gate.stop()
            self.gate = None
        if not self._up:
            return
        self._compose("down", "-v", "-t", "2", "--remove-orphans",
                      check=False, timeout=300)
        self._up = False

    def exec_(self, argv: list[str], *, check: bool = True, timeout: int = 300,
              env: dict | None = None,
              stdin_text: str | None = None) -> subprocess.CompletedProcess:
        flags = ["-i"] if stdin_text is not None else []
        for name, value in sorted((env or {}).items()):
            flags += ["-e", f"{name}={value}"]
        return _run(["docker", "exec", *flags, self.container_id, *argv],
                    check=check, timeout=timeout, stdin_text=stdin_text)

    def copy_in(self, source: Path, target: str) -> None:
        """`docker cp` one host file into the container."""
        _run(["docker", "cp", str(source), f"{self.container_id}:{target}"])

    # --------------------------------------------------------------- prepare
    def prepare(self) -> dict:
        """Make the box a data engineer's box, then take the machinery back out.

        Order matters and it is the ordering the MCP topology already uses:
        strip first, provision second. What is stripped is the compose and
        containment machinery baked into the image (`/opt/stack`) and the
        gateway's own test suite - the agent has a shell in here and those are
        exactly the files that would teach it what it is inside of.
        """
        removed = self.exec_(
            ["sh", "-c",
             "for p in " + " ".join(STRIP_PATHS) +
             "; do if [ -e \"$p\" ]; then rm -rf \"$p\" && echo \"$p\"; fi; done"],
            check=False)
        record = {"stripped": [p for p in removed.stdout.split() if p]}
        # The audited binaries, on the box that runs them. Installed with the
        # container's own interpreter (the default), which is what makes the
        # row-writing import work at all.
        shims = self.exec_([_native_probe().CONTAINER_PYTHON, "-m",
                            "alkera_gateway.native.shims.install",
                            "--bin", SHIMS_IN_CONTAINER])
        record["shims"] = sorted(Path(p).name for p in shims.stdout.split()
                                 if p.strip())
        record["home"] = self._copy_home()
        record["dbt_profiles_rerooted"] = self._stage_profiles()
        return record

    def _copy_home(self) -> list[str]:
        """`<world>/home` -> /root, COPIED and not mounted.

        `world/world/native_workspace.py` writes `home/.dbt/profiles.yml` and
        `home/.config/alkera/services.toml` when the seed says `native: true`,
        and a box where `~` is empty is a box that tells the agent it is fake.
        Copied because a mount would put two more world files inside the blast
        radius this topology exists to shrink: an edit to `~/.dbt/profiles.yml`
        must stay in the container, like every other write outside the
        workspace.
        """
        home = self.world_root / "home"
        if not home.is_dir():
            return []
        names = sorted(p.name for p in home.iterdir())
        for name in names:
            self.copy_in(home / name, f"{HOME_IN_CONTAINER}/{name}")
        return names

    def _stage_profiles(self) -> list[str]:
        """The world's dbt profile, re-rooted onto the container's warehouse.

        Same call the MCP topology makes (`reroot_profiles`), same measured
        reason: a world built on the host writes ABSOLUTE host paths into
        `profiles.yml`, dbt-duckdb then opens a stray file at a Windows path,
        and every model body binds against nothing. Here the target is the
        mounted file at `/world/var/warehouse/<name>.duckdb`.

        A world whose `profiles_dir` holds no `profiles.yml` gets nothing, and
        the dbt surface is then dead for the same reason it is dead on the host
        - which is the correct answer, not a gap this method should paper over.
        """
        source = Path(self.world.get("profiles_dir") or "") / "profiles.yml"
        if not source.is_file():
            return []
        rewritten, moved = reroot_profiles(source.read_text(encoding="utf-8"),
                                           self.world_root)
        staged = self.container_dir / "profiles.yml"
        self.container_dir.mkdir(parents=True, exist_ok=True)
        with open(staged, "w", encoding="utf-8", newline="") as stream:
            stream.write(rewritten)
        self.exec_(["mkdir", "-p", DBT_DIR_IN_CONTAINER])
        self.copy_in(staged, f"{DBT_DIR_IN_CONTAINER}/profiles.yml")
        return moved

    # ------------------------------------------------------------- endpoints
    def episode_endpoints(self) -> dict:
        """The addresses the EPISODE uses: the `<name>.internal` defaults.

        This is the map `blindness.network_violations` is judged against, so it
        has to be the one the episode actually dialled - the service side
        records the alias it was served under, not the peer's address, so a
        127.0.0.1 map here would compare two different things.
        """
        import agent_under_test as aut

        return aut.resolve_endpoints(None)

    def gate_endpoints(self) -> dict:
        """The addresses the HARNESS uses: one relayed loopback socket each.

        A second map, and the split is deliberate. The surface gate runs in the
        harness process and cannot resolve `jira.internal`; the episode must
        never be handed a 127.0.0.1 address, because then the destination log
        and the endpoint map would agree about a host the episode never used.

        The sockets are this process's own and each connection is carried into
        the stack by `ServiceGate` - see its docstring for why they are not
        published ports. `minio` is relayed on the same terms: the s3 surface
        check signs its bucket listing, and a byte relay is the only kind of
        forwarding that does not invalidate a signature.
        """
        import agent_under_test as aut

        targets = {name: (f"{name}.internal", port)
                   for name, (_, port) in aut.NATIVE_SERVICES.items()}
        targets["s3"] = ("minio.internal", MINIO_PORT)
        self.gate = ServiceGate(self.services_container, targets,
                                _native_probe().CONTAINER_PYTHON)
        overrides = self.gate.start()
        self.container_dir.mkdir(parents=True, exist_ok=True)
        path = self.container_dir / "gate_endpoints.json"
        with open(path, "w", encoding="utf-8", newline="") as stream:
            json.dump(overrides, stream, indent=2, sort_keys=True)
        return aut.resolve_endpoints(path)

    # -------------------------------------------------------------- evidence
    def containment_probe(self, *, when: str, canaries: list[str],
                          grading: Path, rollout_dir: Path) -> dict:
        """Containment P7 for THIS rollout's container.

        The probe and its spec live in `gateway/docker/native_episode_probe.py`,
        which `gateway/docker/containment.py --native-episode` also runs, so a
        rollout's evidence and the repository's evidence file are the same
        measurement rather than two that resemble each other.
        """
        import agent_under_test as aut

        endpoints = self.episode_endpoints()
        spec = _native_probe().spec(
            warehouses=self.warehouses,
            services={name: endpoints[name] for name in sorted(aut.NATIVE_SERVICES)},
            forbidden_files=[
                Path(grading, "manifest.yaml").as_posix(),
                Path(grading, "expected.duckdb").as_posix()],
            host_paths=[self.world_root.as_posix(),
                        Path(grading).resolve().as_posix(),
                        Path(rollout_dir).resolve().as_posix(),
                        REPO.as_posix(), "/c", "/host_mnt", "/mnt/c"],
            canaries=canaries,
            repo_root=REPO)
        evidence = _native_probe().run(self.container_id, spec)
        evidence["when"] = when
        evidence["probe_source"] = ("stdin to `python -`; never written to the "
                                    "container disk")
        evidence["image"] = self.image
        evidence["image_digest"] = self.image_digest
        evidence["container_id"] = self.container_id[:12]
        evidence["services_live"] = self.services
        return evidence

    def record(self) -> dict:
        """What the rollout record carries about this stack."""
        return {
            "topology": "container",
            "project": self.project,
            "image": self.image,
            "image_digest": self.image_digest,
            "container_id": self.container_id[:12],
            "services": self.services,
            "compose_files": [str(p) for p in self.compose_files()],
            "warehouse_mounts": list(self.warehouses),
            "shims_in_container": SHIMS_IN_CONTAINER,
        }


def _native_probe():
    """`gateway/docker/native_episode_probe`, imported once, on demand.

    By path rather than as a package, because `gateway/docker` is a directory
    of scripts and not an importable distribution; and lazily, so importing
    this module on a host with no docker checkout layout still works for the
    MCP topology above.
    """
    if str(DOCKER_DIR) not in sys.path:
        sys.path.insert(0, str(DOCKER_DIR))
    import native_episode_probe

    return native_episode_probe


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


# ------------------------------------------------------------ the P7 vantage
def _native_cli(argv: list[str] | None = None) -> int:
    """`native-up` / `native-down`: a native episode stack, kept up.

    It exists for one caller: `gateway/docker/containment.py --native-episode`,
    which measures P7 and deliberately owns no lifecycle - the stack P7
    describes has to be one built from a real task's world, with that world's
    warehouse mounts, and a stack stood up inside the containment script would
    be a stack no rollout ever ran in. So the recipe is three commands and they
    are in `tasks/harness/NATIVE_CONTAINER.md`:

        python container_episode.py native-up --task GT001-aov-halving \
            --dir <scratch> --project alkera-p7
        python ../../gateway/docker/containment.py --native-episode \
            --project alkera-p7 --world <scratch>/world \
            --out ../../gateway/docker/evidence/containment-native-episode.json
        python container_episode.py native-down --dir <scratch> --project alkera-p7

    The world is built by the task's own declared builder, through the same
    `episode.build_world` a rollout calls, so `--dir` ends up holding a world
    and a grading zone laid out exactly as a rollout's.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="container_episode")
    sub = parser.add_subparsers(dest="mode", required=True)
    for name in ("native-up", "native-down"):
        one = sub.add_parser(name)
        one.add_argument("--dir", required=True, type=Path,
                         help="scratch directory: world/, grading/, audit/")
        one.add_argument("--project", required=True)
        one.add_argument("--task", default="GT001-aov-halving")
        one.add_argument("--tasks-dir", default=None)
        one.add_argument("--image", default=DEFAULT_IMAGE)
    args = parser.parse_args(argv)

    import taskspec

    spec = taskspec.load(args.task,
                         Path(args.tasks_dir).resolve() if args.tasks_dir else None)
    root = Path(args.dir).resolve()
    manifest = root / "world.json"
    if args.mode == "native-up":
        root.mkdir(parents=True, exist_ok=True)
        episode.prepare_task(spec)
        world = episode.build_world(spec, root / "world", root / "grading")
        manifest.write_text(json.dumps(world, indent=2), encoding="utf-8")
    elif not manifest.is_file():
        raise ContainerError(f"{manifest} does not exist; nothing to take down")
    else:
        world = json.loads(manifest.read_text(encoding="utf-8"))

    stack = NativeContainerEpisode(
        project=args.project, world=world, audit_dir=root / "audit",
        rollout_dir=root, image=args.image)
    if args.mode == "native-down":
        stack._up = True                      # `down` on a stack this process did not start
        stack.down()
        print(json.dumps({"project": args.project, "state": "down"}, indent=2))
        return 0
    stack.up()
    print(json.dumps({"project": args.project,
                      "episode": stack.container_id[:12],
                      "world": str(root / "world"),
                      "warehouse_mounts": stack.warehouses,
                      "prepared": stack.prepare()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_native_cli())
