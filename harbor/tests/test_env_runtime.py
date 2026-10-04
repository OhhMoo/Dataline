"""The environment runtime on the host: world-seal, world-up, world-down.

No Docker. The image's layout is reproduced under a root prefix R (the runtime
takes `--root R` for exactly this): a task's world is built for each arm with
the harness's own builder, staged as a container-native episode, and arranged
the way the generator ships it (environment/episode/ -> R/world minus its
workspace, R/workspace, R/home/engineer, R/opt/alkera/episode.json and
R/opt/alkera/gateway.json - the services' config from the builder's MANIFEST -
with every path spelled in container terms). Then the real executables run
against R, with a port offset nobody else on this host is using.

GT001 (both arms) runs the whole lifecycle. GT008 (both arms) checks that the
second warehouse its manifest declares (`legacy`, attached into nova's
catalog) reaches the services: the finance dashboard and the consumer metrics
read a view over `legacy.warehouse.rpt_revenue_daily`, which serve.py's
fixed-layout config cannot attach.

The fixture below is the test's own copy of what package A's generator does,
kept here on purpose: the runtime must work on the layout the contract
describes, whatever the generator's code looks like today.

    gateway/.venv/bin/python -m pytest -q harbor/tests/test_env_runtime.py
"""
from __future__ import annotations

import contextlib
import json
import os
import random
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
HARNESS = REPO / "tasks" / "harness"
ENV_TEMPLATE = REPO / "harbor" / "template" / "environment"
BIN = ENV_TEMPLATE / "bin"
PY = REPO / "gateway" / ".venv" / "bin" / "python"
TASK = "GT001-aov-halving"
GT008 = "GT008-warehouse-cutover"
ARMS = ("default", "decision")
AGENT_HOME = "/home/engineer"
AGENT_UID = 1000
EPISODE_SCHEMA = "alkera-harbor-episode/1"

for path in (BIN, HARNESS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import alkera_runtime as rt  # noqa: E402

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="the runtime reads /proc")


# ------------------------------------------------------------- the fixture
@contextlib.contextmanager
def _variant(arm: str):
    previous = os.environ.get("ALKERA_TASK_VARIANT")
    os.environ["ALKERA_TASK_VARIANT"] = arm
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("ALKERA_TASK_VARIANT", None)
        else:
            os.environ["ALKERA_TASK_VARIANT"] = previous


def build_world(arm: str, base: Path, topology: str = "host", task: str = TASK) -> dict:
    """`task`'s world for `arm`, staged exactly as a container-native episode.

    `topology` is what `./ops/sched` gets baked with: "host" (this venv's
    interpreter, so the CLI runs in these tests) or "container" (the image's
    /usr/local/bin/python, for a Docker build).
    """
    import episode
    import native_stack
    import taskspec

    with _variant(arm):
        spec = taskspec.load(task, variant=arm)
        episode.prepare_task(spec)
        world = episode.build_world(spec, base / "build" / "world", base / "build" / "grading")
        native_stack.install_workspace_cli(world, topology=topology)
        try:
            native_stack.stage_discovery(world, home=AGENT_HOME)
        except TypeError:                     # before package A's `home` landed
            native_stack.stage_discovery(world)
        native_stack.sanitize_world_files(world)
    world["_spec_virtual_time"] = spec.virtual_time
    world["_task"] = task
    return world


def _to_container(value: str, world_root: Path) -> str:
    """A host path under the world, spelled the container's way."""
    path = Path(value).resolve()
    try:
        rel = path.relative_to(world_root.resolve())
    except ValueError:
        return value
    if rel.parts and rel.parts[0] == "workspace":
        return "/workspace" + ("/" + rel.relative_to("workspace").as_posix()
                               if len(rel.parts) > 1 else "")
    return "/world" + ("" if rel == Path(".") else "/" + rel.as_posix())


def _reroot(value, world_root: Path):
    """Every host path under the world in a JSON value, spelled the container's way."""
    if isinstance(value, str):
        return _to_container(value, world_root) if value.startswith("/") else value
    if isinstance(value, list):
        return [_reroot(item, world_root) for item in value]
    if isinstance(value, dict):
        return {key: _reroot(item, world_root) for key, item in value.items()}
    return value


def gateway_config(world: dict, episode_id: str) -> dict:
    """What the generator ships as episode/gateway.json: the harness's own
    `episode.write_gateway_config` over the builder's manifest (the call the
    host NativeStack makes), with every path re-rooted to /world, /workspace
    and /audit."""
    import tempfile

    import episode

    src = Path(world["world_root"])
    host = {k: v for k, v in world.items() if not k.startswith("_")}
    with tempfile.TemporaryDirectory() as tmp:
        path = episode.write_gateway_config(
            Path(tmp) / "gateway.json", host, Path("/audit"), episode_id,
            world.get("virtual_time") or world.get("_spec_virtual_time") or "")
        config = json.loads(path.read_text(encoding="utf-8"))
    config = _reroot(config, src)
    assert config["world_root"] == "/world" and config["audit_dir"] == "/audit"
    assert str(src) not in json.dumps(config), "a host path survived the re-root"
    return config


def restore_lists(roots: list[tuple[Path, str]]) -> dict:
    """What a publish round trip loses, as the generator lists it in
    episode.json `restore` (empty directories outside .git, executable files),
    spelled in container paths."""
    empty, executable = [], []
    for root, prefix in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            here = Path(dirpath)
            rel = here.relative_to(root).as_posix()
            spelled = prefix if rel == "." else f"{prefix}/{rel}"
            if ".git" not in here.relative_to(root).parts \
                    and not dirnames and not filenames and rel != ".":
                empty.append(spelled)
            executable += [f"{spelled}/{name}" for name in filenames
                           if os.stat(here / name).st_mode & 0o111]
    return {"empty_dirs": sorted(empty), "executable_files": sorted(executable)}


def publish_round_trip(top: Path) -> None:
    """What `harbor publish` + download does to a tree: file bytes survive,
    every file comes back 0644, empty directories do not come back."""
    for dirpath, dirnames, filenames in os.walk(top, topdown=False):
        here = Path(dirpath)
        for name in filenames:
            if not (here / name).is_symlink():
                os.chmod(here / name, 0o644)
        os.chmod(here, 0o755)
        if here != top and not os.listdir(here):
            here.rmdir()


def shim_watch(world_root: Path) -> str:
    return ":".join(sorted(f"/world/var/warehouse/{p.name}"
                           for p in (world_root / "var" / "warehouse").glob("*.duckdb")))


def arrange(world: dict, out: Path, arm: str, *, layout: str = "root",
            gateway: bool = True) -> dict:
    """Lay the staged world out as the image has it.

    layout="root"     R/world, R/workspace, R/home/engineer,
                      R/opt/alkera/{episode,gateway}.json (the host tests' root prefix)
    layout="context"  out/episode/{world,workspace,home,episode.json,gateway.json}
                      (a Docker build context's per-task payload)
    gateway=False     no gateway.json and no `gateway_config` (the services then
                      derive serve.py's fixed-layout config)
    """
    from container_episode import reroot_profiles

    src = Path(world["world_root"])
    if layout == "root":
        world_dir, workspace_dir = out / "world", out / "workspace"
        home_dir = out / AGENT_HOME.lstrip("/")
        episode_json = out / "opt" / "alkera" / "episode.json"
    else:
        world_dir, workspace_dir = out / "episode" / "world", out / "episode" / "workspace"
        home_dir, episode_json = out / "episode" / "home", out / "episode" / "episode.json"
    gateway_json = episode_json.with_name("gateway.json")
    task = world.get("_task", TASK)
    shutil.copytree(src, world_dir, symlinks=True,
                    ignore=lambda d, names: ["workspace"] if Path(d) == src else [])
    shutil.copytree(src / "workspace", workspace_dir, symlinks=True)
    if (src / "home").is_dir():
        shutil.copytree(src / "home", home_dir, symlinks=True)
    home_dir.mkdir(parents=True, exist_ok=True)
    profiles = Path(world.get("profiles_dir") or "") / "profiles.yml"
    if profiles.is_file():
        text, _ = reroot_profiles(profiles.read_text(encoding="utf-8"), src)
        (home_dir / ".dbt").mkdir(parents=True, exist_ok=True)
        (home_dir / ".dbt" / "profiles.yml").write_text(text, encoding="utf-8")
    manifest = {k: (_to_container(v, src) if isinstance(v, str) and k != "grading" else v)
                for k, v in world.items() if not k.startswith("_")}
    manifest["grading"] = "/tests/episode/grading"
    env = {"ALKERA_TASK_VARIANT": arm, "ALKERA_SHIM_WATCH": shim_watch(src)}
    restore = restore_lists([(world_dir, "/world"), (workspace_dir, "/workspace"),
                             (home_dir, AGENT_HOME)])
    harbor_name = f"alkera/{task.lower()}__{arm}"
    document = {
        "schema": EPISODE_SCHEMA, "task": task, "variant": arm,
        "harbor_name": harbor_name,
        "seed": world.get("seed"), "virtual_now": world.get("_spec_virtual_time"),
        "profile": None, "plugs": {}, "reward_mode": "pass",
        "agent_user": "engineer", "agent_uid": AGENT_UID, "agent_home": AGENT_HOME,
        "world": manifest, "env": env, "artifact": "/workspace/final_report.json",
        "restore": restore,
    }
    episode_json.parent.mkdir(parents=True, exist_ok=True)
    if gateway:
        document["gateway_config"] = rt.GATEWAY_CONFIG
        gateway_json.write_text(json.dumps(gateway_config(world, harbor_name), indent=2,
                                           sort_keys=True) + "\n", encoding="utf-8")
    episode_json.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
    return document


# ------------------------------------------------------------------ helpers
#: Every port one services process binds, before the offset: the nine, the
#: aggregate /health and the scheduler plane (the MinIO receiver is off).
def base_ports() -> list[int]:
    from alkera_gateway.native import serve
    return sorted({*serve.STUB_PORTS.values(), serve.AGGREGATE_PORT, serve.SCHED_PORT})


def expected_hosts() -> set[str]:
    """The single source of truth: each service module's HOST, plus the two
    aliases serve.py mounts itself."""
    from alkera_gateway.native import serve, services

    loaded, missing = services.load()
    assert not missing, missing
    return ({module.HOST for module in loaded.values()}
            | {"native-services.internal", f"{serve.SCHED_NAME}.internal"})


def _free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def listening(ports) -> set[int]:
    """Which of `ports` have a LISTEN socket (TIME_WAIT left by the probes is not one)."""
    return set(ports) & set(rt.listening_sockets())


def free_offset(rng: random.Random) -> int:
    """An offset whose whole block is free now. Below the ephemeral range
    (32768+) and off native_stack's 10000-multiples, so concurrent agents'
    stacks on this host are unlikely to land on it; a race is retried."""
    candidates = [k * 1000 + 500 for k in range(1, 23)]
    rng.shuffle(candidates)
    for offset in candidates:
        if all(_free(p + offset) for p in [*base_ports(), 8110]):
            return offset
    pytest.skip("no free port block on this host")


def agent_env(episode_doc: dict) -> dict:
    """What Harbor's exec would carry: the image ENV the runtime checks, the
    task's [environment.env], and this venv for the services."""
    env = dict(os.environ)
    env.update(episode_doc["env"])
    env.update({
        "ALKERA_SHIM_BIN": "/opt/alkera/shims",
        "ALKERA_SHIM_WORKSPACE": "/workspace",
        "ALKERA_WORKSPACE": "/workspace",
        "ALKERA_NATIVE_AUDIT": "/audit/actions.jsonl",
        "ALKERA_SHIM_AUDIT_UID": str(AGENT_UID),
        # the shims first, as in the image; world-up must strip them for serve
        "PATH": os.pathsep.join(["/opt/alkera/shims", str(PY.parent), os.environ["PATH"]]),
        "PYTHONPATH": str(REPO / "world"),
    })
    env.pop("ALKERA_ROOT", None)
    return env


def run(tool: str, root: Path, *args: str, env: dict, timeout: int = 180):
    return subprocess.run([str(PY), str(BIN / tool), "--root", str(root), *args],
                          capture_output=True, text=True, env=env, timeout=timeout)


def get_health(port: int) -> tuple[int, dict]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def http(method: str, port: int, path: str, body: dict | None = None):
    """(status, decoded body) for one request to a service on 127.0.0.1."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                     method=method,
                                     headers={"content-type": "application/json"})
    try:
        with opener.open(request, timeout=60) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw.decode("utf-8", "replace")


def run_card(offset: int, name: str) -> tuple[int, object, dict]:
    """POST /api/card/<id>/query on Metabase for the card called `name`."""
    from alkera_gateway.native.services import metabase

    status, cards = http("GET", metabase.PORT + offset, "/api/card")
    assert status == 200, cards
    card = next(c for c in cards if c["name"] == name)
    status, body = http("POST", metabase.PORT + offset, f"/api/card/{card['id']}/query", {})
    return status, body, card


def instant_metric(offset: int, metric: str) -> tuple[int, object]:
    from alkera_gateway.native.services import prometheus

    return http("GET", prometheus.PORT + offset, f"/api/v1/query?query={metric}")


def read_rows(root: Path) -> list[dict]:
    path = root / "audit" / "actions.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def serve_pid(root: Path) -> int:
    return json.loads((root / "opt/alkera/run/serve.json").read_text())["pid"]


def alive(pid: int) -> bool:
    found = rt.proc_stat(pid)
    return found is not None and found[0] not in ("Z", "X")


def world_up(root: Path, env: dict, offset: int):
    return run("world-up", root, "--no-hosts", "--port-offset", str(offset),
               "--timeout", "90", env=env)


# --------------------------------------------------------------- the fixture
@pytest.fixture(scope="module", params=ARMS)
def staged(request, tmp_path_factory):
    """One arm's world, sealed under a fresh root prefix. Torn down after."""
    arm = request.param
    base = tmp_path_factory.mktemp(f"envrt-{arm}")
    world = build_world(arm, base)
    root = base / "R"
    document = arrange(world, root, arm)
    sealed = run("world-seal", root, env=agent_env(document))
    assert sealed.returncode == 0, sealed.stderr
    env = agent_env(document)
    yield {"arm": arm, "root": root, "doc": document, "env": env,
           "world": world, "seal": sealed.stdout}
    run("world-down", root, env=env)


@pytest.fixture(scope="module")
def offset():
    return free_offset(random.Random())


def up(staged, offset):
    """world-up, retried on another offset if a concurrent stack took ours."""
    result = world_up(staged["root"], staged["env"], offset)
    attempts = 0
    while result.returncode != 0 and "already held by" in result.stderr and attempts < 3:
        run("world-down", staged["root"], env=staged["env"])
        offset = free_offset(random.Random())
        staged["offset"] = offset
        result = world_up(staged["root"], staged["env"], offset)
        attempts += 1
    staged.setdefault("offset", offset)
    return result


def ensure_up(staged) -> None:
    """The world up (a first start, or world-up's no-op re-check), whatever
    ran before: every test that needs the services calls this, so any subset
    of this module runs on its own."""
    if "offset" not in staged:
        staged["offset"] = free_offset(random.Random())
    result = up(staged, staged["offset"])
    assert result.returncode == 0, result.stderr + result.stdout


# -------------------------------------------------------------------- seal
def test_seal_applies_the_permission_model(staged):
    root = staged["root"]
    assert mode(root / "world") == 0o711
    assert mode(root / "world" / "var") == 0o711
    planes = [p for p in (root / "world" / "var").iterdir() if p.name != "warehouse"]
    planes += [p for p in (root / "world").iterdir() if p.name not in ("var", "workspace")]
    assert planes, "the world has no planes to close"
    for plane in planes:
        assert mode(plane) == (0o700 if plane.is_dir() else 0o600), plane
    warehouse = root / "world" / "var" / "warehouse"
    assert mode(warehouse) == 0o2775
    assert all(mode(f) & 0o600 == 0o600 for f in warehouse.glob("*.duckdb"))
    link = root / "world" / "workspace"
    assert link.is_symlink() and os.readlink(link) == "../workspace"
    assert link.resolve() == (root / "workspace").resolve()
    assert mode(root / "audit") == 0o755
    assert mode(root / "opt" / "alkera" / "episode.json") == 0o600
    assert mode(root / "opt" / "alkera" / "gateway.json") == 0o600
    assert "gateway.json root 0600 (checked)" in staged["seal"]
    assert mode(root / AGENT_HOME.lstrip("/")) == 0o700
    assert "not root: modes applied" in staged["seal"]


def test_seal_is_idempotent(staged):
    again = run("world-seal", staged["root"], env=staged["env"])
    assert again.returncode == 0, again.stderr
    assert os.readlink(staged["root"] / "world" / "workspace") == "../workspace"


# ------------------------------------------------------------------- world-up
def test_world_up_refuses_an_environment_without_the_task_env(staged, offset):
    env = dict(staged["env"])
    env.pop("ALKERA_TASK_VARIANT")
    env["PATH"] = os.environ["PATH"]                  # and no shims in front
    state = staged["root"] / "opt/alkera/run/serve.json"
    before = state.read_bytes() if state.exists() else None
    result = world_up(staged["root"], env, offset)
    assert result.returncode == 1
    assert "ALKERA_TASK_VARIANT" in result.stderr
    assert "task.toml [environment.env] must set it" in result.stderr
    assert "PATH starts with" in result.stderr
    assert (state.read_bytes() if state.exists() else None) == before


def test_world_up_starts_every_service(staged, offset):
    result = up(staged, offset)
    assert result.returncode == 0, result.stderr + result.stdout
    offset = staged["offset"]
    root = staged["root"]
    rows = rt.read_destinations(rt.Layout(root))
    assert {r["host"] for r in rows} == expected_hosts()
    assert sorted(r["port"] for r in rows) == [p + offset for p in base_ports()]
    for row in rows:
        status, payload = get_health(row["port"])
        assert status in (200, 503), (row, status, payload)
        assert "service" in payload
    _, aggregate = get_health(rt_aggregate_port(offset))
    assert aggregate["ok"] is True and aggregate["missing"] == {}
    assert not listening([8110 + offset]), "the MinIO receiver must not be served"
    assert "watermark:" in result.stdout
    assert f"gateway config: {root / 'opt/alkera/run/gateway.json'}" in result.stdout


def rt_aggregate_port(offset: int) -> int:
    from alkera_gateway.native import serve
    return serve.AGGREGATE_PORT + offset


def test_watermark_is_written_once(staged):
    ensure_up(staged)
    root = staged["root"]
    mark = json.loads((root / "audit" / ".episode_start.json").read_text())
    assert set(mark) >= {"actions_lines", "at"}
    assert isinstance(mark["actions_lines"], int)
    assert mark["actions_lines"] <= rt.count_lines(root / "audit" / "actions.jsonl")
    # the episode.start row is the last one it counts, so it is never 0
    rows = read_rows(root)
    start = rows[mark["actions_lines"] - 1]
    assert start["tool"] == rt.EPISODE_START_TOOL
    assert start["seq"] == mark["actions_lines"] == mark["start_seq"] >= 1
    assert "actor:harbor" in start["flags"]
    assert start["op_class"] == "read" and start["targets"] == []
    assert start["args"]["variant"] == staged["arm"]
    assert [r for r in rows if r["tool"] == rt.EPISODE_START_TOOL] == [start]
    assert mark["variant"] == staged["arm"]
    assert mark["at"].endswith("Z")
    # one probe per destination, and nothing else, precedes the episode
    assert set(mark["destinations_baseline"].values()) == {1}


def test_audit_files_are_shared_with_the_agent(staged):
    ensure_up(staged)
    audit = staged["root"] / "audit"
    for name in ("actions.jsonl", "writer.lock"):
        assert mode(audit / name) == 0o664, name
    assert mode(audit) == 0o755
    assert mode(audit / ".episode_start.json") == 0o644


def test_second_world_up_is_a_no_op(staged):
    ensure_up(staged)
    root, offset = staged["root"], staged["offset"]
    mark = (root / "audit" / ".episode_start.json").read_bytes()
    destinations = (root / "audit" / "destinations.jsonl").read_bytes()
    pid = serve_pid(root)
    result = world_up(root, staged["env"], offset)
    assert result.returncode == 0, result.stderr
    assert "re-check: no connection opened" in result.stdout
    assert serve_pid(root) == pid and alive(pid)
    assert (root / "audit" / ".episode_start.json").read_bytes() == mark
    # no probe this time: not one connection counted
    assert (root / "audit" / "destinations.jsonl").read_bytes() == destinations


def test_services_run_without_the_agent_shims_and_with_the_arm(staged):
    ensure_up(staged)
    pid = serve_pid(staged["root"])
    environ = dict(item.split("=", 1) for item in
                   Path(f"/proc/{pid}/environ").read_bytes().decode().split("\0") if "=" in item)
    assert "/opt/alkera/shims" not in environ["PATH"].split(":")
    assert environ["ALKERA_TASK_VARIANT"] == staged["arm"]
    assert "JIRA_URL" not in environ and "ALKERA_SHIM_AUDIT_UID" not in environ
    argv = rt.proc_argv(pid)
    assert argv[argv.index("--minio-audit-port") + 1] == "0"
    assert argv[argv.index("--bind") + 1] == "127.0.0.1"
    # the manifest-derived config, re-rooted under the prefix; serve wrote none
    staged_config = staged["root"] / "opt/alkera/run/gateway.json"
    assert argv[argv.index("--gateway-config") + 1] == str(staged_config)
    config = json.loads(staged_config.read_text())
    assert config["world_root"] == str(staged["root"] / "world")
    assert config["audit_dir"] == str(staged["root"] / "audit")
    assert config["warehouse"] == str(staged["root"] / "world/var/warehouse/warehouse.duckdb")
    assert not (staged["root"] / "audit" / "native_gateway.json").exists()


def test_episode_start_row_is_inert_for_every_reader(staged):
    """No reader counts, charges or attributes the episode.start row."""
    ensure_up(staged)
    import blindness
    import path
    import report
    import taskspec
    from alkera_gateway.audit import validate_trace

    root = staged["root"]
    rows = read_rows(root)
    mark = json.loads((root / "audit" / ".episode_start.json").read_text())["actions_lines"]
    start = rows[mark - 1]
    with _variant(staged["arm"]):
        spec = taskspec.load(TASK, variant=staged["arm"])
    path_spec = spec.matrix.path
    assert path_spec is not None
    allow = report._path_allowlist(spec)
    for actions in ([start], rows[:mark]):
        charged = path.episode_path_penalty(path_spec, actions, allow)
        assert charged["penalty"] == 0 and charged["events"] == [], charged
    assert start not in blindness.subject_rows(rows, before_episode=mark)
    assert blindness.unscoped_shim_rows([start]) == []
    assert start not in report._actions_for({"actions": rows, "action_log_watermark": mark})
    validate_trace(root / "audit" / "actions.jsonl")      # 9.2.2 keys, contiguous seq


def test_the_dashboard_answers_through_the_manifest_config(staged):
    """GT001's BI plane runs its card against the world the manifest names."""
    ensure_up(staged)
    status, body, card = run_card(staged["offset"], "aov_daily")
    assert "warehouse.kpi_daily" in card["dataset_query"]["native"]["query"]
    assert status == 200, body
    assert body["status"] == "completed" and body["row_count"] > 0


def test_sched_plane_answers_the_workspace_cli(staged):
    """./ops/sched in remote mode, as the agent would run it."""
    ensure_up(staged)
    root, offset = staged["root"], staged["offset"]
    from alkera_gateway.native import serve
    env = dict(staged["env"])
    env["SCHED_URL"] = f"http://127.0.0.1:{serve.SCHED_PORT + offset}"
    listing = subprocess.run([str(root / "workspace" / "ops" / "sched"), "list"],
                             cwd=root / "workspace", env=env, capture_output=True,
                             text=True, timeout=120)
    assert listing.returncode == 0, listing.stderr
    assert listing.stdout.strip()


def test_world_up_restarts_after_a_crash(staged):
    ensure_up(staged)
    root, offset = staged["root"], staged["offset"]
    mark = (root / "audit" / ".episode_start.json").read_bytes()
    pid = serve_pid(root)
    os.killpg(pid, signal.SIGKILL)
    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    result = world_up(root, staged["env"], offset)
    assert result.returncode == 0, result.stderr
    assert "started the services" in result.stdout
    new = serve_pid(root)
    assert new != pid and alive(new)
    for row in rt.read_destinations(rt.Layout(root)):
        status, _ = get_health(row["port"])
        assert status in (200, 503)
    assert (root / "audit" / ".episode_start.json").read_bytes() == mark
    # the crashed process's counts are kept beside the new file
    assert list((root / "audit").glob("destinations.*.jsonl"))


def test_world_down_stops_everything(staged):
    ensure_up(staged)
    root, offset = staged["root"], staged["offset"]
    pid = serve_pid(root)
    result = run("world-down", root, env=staged["env"])
    assert result.returncode == 0
    assert "services stopped" in result.stdout
    assert "leftover agent" not in result.stdout       # never as a non-root user
    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not alive(pid)
    assert not listening([p + offset for p in base_ports()])
    assert not (root / "opt/alkera/run/serve.json").exists()
    again = run("world-down", root, env=staged["env"])
    assert again.returncode == 0 and "no services process was running" in again.stdout


def test_world_up_after_world_down_closes_the_planes_again(staged):
    root = staged["root"]
    ensure_up(staged)
    assert run("world-down", root, env=staged["env"]).returncode == 0
    assert mode(root / "world" / "var" / "sched") & 0o055, "world-down opens for collection"
    result = up(staged, staged["offset"])
    assert result.returncode == 0, result.stderr
    assert mode(root / "world" / "var" / "sched") == 0o700
    assert mode(root / "world") == 0o711
    down = run("world-down", root, env=staged["env"])
    assert down.returncode == 0


def test_world_down_stops_the_services_even_when_the_lock_is_held(staged):
    """A hung world-up must not keep the services up through collection."""
    import fcntl

    root = staged["root"]
    result = up(staged, staged["offset"])
    assert result.returncode == 0, result.stderr
    pid = serve_pid(root)
    with open(root / "opt/alkera/run/world-up.lock", "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        down = run("world-down", root, "--timeout", "2", env=staged["env"])
    assert down.returncode == 0
    assert "stopping the services without it" in down.stdout
    deadline = time.monotonic() + 10
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not alive(pid)


# ------------------------------------------------------------ pure helpers
def test_hosts_block_is_idempotent_and_lists_only_what_runs(tmp_path):
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1\tlocalhost\n::1\tlocalhost\n")
    assert rt.ensure_hosts(hosts, ["jira.internal", "sched.internal"])
    text = hosts.read_text()
    assert text.startswith("127.0.0.1\tlocalhost\n::1\tlocalhost\n")
    assert "127.0.0.1\tjira.internal" in text and "127.0.0.1\tsched.internal" in text
    assert not rt.ensure_hosts(hosts, ["sched.internal", "jira.internal"])
    assert rt.ensure_hosts(hosts, ["jira.internal"])          # a profile shrank the set
    text = hosts.read_text()
    assert "sched.internal" not in text
    assert text.count(rt.HOSTS_BEGIN) == 1 and text.count(rt.HOSTS_END) == 1


def test_hosts_are_written_in_place(tmp_path):
    """Docker bind-mounts /etc/hosts: the inode must survive."""
    hosts = tmp_path / "hosts"
    hosts.write_text("127.0.0.1\tlocalhost\n")
    inode = hosts.stat().st_ino
    rt.ensure_hosts(hosts, ["jira.internal"])
    assert hosts.stat().st_ino == inode


def test_serve_argv_carries_profile_and_plugs(tmp_path):
    layout = rt.Layout(tmp_path)
    argv = rt.serve_argv("/usr/local/bin/python3", layout,
                         {"profile": "de/laptop", "plugs": {"object_store": [],
                                                            "bi": ["metabase_fake"]}}, 0)
    assert argv[:3] == ["/usr/local/bin/python3", "-m", "alkera_gateway.native.serve"]
    assert "--port-offset" not in argv
    assert argv[argv.index("--profile") + 1] == "de/laptop"
    plugs = [argv[i + 1] for i, a in enumerate(argv) if a == "--plug"]
    assert plugs == ["bi=metabase_fake", "object_store="]
    assert argv[argv.index("--minio-audit-port") + 1] == "0"


def test_plugs_without_a_profile_are_refused(tmp_path):
    episode_json = tmp_path / "opt" / "alkera" / "episode.json"
    episode_json.parent.mkdir(parents=True)
    episode_json.write_text(json.dumps({"schema": EPISODE_SCHEMA, "env": {},
                                        "plugs": {"bi": []}}))
    with pytest.raises(rt.RuntimeFailure, match="--plug only together with --profile"):
        rt.load_episode(rt.Layout(tmp_path))


def test_serve_env_strips_the_shims(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/opt/alkera/shims:/usr/local/bin:/usr/bin")
    monkeypatch.setenv("JIRA_URL", "http://jira.internal:8080")
    env = rt.serve_env(rt.Layout(tmp_path), {"env": {"ALKERA_TASK_VARIANT": "decision"}})
    assert env["PATH"] == "/usr/local/bin:/usr/bin"
    assert env["ALKERA_TASK_VARIANT"] == "decision"
    assert "JIRA_URL" not in env


def test_seal_refuses_a_world_that_still_holds_its_workspace(tmp_path):
    (tmp_path / "world" / "workspace").mkdir(parents=True)
    (tmp_path / "workspace").mkdir()
    with pytest.raises(rt.RuntimeFailure, match="MINUS its workspace"):
        rt.seal(rt.Layout(tmp_path), {"agent_uid": AGENT_UID})


def test_missing_episode_json_is_an_actionable_failure(tmp_path):
    with pytest.raises(rt.RuntimeFailure, match="environment/episode/episode.json"):
        rt.load_episode(rt.Layout(tmp_path))


def test_layout_maps_container_paths_under_the_root(tmp_path):
    assert rt.Layout(Path("/")).at("/audit") == Path("/audit")
    assert rt.Layout(tmp_path).at("/opt/alkera/episode.json") == \
        tmp_path / "opt" / "alkera" / "episode.json"


# --------------------------------------------------------- the gateway config
@pytest.fixture(scope="module", params=ARMS)
def gt008(request, tmp_path_factory):
    """GT008 (two warehouses: nova primary, legacy attached) for one arm, up."""
    arm = request.param
    base = tmp_path_factory.mktemp(f"envrt-gt008-{arm}")
    world = build_world(arm, base, task=GT008)
    root = base / "R"
    document = arrange(world, root, arm)
    env = agent_env(document)
    sealed = run("world-seal", root, env=env)
    assert sealed.returncode == 0, sealed.stderr
    state = {"arm": arm, "root": root, "doc": document, "env": env, "world": world}
    result = up(state, free_offset(random.Random()))
    assert result.returncode == 0, result.stderr + result.stdout
    yield state
    run("world-down", root, env=env)


def test_gt008_config_declares_the_legacy_warehouse(gt008):
    config = json.loads((gt008["root"] / "opt/alkera/gateway.json").read_text())
    assert config["warehouse_name"] == "nova"
    assert [(w["name"], w["path"], w["attach"]) for w in config["warehouses"]] == \
        [("legacy", "/world/var/warehouse/legacy.duckdb", True)]
    assert config["observability"] == "/world/var/obs/definitions.yaml"


def test_gt008_dashboard_reads_legacy_through_the_services(gt008):
    """Finance's card reads warehouse.v_revenue_daily, which (before the
    cutover) is a view over legacy.warehouse.rpt_revenue_daily."""
    status, body, card = run_card(gt008["offset"], "revenue_by_day")
    assert "warehouse.v_revenue_daily" in card["dataset_query"]["native"]["query"]
    assert status == 200, body
    assert body["status"] == "completed" and body["row_count"] > 0


def test_gt008_metric_reads_legacy_through_the_services(gt008):
    """The observability plane's consumer_days reads the same view."""
    status, body = instant_metric(gt008["offset"], "consumer_days")
    assert status == 200, body
    assert body["status"] == "success", body
    values = [float(sample["value"][1]) for sample in body["data"]["result"]]
    assert values and values[0] > 0, body


def test_gt008_without_the_manifest_config_legacy_is_not_attached(gt008, tmp_path):
    """The control: the same world on serve.py's fixed-layout config (no
    `gateway_config`) cannot resolve the legacy catalog - what package D3's
    GT008 oracle measured. Proves the two tests above exercise the attach."""
    if gt008["arm"] != "default":
        pytest.skip("one control is enough")
    root = tmp_path / "R"
    document = arrange(gt008["world"], root, gt008["arm"], gateway=False)
    env = agent_env(document)
    assert run("world-seal", root, env=env).returncode == 0
    state = {"root": root, "env": env}
    result = up(state, free_offset(random.Random()))
    try:
        assert result.returncode == 0, result.stderr
        assert "--gateway-config" not in rt.proc_argv(serve_pid(root))
        status, body, _ = run_card(state["offset"], "revenue_by_day")
        assert status != 200, body
        failed = [r for r in read_rows(root)
                  if r["tool"] == "metabase.bi_render" and r["exit"] == "error"]
        evidence = json.dumps(failed) + (root / "var/log/alkera/serve.log").read_text()
        assert 'Catalog \\"legacy\\" does not exist' in evidence \
            or 'Catalog "legacy" does not exist' in evidence, evidence[-2000:]
    finally:
        run("world-down", root, env=env)


def test_world_up_fails_loudly_when_the_named_config_is_missing(tmp_path):
    world = build_world("default", tmp_path)
    root = tmp_path / "R"
    document = arrange(world, root, "default")
    env = agent_env(document)
    assert run("world-seal", root, env=env).returncode == 0
    (root / "opt/alkera/gateway.json").unlink()
    result = world_up(root, env, free_offset(random.Random()))
    assert result.returncode == 1
    assert "names gateway_config '/opt/alkera/gateway.json'" in result.stderr
    assert "environment/episode/gateway.json" in result.stderr
    assert not (root / "opt/alkera/run/serve.json").exists()
    assert not (root / "audit" / "destinations.jsonl").exists()
    assert not (root / "audit" / "actions.jsonl").exists()


def _config_layout(tmp_path: Path, **change) -> tuple[rt.Layout, dict]:
    (tmp_path / "world/var/warehouse").mkdir(parents=True)
    (tmp_path / "world/var/warehouse/warehouse.duckdb").write_bytes(b"")
    config = {"world_root": "/world", "audit_dir": "/audit", "workspace": "/workspace",
              "warehouse": "/world/var/warehouse/warehouse.duckdb",
              "virtual_time": "2026-05-12T09:00:00Z", **change}
    (tmp_path / "opt/alkera").mkdir(parents=True)
    (tmp_path / "opt/alkera/gateway.json").write_text(json.dumps(config))
    return rt.Layout(tmp_path), {"gateway_config": rt.GATEWAY_CONFIG}


@pytest.mark.parametrize("change, message", [
    ({"audit_dir": "/tmp/audit"}, "audit_dir is '/tmp/audit'"),
    ({"world_root": "/srv/world"}, "world_root is '/srv/world'"),
    ({"scheduler": "/home/exporter/w/var/sched/state.json"}, "not under /world"),
    ({"warehouses": [{"name": "legacy", "backend": "duckdb", "attach": True,
                      "path": "/world/var/warehouse/legacy.duckdb"}]},
     r"warehouses\[0\]\.path '/world/var/warehouse/legacy.duckdb': no such warehouse"),
])
def test_an_unusable_gateway_config_is_refused(tmp_path, change, message):
    layout, episode = _config_layout(tmp_path, **change)
    with pytest.raises(rt.RuntimeFailure, match=message):
        rt.load_gateway_config(layout, episode)


def test_the_gateway_config_is_rerooted_under_a_prefix(tmp_path):
    layout, episode = _config_layout(tmp_path, warehouses=[], obs_samples="/world/var/obs/s.jsonl")
    arg = rt.stage_gateway_config(layout, episode)
    assert arg == str(tmp_path / "opt/alkera/run/gateway.json")
    staged = json.loads(Path(arg).read_text())
    assert staged["world_root"] == str(tmp_path / "world")
    assert staged["workspace"] == str(tmp_path / "workspace")
    assert staged["obs_samples"] == str(tmp_path / "world/var/obs/s.jsonl")
    assert staged["virtual_time"] == "2026-05-12T09:00:00Z"
    assert mode(Path(arg)) == 0o600
    # in the image the named file is handed over as is
    assert rt.gateway_config_arg(rt.Layout(Path("/")), episode) == rt.GATEWAY_CONFIG
    assert rt.gateway_config_arg(layout, {}) is None
    argv = rt.serve_argv("python3", layout, episode, 0)
    assert argv[argv.index("--gateway-config") + 1] == arg
    assert "--gateway-config" not in rt.serve_argv("python3", layout, {}, 0)


def test_seal_refuses_a_shipped_config_episode_json_does_not_name(tmp_path):
    (tmp_path / "world" / "var").mkdir(parents=True)
    (tmp_path / "workspace").mkdir()
    (tmp_path / "opt" / "alkera").mkdir(parents=True)
    (tmp_path / "opt" / "alkera" / "gateway.json").write_text("{}")
    with pytest.raises(rt.RuntimeFailure, match="names no `gateway_config`"):
        rt.seal(rt.Layout(tmp_path), {"agent_uid": AGENT_UID})


# ---------------------------------------------------- publish round trip
def test_seal_restores_what_a_publish_round_trip_drops(tmp_path):
    """Every file 0644 and no empty directory, as `harbor publish` + download
    returns a task: world-seal brings back the empty dirs (as closed planes) and
    the CLI's exec bit, and leaves the verifier's zone to the verifier."""
    world = build_world("decision", tmp_path)
    root = tmp_path / "R"
    document = arrange(world, root, "decision")
    restore = document["restore"]
    assert "/workspace/ops/sched" in restore["executable_files"]
    assert {"/world/build", "/world/var/ledger"} <= set(restore["empty_dirs"])
    restore["empty_dirs"].append("/tests/episode/grading/pre_repair_world/build")
    (root / "opt/alkera/episode.json").write_text(json.dumps(document))
    publish_round_trip(root)
    assert not (root / "world/build").exists() and not (root / "world/var/ledger").exists()
    assert not os.access(root / "workspace/ops/sched", os.X_OK)

    env = agent_env(document)
    sealed = run("world-seal", root, env=env)
    assert sealed.returncode == 0, sealed.stderr
    assert (f"restored {len(restore['empty_dirs']) - 1} empty dir(s), "
            f"+x on {len(restore['executable_files'])} file(s)") in sealed.stdout
    assert "left 1 verifier-zone restore entry to the verifier" in sealed.stdout
    assert not (root / "tests").exists()
    for plane in ("world/build", "world/var/ledger"):
        assert (root / plane).is_dir() and mode(root / plane) == 0o700, plane
    assert mode(root / "world" / "var") == 0o711
    sched = root / "workspace/ops/sched"
    assert os.access(sched, os.X_OK)
    done = subprocess.run([str(sched), "--help"], capture_output=True, text=True,
                          env=env, cwd=root / "workspace", timeout=120)
    assert done.returncode == 0 and "ops/sched" in done.stdout, done.stderr
    for argv in (["status", "--short"], ["log", "--oneline", "-1"]):
        git = subprocess.run(["git", "-C", str(root / "workspace"), *argv],
                             capture_output=True, text=True, timeout=60)
        assert git.returncode == 0, git.stderr
    # and the world it seals still comes up
    state = {"root": root, "env": env}
    result = up(state, free_offset(random.Random()))
    try:
        assert result.returncode == 0, result.stderr
    finally:
        run("world-down", root, env=env)


def _restore_layout(tmp_path: Path) -> rt.Layout:
    for rel in ("world/var/warehouse", "workspace/ops", "home/engineer", "etc"):
        (tmp_path / rel).mkdir(parents=True)
    (tmp_path / "workspace/ops/sched").write_text("#!/bin/sh\n")
    (tmp_path / "etc/passwd").write_text("root:x:0:0\n")
    (tmp_path / "workspace/escape").symlink_to("../etc")
    return rt.Layout(tmp_path)


@pytest.mark.parametrize("kind, entry, message", [
    ("executable_files", "/etc/passwd", "is outside /world, /workspace, /home/engineer"),
    ("executable_files", "/opt/alkera/bin/world-up", "is outside"),
    ("empty_dirs", "/var/spool/cron", "is outside"),
    ("empty_dirs", "/workspace/../etc/cron.d", "is outside"),
    ("executable_files", "/workspace/escape/passwd", "resolves to"),
    ("empty_dirs", "/workspace", "is outside"),
    ("empty_dirs", "workspace/relative", "not an absolute container path"),
    ("executable_files", "/workspace/ops/missing", "no such file in the payload"),
])
def test_restore_refuses_entries_outside_the_payload(tmp_path, kind, entry, message):
    layout = _restore_layout(tmp_path)
    episode = {"agent_home": AGENT_HOME, "restore": {kind: [entry]}}
    with pytest.raises(rt.RuntimeFailure, match=message):
        rt.restore_payload(layout, episode)
    assert not os.access(tmp_path / "etc/passwd", os.X_OK)


def test_restore_is_idempotent_and_tolerates_no_list(tmp_path):
    layout = _restore_layout(tmp_path)
    episode = {"agent_home": AGENT_HOME,
               "restore": {"empty_dirs": ["/world/var/ledger", "/home/engineer/.cache/x"],
                           "executable_files": ["/workspace/ops/sched"]}}
    for _ in range(2):
        assert rt.restore_payload(layout, episode)[0] == \
            "restored 2 empty dir(s), +x on 1 file(s)"
    assert (tmp_path / "home/engineer/.cache/x").is_dir()
    assert os.access(tmp_path / "workspace/ops/sched", os.X_OK)
    assert rt.restore_payload(layout, {})[0] == "restored 0 empty dir(s), +x on 0 file(s)"
