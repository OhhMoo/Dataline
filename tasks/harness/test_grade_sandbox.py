"""episode.grade(..., sandbox="container"): the grader in a box (decision D4).

    gateway/.venv/bin/python -m pytest tasks/harness/test_grade_sandbox.py -q

The container cases need a docker daemon and the grader image
(`ALKERA_GRADE_IMAGE`, default alkera-episode:head) and skip without them; run
them under scripts/docker_slot.sh. The rest is pure.

A grader parses files the agent wrote. In the container topology it used to run
on the host as the operator, with the harness's whole environment (model
tokens included) and the host's network. Under `sandbox="container"` it runs
in `docker run --network none --read-only --cap-drop ALL`, as the host uid,
seeing only what is mounted: the repository and the interpreter read-only, the
world, the grading zone and the artefact read-only, the rollout directory
writable, and a five-key environment.

What is pinned, and why each matters:

- REVIEW FOCUS 4: the grade JSON a container writes is the host's, byte for
  byte, over the GT001 oracle and the BI001 default oracle. A sandbox that
  moved a score - locale, TZ, hash seed, an absolute path in a detail - would
  be a different benchmark.
- The box: no network (a host listener the host-mode probe reaches is
  unreachable), `os.environ` is exactly PYTHONPATH, PYTHONUTF8, PYTHONHASHSEED,
  TZ and LC_ALL (a host variable the host-mode probe sees does not arrive),
  the world and the grading zone refuse writes, the rollout directory takes
  them, nothing unmounted is visible, no capabilities, no_new_privs.
- The docker argv itself, without docker: every flag of the box.
- Which sandbox a rollout gets: the container topology's default is
  `container`, the host topology keeps `host`, ALKERA_GRADE_SANDBOX overrides
  both, and an unknown value is refused rather than guessed. Harbor's verifier
  (its own container already) never passes `sandbox` and keeps `host`.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import episode  # noqa: E402
import taskspec  # noqa: E402

FIVE = {"PYTHONPATH", "PYTHONUTF8", "PYTHONHASHSEED", "TZ", "LC_ALL"}


@pytest.fixture
def box():
    ok, why = episode.grade_sandbox_available()
    if not ok:
        pytest.skip(f"container grading not exercised: {why}")


# ------------------------------------------------------------------ pure
def test_sandbox_command_is_the_locked_down_box(tmp_path):
    world, out = tmp_path / "r0" / "world", tmp_path / "r0"
    world.mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    argv = episode.sandbox_command(
        [str(episode.PY), "grade.py", "--json"],
        read_only=[world, tmp_path / "elsewhere", tmp_path / "missing"], writable=out,
        image="some/image:tag")
    assert argv[:3] == ["docker", "run", "--rm"]
    joined = " ".join(argv)
    for flag in ("--network none", "--read-only", "--tmpfs /tmp", "--cap-drop ALL",
                 "--security-opt no-new-privileges", "--pull never",
                 f"--user {os.getuid()}:{os.getgid()}"):
        assert flag in joined, flag
    image_at = argv.index("some/image:tag")
    assert argv[image_at - 2:image_at] == ["--entrypoint", "/usr/bin/env"]
    tail = argv[image_at + 1:]
    assert tail[0] == "-i"
    env = dict(a.split("=", 1) for a in tail[1:1 + len(FIVE)])
    assert env == {"PYTHONPATH": str(episode.TRACK_A), "PYTHONUTF8": "1",
                   "PYTHONHASHSEED": "0", "TZ": "UTC", "LC_ALL": "C.UTF-8"}
    assert tail[1 + len(FIVE):] == [str(episode.PY), "grade.py", "--json"]
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "--mount"]
    by_target = {}
    for m in mounts:
        fields = dict(f.partition("=")[::2] for f in m.split(","))
        by_target[fields["target"]] = fields
    # every mount is a bind at its own absolute path; only the rollout dir is writable
    assert by_target[str(world)]["source"] == str(world)
    assert "readonly" in by_target[str(world)]
    assert "readonly" not in by_target[str(out)]
    assert "readonly" in by_target[str(REPO)]
    assert str(tmp_path / "missing") not in by_target          # nothing invented
    assert [t for t, f in by_target.items() if "readonly" not in f] == [str(out)]
    assert all(f["type"] == "bind" for f in by_target.values())


def test_a_path_inside_a_read_only_mount_is_not_mounted_twice(tmp_path):
    world = tmp_path / "world"
    (world / "workspace").mkdir(parents=True)
    artefact = world / "workspace" / "final_report.json"
    artefact.write_text("{}", encoding="utf-8")
    out = tmp_path / "r0"
    out.mkdir()
    argv = episode.sandbox_command(["true"], read_only=[world, artefact], writable=out)
    targets = [dict(f.partition("=")[::2] for f in argv[i + 1].split(","))["target"]
               for i, a in enumerate(argv) if a == "--mount"]
    assert str(world) in targets and str(artefact) not in targets
    assert len(targets) == len(set(targets))


def test_the_interpreter_is_mounted_where_the_venv_names_it():
    """The host's own interpreter runs inside: the venv (under the repository)
    and the base install its pyvenv.cfg `home` names, at that path - a uv
    alias is a symlink, so the real directory is bound at both names."""
    argv = episode.sandbox_command(["true"], read_only=[], writable=None)
    targets = {dict(f.partition("=")[::2] for f in argv[i + 1].split(","))["target"]
               for i, a in enumerate(argv) if a == "--mount"}
    cfg = {k.strip(): v.strip() for k, _, v in
           (line.partition("=") for line in
            (episode.PY.parent.parent / "pyvenv.cfg").read_text().splitlines())}
    base = Path(cfg["home"]).parent
    assert str(base) in targets and str(base.resolve()) in targets


def test_grade_sandbox_follows_the_topology(monkeypatch):
    monkeypatch.delenv(episode.GRADE_SANDBOX_ENV, raising=False)
    assert episode.grade_sandbox("container") == "container"
    assert episode.grade_sandbox("host") == "host"
    monkeypatch.setenv(episode.GRADE_SANDBOX_ENV, "host")
    assert episode.grade_sandbox("container") == "host"
    monkeypatch.setenv(episode.GRADE_SANDBOX_ENV, "container")
    assert episode.grade_sandbox("host") == "container"
    monkeypatch.setenv(episode.GRADE_SANDBOX_ENV, "vm")
    with pytest.raises(episode.HarnessError, match="ALKERA_GRADE_SANDBOX"):
        episode.grade_sandbox("container")


def test_grade_refuses_an_unknown_sandbox(tmp_path):
    spec = SimpleNamespace(grade_entrypoint=tmp_path / "g.py", grade_argv=[])
    world = {"world_root": str(tmp_path / "w"), "grading": str(tmp_path / "g")}
    with pytest.raises(episode.HarnessError, match="sandbox"):
        episode.grade(spec, world, tmp_path / "a.json", tmp_path, sandbox="vm")
    assert not (tmp_path / "grader.stderr.txt").exists()       # refused before running


def test_rollout_grades_in_the_topologys_sandbox():
    """Both grading call sites in rollout.py (the episode and --regrade) ask
    grade_sandbox for the rollout's own topology; harbor's verifier, already
    in its own container, never passes `sandbox` and keeps the host default."""
    source = (HERE / "rollout.py").read_text(encoding="utf-8")
    assert source.count("episode.grade_twice(") == 2
    assert source.count("sandbox=episode.grade_sandbox(topology)") == 1
    assert source.count("sandbox=episode.grade_sandbox(args.topology)") == 1
    verify = (REPO / "harbor" / "template" / "tests" / "verify.py").read_text(encoding="utf-8")
    assert "sandbox=" not in verify


# --------------------------------------------------------------- the box
PROBE = r'''
import json, os, socket, sys, tempfile
from pathlib import Path

a = dict(zip(sys.argv[1::2], sys.argv[2::2]))
out = {"uid": os.getuid(), "gid": os.getgid(), "environ": dict(os.environ)}
try:
    socket.create_connection((a["--host"], int(a["--port"])), timeout=5).close()
    out["connect"] = "connected"
except OSError as exc:
    out["connect"] = f"{type(exc).__name__}: {exc}"
out["interfaces"] = sorted(name for _, name in socket.if_nameindex())

def writes(folder, keep=False):
    try:
        (Path(folder) / ".probe").write_text("x", encoding="utf-8")
    except OSError:
        return False
    if not keep:
        (Path(folder) / ".probe").unlink()
    return True

def writes_tmp():
    try:
        with tempfile.NamedTemporaryFile(dir="/tmp"):
            return True
    except OSError:
        return False

grading = Path(a["--grading"])
out["writes"] = {"world": writes(a["--world-root"]), "grading": writes(grading),
                 "rollout": writes(grading.parent, keep=True), "tmp": writes_tmp(),
                 "repo": os.access(a["--repo"], os.W_OK)}
out["sees_unmounted"] = Path(a["--secret"]).exists()
status = Path("/proc/self/status").read_text()
out["cap_eff"] = [l.split()[1] for l in status.splitlines() if l.startswith("CapEff")][0]
out["no_new_privs"] = [l.split()[1] for l in status.splitlines() if l.startswith("NoNewPrivs")][0]
print(json.dumps({"total": 0.0, "checks": [], "probe": out}))
'''


def _host_ip() -> str | None:
    """This host's address on its default route (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") else ip


@pytest.mark.parametrize("sandbox", ["host", "container"])
def test_container_grader_has_no_network_and_no_host_env(sandbox, tmp_path, monkeypatch,
                                                         request):
    if sandbox == "container":
        request.getfixturevalue("box")
    host_ip = _host_ip()
    if host_ip is None:
        pytest.skip("no non-loopback address to listen on: the network control cannot run")
    listener = socket.create_server((host_ip, 0))
    port = listener.getsockname()[1]

    def serve():
        try:
            while True:
                listener.accept()[0].close()
        except OSError:
            return

    threading.Thread(target=serve, daemon=True).start()
    rollout = tmp_path / "r0"
    world_root, grading = rollout / "world", rollout / "grading"
    world_root.mkdir(parents=True)
    grading.mkdir()
    (tmp_path / "grader").mkdir()
    grader = tmp_path / "grader" / "probe_grade.py"
    grader.write_text(PROBE, encoding="utf-8")
    secret = tmp_path / "operator-secret.txt"              # on the host, never mounted
    secret.write_text("x", encoding="utf-8")
    monkeypatch.setenv("ALKERA_T9_HOST_CANARY", "a host variable")
    spec = SimpleNamespace(grade_entrypoint=grader, grade_argv=[
        "--host", host_ip, "--port", str(port), "--grading", "{grading}",
        "--world-root", "{world_root}", "--repo", str(REPO), "--secret", str(secret)])
    world = {"world_root": str(world_root), "grading": str(grading)}
    try:
        probe = episode.grade(spec, world, world_root / "final_report.json", rollout,
                              sandbox=sandbox)["probe"]
    finally:
        listener.close()

    if sandbox == "host":
        # the controls: everything the box takes away is there without it
        assert probe["connect"] == "connected"
        assert probe["environ"]["ALKERA_T9_HOST_CANARY"] == "a host variable"
        assert probe["sees_unmounted"] is True
        assert probe["writes"]["world"] is True
        return
    assert probe["connect"].startswith("OSError") and "unreachable" in probe["connect"], probe
    assert probe["interfaces"] == ["lo"]
    assert set(probe["environ"]) == FIVE, sorted(probe["environ"])
    assert probe["environ"] == {"PYTHONPATH": str(episode.TRACK_A), "PYTHONUTF8": "1",
                                "PYTHONHASHSEED": "0", "TZ": "UTC", "LC_ALL": "C.UTF-8"}
    assert probe["writes"] == {"world": False, "grading": False, "rollout": True,
                               "tmp": True, "repo": False}
    assert (rollout / ".probe").is_file()            # and the write landed as the operator
    assert (rollout / ".probe").stat().st_uid == os.getuid()
    assert probe["sees_unmounted"] is False
    assert (probe["uid"], probe["gid"]) == (os.getuid(), os.getgid())
    assert int(probe["cap_eff"], 16) == 0
    assert probe["no_new_privs"] == "1"


# ------------------------------------------------------ byte-identity (RF 4)
STAGE = r'''
import json, os, sys
from pathlib import Path

task, arm, base, out = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4])
sys.path.insert(0, str(task / "tests"))
import run_baselines as RB

if hasattr(RB, "Suite"):                                   # GT001's suite
    suite = RB.Suite(arm, base, keep=True)
    ctx = suite.stage("oracle")
    artifact = ctx.workspace / "final_report.json"
    artifact.write_text(json.dumps(RB.p_oracle(ctx), indent=2) + "\n", encoding="utf-8")
    doc = {"world": ctx.world, "artifact": str(artifact), "rollout": str(ctx.base),
           "bi_state": None}
else:                                                      # BI001's
    fn, _ = RB.BASELINES["oracle"]
    world = RB.build_at(base, "oracle")
    services = RB.bi_services.Services(world, base / "oracle" / "audit")
    services.seed()
    fn(world, services)
    doc = {"world": world, "artifact": str(Path(world["workspace"]) / "final_report.json"),
           "rollout": str(base / "oracle"), "bi_state": str(services.bi_state)}
out.write_text(json.dumps(doc), encoding="utf-8")
'''

TASKS = {"GT001": REPO / "tasks" / "GT001-aov-halving",
         "BI001": REPO / "tasks" / "BI001-revenue-disagrees-across-bi"}


def _stage_oracle(task: str, arm: str, base: Path) -> dict:
    """The task's own oracle baseline, staged as its run_baselines.py does, in a
    subprocess: the two tasks' test modules share names and pick their arm at
    import."""
    driver = base / "stage.py"
    driver.write_text(STAGE, encoding="utf-8")
    out = base / "staged.json"
    env = {**os.environ, "PYTHONUTF8": "1",
           # solution.sh calls `python`: the venv's, as a run_baselines run has it
           "PATH": os.pathsep.join([str(episode.PY.parent), os.environ.get("PATH", "")])}
    env.pop(taskspec.VARIANT_ENV, None)
    if arm != "default":
        env[taskspec.VARIANT_ENV] = arm
    done = subprocess.run([sys.executable, str(driver), str(TASKS[task]), arm,
                           str(base / "w"), str(out)],
                          capture_output=True, text=True, env=env, timeout=900)
    assert done.returncode == 0, done.stderr[-4000:]
    return json.loads(out.read_text(encoding="utf-8"))


@pytest.mark.parametrize("task, arm", [("GT001", "default"), ("BI001", "default")])
def test_grade_in_container_matches_host_byte_for_byte(task, arm, tmp_path, box):
    staged = _stage_oracle(task, arm, tmp_path)
    spec = taskspec.load(TASKS[task].name, TASKS[task].parent, variant=arm)
    world, artifact = staged["world"], Path(staged["artifact"])
    rollout = Path(staged["rollout"])
    bi_state = Path(staged["bi_state"]) if staged["bi_state"] else None
    graded = {}
    for sandbox in ("host", "container", "host"):
        score = episode.grade(spec, world, artifact, rollout, bi_state, sandbox=sandbox)
        stderr = (rollout / "grader.stderr.txt").read_bytes()
        graded.setdefault(sandbox, []).append((json.dumps(score).encode("utf-8"), stderr))
    host, again = graded["host"]
    (container,) = graded["container"]
    assert host == again, "the host grade itself moved between two runs"
    assert json.loads(host[0])["total"] > 0, "the oracle scored nothing: not a comparison"
    assert container[0] == host[0]
    assert container[1] == host[1]
