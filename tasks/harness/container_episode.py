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

import hashlib
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import bistate  # where the services' own state lives
import episode  # WORLD_PLANE_KEYS: one list, two topologies

REPO = Path(__file__).resolve().parents[2]
DOCKER_DIR = REPO / "gateway" / "docker"
COMPOSE_BASE = DOCKER_DIR / "compose.yml"
COMPOSE_OVERLAY = DOCKER_DIR / "compose.episode.yml"
SHIM = Path(__file__).resolve().parent / "container_shim.py"

DEFAULT_IMAGE = "alkera-episode:head"


def _backends():
    """`alkera_gateway.backends`, the registry of what a profile can plug into
    this topology. Imported on first use, like `agent_under_test` below."""
    from alkera_gateway import backends

    return backends


def profile_image(image: str, profile) -> str:
    """The episode image a profiled task runs on, when `--image` was left at
    DEFAULT_IMAGE: the profile's role image (`-da`, `-ds`) and one suffix per
    client set its plugged backends need (`-snowflake`), sorted - exactly
    gateway/docker/build_episode.py's `image_tag` (test_profile_wiring and
    test_backends pin the two together). An explicit `--image`, or an
    unprofiled task, is unchanged. Without this a da task started on the de
    image, whose jupyter service has no jupyter-server to run."""
    if profile is None or image != DEFAULT_IMAGE:
        return image
    role = profile.name.split("/", 1)[0]
    suffix = [] if role == "de" else [role]
    suffix += sorted({b.clients for b in _backends().plugged(profile.slots) if b.clients})
    return "-".join([image, *suffix])
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


# ------------------------------------------------------------- Linux hosts
#: Three things Docker Desktop on Windows did for this harness without being
#: asked, and which a Linux host does not. Each is handled here, once, for both
#: topologies; `tasks/harness/README.md#linux-hosts` is the operator's account.
#:
#:   1. SELINUX. Fedora/RHEL enforce it, and a container engine that labels
#:      (rootless podman, Fedora's moby-engine) refuses a bind of an unlabelled
#:      host directory - the world, the audit log, postgres.sql - with EACCES
#:      inside the box. `selinux_overlay` turns labelling off for this project's
#:      containers only (`security_opt: label=disable`) rather than relabelling
#:      host files with `:z`: nothing on the host changes, and the containment
#:      boundary is the mount set and the internal networks, which that option
#:      does not touch.
#:   2. FILE OWNERSHIP. A rootful engine writes the agent's files into the bind
#:      mounts as host root, and the grader, the fingerprint and `rmtree` then
#:      run on the host as the operator. `release_ownership` hands every file
#:      under the binds back before teardown.
#:   3. THE SUBJECT'S LOGIN. See `require_subject_auth`.
#:
#: Parent-session variables. A harness started from inside a Claude Code
#: session (an operator's terminal agent, a CI job driven by one) exports these,
#: and a subject that inherits them is not a fresh process: it can find the
#: parent's IPC socket and session id. Removed from every subject environment.
PARENT_SESSION_VARS = (
    "CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "AI_AGENT",
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_PROJECT_DIR",
)
#: Any one of these lets the CLI authenticate without reading a credentials
#: file out of `$HOME`.
SUBJECT_AUTH_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
                     "ANTHROPIC_AUTH_TOKEN")


def subject_base_env(extra: dict | None = None) -> dict:
    """`os.environ` minus the parent session, plus `extra`. The one place a
    subject's environment starts from."""
    env = {k: v for k, v in os.environ.items() if k not in PARENT_SESSION_VARS}
    env["PYTHONUTF8"] = "1"
    env.update(extra or {})
    return env


def require_subject_auth(env: dict | None = None) -> str:
    """Name the credential a container-topology subject will log in with.

    The shell redirect points the subject's `HOME` at the rollout's own
    directory so the hook is found and the operator's home is untouched. On
    Windows the CLI reads its login from `USERPROFILE`, which stays put; on
    Linux it reads `$HOME/.claude/.credentials.json`, which is now absent, and
    the subject would die at its first turn with every episode scored as a
    harness error. Copying that file in is refused: it holds a refresh token,
    and a subject that rotates it logs the operator out. So a Linux run needs
    the login in the environment - `claude setup-token` prints one.
    """
    env = os.environ if env is None else env
    for name in SUBJECT_AUTH_VARS:
        value = env.get(name, "").strip()
        if not value:
            continue
        # Anthropic keys and `claude setup-token` tokens both start `sk-ant-`.
        # Anything else is a placeholder or a paste gone wrong, and it would
        # otherwise surface as `401 Invalid bearer token` one model turn later
        # (measured 2026-09-25: a literal PASTE_TOKEN_HERE reached the API).
        # ANTHROPIC_AUTH_TOKEN is exempt: it may be a gateway's own bearer.
        if name != "ANTHROPIC_AUTH_TOKEN" and (
                not value.startswith("sk-ant-") or any(c.isspace() for c in value)):
            raise ContainerError(
                f"${name} does not look like an Anthropic credential (expected "
                f"'sk-ant-...' with no spaces; got {len(value)} characters "
                f"starting {value[:6]!r}). Re-run `claude setup-token` and "
                f"export the token it prints.")
        return name
    if not sys.platform.startswith("linux"):
        return "home"
    raise ContainerError(
        "no subject credential in the environment. On Linux the container "
        "topology gives the subject its own HOME, so the CLI cannot read "
        "~/.claude/.credentials.json. Run `claude setup-token` once and export "
        "CLAUDE_CODE_OAUTH_TOKEN=<token> (or ANTHROPIC_API_KEY) before starting "
        "the harness. See tasks/harness/README.md#linux-hosts.")


def selinux_enforcing() -> bool:
    try:
        return Path("/sys/fs/selinux/enforce").read_text().strip() == "1"
    except OSError:
        return False


def selinux_overlay(compose_argv: list[str], env: dict, dest: Path) -> Path | None:
    """`dest`, written with `label=disable` for every service the project
    declares, or None when the host does not enforce SELinux.

    The service list is read from compose itself (`config --services`), so an
    overlay that adds a service is covered without an edit here.
    ALKERA_SELINUX_LABEL=keep leaves labelling on, for an operator who has
    relabelled the rollout directory themselves.
    """
    if not selinux_enforcing() or os.environ.get("ALKERA_SELINUX_LABEL") == "keep":
        return None
    result = subprocess.run([*compose_argv, "config", "--services"],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", env=env, timeout=120)
    if result.returncode != 0:
        raise ContainerError(f"docker compose config --services failed:\n"
                             f"{result.stderr[-2000:]}")
    services = sorted(s for s in result.stdout.split() if s)
    body = ["# Generated by tasks/harness/container_episode.selinux_overlay: this",
            "# host enforces SELinux, so container labelling is off for this project",
            "# only. The mounts and the internal networks are unchanged.",
            "services:"]
    for name in services:
        body += [f"  {name}:", '    security_opt: ["label=disable"]']
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(body) + "\n", encoding="utf-8")
    return dest


#: Run inside the episode container as its root. `/audit` is always a bind of
#: the host's audit directory, so its owner AS SEEN INSIDE is the id that maps
#: back to the operator: their uid under a rootful engine, 0 under a rootless
#: one. Every file under the binds is given to that id. Errors are expected and
#: ignored: `/world/var/ledger` is a read-only tmpfs.
RELEASE_PATHS = ("/workspace", "/world", "/audit")
RELEASE_OWNERSHIP = ("for d in " + " ".join(RELEASE_PATHS) + "; do "
                     "[ -e \"$d\" ] && chown -R --reference=/audit \"$d\" 2>/dev/null; "
                     "done; stat -c %u:%g /audit")
#: native-services' own state directory, inside that container
#: (compose.native.yml; gateway/docker/native_services.py).
SERVICE_STATE_IN_SERVICES = "/service-state"


def release_ownership(container_id: str, *paths: str) -> dict:
    """Give the operator back every file the containers wrote into the binds.
    A no-op off Linux, where the engine's file sharing already maps owners.
    `paths` defaults to the episode's binds; every container this runs in
    mounts `/audit`, which is the reference owner."""
    if not container_id or not sys.platform.startswith("linux"):
        return {"skipped": True}
    script = RELEASE_OWNERSHIP if not paths else RELEASE_OWNERSHIP.replace(
        " ".join(RELEASE_PATHS), " ".join(paths), 1)
    result = _run(["docker", "exec", "-u", "0", container_id, "sh", "-c",
                   script], check=False, timeout=900)
    return {"owner_inside": result.stdout.strip(), "returncode": result.returncode}


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


def container_gateway_config(world: dict, episode_id: str, virtual_time: str, *,
                             profile: str = "", plugs: dict | None = None) -> dict:
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
        # The task's environment profile, last and only when it names one.
        **episode.profile_keys(profile, plugs),
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
                "-f", str(COMPOSE_BASE), "-f", str(COMPOSE_OVERLAY)]
        if not hasattr(self, "_selinux"):
            # Beside the world root, never inside it: the world is mounted.
            self._selinux = selinux_overlay(
                argv, env, Path(self.world_root).resolve().parent
                / f"{self.project}.selinux.yml")
        if self._selinux is not None:
            argv += ["-f", str(self._selinux)]
        argv += list(args)
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
        self.ownership = release_ownership(self.container_id)
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
#: Track A on the episode image's import path. The Dockerfile bakes
#: `PYTHONPATH=/opt/world`; `./ops/sched` bakes its own copy because a CLI that
#: depended on the agent's environment for its own imports would break the
#: moment the agent exported something (shims/install.install_cli).
WORLD_PYTHONPATH_IN_CONTAINER = "/opt/world"
#: Surfaces this topology serves over HTTP rather than off the world's
#: filesystem, handed to `surface_audit.probe_native`. One entry, and it is the
#: whole of what the mount set costs: the scheduler's files are not in the
#: episode, so the gate has to ask `sched.internal` what the subject would ask.
SERVED_PLANES = frozenset({"sched"})
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
#: The compose profile `profile_overlay` gates MinIO behind on a box whose
#: environment profile plugs no `object_store`. Nothing enables it
#: (`compose_env` never names it), so MinIO is not started on such a box at all.
MINIO_COMPOSE_PROFILE = "object_store"
#: The variable `compose_env` hands compose the per-rollout MinIO audit token
#: in. compose.native.yml interpolates it into exactly two environments, MinIO's
#: webhook (`MINIO_AUDIT_WEBHOOK_AUTH_TOKEN_alkera`) and native-services' (the
#: receiver checks it): never the episode's, jupyter's or the subject's.
MINIO_AUDIT_TOKEN_ENV = "ALKERA_MINIO_AUDIT_TOKEN"
#: The static table's backend variables, by the plug that serves them: a
#: profiled box keeps a group only when its profile plugs that backend
#: (`profile_variables`). Unprofiled boxes keep all of them, as before.
BACKEND_ENV = {
    "postgres": ("PGHOST", "PGPORT", "PGDATABASE", "PGUSER", "PGPASSWORD"),
    "snowflake": ("SNOWFLAKE_HOST", "SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER",
                  "SNOWFLAKE_PASSWORD"),
    "airflow": ("AIRFLOW_API_URL", "AIRFLOW_USER", "AIRFLOW_PASSWORD"),
}

#: Each vendor service's variables in the `episode` environment: the URL
#: `agent_under_test.NATIVE_SERVICES` names, and the fake credentials beside it.
#:
#: Two directions. The nine are in compose.native.yml's static environment, and
#: a profile that excludes one drops all of its variables. The BI vendors
#: (`agent_under_test.PLUG_SERVICES`) are in NO static file - an unprofiled
#: stack must not name them - so a profile that plugs one ADDS its variables,
#: with the values `agent_under_test.plug_service_env` gives for the
#: `.internal` alias. HOW a variable is dropped is `profiled_environment`'s
#: business (an `!override` of the whole mapping; `VAR: !reset null` was a
#: no-op on the episode under Compose v5.6). test_profile_wiring.py fails when
#: a nine-row name is missing from the static file, when a vendor's name
#: appears in it, or when a vendor row here is not exactly
#: `plug_service_env`'s keys.
EPISODE_SERVICE_ENV = {
    "jira":         ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN"),
    "slack":        ("SLACK_BASE_URL", "SLACK_BOT_TOKEN"),
    "confluence":   ("CONFLUENCE_URL", "CONFLUENCE_USER", "CONFLUENCE_API_TOKEN"),
    "metabase":     ("METABASE_URL", "METABASE_API_KEY"),
    "prometheus":   ("PROMETHEUS_URL",),
    "alertmanager": ("ALERTMANAGER_URL",),
    "loki":         ("LOKI_URL", "LOKI_ADDR"),
    "airbyte":      ("AIRBYTE_URL", "AIRBYTE_API_KEY"),
    "deploy":       ("DEPLOY_URL", "DEPLOY_API_TOKEN"),
    # explicit-only: added by `profile_overlay`, never in the static file
    "powerbi":      ("POWERBI_API_URL", "FABRIC_API_URL", "AZURE_AUTHORITY_HOST",
                     "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID"),
    "tableau":      ("TABLEAU_SERVER_URL", "TABLEAU_PAT_NAME", "TABLEAU_PAT_SECRET",
                     "TABLEAU_SITE"),
    "sigma":        ("SIGMA_BASE_URL", "SIGMA_CLIENT_ID", "SIGMA_CLIENT_SECRET"),
}

# ------------------------------------------------------------ the hard sandbox
#: `--subject-sandbox container` (README.md#the-subject-sandbox): the model's CLI runs in the
#: `subject` service of this overlay, not on the host, and its Bash tool
#: reaches the episode through rexec.py on `subject_net`.
SUBJECT_OVERLAY = DOCKER_DIR / "compose.subject.yml"
SUBJECT_IMAGE = "alkera-subject:head"
SUBJECT_DOCKERFILE = DOCKER_DIR / "Dockerfile.subject"
SUBJECT_CONTEXT = DOCKER_DIR / "subject"
REXEC_SOURCE = SUBJECT_CONTEXT / "rexec.py"
REXEC_IN_CONTAINER = "/opt/alkera/rexec.py"
REXEC_PORT = 7070
#: The CLI's working directory inside the subject; bash_env.sh rewrites it to
#: /workspace on the way across. Named so it occurs in no real command.
SUBJECT_CWD = "/alkera-subject-cwd"
#: Where the engine socket would be if someone mounted it. Any one of them
#: inside the subject is root on the host.
ENGINE_SOCKETS = ("/var/run/docker.sock", "/run/docker.sock",
                  "/run/podman/podman.sock", "/run/user/1000/podman/podman.sock")

#: Run inside the subject container by `subject_sandbox_probe`, as `python -`,
#: never through bash (bash there is the redirect). SPEC is prepended as JSON.
SUBJECT_PROBE = r"""
import json, os, socket, time
out = {}
status = dict(l.split(":	", 1) for l in open("/proc/self/status").read().splitlines() if ":	" in l)
out["uid"] = os.getuid()
out["cap_eff"] = status.get("CapEff", "").strip()
out["no_new_privs"] = status.get("NoNewPrivs", "").strip()
try:
    open("/usr/.alkera-rw-test", "w").close(); out["root_read_only"] = False
except OSError:
    out["root_read_only"] = True
routes = open("/proc/net/route").read().splitlines()[1:]
out["default_route"] = any(r.split()[1] == "00000000" for r in routes if len(r.split()) > 1)
out["engine_sockets"] = [p for p in SPEC["sockets"] if os.path.exists(p)]
out["host_paths_visible"] = [p for p in SPEC["host_paths"] if p and os.path.exists(p)]
out["mounts"] = [l.split()[1] for l in open("/proc/mounts").read().splitlines()]
def tcp(host, port):
    try:
        socket.create_connection((host, port), timeout=4).close(); return True
    except OSError:
        return False
out["direct_egress"] = {f"{h}:{p}": tcp(h, p) for h, p in (("1.1.1.1", 443), ("8.8.8.8", 53))}
try:
    socket.getaddrinfo("example.com", 443); out["public_dns"] = True
except OSError:
    out["public_dns"] = False
def connect(target):
    try:
        s = socket.create_connection(("egress-proxy", 3128), timeout=10)
        s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
        line = s.recv(200).split(b"\r\n", 1)[0].decode("latin-1"); s.close()
        return line.split()[1] if len(line.split()) > 1 else line
    except OSError as exc:
        return f"error: {exc}"
out["proxy_refuses_other"] = connect("example.com:443")
out["proxy_allows_api"] = connect(SPEC["api"])
out["episode_bridge"] = tcp("episode", SPEC["rexec_port"])
out["episode_services_reachable"] = tcp("postgres.internal", 5432) or tcp("jira.internal", 8080)
print(json.dumps(out))
"""


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


#: The variable the episode's own row writers read the world's clock from
#: (`alkera_gateway.native.audit.virtual_time`: ALKERA_VTS, else the scheduler
#: state ALKERA_SCHEDULER names, else THIS, else GatewayConfig's literal).
CLOCK_ENV = "ALKERA_VIRTUAL_TIME"
#: A backend container (`NativeContainerEpisode.backend_overlay`) that seeds
#: from the world finds the world's warehouse FILES here, read-only.
SEED_IN_CONTAINER = "/seed"
#: Backends whose compose stanza predates the registry: compose.yml declares
#: the service (profile, build context, Dockerfile, volumes, network) and
#: compose.native.yml its episode_net alias, so `backend_overlay` adds only what
#: is per rollout and their overlay bytes stay what they were. Every other
#: backend gets a complete service block and needs no static compose edit.
STATIC_BACKENDS = frozenset({"snowflake"})
#: HOSTED backends whose ENGINE is a compose.yml service of its own, by backend
#: name: (compose service == its compose profile, port, command). The registry
#: row's module is the front door inside native-services; the engine sits on
#: backend_net, which the episode is not on. When one is plugged, `compose_env`
#: turns the engine's profile on and `profile_overlay` puts native-services on
#: backend_net, hands it the engine's address as `ALKERA_<NAME>_UPSTREAM`,
#: orders it after the engine, and restates the engine's command (each argument
#: formatted with the row's `env`).
#:
#: bigquery: goccy's emulator (compose.yml's stanza), started under the
#: registry's project - the stanza names `episode`, the project the MCP
#: topology's ALKERA_BIGQUERY_OPTIONS use, and the emulator serves one project
#: only - and without its `bigquery.internal` alias on backend_net: once
#: native-services joins that network, the name would mean both the proxy and
#: its engine there.
HOSTED_ENGINES = {
    "bigquery": ("bigquery", 9050, ("--project={BIGQUERY_PROJECT}", "--port=9050",
                                    "--grpc-port=9060",
                                    "--database=/bigquery-data/emulator.db")),
    # Databricks tier 2: Spark 4.2 + Delta, its own image (HOSTED_ENGINE_BUILDS),
    # its default command (gateway/docker/spark/spark_start.py).
    "spark_connect": ("spark", 15002, ()),
}
#: HOSTED_ENGINES whose engine is NOT a compose.yml service but an image built
#: from this repository, by backend name: the Dockerfile under gateway/docker.
#: `engine_overlay` writes such an engine's COMPLETE service block (compose
#: profile, build, backend_net, the world's warehouse FILES read-only at /seed,
#: as a container backend's `seeds_world`), so no static compose file names it.
#: Its upstream is `host:port` (gRPC), not a URL.
HOSTED_ENGINE_BUILDS = {
    "spark_connect": "spark/Dockerfile.spark",
}
#: Where a pinned episode_net subnet comes from: the daemon's own pool on the
#: hosts these stacks run on (/etc/docker/daemon.json, `default-address-pools`
#: 10.210.0.0/16 in /24s). With a HOSTED_ENGINES backend plugged, native-services
#: joins backend_net and its agent listeners answer there too; MinIO, a
#: backend_net peer, was measured dialling them for the agent while the box's
#: credential was MinIO's root (gateway/docker/evidence/PEERS_2026-10-13.md;
#: the box is a scoped user since, gateway/docker/minio_start.sh). The filter
#: that refuses such peers (serve.EPISODE_SUBNETS_ENV) needs episode_net's subnet
#: before the network exists, so `profile_overlay` pins one (`episode_subnet`).
EPISODE_SUBNET_POOL = ipaddress.IPv4Network("10.210.0.0/16")
#: serve.EPISODE_SUBNETS_ENV, the same name: what native-services reads it from.
EPISODE_SUBNETS_ENV = "ALKERA_EPISODE_SUBNETS"


def docker_subnets() -> list[ipaddress.IPv4Network]:
    """Every IPv4 subnet an existing docker network holds (`docker network
    inspect` of all of them): what a pinned episode_net must not overlap."""
    ids = _run(["docker", "network", "ls", "-q"]).stdout.split()
    specs = json.loads(_run(["docker", "network", "inspect", *ids]).stdout) if ids else []
    return [ipaddress.IPv4Network(config["Subnet"])
            for spec in specs for config in (spec.get("IPAM") or {}).get("Config") or ()
            if "." in str(config.get("Subnet", ""))]


def free_episode_subnet(project: str,
                        taken: list[ipaddress.IPv4Network]) -> ipaddress.IPv4Network:
    """A /24 of EPISODE_SUBNET_POOL that overlaps nothing in `taken`.

    Taken from the TOP half of the pool, downwards: the daemon hands its pool out
    from the bottom up to every project that does not pin a subnet, so the two
    meet only when the pool is nearly full. The starting /24 is a function of
    the project name, which spreads rollouts that allocate at the same moment
    and gives one project the same answer every time it asks."""
    top = list(EPISODE_SUBNET_POOL.subnets(new_prefix=24))[::-1]
    start = int.from_bytes(hashlib.sha256(project.encode()).digest()[:4], "big") % (
        len(top) // 2)
    for subnet in top[start:] + top[:start]:
        if not any(subnet.overlaps(other) for other in taken):
            return subnet
    raise ContainerError(f"no free /24 in {EPISODE_SUBNET_POOL} for episode_net: "
                         f"every one overlaps an existing docker network")


#: The Snowflake plug, by name, as the checks and tests read it: where the
#: world's warehouse files are bound in the `snowflake` service, the trust
#: bundle its TLS proxy publishes (compose.yml's `snowflaketrust` volume), and
#: the destination file it counts into until `fold_destinations`. The registry
#: derives the same values (`backends.trust_dir`, `bundle`, `destinations_file`;
#: test_backends.py holds them equal).
SNOWFLAKE_SEED_IN_CONTAINER = SEED_IN_CONTAINER
SNOWFLAKE_TRUST = "/etc/alkera/snowflake-tls"
SNOWFLAKE_BUNDLE = f"{SNOWFLAKE_TRUST}/bundle.pem"
SNOWFLAKE_DESTINATIONS = "destinations.snowflake.jsonl"


def world_clock(world: dict) -> str | None:
    """The world's virtual clock as the episode starts: `vnow` of the world's
    own `var/sched/state.json`, else the manifest's `virtual_time`, else None.

    WHY THE OVERLAY CARRIES IT. Every row the PATH shims (episode) and the
    notebook kernel (jupyter) write is stamped by `native.audit.virtual_time`,
    which reads `vnow` from the file `ALKERA_SCHEDULER` names. On the host
    topology that is the world's state file. In these two containers it cannot
    be: P7 forbids mounting `var/sched` (the scheduler is served at
    `sched.internal`, and only for a profile that plugs it), so every
    in-container row fell through to `GatewayConfig().virtual_time`, the literal
    2026-03-05T09:00:00Z - measured on BI001's world, whose clock is
    2026-03-02T00:00:00Z (tasks/harness/evidence/bi_e2e_container.json before
    this). The value is read on the host when the overlay is written, i.e. at
    the start of the episode. What it does NOT follow: a `./ops/sched trigger`
    that moves `vnow` during the episode (only a profile with `ops_sched` has
    one) - rows the services write carry the moved clock, rows the shims write
    keep the start. A live clock in the box would need a mount P7 forbids.
    """
    path = world.get("scheduler")
    if path:
        try:
            vnow = json.loads(Path(path).read_text(encoding="utf-8")).get("vnow")
        except (OSError, ValueError, AttributeError):
            vnow = None
        if isinstance(vnow, str) and vnow:
            return vnow
    value = world.get("virtual_time")
    return value if isinstance(value, str) and value else None


class _Tagged:
    """A compose merge tag (`!override`, `!reset`) read off a static file."""

    def __init__(self, tag: str, value) -> None:
        self.tag, self.value = tag, value


def _compose_yaml(path: Path) -> dict:
    """A static compose file, with `!override` / `!reset` kept as `_Tagged`."""
    import yaml

    class Loader(yaml.SafeLoader):
        pass

    def tagged(loader, suffix, node):
        if isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node, deep=True)
        elif isinstance(node, yaml.MappingNode):
            value = loader.construct_mapping(node, deep=True)
        else:
            value = loader.construct_scalar(node)
        return _Tagged(suffix, value)

    Loader.add_multi_constructor("!", tagged)
    return yaml.load(path.read_text(encoding="utf-8"), Loader=Loader) or {}


def static_environment(service: str) -> dict[str, str]:
    """The environment the static files give `service`, merged in the order
    the native stack layers them (compose.yml, compose.episode.yml,
    compose.native.yml): what an unprofiled `episode` or `jupyter` runs with
    before the per-rollout overlay adds its own lines. Interpolations
    (`${ALKERA_BACKEND:-postgres}`) are kept as written - compose resolves them
    in the overlay exactly as it does in the static file. `VAR: !reset null`
    removes VAR, as compose does in compose.native.yml (compose.yml's
    ALKERA_POSTGRES_DSN).
    """
    env: dict[str, str] = {}
    for path in (COMPOSE_BASE, COMPOSE_OVERLAY, NATIVE_OVERLAY):
        block = ((_compose_yaml(path).get("services") or {}).get(service)
                 or {}).get("environment")
        if isinstance(block, _Tagged):
            # `!override` replaces what the files before it said; `!reset`
            # empties it.
            env = {}
            block = block.value if block.tag == "override" else None
        if not block:
            continue
        if isinstance(block, list):
            block = dict(item.split("=", 1) if "=" in item else (item, None)
                         for item in block)
        for name, value in block.items():
            if isinstance(value, _Tagged) and value.tag == "reset":
                env.pop(str(name), None)
                continue
            if value is None or isinstance(value, (_Tagged, dict, list)):
                raise ContainerError(
                    f"{path.name}: {service}.environment.{name} has no literal "
                    f"value; a profiled overlay restates the whole mapping and "
                    f"cannot restate that")
            env[str(name)] = str(value)
    return env


def _env_lines(mapping: dict[str, str]) -> str:
    """`environment:` entries, one per line, JSON-quoted (a YAML double-quoted
    scalar: `TABLEAU_SITE: ""` is an empty string and not a null)."""
    return "".join(f"\n      {name}: {json.dumps(value)}"
                   for name, value in mapping.items())


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


#: Same label `gateway/docker/build_episode.py` stamps and
#: `test_image_was_built_from_current_head` reads. One key, one scope.
SOURCE_REVISION_LABEL = "org.alkera.source.revision"


def _inspect_image_labels(image: str) -> dict | None:
    """`docker image inspect` → `{"Labels": {...}}`, or None if the image is gone."""
    try:
        result = _run(
            ["docker", "image", "inspect", image, "--format",
             "{{json .Config.Labels}}"],
            check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    labels = json.loads(result.stdout.strip() or "null") or {}
    return {"Labels": labels}


def _git_head() -> str:
    result = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise episode.HarnessError(
            f"stale_episode_image: cannot read HEAD: {result.stderr.strip()}")
    return result.stdout.strip()


#: Build evidence (`gateway/docker/evidence/`) is a record ABOUT images, not an
#: input to them: counting it made every committed build record stale its own
#: build, so each evidence commit demanded another rebuild (2026-10-04).
IMAGE_SCOPE = ("gateway", "world", ":(exclude)gateway/docker/evidence")


def _image_scope_changed_paths(old: str, new: str) -> list[str]:
    """Paths under gateway/ or world/ that differ, same scope as image parity
    (build evidence excluded: IMAGE_SCOPE)."""
    result = subprocess.run(
        ["git", "-C", str(REPO), "diff", "--name-only", old, new,
         "--", *IMAGE_SCOPE],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise episode.HarnessError(
            f"stale_episode_image: cannot diff {old}..{new} under gateway/ world/")
    return [line for line in result.stdout.splitlines() if line.strip()]


def assert_image_current(image: str, *, inspect=None, head=None,
                         changed_paths=None) -> dict:
    """Return {image, revision, head} when the image is current enough to measure.

    inspect(image) -> {"Labels": {"org.alkera.source.revision": sha}} or None
        if the image does not exist. Default inspect uses `docker image inspect`.
    head() -> current git HEAD hex. Default: git rev-parse HEAD in the repo.
    changed_paths(old, new) -> list of paths under gateway/ or world/ that
        differ. Default: git diff --name-only old new -- gateway world
        (same scope as test_image_was_built_from_current_head).

    Raise episode.HarnessError whose message contains the token
    stale_episode_image when:
      - inspect returns None (image missing)
      - labels lack org.alkera.source.revision
      - revision != head AND changed_paths(revision, head) is non-empty
    """
    inspect_fn = _inspect_image_labels if inspect is None else inspect
    head_fn = _git_head if head is None else head
    changed_fn = (_image_scope_changed_paths if changed_paths is None
                  else changed_paths)

    spec = inspect_fn(image)
    if spec is None:
        raise episode.HarnessError(
            f"stale_episode_image: image {image!r} is missing")
    labels = spec.get("Labels") or {}
    revision = (labels.get(SOURCE_REVISION_LABEL)
                if isinstance(labels, dict) else None)
    if not revision:
        raise episode.HarnessError(
            f"stale_episode_image: image {image!r} has no "
            f"{SOURCE_REVISION_LABEL} label")
    current = head_fn()
    if revision != current:
        drift = changed_fn(revision, current)
        if drift:
            raise episode.HarnessError(
                f"stale_episode_image: {image!r} was built from {revision} "
                f"but HEAD is {current}; image-bearing paths changed: "
                f"{list(drift)[:10]}")
    return {"image": image, "revision": revision, "head": current}


# ------------------------------------------------------------ the bash wrapper
#: How the subject's shell is moved into the episode container, and why it is
#: this and not a wrapper on PATH.
#:
#: MEASURED 2026-09-17, one model turn per configuration, prompt "Run these two
#: commands and reply with their raw output only: hostname; ls /". Those turns
#: were taken by hand while the mechanism was being found; what is ON DISK is
#: `gateway/docker/evidence/shell-redirect-harness-proof.json` (the redirect
#: through the subject's launcher and env, no model: hostname and `ls /` are the
#: container's; written by test_native_container.py's live half) and, once a
#: `--model-smoke <model>` turn verifies, `shell-redirect-bash-env.json` (the
#: CLI's Bash tool taking that path). Until the second file exists, the claim
#: "a real model's shell lands in the container" rests on the by-hand turns:
#:
#:   * nothing set                      -> `OhhMooZephyrus`, a Git Bash root
#:     (`bin cmd mingw64 …`). The CLI finds Git Bash itself.
#:   * `CLAUDE_CODE_GIT_BASH_PATH` at a `.cmd`, plus that directory first on
#:     PATH  -> **the variable is ignored, silently**: the wrapper was never
#:     called once, and the subject ran on the host. This is the configuration
#:     that shipped, and it is how run-id `native-container-20260917-k5` put a
#:     real model on the HOST filesystem while the record said
#:     `filesystem_isolated`.
#:   * `CLAUDE_CODE_GIT_BASH_PATH` at a path ending `.exe` -> honoured: the CLI
#:     spawned it twice on start-up (`-c env`, `-lc echo "$PATH"`). A shell
#:     script wearing that name then failed `EUNKNOWN: uv_spawn`, because the
#:     CLI spawns it directly rather than through a shell. So the path has to be
#:     a real executable, and the only real executable we have is bash itself.
#:
#: Hence: point the CLI at the REAL `bash.exe` and redirect from inside bash.
#: `BASH_ENV` is read by every non-interactive bash (`bash -c …`) and
#: `$HOME/.bash_profile` by every login one (`bash -lc …`); the CLI uses both
#: forms, so both are written and both do the same thing. `HOME` is pointed at
#: this rollout's own directory so nothing in the operator's home is touched -
#: on Windows the CLI reads its own credentials from `USERPROFILE`, which is
#: left alone.
#:
#: `$BASH_EXECUTION_STRING` is the `-c` argument, so the hook can hand the
#: command to the container verbatim and the transcript still records what the
#: model asked for.
#:
#: IT IS A REDIRECT, NOT A BOUNDARY. A subject that unsets `BASH_ENV` (`env -u
#: BASH_ENV bash -c …`) is back on the host. (`--noprofile --norc` is NOT an
#: escape: `BASH_ENV` is read by every non-interactive bash regardless -
#: measured in test_native_container.py's pure half.) That is why
#: `rollout.py` verifies it by measurement before handing the terminal over and
#: refuses the `filesystem_isolated` guarantee when the proof is missing -
#: `container_execution_proof`. The alternative, running the model process
#: itself inside the episode container, is written up in
#: `docs/implementation_plan/harness/NATIVE_CONTAINER.md`: it would be a real boundary and it would
#: cost the episode container its "no egress" property, because the CLI needs
#: the inference API.
BASH_HOOK = """# Generated per rollout by tasks/harness/container_episode.py.
# Sourced by every bash this rollout's subject starts: as $BASH_ENV for
# `bash -c` and from .bash_profile/.bashrc for `bash -lc`. When the shell was
# given a command, that command is executed INSIDE the episode container and
# this shell is replaced by the `docker exec` that carries it.
if [ -n "${{BASH_EXECUTION_STRING:-}}" ]; then
  # The CLI keeps its own working directory by putting a `cd <that directory>`
  # in the command it sends, and that directory is a HOST path which does not
  # exist in the container: measured, the first container turn came back
  # correct but with `bash: line 1: /c/Users/.../claude-<id>-cwd: No such file
  # or directory` and exit 1 on every command. So the host cwd is rewritten to
  # the container's before the command crosses over. Rewritten and not created:
  # a directory of that name inside the container would put the host's own
  # layout in front of the subject, which is the thing this topology exists to
  # take away.
  __alkera_cmd=${{BASH_EXECUTION_STRING//"$PWD"/{workdir_plain}}}
  exec {docker} exec -i -w {workdir}{env_args} {container} bash -c "$__alkera_cmd"
fi
"""

#: The two files a login shell reads, both of which just source the hook. Two
#: and not one because bash reads `.bash_profile` for a login shell and
#: `.bashrc` for an interactive one, and the CLI has used both spellings.
PROFILE = """# Generated per rollout. See {hook}.
. "{hook}"
"""


#: Where Git for Windows puts the bash the CLI wants, in the order the CLI's
#: own documentation names them. Checked before PATH, because PATH on a
#: Windows box with WSL puts `C:\WINDOWS\system32\bash.exe` first and that is
#: not a bash at all - it is the WSL launcher, it does not read a Windows
#: `BASH_ENV`, and the redirect would never fire. MEASURED 2026-09-17: the
#: harness process's `shutil.which("bash")` returned the WSL launcher on this
#: machine; `verify_container_execution` would have refused the rollout, which
#: is the right outcome, but with a message that blames the container.
GIT_BASH_CANDIDATES = (
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
)


def _real_bash() -> str:
    """The bash the CLI will actually spawn.

    On Windows it must be Git for Windows' `bash.exe`: the CLI ignores a
    `CLAUDE_CODE_GIT_BASH_PATH` that does not end in `.exe` (the measurement at
    the top of this section), and the WSL launcher that Windows itself puts on
    PATH under the same name does not read the hook. An operator who has set
    `CLAUDE_CODE_GIT_BASH_PATH` already is believed; otherwise the standard
    install locations are tried, then PATH, skipping anything under System32.
    """
    pinned = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH", "").strip()
    if pinned and pinned.lower().endswith(".exe") and Path(pinned).exists():
        return pinned
    if os.name == "nt":
        for candidate in GIT_BASH_CANDIDATES:
            if Path(candidate).exists():
                return candidate
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if os.name == "nt" and "system32" in directory.lower():
            continue
        found = shutil.which("bash", path=directory)
        if found:
            return found
    raise ContainerError(
        "no usable `bash` found: not at CLAUDE_CODE_GIT_BASH_PATH, not at a Git "
        "for Windows install location, not on PATH outside System32. The "
        "container topology redirects the subject's shell from inside bash and "
        "needs a real one to point the CLI at (the WSL launcher is not one).")


def write_bash_wrapper(bin_dir: Path, *, container: str, interpreter: str,
                       workdir: str = WORKSPACE_IN_CONTAINER,
                       env: dict | None = None, docker: str | None = None) -> dict:
    """Materialise the redirect and return the environment that arms it.

    `interpreter` is unused by the hook itself and is kept because the caller
    knows which Python this episode runs under and a future mechanism may need
    it; it is recorded in the config file beside the hook so a reader of a
    rollout can see what the harness thought it was doing.

    `env` is forwarded to every `docker exec`, baked into the hook as literal
    `-e K=V` arguments rather than passed through a shell variable, because a
    value with a space in it would word-split.

    `docker` names the binary the hook execs; the default is the one on PATH.
    `test_native_container.py` passes a stand-in so the redirect itself - the
    hook firing from `BASH_ENV`, the cwd rewrite, the argument order - can be
    exercised through a real bash with no daemon and no model.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    docker = docker or shutil.which("docker") or "docker"
    env_args = "".join(f' -e {name}={_sh_quote(value)}'
                       for name, value in sorted((env or {}).items()))
    hook = bin_dir / "alkera_bash_env.sh"
    with open(hook, "w", encoding="utf-8", newline="") as stream:
        stream.write(BASH_HOOK.format(
            docker=_sh_quote(docker), workdir=_sh_quote(workdir),
            workdir_plain=str(workdir), env_args=env_args,
            container=_sh_quote(container)))
    written = [hook]
    for name in (".bash_profile", ".bashrc", ".profile"):
        path = bin_dir / name
        with open(path, "w", encoding="utf-8", newline="") as stream:
            stream.write(PROFILE.format(hook=hook.as_posix()))
        written.append(path)
    config = bin_dir / "container_bash.json"
    with open(config, "w", encoding="utf-8", newline="") as stream:
        json.dump({"container": container, "workdir": workdir,
                   "env": dict(env or {}), "bash": _real_bash(),
                   "docker": docker, "interpreter": interpreter},
                  stream, indent=2, sort_keys=True)
    written.append(config)
    return {
        "bin": str(bin_dir),
        "hook": str(hook),
        "config": str(config),
        "launcher": _real_bash(),
        "files": [str(p) for p in written],
        # What the subject's process must carry for the redirect to arm. The
        # CLI is pinned to the same bash the harness verified with, `HOME` is
        # this rollout's own so the hook is found and the operator's home is
        # untouched, and MSYS is told to stop rewriting `/workspace` into a
        # Windows path on its way to `docker exec`.
        # `SHELL` is the Linux/macOS half of the pin: off Windows the CLI
        # ignores CLAUDE_CODE_GIT_BASH_PATH and spawns `$SHELL`, and an
        # operator whose login shell is zsh or fish would get a shell that never
        # reads BASH_ENV - the redirect would silently not fire.
        "subject_env": {
            "CLAUDE_CODE_GIT_BASH_PATH": _real_bash(),
            "SHELL": _real_bash(),
            "HOME": str(bin_dir),
            "BASH_ENV": hook.as_posix(),
            "MSYS_NO_PATHCONV": "1",
            "MSYS2_ARG_CONV_EXCL": "*",
        },
    }


def subject_image_state(image: str = SUBJECT_IMAGE) -> dict:
    """The subject image's id and the CLI version baked into it, or a refusal.

    The CLI is part of the measured system - its tool loop, its prompts - so a
    rollout records which build of it ran, read off the image rather than the
    operator's PATH.
    """
    labels = _inspect_image_labels(image)
    if labels is None:
        raise episode.HarnessError(
            f"subject image {image!r} is not built; `scripts/setup_linux.sh "
            f"--skip-packages` builds it (or pass --subject-sandbox host)")
    ident = _run(["docker", "image", "inspect", image, "--format", "{{.Id}}"],
                 check=False).stdout.strip()
    return {"image": image, "id": ident,
            "claude_version": labels["Labels"].get("org.alkera.claude.version")}


def subject_exec_argv(wrapper: dict, env: dict) -> list[str]:
    """`docker exec` into the sandboxed subject, carrying the credential by
    NAME: `-e VAR` takes the value from this process's environment, so the
    token is never in an argv that is written to the rollout record."""
    argv = ["docker", "exec", "-i", "-w", wrapper.get("cwd", SUBJECT_CWD)]
    for name in SUBJECT_AUTH_VARS:
        if env.get(name, "").strip():
            argv += ["-e", name]
    return argv + [wrapper["container"]]


def kill_subject(wrapper: dict) -> None:
    """Killing the host's `docker exec` does not reach the CLI inside the
    subject; this does. Best effort: teardown removes the container anyway."""
    if wrapper.get("mode") == "subject_container":
        _run(["docker", "exec", wrapper["container"], "pkill", "-KILL", "-x",
              "claude"], check=False, timeout=60)


def _sh_quote(text: str) -> str:
    """One shell word, for a path that may contain spaces (`Program Files`)."""
    return "'" + str(text).replace("'", "'\\''") + "'"


def verify_container_execution(wrapper: dict, container: str,
                               timeout: int = 120) -> dict:
    """Run `hostname` through the SUBJECT'S OWN shell path and compare.

    This is the whole of what stands between the container topology and the
    failure it has already had once: a mechanism that silently does not fire,
    and a rollout that records `filesystem_isolated` about an episode that ran
    on the host. The proof is taken the way the subject will take it - the same
    binary, the same environment, the same `-lc` - immediately before the
    terminal is handed over, and `blindness.guarantee` will not issue the
    container guarantee without it.
    """
    expected = _run(["docker", "exec", container, "hostname"]).stdout.strip()
    env = subject_base_env(wrapper["subject_env"])
    result = subprocess.run([wrapper["launcher"], "-lc", "hostname"],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", env=env, timeout=timeout)
    observed = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
    return {
        "how": f"{Path(wrapper['launcher']).name} -lc hostname, with the "
               f"subject's own environment",
        "expected": expected,
        "observed": observed,
        "returncode": result.returncode,
        "stderr": (result.stderr or "")[-400:],
        "verified": bool(expected) and observed == expected,
    }


#: The prompt of the one-turn model smoke, verbatim from the measurement at the
#: top of this section, so a re-run is the same measurement.
SMOKE_PROMPT = ("Run these two commands and reply with their raw output only: "
                "hostname; ls /")
#: Entries a Git Bash root lists and a Linux container root does not. Any one
#: of them in the model's `ls /` output means the shell ran on the host.
HOST_ROOT_MARKERS = ("mingw64", "git-bash.exe", "git-cmd.exe", "cmd", "ReleaseNotes.html",
                     "LICENSE.txt", "unins000.exe")
#: Entries the episode container's root must show: the three mounts this
#: topology makes and two that any Linux root has.
CONTAINER_ROOT_MARKERS = ("workspace", "world", "audit", "etc", "usr")


def model_shell_smoke(wrapper: dict, container: str, out_dir: Path, *,
                      model: str, claude: str | None = None,
                      timeout: int = 300) -> dict:
    """ONE model turn through the subject's real path: the CLI, its Bash tool,
    the hook, `docker exec`. The measurement `verify_container_execution`
    approximates from outside the CLI, taken from inside it.

    The scripted subject and the harness's own `bash -lc hostname` both go
    through `bash.exe`; the CLI's Bash tool goes through whatever the CLI
    decides to spawn, and that decision is what silently put run-id
    `native-container-20260917-k5` on the host. So the acceptance for this
    topology is a real model, asked to run `hostname; ls /`, and its transcript
    read for three things: the Bash tool was used, the hostname it printed is
    the container's, and the root it listed is a Linux root with this
    topology's three mounts and none of Git Bash's entries.

    Writes `transcript.jsonl` and `smoke.json` into `out_dir` and returns the
    verdict. Costs one short turn of whichever model is named; the shell
    mechanics it measures do not depend on the model.
    """
    claude = claude or shutil.which("claude")
    if not claude:
        raise ContainerError("no `claude` on PATH; the model smoke needs the CLI")
    require_subject_auth()
    expected = _run(["docker", "exec", container, "hostname"]).stdout.strip()
    out_dir.mkdir(parents=True, exist_ok=True)
    argv = [claude, "--print", "--output-format", "stream-json", "--verbose",
            "--model", model, "--setting-sources", "", "--disable-slash-commands",
            "--no-session-persistence", "--permission-mode", "bypassPermissions",
            "--permission-prompts", "none", "--strict-mcp-config",
            "--mcp-config", json.dumps({"mcpServers": {}}), "--tools", "Bash"]
    env = subject_base_env(wrapper["subject_env"])
    if wrapper.get("mode") == "subject_container":
        argv = subject_exec_argv(wrapper, env) + ["claude"] + argv[1:]
    started = time.time()
    proc = subprocess.run(argv, input=SMOKE_PROMPT, cwd=str(out_dir),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=timeout)
    (out_dir / "transcript.jsonl").write_text(proc.stdout, encoding="utf-8")
    (out_dir / "agent.stderr.txt").write_text(proc.stderr or "", encoding="utf-8")

    commands, results, final = [], [], ""
    for line in proc.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            final = ev.get("result") or final
        for block in ((ev.get("message") or {}).get("content") or []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "Bash":
                commands.append(str((block.get("input") or {}).get("command", "")))
            if block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    content = "\n".join(str(c.get("text", "")) for c in content
                                        if isinstance(c, dict))
                results.append(str(content or ""))
    seen = "\n".join(results + [final])
    tokens = set(re.findall(r"[A-Za-z0-9_.\-]+", seen))
    host_markers = sorted(m for m in HOST_ROOT_MARKERS if m in tokens)
    container_markers = sorted(m for m in CONTAINER_ROOT_MARKERS if m in tokens)
    verdict = {
        "prompt": SMOKE_PROMPT,
        "model": model,
        "claude": claude,
        "argv": argv,
        "subject_env": wrapper["subject_env"],
        "launcher": wrapper["launcher"],
        "expected_hostname": expected,
        "bash_tool_used": bool(commands),
        "commands": commands,
        "tool_results": [r[-600:] for r in results],
        "final": final[-1200:],
        "hostname_is_the_container": bool(expected) and expected in tokens,
        # A Linux host's `ls /` shares most entries with the container's, so
        # the Git Bash markers say nothing there; the host's own hostname does.
        "host_hostname_seen": (socket.gethostname() != expected
                               and socket.gethostname() in tokens),
        "host_root_markers_seen": host_markers,
        "container_root_markers_seen": container_markers,
        "returncode": proc.returncode,
        "stderr": (proc.stderr or "")[-600:],
        "wall_s": round(time.time() - started, 2),
        "measured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    verdict["verified"] = (verdict["bash_tool_used"]
                           and verdict["hostname_is_the_container"]
                           and not host_markers
                           and not verdict["host_hostname_seen"]
                           and set(container_markers) >= {"workspace", "world", "audit"})
    with open(out_dir / "smoke.json", "w", encoding="utf-8", newline="") as stream:
        json.dump(verdict, stream, indent=2, sort_keys=True)
    return verdict


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
    #: The context variant this rollout's world was built from, "" for a task
    #: with one context. It is carried INTO the container (docs/implementation_plan/harness/VARIANTS_REQUESTS.md
    #: item 6) because everything the subject runs in there that re-resolves the
    #: task - `./ops/sched`, a `dbt` invocation, a generator the workspace
    #: ships - reads `ALKERA_TASK_VARIANT`, and a container that did not carry
    #: it would silently answer for `default` while the world around it is an
    #: arm. The world itself is built on the HOST, before the container exists,
    #: so this is about agreement and not about which world got built.
    variant: str = ""
    #: The task's environment profile (`alkera_gateway.plugs`), "" for none.
    #: With one, native-services serves only its services and the episode
    #: network carries only their aliases - see `profile_overlay`.
    profile: str = ""
    plugs: dict = field(default_factory=dict)
    container_id: str = ""
    image_digest: str = ""
    services: list = field(default_factory=list)
    warehouses: list = field(default_factory=list)
    services_container: str = ""
    gate: object = None
    #: The hard sandbox (README.md#the-subject-sandbox). False keeps the host-subject topology.
    subject_sandbox: bool = False
    subject_image: str = SUBJECT_IMAGE
    subject_container: str = ""
    proxy_container: str = ""
    rexec_token: str = field(default_factory=lambda: secrets.token_hex(16))
    #: The shared secret MinIO's audit webhook sends and native-services'
    #: receiver (:8110) requires, so a POST from the episode cannot forge
    #: `minio.*` rows (gateway/docker/evidence/CONTAINMENT_2026-10-07.md, the
    #: 8110 row). Minted per rollout and handed to compose in its process
    #: environment only (`compose_env`): it is in no file the rollout writes.
    minio_audit_token: str = field(default_factory=lambda: secrets.token_hex(32))
    #: What `down()` folded from the Postgres server's statement log
    #: (`postgres_live.fold_statement_log`): rows, or the `error` that makes the
    #: rollout a harness failure (rollout.py), or `skipped`. {} until `down()`.
    postgres_statements: dict = field(default_factory=dict)
    _up: bool = False
    #: `episode_subnet()`'s answer, kept: the overlay's ipam pin and the
    #: native-services variable must name the same /24.
    _episode_subnet: str = ""

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

    @property
    def service_state_dir(self) -> Path:
        """`<rollout>/service-state`: native-services' /service-state, the one
        directory of this stack only that container mounts (bistate.py)."""
        return bistate.service_state_dir(Path(self.rollout_dir).resolve())

    def compose_files(self) -> list[Path]:
        files = [COMPOSE_BASE, COMPOSE_OVERLAY, NATIVE_OVERLAY, self.overlay]
        return files + [SUBJECT_OVERLAY] if self.subject_sandbox else files

    # -------------------------------------------------------------- overlay
    def variant_env(self) -> dict:
        """`{ALKERA_TASK_VARIANT: <arm>}`, or `{}` for a one-context task.

        Empty rather than `""` on purpose: an empty value exported into the
        container is a value, and `taskspec.requested_variant` reads any
        non-empty string as a request. A task with one context must look to
        everything inside the box exactly as it did before variants existed.
        """
        return {"ALKERA_TASK_VARIANT": self.variant} if self.variant else {}

    def environment_profile(self):
        """The resolved profile, or None. Raises `episode.HarnessError`."""
        return episode.environment_profile(self.profile, self.plugs)

    def plugged_backends(self) -> tuple:
        """The registry backends this rollout's profile plugs
        (`alkera_gateway.backends.plugged`), in registry order; () unprofiled.
        Explicit-only: an unprofiled stack never starts one. Raises the
        registry's `ProfileError` for a profile that plugs two TLS backends,
        before a byte of the overlay is written."""
        profile = self.environment_profile()
        return () if profile is None else _backends().plugged(profile.slots)

    def backend_overlay(self, b, clock: str | None) -> str:
        """Backend `b`'s service for this rollout, "" for a hosted one (a
        module inside native-services, which `profile_overlay` aliases).

        Built from `b.dockerfile` on THIS episode's image (`BASE`), so the
        backend runs the alkera_gateway every other service of the stack runs.
        It mounts the rollout's audit directory (one 9.2.2 row per statement,
        destinations counted into its own file, folded in by `down`) and, when
        it seeds from the world, the world's warehouse FILES read-only under
        /seed. Readiness is read from /proc/net/tcp (the port in LISTEN), never
        dialled, for the reason native-services' healthcheck gives.

        A STATIC_BACKENDS name (Snowflake) already has its stanza in
        compose.yml + compose.native.yml, so only the per-rollout part is
        written here - byte for byte the overlay it always had. Any other
        backend gets the COMPLETE block (profile, build context, network and
        alias, its private data volume, the trust volume for a TLS backend),
        so it needs no edit to a static compose file.
        """
        if b.hosted:
            return ""
        backends = _backends()
        audit = Path(self.audit_dir).resolve().as_posix()
        volumes = [f'      - "{audit}:/audit"']
        if b.seeds_world:
            volumes += [f'      - "{(self.world_root / rel).as_posix()}:'
                        f'{SEED_IN_CONTAINER}/{Path(rel).name}:ro"'
                        for rel in self.warehouses]
        tag = self.image.rsplit(":", 1)
        image = f"alkera-{b.name}:native-{tag[1] if len(tag) == 2 else 'latest'}"
        upper = b.name.upper()
        env = {"ALKERA_NATIVE_AUDIT": "/audit/actions.jsonl",
               **({f"ALKERA_{upper}_SEED": SEED_IN_CONTAINER} if b.seeds_world else {}),
               f"ALKERA_{upper}_DESTINATIONS": f"/audit/{backends.destinations_file(b)}",
               # The backend's own statement log, in its private volume (never
               # /audit): the per-statement record the audit rows are checked
               # 1:1 against (snowflake_e2e_check).
               f"ALKERA_{upper}_STATEMENT_LOG": f"/{b.name}-data/statements.jsonl",
               **({CLOCK_ENV: clock} if clock else {})}
        healthcheck = f"""    healthcheck:
      test: ["CMD", "/usr/local/bin/python", "-c",
             "import pathlib,sys;sys.exit(0 if any(f[1].endswith(':{b.port:04X}') and f[3]=='0A' for f in (l.split() for l in pathlib.Path('/proc/net/tcp').read_text().splitlines()[1:])) else 1)"]
      interval: 2s
      timeout: 5s
      retries: 90
      start_period: 5s
"""
        if b.name in STATIC_BACKENDS:
            return f"""
# WAREHOUSE PLUG {b.name}: compose.yml's `{b.name}` service (compose profile
# `{b.name}`, enabled by compose_env), reachable as {b.host}:{b.port}
# (compose.native.yml), seeded from this world's warehouse and audited.
  {b.name}:
    image: {image}
    # Always built (layer-cached): the service runs this working tree's
    # {b.name}_start.py, never a stale image of an older one.
    pull_policy: build
    build:
      args:
        BASE: {self.image}
    stop_grace_period: 2s
    environment:{_env_lines(env)}
    volumes:
""" + "\n".join(volumes) + "\n" + healthcheck
        volumes.insert(1, f"      - {b.name}data:/{b.name}-data")
        if b.tls:
            volumes.append(f"      - {b.name}trust:{backends.trust_dir(b)}")
        seeded = ", seeded from this world's warehouse" if b.seeds_world else ""
        return f"""
# BACKEND {b.name} (alkera_gateway.backends): its own container, compose
# profile `{b.name}` (enabled by compose_env), reachable as {b.host}:{b.port}
# on episode_net{seeded} and audited.
  {b.name}:
    profiles: [{b.name}]
    image: {image}
    # Always built (layer-cached) from this working tree, never a stale image.
    pull_policy: build
    build:
      context: ../..
      dockerfile: gateway/docker/{b.dockerfile}
      args:
        BASE: {self.image}
    stop_grace_period: 2s
    networks:
      episode_net:
        aliases:
          - {b.host}
    environment:{_env_lines(env)}
    volumes:
""" + "\n".join(volumes) + "\n" + healthcheck

    def backend_volumes(self) -> str:
        """The top-level `volumes:` the plugged backends' blocks name, "" when
        there are none: a complete block's private data volume and a TLS
        backend's trust volume. STATIC_BACKENDS have theirs in compose.yml."""
        names = []
        for b in self.plugged_backends():
            if b.name in STATIC_BACKENDS:
                continue
            if not b.hosted:
                names.append(f"{b.name}data")
            if b.tls:
                names.append(f"{b.name}trust")
        if not names:
            return ""
        return "\nvolumes:\n" + "".join(f"  {name}:\n" for name in names)

    def episode_subnet(self) -> str:
        """The /24 pinned on episode_net, "" when nothing is pinned.

        Pinned only when native-services joins backend_net (a plugged
        HOSTED_ENGINES backend, `profile_overlay`): only then does an agent
        listener answer a peer that is not on episode_net, and the filter
        (serve.EPISODE_SUBNETS_ENV) must name episode_net's subnet before compose
        has created it. Every other stack - unprofiled above all - keeps
        compose's own allocation, and its overlay is the bytes it always was."""
        if not any(b.hosted and b.name in HOSTED_ENGINES for b in self.plugged_backends()):
            return ""
        if not self._episode_subnet:
            self._episode_subnet = str(free_episode_subnet(self.project, docker_subnets()))
        return self._episode_subnet

    def episode_network(self) -> str:
        """The top-level `networks:` that pins `episode_subnet()`, "" with none.
        compose.yml's `internal: true` is merged with it, not replaced."""
        subnet = self.episode_subnet()
        if not subnet:
            return ""
        return f"""
# PEER FILTER: native-services is on backend_net for the engine, so its agent
# listeners admit episode_net's peers only ({EPISODE_SUBNETS_ENV}, above). The
# subnet is pinned here because the filter needs it before the network exists.
networks:
  episode_net:
    ipam:
      config:
        - subnet: {subnet}
"""

    def fold_destinations(self) -> list[dict]:
        """Merge every plugged backend's destination counts
        (`backends.destinations_file`) into destinations.jsonl, by (host,
        port). native-services rewrites that file whole on every accept, so a
        second writer keeps its own file until the episode is over; this is
        the one place they become one log."""
        backends = _backends()
        audit = Path(self.audit_dir)
        extras = [audit / backends.destinations_file(b) for b in self.plugged_backends()]
        extras = [extra for extra in extras if extra.is_file()]
        if not extras:
            return []
        main = audit / "destinations.jsonl"
        rows = {}
        for source in (main, *extras):
            if source.is_file():
                for line in source.read_text(encoding="utf-8").splitlines():
                    if line.strip():
                        row = json.loads(line)
                        rows[(row.get("host"), row.get("port"))] = row
        folded = [rows[key] for key in sorted(rows, key=lambda k: (str(k[0]), k[1] or 0))]
        # Replaced, not rewritten in place: the file is native-services' (root
        # inside its container), the directory is the operator's.
        tmp = main.with_name(main.name + ".fold")
        tmp.write_text("".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n"
                               for r in folded), encoding="utf-8")
        os.replace(tmp, main)
        return folded

    def notebook_plugged(self) -> bool:
        """The profile plugs the notebook slot: compose.native.yml's `jupyter`
        service (profile `notebook`) runs, and the episode is told where."""
        profile = self.environment_profile()
        return profile is not None and "notebook" in profile.slots

    def minio_unstarted(self) -> bool:
        """A profiled box that plugs no `object_store`: MinIO is not started
        (`profile_overlay`) and the episode does not wait for it
        (`write_overlay`). Unprofiled boxes plug the store, as before."""
        profile = self.environment_profile()
        return profile is not None and "object_store" not in profile.slots

    def profile_variables(self) -> tuple[list[str], dict[str, str]] | None:
        """`(dropped, added)` for a profiled task, None for an unprofiled one.

        `dropped` are the static table's variables for what the profile
        EXCLUDES: every variable of an excluded one of the nine, `SCHED_URL`
        unless the orchestrator plugs `ops_sched`, the four `AWS_*` unless the
        object store is plugged - so nothing in the environment names an
        address that does not resolve. `added` are a plugged BI vendor's
        (`agent_under_test.plug_service_env` at its `.internal` alias), which no
        static file names because the static file is the unprofiled box.
        """
        profile = self.environment_profile()
        if profile is None:
            return None
        import agent_under_test as aut

        with_sched = "ops_sched" in profile.slots.get("orchestrator", ())
        with_store = "object_store" in profile.slots
        dropped = [var for name in aut.NATIVE_SERVICES
                   if name not in profile.services
                   for var in EPISODE_SERVICE_ENV[name]]
        if not with_sched:
            dropped.append("SCHED_URL")
        if not with_store:
            dropped += ["AWS_ENDPOINT_URL", "AWS_ACCESS_KEY_ID",
                        "AWS_SECRET_ACCESS_KEY", "AWS_DEFAULT_REGION"]
        # The backends follow the profile's PLUGS, as the world's own discovery
        # files do (world/native_workspace.py): a DuckDB box gets no PG*, no
        # SNOWFLAKE_*, and no AIRFLOW_* unless Airflow is its orchestrator.
        warehouses = set(profile.slots.get("warehouse", ()))
        for plug, names in BACKEND_ENV.items():
            slot = "orchestrator" if plug == "airflow" else "warehouse"
            plugged = profile.slots.get(slot, ()) if slot != "warehouse" else warehouses
            if plug not in plugged:
                dropped += list(names)
        added: dict[str, str] = {}
        backends = _backends()
        plugged_backends = self.plugged_backends()
        for b in plugged_backends:
            if b.tls:
                # The clients (the Snowflake connector, and dbt-snowflake
                # through it) verify the backend's certificate against this
                # bundle: certifi plus the local CA. One TLS backend at most
                # (`backends.plugged`), so one bundle.
                added["REQUESTS_CA_BUNDLE"] = backends.bundle(b)
            # Its agent-facing variables. A name the static table already
            # carries (SNOWFLAKE_HOST, AIRFLOW_API_URL, ...) keeps the static
            # line's position, but with THIS value: `profiled_environment`
            # lets what the profile plugs win.
            added.update(b.env)
        for name in profile.services:
            if name in aut.PLUG_SERVICES and name not in backends.BACKENDS:
                port = aut.PLUG_SERVICES[name][1]
                added.update(aut.plug_service_env(
                    name, f"http://{name}.internal:{port}"))
        return dropped, added

    def profiled_environment(self, service: str, own: dict[str, str]) -> str:
        """`service`'s COMPLETE environment under the profile, as overlay text.

        `environment: !override` and every variable restated: the overlay's own
        lines (`own`), then the static table (`static_environment`) minus
        `dropped`, then `added`. A merge cannot take a key away: `VAR: !reset
        null` lines were the mechanism until 2026-10-04 and `docker compose
        config` on Compose v5.6.0 measured them doing nothing on the episode -
        SCHED_URL, METABASE_URL, PROMETHEUS_URL, AWS_ENDPOINT_URL ... all still
        set for a da/laptop box whose network resolves none of them.
        `!override` (Compose >= 2.24, which the README already requires) is the
        one form that works on both, and `docker compose config` is how it was
        checked. An unprofiled overlay never takes this path, so its bytes are
        the plain merge they always were.
        """
        dropped, added = self.profile_variables()
        static = static_environment(service)
        # What the profile plugs wins over the static table: a plugged
        # backend's variable that the static table also carries (Airflow's
        # AIRFLOW_API_URL, served elsewhere once Airflow is a registry backend)
        # takes the static line's place with the backend's value. Snowflake's
        # values are the static ones, so its bytes do not move.
        kept = {k: added.get(k, v) for k, v in static.items()
                if k not in dropped and k not in own}
        plugged = {k: v for k, v in added.items() if k not in own and k not in kept}
        gone = sorted(set(dropped) & set(static))
        note = textwrap.wrap(
            "the static table (compose.yml + compose.native.yml) minus what the "
            "profile excludes: " + (", ".join(gone) or "nothing"), 70)
        lines = ["    # ENVIRONMENT PROFILE: the complete mapping, not a merge",
                 "    # (container_episode.profiled_environment).",
                 "    environment: !override"]
        text = "\n".join(lines) + _env_lines(own)
        text += "".join(f"\n      # {line}" for line in note) + _env_lines(kept)
        if plugged:
            text += "\n      # what the profile plugs" + _env_lines(plugged)
        return text

    def profile_overlay(self) -> str:
        """The services after `episode` for a profiled task, "" for an
        unprofiled one.

        An excluded service must not RESOLVE on episode_net, not merely answer
        404 from a native-services process that did not mount it, so the alias
        list is replaced (`!override`) with the profile's own. `sched.internal`
        stays only when the orchestrator plugs `ops_sched`. MinIO runs only
        when the object store is plugged: otherwise it is gated behind a compose
        profile nothing enables (MINIO_COMPOSE_PROFILE), so it is not started,
        and `write_overlay` resets the episode's `depends_on: minio`. Dropping
        only its `minio.internal` alias, which is what this did from 2026-10-04,
        left the compose name `minio` resolving on episode_net to a MinIO that
        no receiver audited (gateway/docker/evidence/CONTAINMENT_2026-10-07.md,
        finding 1). The environment that matches is `profiled_environment`'s.

        A plugged BI vendor (`agent_under_test.PLUG_SERVICES`) goes the other
        way: its alias is ADDED to the replaced list, because
        compose.native.yml names no vendor at all - the static file is the
        unprofiled box. So is a plugged HOSTED backend's (`backends.Backend`
        with `hosted=True`, a module inside native-services), and a hosted TLS
        backend gets its trust volume here, writable, to publish its bundle
        into (`native.tls.mint`); the episode mounts the same volume read-only.
        """
        profile = self.environment_profile()
        if profile is None:
            return ""
        import agent_under_test as aut

        with_sched = "ops_sched" in profile.slots.get("orchestrator", ())
        vendors = [name for name in profile.services if name in aut.PLUG_SERVICES]
        aliases = ["native-services.internal",
                   *(f"{name}.internal" for name in profile.services
                     if name in aut.NATIVE_SERVICES or name in vendors)]
        hosted = [b for b in self.plugged_backends() if b.hosted]
        aliases += [b.host for b in hosted if b.host not in aliases]
        if with_sched:
            aliases.append("sched.internal")
        alias_lines = "\n".join(f"          - {a}" for a in aliases)
        body = f"""
# ENVIRONMENT PROFILE {profile.name} (plugs: {json.dumps(self.plugs, sort_keys=True)}).
# native-services runs with --profile (ALKERA_NATIVE_PROFILE_ARGS), and the
# aliases below REPLACE compose.native.yml's: an excluded service has no name
# on episode_net at all.
  native-services:
    networks:
      episode_net:
        aliases: !override
{alias_lines}
"""
        engines = [b for b in hosted if b.name in HOSTED_ENGINES]
        if engines:
            body += "      backend_net: {}\n"
        trust = [b for b in hosted if b.tls]
        if trust:
            body += "    volumes:\n" + "".join(
                f"      - {b.name}trust:{_backends().trust_dir(b)}\n" for b in trust)
        if engines:
            body += self.engine_overlay(engines)
        if self.minio_unstarted():
            body += f"""
# NO OBJECT STORE: this profile plugs no object_store, so MinIO is NOT STARTED.
# Its compose profile `{MINIO_COMPOSE_PROFILE}` is one nothing enables, and the
# episode's `depends_on: minio` is reset above. A service that does not run
# cannot be resolved or reached from any container of this project; with only
# its alias gone, the compose name `minio` still resolved, and no receiver
# audited what reached it (CONTAINMENT_2026-10-07.md, finding 1).
  minio:
    profiles: [{MINIO_COMPOSE_PROFILE}]
"""
        return body

    def engine_overlay(self, engines) -> str:
        """The rest of native-services' block for plugged HOSTED_ENGINES
        backends - each engine's address (`ALKERA_<NAME>_UPSTREAM`, which the
        module forwards to and seeds through at import) and the start order -
        then each engine's own block. The engine has no healthcheck (goccy's
        image has no shell to run one in, and a probe would be a dial): it only
        has to be STARTED, because the module polls it before seeding and
        native-services binds nothing until the seed is in."""
        text = "    environment:\n" + "".join(
            f"      ALKERA_{b.name.upper()}_UPSTREAM: "
            f"{'' if b.name in HOSTED_ENGINE_BUILDS else 'http://'}"
            f"{HOSTED_ENGINES[b.name][0]}:{HOSTED_ENGINES[b.name][1]}\n"
            for b in engines)
        # On backend_net its agent listeners answer the engine, postgres and
        # minio too; they admit episode_net's pinned subnet only (`episode_network`).
        text += f"      {EPISODE_SUBNETS_ENV}: {self.episode_subnet()}\n"
        text += "    depends_on:\n" + "".join(
            f"      {HOSTED_ENGINES[b.name][0]}:\n        condition: service_started\n"
            for b in engines)
        if any(b.name in HOSTED_ENGINE_BUILDS for b in engines):
            # A JVM engine's cold start (Spark: ~30 s, plus its seed) is longer
            # than compose.native.yml's 60 s of retries, and native-services binds
            # nothing until it answers; failures in the start period do not count,
            # and the first pass ends it.
            text += "    healthcheck:\n      start_period: 600s\n"
        for b in engines:
            service, _, command = HOSTED_ENGINES[b.name]
            if b.name in HOSTED_ENGINE_BUILDS:
                text += self.engine_build_overlay(b, service)
                continue
            args = ", ".join(json.dumps(arg.format(**b.env)) for arg in command)
            text += f"""
# HOSTED ENGINE {b.name}: compose.yml's `{service}` service (compose profile
# `{service}`, enabled by compose_env), on backend_net only. The episode reaches
# it through the audited proxy in native-services, {b.host}:{b.port}.
  {service}:
    command: [{args}]
    networks:
      backend_net:
        aliases: !reset []
"""
        return text

    def engine_build_overlay(self, b, service: str) -> str:
        """A HOSTED_ENGINE_BUILDS engine's complete service: built from this
        working tree (layer-cached, never a stale image), under its own compose
        profile, on backend_net only with no alias there (native-services dials
        it by its service name), the world's warehouse FILES read-only at /seed.
        No healthcheck: the module in native-services waits for the engine's
        port before native-services binds anything, and the engine binds only
        once its seed is in."""
        tag = self.image.rsplit(":", 1)
        image = f"alkera-{service}:native-{tag[1] if len(tag) == 2 else 'latest'}"
        volumes = "\n".join(
            f'      - "{(self.world_root / rel).as_posix()}:'
            f'{SEED_IN_CONTAINER}/{Path(rel).name}:ro"' for rel in self.warehouses)
        return f"""
# HOSTED ENGINE {b.name}: its own image (gateway/docker/{HOSTED_ENGINE_BUILDS[b.name]}),
# compose profile `{service}` (enabled by compose_env), on backend_net only and
# seeded from this world's warehouse. The episode reaches it through the audited
# proxy in native-services, {b.host}:{b.port}.
  {service}:
    profiles: [{service}]
    image: {image}
    pull_policy: build
    build:
      context: ../..
      dockerfile: gateway/docker/{HOSTED_ENGINE_BUILDS[b.name]}
    stop_grace_period: 5s
    networks:
      backend_net:
        aliases: !reset []
    volumes:
{volumes}
"""

    def write_overlay(self) -> Path:
        """The per-rollout overlay: warehouse binds, shim PATH, the world's
        clock, and - under an environment profile - the profile's network and
        environment.

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
        # Asked first: a profile that plugs two TLS backends stops here.
        plugged_backends = self.plugged_backends()
        notebook = self.notebook_plugged()
        clock = world_clock(self.world)
        # The shim watches the same files it cannot see through the workspace
        # snapshot, so a `dbt build` that rewrites a warehouse names it
        # (shims/run.py ALKERA_SHIM_WATCH; blindness.UNSCOPED_WAREHOUSE_SHIMS).
        watch = ":".join(f"{WORLD_IN_CONTAINER}/{rel}" for rel in self.warehouses)
        # The overlay's own variables, for the episode and (minus JUPYTER_URL)
        # for jupyter, in the order they have always been written.
        own = {"PATH": f"{SHIMS_IN_CONTAINER}:{image_path(self.image)}",
               "ALKERA_SHIM_BIN": SHIMS_IN_CONTAINER,
               "ALKERA_SHIM_WATCH": watch,
               "ALKERA_SHIM_WORKSPACE": WORKSPACE_IN_CONTAINER,
               "ALKERA_WORKSPACE": WORKSPACE_IN_CONTAINER,
               **({CLOCK_ENV: clock} if clock else {}),
               **dict(sorted(self.variant_env().items()))}
        episode_own = {**own, **({"JUPYTER_URL": "http://jupyter.internal:8888"}
                                 if notebook else {})}
        profiled = self.environment_profile() is not None

        def environment(service: str, mapping: dict[str, str]) -> str:
            if profiled:
                return self.profiled_environment(service, mapping)
            return "    environment:" + _env_lines(mapping)

        binds = "\n".join(
            f'      - "{(self.world_root / rel).as_posix()}:'
            f'{WORLD_IN_CONTAINER}/{rel}"'
            for rel in self.warehouses)
        for b in plugged_backends:
            if b.tls:
                # Public certificates only (the key stays with the backend).
                binds += f"\n      - {b.name}trust:{_backends().trust_dir(b)}:ro"
        containers = [b for b in plugged_backends if not b.hosted]
        waits = "".join(f"      {b.name}:\n        condition: service_healthy\n"
                        for b in containers)
        if self.minio_unstarted():
            # Not started on this box (`profile_overlay`), so not waited for.
            # `!reset` on this one key, measured with `docker compose config`
            # on Compose v5.6.0: the entry is gone and the rest of the static
            # mapping (postgres, native-services) stays.
            waits += "      minio: !reset null\n"
        depends_on = "    depends_on:\n" + waits if waits else ""
        body = f"""# Generated per rollout by tasks/harness/container_episode.py. Do not edit:
# it is rewritten on every `up()` and is kept here as the record of which host
# files this episode's container could see.
#
# Three things compose.native.yml cannot say statically:
#
#   1. THE WAREHOUSE FILES. Their names and their number come from the world
#      this rollout built ({len(self.warehouses)} here), read by the same rule
#      world/world/fingerprint.py hashes by. Each is bound as a FILE at its own
#      world-relative path, so `/world/{WAREHOUSE_REL_DIR}` is a directory of
#      the CONTAINER's - and a `cp` beside the warehouse, which is what two of
#      five GT001 native subjects did before deleting rows, lands there instead
#      of in the world.
#   2. THE SHIM PATH, and the CONTEXT VARIANT. The shims go in front of the
#      image's own PATH, which is read off the image so /opt/toolbelt/bin
#      survives; `ALKERA_TASK_VARIANT` appears only when this world was built
#      from an arm, because an empty value is still a value to
#      `taskspec.requested_variant`.
#   3. THE WORLD'S CLOCK. `{CLOCK_ENV}` is this world's `vnow` as the
#      episode starts, so the rows the shims and the notebook kernel write
#      carry the world's time and not GatewayConfig's literal: `var/sched`,
#      where the host topology reads it, is not mounted here (P7).
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
{environment("episode", episode_own)}
    volumes:
{binds}
""" + depends_on + self.profile_overlay() + "".join(
            self.backend_overlay(b, clock) for b in plugged_backends)
        if notebook:
            # The kernel is the agent's own interpreter in a second container,
            # so it gets the box the shell gets: the shim PATH (a `!duckdb` in a
            # cell is audited by the same shim), the watch list (a cell that
            # rewrites a warehouse through the `duckdb` library names it), the
            # world's clock, the warehouse FILE binds and the same profile.
            body += f"""
# NOTEBOOK PLUG: the `jupyter` service (compose profile `notebook`, enabled by
# compose_env) sees exactly what the episode sees.
  jupyter:
{environment("jupyter", own)}
    volumes:
{binds}
"""
        body += self.backend_volumes() + self.episode_network()
        self.container_dir.mkdir(parents=True, exist_ok=True)
        with open(self.overlay, "w", encoding="utf-8", newline="") as stream:
            stream.write(body)
        return self.overlay

    # ------------------------------------------------------------- lifecycle
    def compose_env(self) -> dict:
        # compose profile `notebook` starts the `jupyter` service, and each
        # plugged backend with a container of its own starts under its name
        # (compose profile `snowflake` for compose.yml's stanza, the profile
        # `backend_overlay` declares for any other). A hosted backend whose
        # engine is a compose.yml service (HOSTED_ENGINES: BigQuery's emulator)
        # starts that service under its profile.
        profiles = [name for name, on in (("notebook", self.notebook_plugged()),)
                    if on] + [b.name for b in self.plugged_backends() if not b.hosted] + [
            HOSTED_ENGINES[b.name][0] for b in self.plugged_backends()
            if b.hosted and b.name in HOSTED_ENGINES]
        return {
            **os.environ,
            "ALKERA_EPISODE_WORLD": self.world_root.as_posix(),
            "ALKERA_EPISODE_AUDIT": Path(self.audit_dir).resolve().as_posix(),
            # native-services' own state (the BI vendors' published content,
            # the config the services read): mounted into that service ONLY,
            # and where bistate.source_for stages `{bi_state}` from.
            "ALKERA_EPISODE_SERVICE_STATE": self.service_state_dir.as_posix(),
            "ALKERA_EPISODE_IMAGE": self.image,
            "ALKERA_SUBJECT_IMAGE": self.subject_image,
            "ALKERA_REXEC_TOKEN": self.rexec_token,
            # MinIO's webhook sends it and native-services' receiver requires
            # it (compose.native.yml interpolates it into those two services
            # only). Set on every stack; a box with no object store runs
            # neither, and the value goes unused.
            MINIO_AUDIT_TOKEN_ENV: self.minio_audit_token,
            # `--profile ... --plug ...` on the native-services command line
            # (compose.native.yml). Always set, so an empty value from this
            # process and not one inherited from the caller's shell is what an
            # unprofiled stack runs with.
            "ALKERA_NATIVE_PROFILE_ARGS": shlex.join(
                episode.profile_argv(self.profile, self.plugs)),
            # Set only when the environment profile plugs one of `profiles`,
            # appended to whatever the caller's shell already enables.
            **({"COMPOSE_PROFILES": ",".join(
                [p for p in os.environ.get("COMPOSE_PROFILES", "").split(",") if p]
                + profiles)} if profiles else {}),
        }

    def _compose(self, *args, check: bool = True, timeout: int = 900):
        env = self.compose_env()
        argv = ["docker", "compose", "-p", self.project]
        for path in self.compose_files():
            argv += ["-f", str(path)]
        if not hasattr(self, "_selinux"):
            self._selinux = selinux_overlay(
                argv, env, self.container_dir / "compose.selinux.yml")
        if self._selinux is not None:
            argv += ["-f", str(self._selinux)]
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
        # Made here, by the operator, so the bind source is theirs and not a
        # directory the engine creates as root.
        self.service_state_dir.mkdir(parents=True, exist_ok=True)
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
        if self.subject_sandbox:
            by_name = {s["Config"]["Labels"]["com.docker.compose.service"]: s
                       for s in specs}
            missing = {"subject", "egress-proxy"} - set(by_name)
            if missing:
                raise ContainerError(f"--subject-sandbox container: no "
                                     f"{sorted(missing)} in {self.services}")
            self.subject_container = by_name["subject"]["Id"]
            self.proxy_container = by_name["egress-proxy"]["Id"]
        return self.container_id

    def down(self) -> None:
        if self.gate is not None:
            self.gate.stop()
            self.gate = None
        if not self._up:
            return
        # FIRST, while every container is still up: the Postgres server's
        # statement log becomes `psql.statement` rows (postgres_live). The
        # tailer appends through native.audit.record, which takes each `seq`
        # under <audit>/writer.lock like every other writer, so the sequence
        # stays contiguous; the rows are in actions.jsonl before ownership is
        # handed back, before `compose down -v` deletes the log with its
        # volume, and before destinations are folded or a grader reads /audit.
        # Whatever it raises (a timed-out `docker exec` included) is kept, not
        # thrown: the rest of the teardown always runs, and the error is raised
        # once the stack is gone.
        import postgres_live

        try:
            self.postgres_statements = postgres_live.fold_statement_log(
                self, clock=world_clock(self.world))
        except Exception as exc:  # noqa: BLE001 - raised below, after teardown
            self.postgres_statements = {"rows": None,
                                        "error": f"{type(exc).__name__}: {exc}"}
        if self.proxy_container:
            # Every connection the subject tried to make off the box, kept
            # beside the mounts overlay as part of the rollout's record.
            logs = _run(["docker", "logs", self.proxy_container], check=False)
            self.container_dir.mkdir(parents=True, exist_ok=True)
            (self.container_dir / "egress.jsonl").write_text(
                logs.stdout, encoding="utf-8")
        self.ownership = release_ownership(self.container_id)
        # The services' own state is written by native-services as ITS root,
        # and the episode does not mount it, so its files are handed back from
        # there: the grader stages them as the operator after teardown.
        self.ownership_service_state = release_ownership(
            self.services_container, SERVICE_STATE_IN_SERVICES)
        self._compose("down", "-v", "-t", "2", "--remove-orphans",
                      check=False, timeout=300)
        # AFTER down: native-services rewrites destinations.jsonl once more
        # from its signal handler, so a fold before it would be overwritten.
        if self.plugged_backends():
            self.destinations_folded = self.fold_destinations()
        self._up = False
        if self.postgres_statements.get("error"):
            # Loud, and only once the stack is gone: an action log missing the
            # Postgres statements is not this rollout's record.
            raise ContainerError(
                f"postgres statement log not folded into the action log: "
                f"{self.postgres_statements['error']}")

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
        # The world's warehouse.* into Postgres, only for a profile that plugs
        # it (postgres_live.seed); an unprofiled box's server stays compose's.
        import postgres_live

        seeded = postgres_live.seed(self)
        if seeded is not None:
            record["postgres_seed"] = seeded
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

        A profile that plugs a backend with a dbt adapter (Snowflake) gets the
        BOX profile instead (`world.native_workspace.box_profiles_yaml`): the
        same re-rooted DuckDB targets, plus a target for the backend that is
        the default, so a bare `dbt` on a Snowflake box talks to Snowflake.
        Anything else is staged byte for byte as before.
        """
        source = Path(self.world.get("profiles_dir") or "") / "profiles.yml"
        if not source.is_file():
            return []
        rewritten, moved = reroot_profiles(source.read_text(encoding="utf-8"),
                                           self.world_root)
        plugged = [b.name for b in self.plugged_backends()]
        if plugged:
            if str(episode.TRACK_A) not in sys.path:
                sys.path.insert(0, str(episode.TRACK_A))
            from world.native_workspace import box_profiles_yaml

            rewritten = box_profiles_yaml(rewritten, plugged)
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

        return aut.profile_endpoints(aut.resolve_endpoints(None),
                                     self.environment_profile())

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

        profile = self.environment_profile()
        targets = {name: (f"{name}.internal", port)
                   for name, (_, port) in {
                       **aut.NATIVE_SERVICES,
                       # The scheduler plane is relayed like the nine, because
                       # the gate's `sched` check is a `/health` here and not a
                       # read of the world's state file: the subject cannot
                       # reach that file, so neither may the gate.
                       **aut.NATIVE_WORKSPACE_SERVICES}.items()}
        targets["s3"] = ("minio.internal", MINIO_PORT)
        # A plugged BI vendor is relayed like the nine - only when plugged, so
        # an unprofiled gate binds exactly the sockets it always did.
        for name, (_, port) in aut.PLUG_SERVICES.items():
            if profile is not None and name in profile.services:
                targets[name] = (f"{name}.internal", port)
        # Only what the profile serves: a relay to an alias that does not
        # resolve would be a socket that answers nothing.
        served = aut.profile_endpoints(dict.fromkeys(targets, ""), profile)
        targets = {name: target for name, target in targets.items()
                   if name in served}
        self.gate = ServiceGate(self.services_container, targets,
                                _native_probe().CONTAINER_PYTHON)
        overrides = self.gate.start()
        self.container_dir.mkdir(parents=True, exist_ok=True)
        path = self.container_dir / "gate_endpoints.json"
        with open(path, "w", encoding="utf-8", newline="") as stream:
            json.dump(overrides, stream, indent=2, sort_keys=True)
        return aut.profile_endpoints(aut.resolve_endpoints(path), profile)

    # ----------------------------------------------------------------- shell
    def shell(self, bin_dir: Path, env: dict | None = None) -> dict:
        """The subject's shell redirect for THIS container, and its proof.

        One method, two callers - the scripted subject and `agent_under_test`
        for a model - so the thing verified before the terminal is handed over
        is the thing the subject then uses, rather than something that resembles
        it.
        """
        if self.subject_sandbox:
            return self.subject_shell(env)
        wrapper = write_bash_wrapper(bin_dir, container=self.container_id,
                                     interpreter=str(_interpreter()),
                                     env=env or {})
        wrapper["proof"] = verify_container_execution(wrapper,
                                                      self.container_id)
        return wrapper

    # ------------------------------------------------------------ hard sandbox
    def start_rexec(self, env: dict | None = None) -> None:
        """Start the command bridge in the episode container and wait for it.

        Started with `docker exec -d`, so it inherits the container's own
        environment (the shim PATH, the endpoint variables) exactly as the
        host topology's per-command `docker exec` did; `env` adds what that
        topology passed with `-e`. The interpreter is named absolutely because
        `python` on the episode's PATH is the audited shim.
        """
        env = dict(env or {})
        if getattr(self, "_rexec_started", False):
            if env == self._rexec_env:
                return
            # A second caller with its own variables (the scripted subject, after
            # rollout.py's proof) gets a bridge that carries them, as the host
            # topology's per-shell `-e` did. Reusing the first bridge dropped
            # them silently: measured, `ALKERA_WAREHOUSE: parameter not set`.
            self.exec_(["pkill", "-f", f"{REXEC_IN_CONTAINER} serve"], check=False)
            time.sleep(0.5)
            self._rexec_started = False
        self._rexec_env = env
        self.exec_(["mkdir", "-p", str(Path(REXEC_IN_CONTAINER).parent)])
        self.copy_in(REXEC_SOURCE, REXEC_IN_CONTAINER)
        flags = ["-d", "-w", WORKSPACE_IN_CONTAINER,
                 "-e", f"ALKERA_REXEC_TOKEN={self.rexec_token}"]
        for name, value in sorted((env or {}).items()):
            flags += ["-e", f"{name}={value}"]
        _run(["docker", "exec", *flags, self.container_id,
              "/usr/local/bin/python", REXEC_IN_CONTAINER, "serve",
              "--port", str(REXEC_PORT), "--workdir", WORKSPACE_IN_CONTAINER])
        wait = ("import socket,sys,time\n"
                "for _ in range(60):\n"
                "    try:\n"
                f"        socket.create_connection(('episode',{REXEC_PORT}),2).close(); sys.exit(0)\n"
                "    except OSError: time.sleep(0.5)\n"
                "sys.exit(1)")
        r = _run(["docker", "exec", self.subject_container, "python", "-c", wait],
                 check=False, timeout=120)
        if r.returncode != 0:
            raise ContainerError("the rexec bridge did not come up on "
                                 f"episode:{REXEC_PORT}: {r.stderr[-800:]}")
        self._rexec_started = True

    def subject_sandbox_probe(self, host_paths: list[str] | None = None) -> dict:
        """What the subject container can reach, read from inside it.

        Every property the sandbox claims, measured rather than read off the
        compose file: no capabilities, not root, read-only root, no default
        route, no engine socket, no host path, no direct egress or public
        DNS, the proxy refusing a host off the allowlist and passing the API,
        the bridge reachable and the episode's services NOT (they are the
        episode's; the subject reaches them only through its commands).
        """
        allow = os.environ.get("ALKERA_EGRESS_ALLOW", "api.anthropic.com:443")
        spec = {"sockets": list(ENGINE_SOCKETS), "rexec_port": REXEC_PORT,
                "api": allow.split(",")[0].strip(),
                "host_paths": [p for p in (host_paths or []) if p]}
        script = f"SPEC = {json.dumps(spec)}\n" + SUBJECT_PROBE
        r = _run(["docker", "exec", "-i", self.subject_container, "python", "-"],
                 stdin_text=script, check=False, timeout=180)
        try:
            seen = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"verified": False, "error": (r.stderr or r.stdout)[-800:]}
        checks = {
            "not_root": seen["uid"] != 0,
            "no_capabilities": int(seen["cap_eff"] or "0", 16) == 0,
            "no_new_privileges": seen["no_new_privs"] == "1",
            "root_read_only": seen["root_read_only"],
            "no_default_route": not seen["default_route"],
            "no_engine_socket": not seen["engine_sockets"],
            "no_host_paths": not seen["host_paths_visible"],
            "no_direct_egress": not any(seen["direct_egress"].values()),
            "no_public_dns": not seen["public_dns"],
            "proxy_refuses_off_allowlist": seen["proxy_refuses_other"] == "403",
            "proxy_passes_api": seen["proxy_allows_api"] == "200",
            "bridge_reachable": seen["episode_bridge"],
            "episode_services_unreachable": not seen["episode_services_reachable"],
        }
        return {"checks": checks, "observed": seen,
                "verified": all(checks.values()),
                "measured_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    def subject_shell(self, env: dict | None = None,
                      host_paths: list[str] | None = None) -> dict:
        """The sandboxed counterpart of `write_bash_wrapper` + its proof.

        `launcher_argv` is how a caller starts the subject's bash: the scripted
        subject appends `-lc <command>`, `agent_under_test` swaps `bash` for
        `claude`. Proof is `hostname` through both bash forms the CLI uses,
        landing in the episode, AND the sandbox probe; `verified` needs both.
        """
        self.start_rexec(env)
        expected = self.exec_(["hostname"]).stdout.strip()
        observed = {}
        for form in ("-c", "-lc"):
            r = _run(["docker", "exec", "-w", SUBJECT_CWD, self.subject_container,
                      "bash", form, "hostname"], check=False, timeout=120)
            observed[form] = (r.stdout.strip().splitlines() or [""])[-1]
        sandbox = self.subject_sandbox_probe(host_paths)
        verified = (bool(expected) and all(v == expected for v in observed.values())
                    and sandbox["verified"])
        return {
            "mode": "subject_container",
            "container": self.subject_container,
            "launcher": "docker",
            "launcher_argv": ["docker", "exec", "-i", "-w", SUBJECT_CWD,
                              self.subject_container, "bash"],
            "cwd": SUBJECT_CWD,
            "subject_env": {},
            "proof": {"how": "bash -c / bash -lc hostname inside the subject "
                             "container, forwarded by rexec",
                      "expected": expected, "observed": observed,
                      "sandbox": sandbox, "verified": verified},
        }

    # -------------------------------------------------------------- evidence
    def containment_probe(self, *, when: str, canaries: list[str],
                          grading: Path, rollout_dir: Path) -> dict:
        """Containment P7 for THIS rollout's container.

        The probe and its spec live in `gateway/docker/native_episode_probe.py`,
        which `gateway/docker/containment.py --native-episode` also runs, so a
        rollout's evidence and the repository's evidence file are the same
        measurement rather than two that resemble each other.

        Profile-aware, by the probe's own `expectations` (serve.py's rules):
        every service this box serves - the nine or the profile's own, plugged
        BI vendors included - must answer; every one it excludes must not even
        resolve; the scheduler plane is asked for jobs only when `ops_sched` is
        plugged, and otherwise its check is recorded as not applicable WITH the
        reason and `sched.internal` joins the must-not-resolve list. Plus the
        services' own state (question H): not mounted in this container, and -
        read off `docker inspect` of the whole project - mounted read-write in
        native-services and in no other container
        (`services_state_is_mounted_only_in_native_services`).
        """
        probe = _native_probe()
        expected = probe.expectations(self.profile or None, self.plugs)
        spec = probe.spec(
            warehouses=self.warehouses,
            services=expected["services"],
            excluded=expected["excluded"],
            scheduler=expected["scheduler"],
            not_applicable=expected["not_applicable"],
            service_state={"targets": [SERVICE_STATE_IN_SERVICES],
                           "roots": [self.service_state_dir.as_posix()]},
            forbidden_files=[
                Path(grading, "manifest.yaml").as_posix(),
                Path(grading, "expected.duckdb").as_posix()],
            host_paths=[self.world_root.as_posix(),
                        Path(grading).resolve().as_posix(),
                        Path(rollout_dir).resolve().as_posix(),
                        REPO.as_posix(), "/c", "/host_mnt", "/mnt/c"],
            canaries=canaries,
            repo_root=REPO)
        evidence = probe.run(self.container_id, spec)
        isolation = probe.service_state_isolation(self._project_specs(),
                                                  self.service_state_dir)
        evidence["service_state_mounts"] = isolation
        evidence.setdefault("checks", {})[SERVICE_STATE_STACK_CHECK] = \
            isolation["isolated"]
        evidence["contained"] = all(evidence["checks"].values())
        evidence["expectations"] = expected
        evidence["when"] = when
        evidence["probe_source"] = ("stdin to `python -`; never written to the "
                                    "container disk")
        evidence["image"] = self.image
        evidence["image_digest"] = self.image_digest
        evidence["container_id"] = self.container_id[:12]
        evidence["services_live"] = self.services
        return evidence

    def _project_specs(self) -> list[dict]:
        """`docker inspect` of every container in this compose project."""
        ids = _run(["docker", "ps", "-q", "--filter",
                    f"label=com.docker.compose.project={self.project}"],
                   check=False).stdout.split()
        return [json.loads(_run(["docker", "inspect", cid]).stdout)[0] for cid in ids]

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
            "variant": self.variant,
            **({"environment_profile": {"profile": self.profile,
                                        "plugs": self.plugs}}
               if self.profile else {}),
            "shims_in_container": SHIMS_IN_CONTAINER,
            "subject_sandbox": ({"image": self.subject_image,
                                 "subject_container": self.subject_container[:12],
                                 "proxy_container": self.proxy_container[:12],
                                 "egress_allow": os.environ.get(
                                     "ALKERA_EGRESS_ALLOW", "api.anthropic.com:443")}
                                if self.subject_sandbox else None),
        }


def _interpreter() -> Path:
    """The interpreter this harness runs episodes under (`episode.PY`).

    Imported here rather than at module import time: `episode` is already a
    dependency of this module, and the indirection keeps the one place that
    knows the venv layout in one file.
    """
    return episode.PY


#: P7's scheduler check (`native_episode_probe.SCHEDULER_CHECK`). On a box
#: whose profile plugs no `ops_sched` it is not asked: the probe records it
#: under `checks_not_applicable` with the reason and asserts instead that
#: `sched.internal` does not resolve (`native_episode_probe.expectations`).
#: Until 2026-10-04 the harness popped it AFTER the probe had answered False
#: (`_scheduler_not_served`), which recorded the omission but asserted nothing
#: in its place; an orchestrator-plugging or unprofiled box was never affected
#: and still is not.
SCHEDULER_CHECK = "the_scheduler_plane_answers_with_jobs"
#: The stack half of P7's question H, added to `checks` by `containment_probe`.
SERVICE_STATE_STACK_CHECK = "services_state_is_mounted_only_in_native_services"


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
    are in `docs/implementation_plan/harness/NATIVE_CONTAINER.md`:

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
        one.add_argument("--skip-prepare", action="store_true",
                         help="do not run the builder's --prepare-only first: "
                              "rollout.py never does, and a builder without that "
                              "mode (BI001's) refuses it")
    args = parser.parse_args(argv)

    import taskspec

    spec = taskspec.load(args.task,
                         Path(args.tasks_dir).resolve() if args.tasks_dir else None)
    root = Path(args.dir).resolve()
    manifest = root / "world.json"
    if args.mode == "native-up":
        root.mkdir(parents=True, exist_ok=True)
        if not args.skip_prepare:
            episode.prepare_task(spec)
        world = episode.build_world(spec, root / "world", root / "grading")
        manifest.write_text(json.dumps(world, indent=2), encoding="utf-8")
    elif not manifest.is_file():
        raise ContainerError(f"{manifest} does not exist; nothing to take down")
    else:
        world = json.loads(manifest.read_text(encoding="utf-8"))

    # The task's own environment profile and arm, exactly as rollout.py builds
    # the stack: P7 on a profiled task (BI001's da/laptop + three BI vendors)
    # is a claim about the box that task is served, not the unprofiled one.
    stack = NativeContainerEpisode(
        project=args.project, world=world, audit_dir=root / "audit",
        rollout_dir=root, image=args.image, variant=spec.variant or "",
        profile=getattr(spec, "profile", "") or "",
        plugs=dict(getattr(spec, "plugs", None) or {}))
    if args.mode == "native-down":
        stack._up = True                      # `down` on a stack this process did not start
        # ...whose container ids this process never learned. Found by compose
        # label, so `down` can hand the operator back what the episode and
        # native-services wrote into the binds as root.
        for spec in stack._project_specs():
            service = spec["Config"]["Labels"].get("com.docker.compose.service")
            if service == "episode":
                stack.container_id = spec["Id"]
            elif service == "native-services":
                stack.services_container = spec["Id"]
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
