#!/usr/bin/env python3
"""Host-topology bring-up for `--driver native`: the stack, not the subject.

`docs/implementation_plan/harness/NATIVE_DRIVER.md` describes a native episode as a subject with
bash, an environment full of addresses, and nine vendor-shaped services behind
`<name>.internal`. Those aliases exist inside the episode network and nowhere
else, which is what makes a native episode pointed at the defaults fail to
connect rather than reach something real - and it is also why the driver could
not be run on a laptop at all until this module existed. Here the same nine
services are served on loopback and the episode is handed an endpoint map
(`--native-endpoints`) instead of a DNS zone.

WHAT IT BRINGS UP, AND WHAT IT DOES NOT
---------------------------------------
* `alkera_gateway.native.serve` as a child process: all nine services, the
  aggregate `/health` and MinIO's audit receiver, one port each, bound to
  127.0.0.1. It writes this episode's `actions.jsonl` and `destinations.jsonl`.
* The PATH-front shims (`duckdb`, `dbt`, `python`, `git`), materialised into the
  rollout directory and prepended to this PROCESS's `PATH` - so every subject
  this process starts inherits them, the scripted one and a model alike,
  without `agent_under_test.run` needing an argument it does not have.
* MinIO, in docker, ONLY when the task REQUIRES the object-storage surface.
  Most tasks do not: GT001 declares an `s3_list` probe and requires nothing, and
  starting a container per rollout for a surface the task does not require is a
  minute of docker per rollout bought for nothing.
* NOT Postgres. The 2026-09-16 amendment to `gateway/NATIVE.md` keeps
  the warehouse a DuckDB FILE for the existing tasks, so the warehouse the
  subject reaches is the world's own file through the `duckdb` shim. A task that
  declares `warehouse_backend: postgres` needs a server this module does not
  start; `gateway/NATIVE.md` records that as designed, not built.

PORTS
-----
Every port is the service's own plus one offset
(`alkera_gateway.native.serve --port-offset`), so `jira` on 8080 becomes 18080
and a reader can still check a number against docs/implementation_plan/harness/NATIVE_DRIVER_REQUESTS.md item 3
by subtracting. The offset is DERIVED FROM THE ROLLOUT'S IDENTITY, not
allocated: `choose_offset` hashes the key into the candidate list and takes the
first candidate whose whole block is free, so the same rollout re-run on a quiet
host gets the same ports, and K rollouts of one run get different ones because
their keys differ. Idempotent: `up()` on a stack that is already up returns the
endpoints it already published and starts nothing.

TEARDOWN
--------
`down()` stops the child, waits for it, and removes the MinIO container if one
was started. It is safe to call when `up()` failed half-way and safe to call
twice, because a rollout's `finally` is the only place it is ever called from.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_under_test as aut  # noqa: E402
import container_episode        # noqa: E402
import episode                  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
GATEWAY_SRC = REPO / "gateway" / "src"
#: The image `gateway/docker/compose.native.yml` pins, by the same digest, so
#: the host topology and the container topology audit the same MinIO.
MINIO_IMAGE = ("docker.io/pgsty/minio@sha256:"
               "b6bfe7239bfc83fb90d31612d9704d86039dd714f7904b3f1ad68f211e602372")
#: Offsets are multiples of this, so a published port reads as
#: `<offset><service port>` and the service port is recoverable by eye.
OFFSET_STEP = 10_000
#: Below 10000 a service port could collide with the offset's own block; above
#: 50000 the highest service port (9093) leaves the ephemeral range.
OFFSET_CANDIDATES = tuple(range(10_000, 50_000, OFFSET_STEP))
#: How long `up()` waits for the aggregate /health to answer.
STARTUP_TIMEOUT_S = 60.0
#: How long `down()` waits for the child to exit before killing it.
SHUTDOWN_TIMEOUT_S = 15.0
SHIM_DIR_NAME = "shims"
#: The scheduler CLI, as the repo refers to it and as `docs/onboarding.md`
#: would: a tool the team wrote, invoked by path from the working tree, not a
#: PATH-front wrapper in front of a binary. `world/world/native_workspace.py`
#: owns the onboarding page and does not yet name it - recorded in
#: `docs/implementation_plan/harness/NATIVE_DRIVER.md` rather than written into a file that is not
#: this builder's.
SCHED_CLI_PATH = "ops/sched"
SCHED_CLI_MODULE = "alkera_gateway.native.shims.sched_cli"


def service_ports(profile=None) -> list[int]:
    """Every port one `serve.py` process binds, before the offset is added.

    With an environment profile (`episode.environment_profile`), only the ports
    `serve.py --profile` binds: the profile's services, the aggregate, and the
    MinIO audit receiver only when the object store is plugged.
    """
    from alkera_gateway.native import serve

    if profile is None:
        return sorted({*serve.STUB_PORTS.values(), serve.AGGREGATE_PORT,
                       serve.MINIO_AUDIT_PORT})
    # A plugged BI vendor binds its own port (serve.PLUG_PORTS), outside the
    # nine an unprofiled stack waits for.
    ports = {({**serve.STUB_PORTS, **serve.PLUG_PORTS})[name] for name in profile.services
             if name in serve.STUB_PORTS or name in serve.PLUG_PORTS}
    # So does a plugged hosted backend, on its registry row's port.
    from alkera_gateway.backends import plugged

    ports |= {b.port for b in plugged(profile.slots) if b.hosted}
    ports.add(serve.AGGREGATE_PORT)
    if "object_store" in profile.slots:
        ports.add(serve.MINIO_AUDIT_PORT)
    return sorted(ports)


def port_free(port: int) -> bool:
    """Can this process bind 127.0.0.1:port right now?

    `SO_REUSEADDR` is deliberately NOT set: the question is whether the port is
    free for a server, and a port in TIME_WAIT from a previous rollout is not.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def choose_offset(key: str, ports: list[int] | None = None) -> int:
    """The port offset for a rollout, derived from its identity then checked.

    The hash picks the starting candidate so that two rollouts of one run do
    not race for the same block and one rollout re-run lands where it landed
    before. The scan is what makes it true rather than hopeful: the first
    candidate whose entire block is free wins, and when none is, that is raised
    rather than papered over - a stack half-bound to another rollout's ports is
    the failure mode this whole module exists to avoid.
    """
    ports = ports or service_ports()
    start = int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)
    for step in range(len(OFFSET_CANDIDATES)):
        offset = OFFSET_CANDIDATES[(start + step) % len(OFFSET_CANDIDATES)]
        if all(port_free(port + offset) for port in ports):
            return offset
    raise episode.HarnessError(
        f"no free port block for the native stack: every offset in "
        f"{OFFSET_CANDIDATES} has at least one of {ports} taken. Another "
        f"rollout's stack is probably still up.")


def needs_object_storage(spec) -> bool:
    """Does this task REQUIRE the s3 surface?

    `required_surfaces` and not the declared probes: a probe says the task
    looked at a surface, a requirement says the task cannot be scored without
    it (`surface_audit.audit`). MinIO costs a container per rollout, and GT001
    - which declares `s3_list` and requires nothing - is the case that decides
    the rule.
    """
    import surface_audit

    return any(surface_audit.native_surface_for(str(name)) == "s3"
               for name in (getattr(spec, "required_surfaces", None) or ()))


def venv_env() -> dict:
    """This process's environment, with `gateway/src` AND `world/` importable.

    Track A is on the path for the same reason `episode.venv_env` puts it there
    for the MCP gateway's child: `native/context.py` builds its `Operations`
    with `scheduling.episode_scheduler(config)`, and that function REFUSES to
    fall back to a scheduler that cannot execute - a world root with
    `var/sched/state.json` and no importable `world` package raises rather than
    silently downgrading the episode. Measured: without this every one of the
    nine services answered 500 to every request, `/health` included.
    """
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    parts = [str(GATEWAY_SRC), str(episode.TRACK_A)] + (
        [existing] if existing else [])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def stage_discovery(world: dict, home: str | None = None) -> dict:
    """Import write_for_built_world via episode.TRACK_A. Call it on the world root.

    World root = world["world_root"] if present else Path(world["workspace"]).parent.
    Return {"files": [str, ...], "root": str(root)}.

    `home` is the HOME of the box the world is served on; None keeps
    `write_for_built_world`'s default (/root). The Harbor export passes
    /home/engineer so the `.envrc`'s DBT_PROFILES_DIR names the agent's HOME.
    """
    sys.path.insert(0, str(episode.TRACK_A))
    from world.native_workspace import write_for_built_world  # noqa: E402

    root = (Path(world["world_root"]) if world.get("world_root")
            else Path(world["workspace"]).parent)
    # A world the world factory built with `native: true` under a profile
    # already carries ITS discovery files - written by world.build from the
    # profile's own slots and plugs (native_workspace.write) and committed in
    # the repository. write_for_built_world knows no profile: it would replace
    # them with the unprofiled box's (nine services, no BI vendor projects on
    # the onboarding page) and leave the agent's repo dirty. Keep them.
    if home is None and (root / "var" / "world" / "profile.json").is_file() \
            and (root / "home" / ".config" / "alkera" / "services.toml").is_file():
        return {"files": [], "root": str(root),
                "kept": "the world's own profiled discovery files (world.build "
                        "native: true under a profile)"}
    written = (write_for_built_world(root) if home is None
               else write_for_built_world(root, home=home))
    return {"files": [str(path) for path in written], "root": str(root)}


def sanitize_world_files(world: dict) -> dict:
    """Strip the author-side material out of the files a native subject can read.

    MCP responses strip `authoring` blocks on the way out (gateway
    workflow.py's AUTHOR_SIDE); a native subject has a shell, and the staged
    file on disk is the thing that shell reads instead. Measured 2026-09-16
    (STATUS 3.3): a GT004 native episode read DATA-4402's `authoring` note -
    which names the second defect verbatim - before its first identity query,
    and var/obs/definitions.yaml carries the designer's commentary ("a panel
    whose whole job is to notice it would delete the task"). The container
    topology mounts the workspace and the warehouse files and nothing else, so
    these files never reach the episode's filesystem there; this strip is what
    keeps the same guarantee on any topology that mounts or serves the staged
    tree, and on host smoke runs.

    Graders read the same fields the services serve, never `authoring`, so the
    staged copy strips clean without moving a score. Runs BEFORE the world is
    fingerprinted (called from rollout.py beside stage_discovery) so the record
    hashes the world the subject actually saw. Returns what it removed; a
    world with nothing to strip returns empty maps, not an error.
    """
    sys.path.insert(0, str(episode.TRACK_A))
    from world.hashing import json_canon                       # noqa: E402
    from world.workflow import strip_authoring                 # noqa: E402

    root = (Path(world["world_root"]) if world.get("world_root")
            else Path(world["workspace"]).parent)
    out: dict = {"workflow": {}, "obs_comment_lines_removed": 0}
    workflow_dir = root / "var" / "workflow"
    if workflow_dir.is_dir():
        for path in sorted(workflow_dir.glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            removed = strip_authoring(document)
            if removed:
                path.write_bytes(json_canon(document) + b"\n")
                out["workflow"][path.name] = removed
    definitions = root / "var" / "obs" / "definitions.yaml"
    if definitions.is_file():
        lines = definitions.read_text(encoding="utf-8").splitlines(keepends=True)
        kept = [ln for ln in lines if not ln.lstrip().startswith("#")]
        removed = len(lines) - len(kept)
        if removed:
            definitions.write_text("".join(kept), encoding="utf-8", newline="")
            out["obs_comment_lines_removed"] = removed
    return out


def install_workspace_cli(world: dict, topology: str = "host") -> dict:
    """Write `./ops/sched` into the working tree. Returns what it wrote.

    Installed under BOTH topologies, and the difference is two baked-in strings.
    On the host the CLI runs the three `Operations` methods in its own process
    against the world's own files. In the container it is written with the
    IMAGE's interpreter and Track A's path inside the image, and `SCHED_URL`
    (set by `gateway/docker/compose.native.yml`) makes it send the same three
    operations to the scheduler plane `native/serve.py` serves at
    `sched.internal` - which it must, because the episode container mounts the
    workspace and the warehouse files and nothing else, so `var/sched`, its run
    logs, `var/obs` and `build/sched` are not on the subject's filesystem.

    The agent sees one CLI either way: `shims/sched_cli` shares the three
    operations and the renderer between the modes, and
    `tasks/harness/test_native_container.py` diffs local against remote for
    `list`, `logs` and `trigger` over one world rather than asserting they
    agree.

    CALLED BEFORE THE WORLD IS FINGERPRINTED, from `rollout.py`, and that is
    the whole of why it is not part of `up()`. The workspace is fingerprinted
    and diffed: a file that appears in it after `tree_manifest` has been taken
    is a world change nothing in the action log can explain, so the episode's
    own tooling has to be there before the episode's "before" picture is taken -
    exactly as a team's `./ops/deploy` is in the repo before anyone clones it.

    `world/` is on the CLI's baked-in PYTHONPATH because the scheduler it drives
    is Track A's (`world.gateway_scheduler.WorldScheduler`, injected by
    `alkera_gateway.scheduling.episode_scheduler`) and the gateway venv does not
    install it.

    NOT INSTALLED on a box whose profile has no `ops_sched` orchestrator: the
    world's profile record (`episode.WORLD_PROFILE_RECORD`, which
    `episode.check_world_profile` has already matched to the task) plugs no
    `orchestrator` slot (every da/* profile), or names another orchestrator
    plug. The CLI's host-topology local mode needs no service, so installing
    it there handed such a box a scheduler that listed, read and ran jobs
    (measured by bi_e2e_check.py). The record says why, with `cli` None. A
    world with no record (unprofiled) is installed exactly as before.
    """
    from alkera_gateway.native.shims import install

    skipped = _no_sched_cli(world)
    if skipped:
        return {"cli": None, "topology": topology, "interpreter": None,
                "pythonpath": None, "files": [], "skipped": skipped}
    target = Path(world["workspace"]) / SCHED_CLI_PATH
    if topology == "container":
        interpreter = container_episode._native_probe().CONTAINER_PYTHON
        pythonpath = container_episode.WORLD_PYTHONPATH_IN_CONTAINER
    else:
        interpreter, pythonpath = str(episode.PY), str(episode.TRACK_A)
    written = install.install_cli(target, SCHED_CLI_MODULE,
                                  interpreter=interpreter,
                                  pythonpath=pythonpath)
    return {"cli": SCHED_CLI_PATH, "topology": topology,
            "interpreter": interpreter, "pythonpath": pythonpath,
            "files": [str(p) for p in written]}


#: The orchestrator plug `./ops/sched` is (alkera_gateway.plugs), and every
#: stack's default for the orchestrator slot.
SCHED_CLI_PLUG = "ops_sched"


def _no_sched_cli(world: dict) -> str:
    """Why this world's box gets no `./ops/sched`, or "" when it does.

    Read from the world's profile record: no record (an unprofiled world) is
    "" - the CLI as it always was."""
    root = (Path(world["world_root"]) if world.get("world_root")
            else Path(world["workspace"]).parent)
    path = root / episode.WORLD_PROFILE_RECORD
    if not path.is_file():
        return ""
    record = json.loads(path.read_text(encoding="utf-8"))
    name = record.get("name")
    if "orchestrator" not in (record.get("slots") or []):
        return f"profile {name!r} plugs no orchestrator"
    plugged = (record.get("plugs") or {}).get("orchestrator")
    if plugged is not None and SCHED_CLI_PLUG not in plugged:
        return f"profile {name!r} plugs orchestrator {sorted(plugged)}, not {SCHED_CLI_PLUG}"
    return ""


class NativeStack:
    """One native stack for one rollout. `up()` / `down()`, and nothing else."""

    def __init__(self, *, world: dict, audit_dir: Path, rollout_dir: Path,
                 spec=None, key: str = "", offset: int | None = None) -> None:
        self.world = world
        self.audit_dir = Path(audit_dir)
        self.rollout_dir = Path(rollout_dir)
        self.spec = spec
        # The task's environment profile: "" and {} for an unprofiled task,
        # which then runs exactly as it did before profiles existed.
        self.profile_name = getattr(spec, "profile", "") or ""
        self.plugs = dict(getattr(spec, "plugs", None) or {})
        self.profile = episode.environment_profile(self.profile_name, self.plugs)
        self.key = key or self.rollout_dir.as_posix()
        self._offset = offset
        self.endpoints: dict | None = None
        self.shim_dir = self.rollout_dir / SHIM_DIR_NAME
        self.endpoints_file = self.rollout_dir / "native_endpoints.json"
        self.config_file = self.rollout_dir / "native_gateway.json"
        self.log_path = self.rollout_dir / "native_services.log"
        self._proc: subprocess.Popen | None = None
        self._log = None
        self._minio: str | None = None

    # ------------------------------------------------------------- bring-up
    def up(self) -> dict:
        """Start everything and return the endpoint map. Idempotent."""
        if self.endpoints is not None and self._proc and self._proc.poll() is None:
            return self.endpoints
        self.rollout_dir.mkdir(parents=True, exist_ok=True)
        self.audit_dir.mkdir(parents=True, exist_ok=True)
        self._offset = self._offset if self._offset is not None else \
            choose_offset(self.key)
        try:
            s3_url = self._start_minio() if (self.spec is not None
                                             and needs_object_storage(self.spec)) \
                else None
            self._start_services()
            self._install_shims()
            self.endpoints = self._write_endpoints(s3_url)
        except Exception:
            self.down()
            raise
        return self.endpoints

    @property
    def offset(self) -> int:
        if self._offset is None:
            raise episode.HarnessError("the native stack has not been brought up")
        return self._offset

    def _config_path(self) -> Path:
        """The gateway config the services resolve this episode's world through.

        Written from the world builder's own manifest rather than derived from
        the world root, because the manifest is the only thing that knows where
        a task put its planes - `episode.write_gateway_config` is the same call
        the MCP driver makes, so the two transports serve one world by
        construction rather than by two agreeing conventions.
        """
        return episode.write_gateway_config(
            self.config_file, self.world,
            self.audit_dir, "native-host",
            self.world.get("virtual_time") or "",
            profile=self.profile_name, plugs=self.plugs, service_state=True)

    def _serve_argv(self) -> list[str]:
        return [str(episode.PY), "-m", "alkera_gateway.native.serve",
                "--audit", str(self.audit_dir),
                "--world", str(self.world["world_root"]),
                "--bind", "127.0.0.1",
                "--port-offset", str(self.offset),
                "--destination-host", "127.0.0.1",
                "--gateway-config", str(self._config_path()),
                *episode.profile_argv(self.profile_name, self.plugs)]

    def _start_services(self) -> None:
        argv = self._serve_argv()
        self._log = open(self.log_path, "w", encoding="utf-8", newline="")
        self._proc = subprocess.Popen(argv, stdout=self._log,
                                      stderr=subprocess.STDOUT,
                                      env=venv_env(), cwd=str(self.rollout_dir))
        self._await_listening()

    def _await_listening(self) -> None:
        """Wait until every port is bound, WITHOUT connecting to any of them.

        The obvious readiness check is `GET /health` on the aggregate, and it
        is wrong here: `serve.py` counts accepted connections into
        `destinations.jsonl`, `blindness.network_violations` reads that file as
        the episode's own traffic, and the aggregate is not in the endpoint map
        the episode was handed - so one readiness poll by the HARNESS
        quarantined a whole rollout with `the episode connected to
        127.0.0.1:18099`. Measured, on the first end-to-end native rollout.

        Readiness is therefore asked the way `compose.native.yml`'s own
        healthcheck asks it - `destinations.jsonl` exists, which `serve.py`
        writes before it opens a socket - plus a bind attempt per port, which
        proves a listener holds it and opens nothing. Each service's own
        `/health` is still checked, once per surface, by the surface gate,
        whose connections ARE to the episode's endpoints and are counted there.
        """
        from alkera_gateway.native import serve

        destinations = self.audit_dir / serve.DESTINATIONS
        ports = [port + self.offset for port in service_ports(self.profile)]
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise episode.HarnessError(
                    f"the native services process exited "
                    f"{self._proc.returncode} during start-up:\n"
                    f"{self._tail_log()}")
            if destinations.exists() and not any(port_free(p) for p in ports):
                return
            time.sleep(0.25)
        unbound = [p for p in ports if port_free(p)]
        raise episode.HarnessError(
            f"the native services did not bind {unbound} within "
            f"{STARTUP_TIMEOUT_S:.0f}s:\n{self._tail_log()}")

    def _tail_log(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8",
                                           errors="replace")[-2000:]
        except OSError:
            return "(no log)"

    def _install_shims(self) -> None:
        """Materialise the PATH-front shims and put them in front of PATH.

        `os.environ` and not a returned overlay: `agent_under_test.run` builds
        the subject's environment as `{**os.environ, ...}`, so a shim directory
        on THIS process's PATH is on the subject's PATH whatever subject it is.
        One rollout is one process (`run_rollouts._one` forks `rollout.py`), so
        the mutation cannot reach another rollout.
        """
        from alkera_gateway.native.shims import install

        install.install(self.shim_dir, interpreter=str(episode.PY))
        entries = os.environ.get("PATH", "").split(os.pathsep)
        # The interpreter's own scripts directory, BEHIND the shims. `dbt` is
        # installed into `gateway/.venv/Scripts` and a venv Python launched by
        # absolute path does not put that directory on PATH, so without this the
        # `dbt` shim resolves no binary, the agent's `dbt` exits 127, and the
        # surface gate is right to call the dbt surface dead. Behind the shims
        # so that `python` is still the audited wrapper and not the venv's.
        scripts = str(Path(episode.PY).parent)
        for entry in (scripts, str(self.shim_dir)):
            if entry not in entries:
                entries.insert(0, entry)
        os.environ["PATH"] = os.pathsep.join(entries)
        os.environ["ALKERA_SHIM_BIN"] = str(self.shim_dir)
        os.environ["ALKERA_SHIM_WORKSPACE"] = str(self.world["workspace"])

    def _write_endpoints(self, s3_url: str | None) -> dict:
        """The `--native-endpoints` file, then the map the harness resolves.

        Written through `aut.resolve_endpoints` rather than assembled here, so
        a key this module invents is REFUSED by the same rule a typo in a
        hand-written file is - there is one definition of what a native surface
        is and it is the harness's.
        """
        overrides = {
            name: f"http://127.0.0.1:{port + self.offset}"
            for name, (_, port) in aut.NATIVE_SERVICES.items()
            if self.profile is None or name in self.profile.services
        }
        # BI vendors are explicit-only: addressed only when the profile plugs them.
        if self.profile is not None:
            overrides.update({
                name: f"http://127.0.0.1:{port + self.offset}"
                for name, (_, port) in aut.PLUG_SERVICES.items()
                if name in self.profile.services})
            # A plugged hosted backend is served by this same process, on its
            # registry row's port and scheme.
            from alkera_gateway.backends import plugged

            overrides.update({
                b.name: f"{b.scheme}://127.0.0.1:{b.port + self.offset}"
                for b in plugged(self.profile.slots) if b.hosted})
        if s3_url:
            overrides["s3"] = s3_url
        self.endpoints_file.write_text(
            json.dumps(overrides, indent=2, sort_keys=True), encoding="utf-8")
        return aut.profile_endpoints(aut.resolve_endpoints(self.endpoints_file),
                                     self.profile)

    # --------------------------------------------------------------- minio
    def _minio_name(self) -> str:
        digest = hashlib.sha256(self.key.encode("utf-8")).hexdigest()[:10]
        return f"alkera-native-minio-{digest}"

    def _start_minio(self) -> str:
        """MinIO on a published port, audit-webhooking back into this stack.

        The receiver is inside the `serve.py` process on the host, so the
        container reaches it through `host.docker.internal`, which is added
        explicitly rather than relied on: it exists by default on Docker
        Desktop and not on Linux, and a webhook that silently fails to deliver
        would cost the episode its storage rows without costing it a test.
        """
        from alkera_gateway.native import serve

        name = self._minio_name()
        subprocess.run(["docker", "rm", "-f", name], capture_output=True,
                       text=True, encoding="utf-8")
        port = 9000 + self.offset
        receiver = serve.MINIO_AUDIT_PORT + self.offset
        endpoint = f"http://host.docker.internal:{receiver}/minio/audit"
        argv = [
            "docker", "run", "-d", "--name", name,
            "--add-host", "host.docker.internal:host-gateway",
            "-p", f"127.0.0.1:{port}:9000",
            "-e", f"MINIO_ROOT_USER={aut.NATIVE_FAKE_CREDENTIAL}",
            "-e", f"MINIO_ROOT_PASSWORD={aut.NATIVE_FAKE_CREDENTIAL}",
            "-e", "MINIO_AUDIT_WEBHOOK_ENABLE_alkera=on",
            "-e", f"MINIO_AUDIT_WEBHOOK_ENDPOINT_alkera={endpoint}",
            "-e", "MINIO_AUDIT_WEBHOOK_QUEUE_DIR_alkera=/audit-spool",
            "--entrypoint", "sh", MINIO_IMAGE, "-c",
            f"mkdir -p /data/{aut.NATIVE_DEFAULTS['s3_bucket']} /audit-spool "
            f"&& exec minio server /data",
        ]
        result = subprocess.run(argv, capture_output=True, text=True,
                                encoding="utf-8")
        if result.returncode != 0:
            raise episode.HarnessError(
                f"could not start MinIO for the native stack:\n{result.stderr}")
        self._minio = name
        return f"http://127.0.0.1:{port}"

    # ------------------------------------------------------------ teardown
    def down(self) -> None:
        """Stop everything this stack started. Safe to call twice."""
        if self._proc is not None:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=SHUTDOWN_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=SHUTDOWN_TIMEOUT_S)
            self._proc = None
        if self._log is not None:
            self._log.close()
            self._log = None
        if self._minio is not None:
            subprocess.run(["docker", "rm", "-f", self._minio],
                           capture_output=True, text=True, encoding="utf-8")
            self._minio = None

    def record(self) -> dict:
        """What the rollout record should carry about this stack."""
        return {
            "topology": "host",
            "port_offset": self._offset,
            "endpoints_file": str(self.endpoints_file),
            "shim_dir": str(self.shim_dir),
            "shims": sorted(p.name for p in self.shim_dir.glob("*")
                            ) if self.shim_dir.is_dir() else [],
            "services_log": str(self.log_path),
            "gateway_config": str(self.config_file),
            "minio_container": self._minio,
        }


# ------------------------------------------------------------ scripted subject
#: The acceptance subject. A real bash process working all four native planes:
#: the scheduler through `./ops/sched` (a read AND a run, because `sched_list`
#: and `sched_trigger` are two different rules in `blindness._unattributed`),
#: the warehouse through the `duckdb` shim,
#: a vendor service over HTTP, and an edit to the working tree with `sed`.
#:
#: The trigger is a LOOP over `$ALKERA_TRIGGER_JOBS`, which `trigger_plan` fills
#: with this world's first job or - with `ALKERA_TRIGGER_ALL` set in the
#: harness's environment - with every job it declares, in declaration order.
#: That second mode is the pre-campaign smoke: the world-diff allowlist in
#: `blindness.py` is a hand-kept list of paths, every entry was added the day a
#: rollout tripped over it, and running every job kind once is the only way to
#: find the next entry before a scored campaign finds it. It is
#: NOT a model and it is not a measurement - it exists so the native driver can
#: be run end to end without spending model quota, exactly as
#: `redteam/scripted_subject.py` exists so the canary detector can be watched
#: firing. `psql` is deliberately absent: the amendment keeps this world's
#: warehouse a DuckDB file.
#:
#: The warehouse statements CREATE, DELETE and DROP a throwaway table, in that
#: order, and each of the three matters. The DELETE is the case
#: `gateway/NATIVE.md` recorded as the weakest finding - a DuckDB
#: write whose row names TABLES and cannot be matched against the warehouse
#: FILE - so an acceptance whose only statement was a `SELECT`, which is what
#: this script used to run, could not have caught it. The DROP puts the
#: warehouse back the way it was logically while leaving the file changed, which
#: is exactly the state the world-diff rule has to attribute.
SCRIPTED_SUBJECT = r"""#!/bin/sh
set -eu
cd "$ALKERA_WORKSPACE"
echo "== scheduler =="
# a profile that plugs no orchestrator has no ./ops/sched (install_workspace_cli)
if [ -x ./ops/sched ]; then ./ops/sched list; else echo "(no ./ops/sched on this box)"; fi
echo "== warehouse =="
duckdb "$ALKERA_WAREHOUSE" \
  -c "CREATE TABLE main.$ALKERA_SCRATCH_TABLE AS SELECT 1 AS n" \
  -c "DELETE FROM main.$ALKERA_SCRATCH_TABLE WHERE n = 1" \
  -c "DROP TABLE main.$ALKERA_SCRATCH_TABLE"
echo "== scheduler runs =="
if [ -x ./ops/sched ]; then
  for job in $ALKERA_TRIGGER_JOBS; do
    echo "-- trigger $job"
    ./ops/sched trigger "$job"
  done
fi
echo "== jira =="
if [ -n "$ALKERA_ISSUE_KEY" ]; then
  curl -sS "$JIRA_URL/rest/api/3/issue/$ALKERA_ISSUE_KEY" | head -c 400
else
  curl -sS "$JIRA_URL/rest/api/3/search?maxResults=1" | head -c 400
fi
echo
echo "== workspace =="
sed -i "1i -- touched by the native acceptance subject (not a fix)" "$ALKERA_EDIT_FILE"
echo "edited $ALKERA_EDIT_FILE"
"""


def write_scripted_subject(path: Path) -> Path:
    """Materialise the scripted subject beside the rollout, LF endings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as stream:
        stream.write(SCRIPTED_SUBJECT)
    return path


def _transcript(path: Path, script: Path, stdout: str) -> Path:
    """A stream-json transcript of what the scripted subject really did.

    The same shape `agent_under_test._run_scripted` captures from the redteam
    subject, and it has to be: `blindness.load_transcript` reads it, the
    working-tree rule asks whether the transcript holds a `Bash` tool_use at
    all, and a native episode whose transcript held none would quarantine for
    its own `sed`.
    """
    events = [
        {"type": "system", "subtype": "init", "tools": ["Bash"],
         "mcp_servers": [], "model": "scripted-native-subject",
         "session_id": "scripted-native"},
        {"type": "user", "message": {"role": "user", "content": "<the ticket>"}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": f"sh {script.as_posix()}"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": stdout}]}},
        {"type": "result", "subtype": "success", "num_turns": 1,
         "result": stdout.strip()[-2000:]},
    ]
    with open(path, "w", encoding="utf-8", newline="") as stream:
        for event in events:
            stream.write(json.dumps(event) + "\n")
    return path


#: The table the scripted subject creates, empties and drops. In `main`, the
#: schema every DuckDB database has, so the script is the same on a world whose
#: marts live in `warehouse` and on one whose live in `nova`.
SCRATCH_TABLE = "native_acceptance_scratch"


def run_scripted(*, rollout_dir: Path, world: dict, audit_dir: Path,
                 endpoints: dict, wall_clock_s: int, instruction: str = "",
                 issue_key: str = "", variant: str = "",
                 gateway_config: str | Path = "") -> aut.AgentRun:
    """Run the scripted native subject and return an `AgentRun` like any other.

    `agent_under_test._run_scripted` cannot be reused as it stands: it starts
    one fixed python file with no native environment, because the `scripted`
    DRIVER it serves has no services to address. Everything the harness reads
    afterwards - the `AgentRun` fields, the stream-json transcript, the audit
    rows the shims and services wrote - is the same.
    """
    script = write_scripted_subject(rollout_dir / "native_subject.sh")
    # The prompt is written even though this subject never reads it: every
    # rollout directory carries the instruction the episode was run under, and
    # a native rollout missing one would look like a rollout whose prompt was
    # lost rather than one whose subject is a shell script.
    (rollout_dir / "prompt.txt").write_text(
        instruction or "(the scripted native subject runs a fixed script)",
        encoding="utf-8")
    bash = _bash()
    workspace = Path(world["workspace"])
    edit_file = _editable_file(workspace)
    jobs = trigger_plan(world)
    env = {**os.environ, "PYTHONUTF8": "1",
           **aut.native_env(endpoints=endpoints, audit_dir=audit_dir,
                            profiles_dir=world.get("profiles_dir") or "",
                            scheduler=world.get("scheduler") or "",
                            gateway_config=gateway_config, variant=variant),
           "ALKERA_WORKSPACE": str(workspace),
           "ALKERA_WAREHOUSE": str(world["warehouse"]),
           "ALKERA_SCRATCH_TABLE": SCRATCH_TABLE,
           # Job ids are identifiers, so a space-separated list is safe to
           # word-split in `sh` and the script stays one loop for both modes.
           "ALKERA_TRIGGER_JOBS": " ".join(jobs),
           "ALKERA_EDIT_FILE": edit_file,
           "ALKERA_ISSUE_KEY": issue_key or _first_issue_key(world)}
    (rollout_dir / "native_env.json").write_text(
        json.dumps({k: v for k, v in env.items() if k.startswith(
            ("ALKERA_", "JIRA", "SLACK", "AWS_", "PG", "DBT_", "LOKI",
             "METABASE", "PROMETHEUS", "ALERTMANAGER", "AIRBYTE", "DEPLOY",
             "CONFLUENCE"))}, indent=2, sort_keys=True), encoding="utf-8")

    started = time.time()
    proc = subprocess.run([bash, script.as_posix()], cwd=str(workspace),
                          env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace",
                          timeout=wall_clock_s)
    ended = time.time()
    transcript = _transcript(rollout_dir / "transcript.jsonl", script,
                             proc.stdout)
    return aut.AgentRun(
        driver="native", model="scripted-native-subject",
        exit_reason="completed" if proc.returncode == 0 else "agent_error",
        returncode=proc.returncode, wall_clock_s=round(ended - started, 2),
        started_wts=int(started * 1000), ended_wts=int(ended * 1000),
        transcript_path=str(transcript), events=5,
        final_text=proc.stdout.strip()[-2000:], session_id="scripted-native",
        usage={"scripted": True}, stderr_tail=(proc.stderr or "")[-4000:],
        argv=[bash, script.as_posix()],
        notes=(f"scripted native subject: not a model and not a measurement; "
               f"triggered {jobs or '(no scheduler plane)'}"
               + (f" ({TRIGGER_ALL_ENV} set: every job this world declares)"
                  if os.environ.get(TRIGGER_ALL_ENV) else "")))


def _bash() -> str:
    import shutil

    found = shutil.which("bash")
    if found is None:
        raise episode.HarnessError(
            "the scripted native subject needs a real POSIX shell; no `bash` "
            "is on PATH")
    return found


#: Extensions the scripted subject will edit, best first. A line-comment
#: prefixed line is valid in all of them, which is what `sed -i 1i` inserts.
EDITABLE_SUFFIXES = (".md", ".sql", ".yml", ".yaml", ".txt", ".py")


def _editable_file(workspace: Path) -> str:
    """The workspace file the subject edits, as an absolute path.

    A real file already in the tree and not one the subject creates: the rule
    under test is `out_of_band_world_change` over the WORKING TREE, and a file
    that did not exist before the episode changes the world just as much but
    exercises the `added` half of a diff rather than the `changed` half.

    GT001's whole working tree is one dbt model, which is why this is a
    preference order over what is there and not a fixed name.
    """
    candidates = [path for path in sorted(workspace.rglob("*"))
                  if path.is_file() and ".git" not in path.parts]
    for suffix in EDITABLE_SUFFIXES:
        for path in candidates:
            if path.suffix == suffix:
                return str(path)
    raise episode.HarnessError(
        f"no file under {workspace} with an editable extension "
        f"{EDITABLE_SUFFIXES} for the scripted subject to edit")


def scheduler_jobs(world: dict) -> list[str]:
    """Every job this world's scheduler declares, in DECLARATION order.

    From the state file and not from `./ops/sched list`, for the reason
    `_first_issue_key` reads the fixture rather than asking jira: every run of
    the CLI writes a 9.2.2 row, and a row the HARNESS caused would be inside the
    episode's measurement. A world with no scheduler plane yields `[]` and the
    subject triggers nothing.

    Declaration order, which for a json object is the order `json.loads`
    preserves, because the point of `ALKERA_TRIGGER_ALL` is to exercise the job
    KINDS a world ships and a world's own order is the only one that is not this
    module's invention.
    """
    try:
        state = json.loads(
            Path(world["scheduler"]).read_text(encoding="utf-8"))
        jobs = state["jobs"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    if isinstance(jobs, dict):
        return [str(name) for name in jobs]
    return [str(job.get("id", "")) for job in jobs if job.get("id")]


#: Set in the HARNESS's environment (`ALKERA_TRIGGER_ALL=1 run_rollouts.py ...`)
#: to make the scripted subject trigger every job this world declares instead of
#: its first. It is a smoke lever, not a measurement: the world-diff allowlist in
#: `blindness.py` is a hand-kept list of paths and every entry was added the day
#: a rollout tripped over it, so the only way to know what a task's job kinds
#: write is to run them all once before a campaign does it by accident.
TRIGGER_ALL_ENV = "ALKERA_TRIGGER_ALL"


def trigger_plan(world: dict) -> list[str]:
    """The jobs the scripted subject will trigger, in order."""
    jobs = scheduler_jobs(world)
    return jobs if os.environ.get(TRIGGER_ALL_ENV) else jobs[:1]


def _first_issue_key(world: dict) -> str:
    """An issue key this world actually has, read from the world's own fixture.

    From `var/workflow/issues.json` and NOT from the jira service, although the
    service is up and would answer: every request it serves writes a 9.2.2 row,
    and a row the HARNESS caused would be inside the episode's measurement.
    Only the `health:` namespace is excluded from that
    (`blindness.is_harness_row`), so anything else the harness asks a service
    before the subject runs is counted against the subject.
    """
    root = Path(world["world_root"])
    path = Path(world.get("workflow_dir") or root / "var" / "workflow") / "issues.json"
    try:
        issues = json.loads(path.read_text(encoding="utf-8"))["issues"]
        # `id` and `key` are both spelled in the corpus - GT001's fixture says
        # `key`, GT004's says `id` - and neither is this module's to decide, so
        # both are read and a fixture that says neither falls through to the
        # search below rather than throwing the rollout away as a harness error.
        # Measured: GT004's first native rollout was `harness_error: KeyError`.
        return str((issues[0].get("key") or issues[0].get("id") or "")
                   if issues else "")
    except (OSError, ValueError, KeyError, AttributeError, IndexError):
        # Not an error: a task's world need not seed the human plane at all -
        # GT001's seeds none, so its jira serves nothing and says so with a 503
        # on /health. The subject then asks jira for a SEARCH rather than for
        # an issue that does not exist, which is a real request against a real
        # endpoint and leaves a real row; what the row's `exit` says about the
        # world is then the world's business, not the subject's.
        return ""


# ==================================================== the container topology
# `--driver native --topology container`. The stack is compose's
# (`container_episode.NativeContainerEpisode`); what lives here is the subject
# side of it - which script runs, where its files are, and what the world's
# paths are called once the episode is inside the box.
#
# WHERE THE WORLD IS, ONCE THE EPISODE CANNOT SEE IT
# --------------------------------------------------
# The mount set is the workspace, the warehouse FILES and the audit directory.
# Everything else in the world tree - `var/sched`, `var/obs`, `var/workflow`,
# `var/bi`, `var/world`, `build` - is reached through the `native-services`
# container, which mounts the whole world exactly as before. Eight planes were
# already served there by the nine vendor-shaped services; the SCHEDULER is the
# ninth plane and it is served too, at `sched.internal`, by
# `alkera_gateway.native.serve.sched_plane`. `./ops/sched` is still the surface
# the agent finds in its own repo: the CLI's remote mode (`SCHED_URL`) sends the
# same three operations that its local mode runs in-process on the host
# topology, and the row is written by whichever side ran them.

#: Printed once at the start of every `--driver native --topology host` run.
#:
#: It is not a style note. GT001 K=5 under this topology did the work in five of
#: five episodes and QUARANTINED THREE, all three on actions the host topology
#: makes reachable and nothing audits: two `cp` backups of the warehouse file
#: into the world tree, and one `ls` of the rollout directory that put a planted
#: canary in the transcript. A rate computed over what survives that is a rate
#: over the subjects that happened not to take a backup, which is not a property
#: of the task.
HOST_TOPOLOGY_WARNING = "\n".join((
    "  WARNING  --driver native --topology host is NOT measurement_valid for",
    "           a task whose subject can reach the world's var/ tree or the",
    "           rollout directory. The subject's bash is a HOST process here:",
    "           a backup taken beside the warehouse is an unattributable world",
    "           change (hard) and an `ls` of the rollout directory is a canary",
    "           leak (hard). Measured: GT001 K=5, run-id native-20260916-k5,",
    "           quarantined 3 of 5 for exactly those two behaviours, both of",
    "           them good practice. Use --topology container to measure; the",
    "           design, the mount table and what it costs are in",
    "           docs/implementation_plan/harness/NATIVE_CONTAINER.md.",
    "",
))

#: Where the subject's script is put inside the episode container. `/tmp` and
#: not the workspace: the workspace is fingerprinted and diffed, and a file the
#: HARNESS added to it mid-episode is a world change with no row to explain it.
SUBJECT_IN_CONTAINER = "/tmp/native_subject.sh"
#: The delete the GT001 r0 and r2 subjects ran after taking their backup. It is
#: quoted from the transcripts of run-id `native-20260916-k5` rather than
#: invented, because the whole claim of the replay is that THIS sequence is
#: clean here and quarantined there.
REPLAY_DELETE_SQL = ("delete from warehouse.warehouse.fct_orders "
                     "where batch_id = 'retry-20260303-0412'")
#: A canary-SHAPED needle for the r3 replay. Deliberately the prefix and not a
#: planted token: `blindness` matches whole tokens, and a probe that put a real
#: one into the command text would quarantine itself for looking.
CANARY_NEEDLE = "ALKERA-CANARY"

#: The scripted subjects, by the name `ALKERA_NATIVE_SUBJECT` selects.
#:
#: `default` is the container analogue of `SCRIPTED_SUBJECT` above: the
#: warehouse through the `duckdb` shim (CREATE, DELETE, DROP - the DELETE is
#: the row that has to explain the warehouse file), a vendor service over HTTP,
#: and an edit to the working tree. `./ops/sched` is absent for the reason in
#: the section comment.
#:
#: `r0`, `r2` and `r3` are the three GT001 rollouts that quarantined under the
#: host topology, replayed command for command. They are not measurements:
#: they exist so that "impossible by construction" is a thing that was tried.
CONTAINER_SUBJECTS = {
    "default": r"""#!/bin/sh
set -eu
cd "$ALKERA_WORKSPACE"
echo "== scheduler =="
# a profile that plugs no orchestrator has no ./ops/sched (install_workspace_cli)
if [ -x ./ops/sched ]; then ./ops/sched list; else echo "(no ./ops/sched on this box)"; fi
echo "== warehouse =="
duckdb "$ALKERA_WAREHOUSE" \
  -c "CREATE TABLE main.$ALKERA_SCRATCH_TABLE AS SELECT 1 AS n" \
  -c "DELETE FROM main.$ALKERA_SCRATCH_TABLE WHERE n = 1" \
  -c "DROP TABLE main.$ALKERA_SCRATCH_TABLE"
echo "== scheduler runs =="
if [ -x ./ops/sched ]; then
  for job in $ALKERA_TRIGGER_JOBS; do
    echo "-- trigger $job"
    ./ops/sched trigger "$job"
  done
  ./ops/sched logs | tail -5
fi
echo "== jira =="
if [ -n "$ALKERA_ISSUE_KEY" ]; then
  curl -sS "$JIRA_URL/rest/api/3/issue/$ALKERA_ISSUE_KEY" | head -c 400
else
  curl -sS "$JIRA_URL/rest/api/3/search?maxResults=1" | head -c 400
fi
echo
echo "== workspace =="
sed -i "1i -- touched by the native acceptance subject (not a fix)" "$ALKERA_EDIT_FILE"
echo "edited $ALKERA_EDIT_FILE"
""",
    "r0": r"""#!/bin/sh
set -eu
# GT001 native-20260916-k5 r0, replayed: take a backup beside the warehouse,
# look at what is there, then delete the duplicated batch. Under the host
# topology the backup was a new file in the world tree that no row could
# explain, and the rollout was quarantined HARD on it.
cd "$(dirname "$ALKERA_WAREHOUSE")"
cp warehouse.duckdb warehouse.duckdb.pre-DATA-4471.bak
ls -la
duckdb warehouse.duckdb -c "$ALKERA_DELETE_SQL"
echo "r0 replay done"
""",
    "r2": r"""#!/bin/sh
set -eu
# GT001 native-20260916-k5 r2, replayed: a backup in a directory of its own,
# the delete, and then the restore r2 also ran. The restore writes THROUGH the
# bind, so the warehouse file really does move twice here - the point is that
# the duckdb row explains the file and the backup never reaches the world.
cd "$(dirname "$ALKERA_WAREHOUSE")"
mkdir -p backup
cp warehouse.duckdb backup/warehouse.pre-DATA-4471.duckdb
ls -la . backup
duckdb warehouse.duckdb -c "$ALKERA_DELETE_SQL"
cp backup/warehouse.pre-DATA-4471.duckdb warehouse.duckdb
echo "r2 replay done"
""",
    "r3": r"""#!/bin/sh
set -eu
# GT001 native-20260916-k5 r3, replayed: the subject listed the rollout
# directory and a planted canary filename came back in the tool result. Here
# the same hunt is run harder than r3 ran it - a recursive grep for a
# canary-shaped string over the whole filesystem and a find for every json -
# and the listing of what the container CAN see is the answer.
echo "== / =="
ls -la /
echo "== the mounts =="
ls -la /world /world/var /world/var/warehouse /workspace /audit
echo "== one level up from the world =="
ls -la /world/.. 2>&1 | head -20
echo "== home =="
ls -la "$HOME" 2>&1 | head -20
echo "== grep for a canary, everywhere =="
timeout 240 grep -rl --binary-files=without-match "$ALKERA_CANARY_NEEDLE" / 2>/dev/null | head -20
echo "grep exit=$?"
echo "== every json on the box =="
timeout 120 find / -xdev -name '*.json' 2>/dev/null | head -40
echo "r3 replay done"
""",
}
#: Set in the HARNESS's environment to choose which of the above runs. Default
#: is `default`; `r0`, `r2` and `r3` are the replays.
SUBJECT_ENV = "ALKERA_NATIVE_SUBJECT"


def container_subject_name() -> str:
    """Which scripted subject this rollout runs, and it must be one we have."""
    name = os.environ.get(SUBJECT_ENV) or "default"
    if name not in CONTAINER_SUBJECTS:
        raise episode.HarnessError(
            f"{SUBJECT_ENV}={name!r} names no scripted container subject; "
            f"known: {sorted(CONTAINER_SUBJECTS)}")
    return name


def in_container(world: dict, host_path: str | Path,
                 world_root_in_container: str = "/world",
                 workspace_in_container: str = "/workspace") -> str:
    """A host path under this world, spelled the way the episode container
    spells it.

    The working tree is bound twice - at `/workspace` (contract 9.2.1) and
    nowhere else - so a path inside it gets the workspace spelling and every
    other world path gets `/world/<rel>`. A path that is not under the world at
    all is returned unchanged and the caller finds out by the command failing,
    which is better than a silently rewritten path.
    """
    root = Path(world["world_root"]).resolve()
    workspace = Path(world["workspace"]).resolve()
    target = Path(host_path).resolve()
    try:
        return f"{workspace_in_container}/{target.relative_to(workspace).as_posix()}"
    except ValueError:
        pass
    try:
        return f"{world_root_in_container}/{target.relative_to(root).as_posix()}"
    except ValueError:
        return Path(host_path).as_posix()


def run_scripted_container(*, rollout_dir: Path, world: dict, audit_dir: Path,
                           stack, wall_clock_s: int, instruction: str = "",
                           issue_key: str = "", variant: str = "") -> aut.AgentRun:
    """Run the scripted subject INSIDE the episode container, through the
    wrapper the model would use.

    The wrapper is not bypassed, and that is deliberate: `claude --print` is the
    only thing this harness cannot run here without spending model quota, so
    everything else on that path - the launcher, the argv contract, the
    `docker exec`, the environment forwarding - is exercised by the scripted
    subject instead of being asserted about.
    """
    name = container_subject_name()
    script = rollout_dir / "native_subject.sh"
    with open(script, "w", encoding="utf-8", newline="") as stream:
        stream.write(CONTAINER_SUBJECTS[name])
    (rollout_dir / "prompt.txt").write_text(
        instruction or "(the scripted native subject runs a fixed script)",
        encoding="utf-8")
    stack.copy_in(script, SUBJECT_IN_CONTAINER)

    workspace = Path(world["workspace"])
    env = {
        "ALKERA_WORKSPACE": "/workspace",
        "ALKERA_WAREHOUSE": in_container(world, world["warehouse"]),
        "ALKERA_SCRATCH_TABLE": SCRATCH_TABLE,
        "ALKERA_EDIT_FILE": in_container(world, _editable_file(workspace)),
        "ALKERA_ISSUE_KEY": issue_key or _first_issue_key(world),
        "ALKERA_DELETE_SQL": REPLAY_DELETE_SQL,
        "ALKERA_CANARY_NEEDLE": CANARY_NEEDLE,
        # Job ids are identifiers, so a space-separated list is safe to
        # word-split in `sh`. `trigger_plan` reads them out of the world's own
        # state file in declaration order, and `ALKERA_TRIGGER_ALL` in the
        # HARNESS's environment is what turns the first into all of them.
        "ALKERA_TRIGGER_JOBS": " ".join(trigger_plan(world)),
        # The arm this world was built from, forwarded into the container with
        # the rest (docs/implementation_plan/harness/VARIANTS_REQUESTS.md item 6). The compose overlay carries it
        # too; this is the path a MODEL's command would take, and both have to
        # agree or a variant rollout would answer for `default` inside an arm.
        **({"ALKERA_TASK_VARIANT": variant} if variant else {}),
    }
    # The same call `rollout.py` verified with, with this subject's own
    # variables added: one mechanism, and the thing proved is the thing used.
    wrapper = stack.shell(rollout_dir / "shell", env)
    served = stack.episode_endpoints()
    (rollout_dir / "native_env.json").write_text(
        json.dumps({"in_container": env,
                    "from_compose": sorted(n for n in aut.NATIVE_SERVICES
                                           if n in served),
                    "shell": wrapper["launcher"],
                    "subject_env": wrapper["subject_env"],
                    "container_execution_proof": wrapper["proof"]},
                   indent=2, sort_keys=True), encoding="utf-8")
    if not wrapper["proof"]["verified"]:
        raise episode.HarnessError(
            f"the subject's shell does not reach the episode container: "
            f"{wrapper['proof']}")

    command = f"sh {SUBJECT_IN_CONTAINER}"
    argv = [*wrapper.get("launcher_argv", [wrapper["launcher"]]), "-lc", command]
    started = time.time()
    proc = subprocess.run(argv, cwd=str(rollout_dir),
                          env={**os.environ, **wrapper["subject_env"]},
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=wall_clock_s)
    ended = time.time()
    transcript = _transcript_command(rollout_dir / "transcript.jsonl", command,
                                     proc.stdout)
    return aut.AgentRun(
        driver="native", model="scripted-native-subject",
        exit_reason="completed" if proc.returncode == 0 else "agent_error",
        returncode=proc.returncode, wall_clock_s=round(ended - started, 2),
        started_wts=int(started * 1000), ended_wts=int(ended * 1000),
        transcript_path=str(transcript), events=5,
        final_text=proc.stdout.strip()[-2000:], session_id="scripted-native",
        usage={"scripted": True}, stderr_tail=(proc.stderr or "")[-4000:],
        argv=argv,
        notes=(f"scripted native subject {name!r}, run inside the episode "
               f"container through the bash wrapper: not a model and not a "
               f"measurement; triggered {trigger_plan(world) or '(no jobs)'}"
               + (f" ({TRIGGER_ALL_ENV} set: every job this world declares)"
                  if os.environ.get(TRIGGER_ALL_ENV) else "")))


def _transcript_command(path: Path, command: str, stdout: str) -> Path:
    """The same stream-json shape `_transcript` writes, for a command string.

    Separate from `_transcript` because the container subject is run as
    `bash -c <command>` rather than as a host script path, and the transcript
    has to record the command text the wrapper actually executed - that text is
    what `blindness` reads as the subject's own words.
    """
    events = [
        {"type": "system", "subtype": "init", "tools": ["Bash"],
         "mcp_servers": [], "model": "scripted-native-subject",
         "session_id": "scripted-native"},
        {"type": "user", "message": {"role": "user", "content": "<the ticket>"}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": command}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": stdout}]}},
        {"type": "result", "subtype": "success", "num_turns": 1,
         "result": stdout.strip()[-2000:]},
    ]
    with open(path, "w", encoding="utf-8", newline="") as stream:
        for event in events:
            stream.write(json.dumps(event) + "\n")
    return path
