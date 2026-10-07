"""world-up's supervised backends and the services' private state, on the host.

The `episode.json` `backends` contract (alkera_runtime.py's module docstring):
a process the world needs beside the services is declared - name, argv, user,
ports, hosts, health - and world-up spawns it in its own session, waits inside
the one start-up budget until every declared port is LISTENed by a process of
that session (read from /proc, never dialled), and only then aliases its hosts.
world-down stops it, in reverse order, before the services. Plan Task 10 fills
the list for real backends; here a stub (`python -m http.server`) stands in.

The services' state (`/var/lib/alkera/service-state`, root 0700) reaches the
verifier as world-down's copy in the audit artifact. The last test runs the
EXPORTER on BI001 and the runtime on what it wrote: the three BI vendors keep
their published state under the profiled gateway config's `bi_state_dir`, and
world-down copies it to `/audit/service-state/bi`.

No Docker: the image layout under a root prefix, as test_env_runtime.py does
(its fixture helpers are reused, not copied).

    gateway/.venv/bin/python -m pytest -q harbor/tests/test_world_up_backends.py
"""
from __future__ import annotations

import json
import os
import pwd
import random
import shutil
import socket
import sys
import time
from pathlib import Path

import pytest

import test_env_runtime as envrt

REPO = Path(__file__).resolve().parents[2]
HARNESS = REPO / "tasks" / "harness"
for path in (envrt.BIN, HARNESS, REPO / "harbor"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import alkera_runtime as rt  # noqa: E402

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                reason="the runtime reads /proc")

ME = pwd.getpwuid(os.getuid()).pw_name
BI001 = "BI001-revenue-disagrees-across-bi"


def free_port() -> int:
    """A port nothing listens on now, outside every serve port block."""
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        if port not in rt.listening_sockets():
            return port
    pytest.skip("no free port")


def stub(name: str, port: int, *, argv: list[str] | None = None,
         health: str = "tcp") -> dict:
    return {"name": name,
            "argv": argv or [sys.executable, "-m", "http.server", str(port),
                             "--bind", "127.0.0.1"],
            "user": ME, "ports": [port], "hosts": [f"{name}.internal"],
            "health": health}


@pytest.fixture(scope="module")
def gt001(tmp_path_factory):
    """GT001's default world, built and staged once (test_env_runtime's builder)."""
    return envrt.build_world("default", tmp_path_factory.mktemp("backends-world"))


@pytest.fixture
def box(gt001, tmp_path):
    """A fresh, sealed root prefix for one test; world-down afterwards, always."""
    root = tmp_path / "R"
    document = envrt.arrange(gt001, root, "default")
    state = {"root": root, "doc": document, "env": envrt.agent_env(document)}

    def declare(backends: list[dict]) -> None:
        document["backends"] = backends
        (root / "opt/alkera/episode.json").write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        sealed = envrt.run("world-seal", root, env=state["env"])
        assert sealed.returncode == 0, sealed.stderr

    state["declare"] = declare
    yield state
    envrt.run("world-down", root, env=state["env"])


def world_up(box, *args: str, timeout: float = 90):
    """world-up WITH the hosts file (R/etc/hosts under the prefix)."""
    offset = envrt.free_offset(random.Random())
    return envrt.run("world-up", box["root"], "--port-offset", str(offset),
                     "--timeout", str(timeout), *args, env=box["env"])


def hosts_block(root: Path) -> list[str]:
    path = root / "etc" / "hosts"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    if rt.HOSTS_BEGIN not in lines:
        return []
    return lines[lines.index(rt.HOSTS_BEGIN) + 1:lines.index(rt.HOSTS_END)]


# ------------------------------------------------------------- the contract
@pytest.mark.parametrize("bad,match", [
    ({"argv": []}, "argv must be a non-empty list"),
    ({"ports": [0]}, "ports must be distinct integers"),
    ({"ports": [5432, 5432]}, "ports must be distinct integers"),
    ({"hosts": ["not a host"]}, "hosts must be DNS names"),
    ({"health": "ping"}, "health must be one of"),
    ({"user": "Root!"}, "is not a user name"),
    ({"name": "Bad Name"}, "is not a unique"),
    ({"extra": 1}, "exactly the keys"),
])
def test_backends_contract_is_checked(bad, match):
    spec = {**stub("pg", 5432), **bad}
    with pytest.raises(rt.RuntimeFailure, match=match):
        rt.load_backends({"backends": [spec]})


def test_backends_contract_refuses_shared_ports_and_names():
    with pytest.raises(rt.RuntimeFailure, match="is also pg's"):
        rt.load_backends({"backends": [stub("pg", 5432), stub("proxy", 5432)]})
    with pytest.raises(rt.RuntimeFailure, match="is not a unique"):
        rt.load_backends({"backends": [stub("pg", 5432), stub("pg", 5433)]})
    assert rt.load_backends({}) == [] and rt.load_backends({"backends": None}) == []


def test_episode_json_backends_are_checked_at_build(gt001, tmp_path):
    """world-seal (the Dockerfile's last RUN) loads episode.json: a malformed
    `backends` fails the image build, not the first trial."""
    root = tmp_path / "R"
    document = envrt.arrange(gt001, root, "default")
    document["backends"] = [{"name": "pg"}]
    (root / "opt/alkera/episode.json").write_text(json.dumps(document), encoding="utf-8")
    sealed = envrt.run("world-seal", root, env=envrt.agent_env(document))
    assert sealed.returncode == 1 and "backends[0]" in sealed.stderr


# ------------------------------------------------------------- world-up
def test_world_up_supervises_declared_backend(box):
    port = free_port()
    box["declare"]([stub("stub", port)])
    result = world_up(box)
    assert result.returncode == 0, result.stderr + result.stdout
    root = box["root"]
    state = json.loads((root / "opt/alkera/run/backends.json").read_text())
    pid = state["stub"]["pid"]
    assert rt.backend_alive(state["stub"]) and rt.proc_session(pid) == pid
    # the port is LISTENed by a process of the backend's own session
    assert rt.listening_sockets()[port] & rt.session_socket_inodes(pid)
    # ... and only then aliased, with the services' own names
    block = hosts_block(root)
    assert "127.0.0.1\tstub.internal" in block
    assert "127.0.0.1\tjira.internal" in block
    mark = json.loads((root / "audit" / rt.WATERMARK).read_text())
    assert mark["backends"] == {"stub": {"ports": [port], "hosts": ["stub.internal"],
                                         "health": "tcp"}}
    assert mark["backends_probed"] == {}            # tcp: nothing was dialled
    start = [json.loads(line) for line in (root / "audit/actions.jsonl").read_text()
             .splitlines() if line.strip()][mark["actions_lines"] - 1]
    assert start["tool"] == rt.EPISODE_START_TOOL
    assert start["result"]["backends"] == [f"stub.internal:{port}"]
    # world-down stops it before the services, and forgets it
    down = envrt.run("world-down", root, env=box["env"])
    assert down.returncode == 0
    assert "backend stub stopped" in down.stdout
    assert down.stdout.index("backend stub stopped") < down.stdout.index("services stopped")
    assert envrt.alive(pid) is False
    assert port not in rt.listening_sockets()
    assert not (root / "opt/alkera/run/backends.json").exists()


def test_world_up_keeps_a_running_backend(box):
    port = free_port()
    box["declare"]([stub("stub", port)])
    offset = envrt.free_offset(random.Random())
    first = envrt.run("world-up", box["root"], "--port-offset", str(offset),
                      env=box["env"])
    assert first.returncode == 0, first.stderr
    pid = json.loads((box["root"] / "opt/alkera/run/backends.json").read_text())["stub"]["pid"]
    second = envrt.run("world-up", box["root"], "--port-offset", str(offset),
                       env=box["env"])
    assert second.returncode == 0, second.stderr
    assert f"backend stub: running (pid {pid})" in second.stdout
    assert "re-check: no connection opened" in second.stdout


def test_world_up_fails_when_backend_never_listens(box):
    """A backend that is alive but never binds: world-up exits non-zero within
    its budget, says which backend and why, and writes no host for it (nor any
    other: no name is handed out before every backend listens)."""
    port = free_port()
    box["declare"]([stub("never", port, argv=[sys.executable, "-c",
                                               "import time; time.sleep(600)"])])
    budget = 8.0
    started = time.monotonic()
    result = world_up(box, timeout=budget)
    elapsed = time.monotonic() - started
    assert result.returncode == 1
    assert "backend never did not listen on" in result.stderr
    assert elapsed < budget + 30, elapsed
    assert not any("never.internal" in line for line in hosts_block(box["root"]))
    assert not (box["root"] / "audit" / rt.WATERMARK).exists()


def test_world_up_fails_when_backend_exits(box):
    port = free_port()
    box["declare"]([stub("crash", port, argv=[sys.executable, "-c",
                                               "import sys; print('boom'); sys.exit(3)"])])
    result = world_up(box, timeout=20)
    assert result.returncode == 1
    assert "backend crash" in result.stderr and "exited during start-up" in result.stderr
    assert "boom" in result.stderr                   # the backend's own log tail
    assert hosts_block(box["root"]) == []


def test_world_up_refuses_a_port_held_by_another_process(box):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen()
        port = squatter.getsockname()[1]
        box["declare"]([stub("stub", port)])
        result = world_up(box, timeout=20)
    assert result.returncode == 1
    assert f"port {port} is held by" in result.stderr
    assert "not a process world-up supervises" in result.stderr


def test_http_health_is_probed_once_and_recorded(box, tmp_path):
    served = tmp_path / "served"
    served.mkdir()
    (served / "health").write_text("ok\n")
    port = free_port()
    argv = [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1",
            "--directory", str(served)]
    box["declare"]([stub("web", port, argv=argv, health="http")])
    result = world_up(box)
    assert result.returncode == 0, result.stderr + result.stdout
    mark = json.loads((box["root"] / "audit" / rt.WATERMARK).read_text())
    assert mark["backends_probed"] == {"web": f"web.internal:{port}"}
    # anything but a 2xx fails the probe
    (served / "health").unlink()
    with pytest.raises(rt.RuntimeFailure, match="answered 404"):
        rt.probe_backends(rt.load_backends(box["doc"]))


# ------------------------------------------------------------- service state
def test_world_down_copies_service_state(box):
    """world-up makes the services' private directory (0700); world-down copies
    what they kept into the audit artifact, minus what bistate never stages."""
    box["declare"]([])
    result = world_up(box)
    assert result.returncode == 0, result.stderr
    state = box["root"] / rt.SERVICE_STATE.lstrip("/")
    assert state.is_dir() and envrt.mode(state) == 0o700
    (state / "bi" / "sigma").mkdir(parents=True)
    (state / "bi" / "sigma" / "state.json").write_text('{"published": 1}\n')
    (state / "bi" / "powerbi" / "snapshots").mkdir(parents=True)
    (state / "bi" / "powerbi" / "snapshots" / "x.duckdb").write_bytes(b"cache")
    (state / "bi" / "powerbi" / "datasets.json").write_text("{}\n")
    (state / "bi" / "tableau.lock").write_text("")
    down = envrt.run("world-down", box["root"], env=box["env"])
    assert "service state copied for the verifier" in down.stdout
    copy = box["root"] / "audit" / rt.SERVICE_STATE_COPY
    files = sorted(p.relative_to(copy).as_posix() for p in copy.rglob("*") if p.is_file())
    assert files == ["bi/powerbi/datasets.json", "bi/sigma/state.json"]
    # readable for collection, like the rest of /audit
    assert envrt.mode(copy / "bi" / "sigma" / "state.json") & 0o444 == 0o444
    # a second world-down replaces the copy whole
    (state / "bi" / "sigma" / "state.json").unlink()
    envrt.run("world-down", box["root"], env=box["env"])
    assert not (copy / "bi" / "sigma" / "state.json").exists()


def test_state_skip_rules_are_bistates():
    import bistate

    assert rt.STATE_SKIP_DIRS == bistate.SKIP_DIRS
    assert rt.STATE_SKIP_SUFFIXES == bistate.SKIP_SUFFIXES
    assert rt.SERVICE_STATE_COPY == bistate.SERVICE_STATE_REL.as_posix()


def test_gateway_config_may_keep_bi_state_in_service_state(tmp_path):
    layout = rt.Layout(tmp_path)
    (tmp_path / "world/var/warehouse").mkdir(parents=True)
    (tmp_path / "world/var/warehouse/warehouse.duckdb").write_bytes(b"")
    config = {"world_root": "/world", "audit_dir": "/audit", "workspace": "/workspace",
              "warehouse": "/world/var/warehouse/warehouse.duckdb",
              "bi_state_dir": f"{rt.SERVICE_STATE}/bi"}
    (tmp_path / "opt/alkera").mkdir(parents=True)
    (tmp_path / "opt/alkera/gateway.json").write_text(json.dumps(config))
    episode = {"gateway_config": rt.GATEWAY_CONFIG}
    assert rt.load_gateway_config(layout, episode)["bi_state_dir"] == f"{rt.SERVICE_STATE}/bi"
    staged = json.loads(Path(rt.stage_gateway_config(layout, episode)).read_text())
    assert staged["bi_state_dir"] == str(tmp_path / rt.SERVICE_STATE.lstrip("/") / "bi")
    # only the state keys may leave /world, /workspace and /audit
    for key in ("profiles_dir", "bi_state_dir"):
        bad = {**config, key: "/tmp/elsewhere"}
        (tmp_path / "opt/alkera/gateway.json").write_text(json.dumps(bad))
        with pytest.raises(rt.RuntimeFailure, match=key):
            rt.load_gateway_config(layout, episode)
    config["profiles_dir"] = f"{rt.SERVICE_STATE}/dbt"
    (tmp_path / "opt/alkera/gateway.json").write_text(json.dumps(config))
    with pytest.raises(rt.RuntimeFailure, match="profiles_dir"):
        rt.load_gateway_config(layout, episode)


# ------------------------------------------------- BI001, exporter + runtime
@pytest.fixture(scope="module")
def bi001_task(tmp_path_factory):
    """BI001:default exactly as the exporter writes it (no Docker)."""
    from alkera_harbor.adapter import AlkeraHarborAdapter
    from alkera_harbor.naming import ArmRef

    out = tmp_path_factory.mktemp("bi001-export")
    adapter = AlkeraHarborAdapter(output_dir=out, task_ids=[f"{BI001}:default"],
                                  log=lambda *a: None)
    adapter._snapshot_templates(out / ".templates")
    result = adapter.generate_task(ArmRef(BI001, "default"), out / "task")
    assert result.status == "ok"
    return out / "task"


def lay_out(task: Path, root: Path) -> dict:
    """environment/episode/ -> the image's paths under `root` (the Dockerfile's
    per-task COPY block)."""
    episode = task / "environment" / "episode"
    shutil.copytree(episode / "world", root / "world", symlinks=True)
    shutil.copytree(episode / "workspace", root / "workspace", symlinks=True)
    shutil.copytree(episode / "home", root / "home" / "engineer", symlinks=True)
    (root / "opt" / "alkera").mkdir(parents=True)
    shutil.copyfile(episode / "episode.json", root / "opt/alkera/episode.json")
    shutil.copyfile(episode / "gateway.json", root / "opt/alkera/gateway.json")
    return json.loads((episode / "episode.json").read_text(encoding="utf-8"))


def test_bi_vendor_state_reaches_the_audit_copy(bi001_task, tmp_path):
    """The profiled gateway config keeps the vendors' state in the services'
    private directory (not /audit/bi, which the services would otherwise
    default to), the vendors publish their provisioned state there at start,
    and world-down's copy is what the verifier grades as `{bi_state}`."""
    config = json.loads((bi001_task / "environment/episode/gateway.json").read_text())
    assert config["bi_state_dir"] == f"{rt.SERVICE_STATE}/bi"
    assert config["profile"] == "da/laptop"
    root = tmp_path / "R"
    document = lay_out(bi001_task, root)
    assert document["backends"] == [] and document["profile"] == "da/laptop"
    env = envrt.agent_env(document)
    sealed = envrt.run("world-seal", root, env=env)
    assert sealed.returncode == 0, sealed.stderr
    try:
        up = envrt.run("world-up", root, "--no-hosts", "--port-offset",
                       str(envrt.free_offset(random.Random())), env=env)
        assert up.returncode == 0, up.stderr + up.stdout
        published = root / rt.SERVICE_STATE.lstrip("/") / "bi"
        assert {p.name for p in published.iterdir()} >= {"powerbi", "tableau", "sigma"}
        assert not (root / "audit" / "bi").exists()
    finally:
        down = envrt.run("world-down", root, env=env)
    copy = root / "audit" / rt.SERVICE_STATE_COPY / "bi"
    assert {p.name for p in copy.iterdir()} >= {"powerbi", "tableau", "sigma"}, down.stdout
    assert not [p for p in copy.rglob("*") if p.suffix == ".duckdb"]
