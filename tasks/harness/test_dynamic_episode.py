"""Task 6: the world clock in an episode (tasks/harness/dynamic.py).

    PYTHONPATH=world gateway/.venv/bin/python -m pytest -q tasks/harness/test_dynamic_episode.py

The live half - the clock driven through rexec in a real episode container, the
ledgers out of the agent's reach - is the scripted DW001 rollout
(`run_rollouts.py --task DW001-live-revenue --model scripted`). Everything here
runs without an engine.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import classify                                    # noqa: E402
import container_episode as ce                     # noqa: E402
import dynamic                                     # noqa: E402
import episode                                     # noqa: E402
import taskspec                                    # noqa: E402

TASK = HERE.parent / "DW001-live-revenue"


# ------------------------------------------------------------------ opt-in
def test_static_tasks_are_not_dynamic():
    for name in ("GT001-aov-halving", "GT004-doubling-window",
                 "GT009-settlement-cutoff"):
        spec = taskspec.load(name)
        assert spec.dynamic is None
        assert dynamic.load(spec) is None
        assert spec.pass_requires_checks and spec.pass_min_total > 0


def test_dw001_declares_a_clock():
    spec = taskspec.load("DW001-live-revenue")
    dyn = dynamic.load(spec)
    assert (dyn.m, dyn.h_hours, dyn.T_hours) == (10, 6.0, 168.0)
    assert dyn.damage_path == TASK / "damage.yaml"
    assert dyn.shocks_path == TASK / "shock_spec.yaml"
    assert spec.scores_pass({"checks": [], "total": 0}) is False


@pytest.mark.parametrize("block, needle", [
    ({"h_hours": 6, "T_hours": 1, "damage": "d", "shocks": "s"}, "missing"),
    ({"m": 0, "h_hours": 6, "T_hours": 1, "damage": "damage.yaml",
      "shocks": "shock_spec.yaml"}, "m must be"),
    ({"m": 2, "h_hours": 6, "T_hours": 1, "damage": "nope.yaml",
      "shocks": "shock_spec.yaml"}, "does not exist"),
])
def test_bad_dynamic_block_refused(block, needle):
    with pytest.raises(dynamic.DynamicError, match=needle):
        dynamic.parse(TASK, {"dynamic": block})


def test_only_the_rexec_topology_ticks():
    dynamic.require_supported(driver="native", topology="container",
                              subject_sandbox="container")
    for combo in (("mcp", "container", "container"), ("native", "host", "container"),
                  ("native", "container", "host")):
        with pytest.raises(dynamic.DynamicError):
            dynamic.require_supported(driver=combo[0], topology=combo[1],
                                      subject_sandbox=combo[2])


# ------------------------------------------------- static bridge unchanged
def _stack(tmp_path, clock=None):
    world = {"world_root": str(tmp_path / "w")}
    stack = ce.NativeContainerEpisode(project="p", world=world,
                                      audit_dir=tmp_path / "a",
                                      rollout_dir=tmp_path / "r",
                                      subject_sandbox=True, clock=clock)
    stack.container_id = "EPISODE"
    stack.subject_container = "SUBJECT"
    stack.rexec_token = "TOKEN"
    return stack


def test_static_bridge_argv_is_byte_identical(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ce, "_run", lambda argv, **kw: calls.append(argv)
                        or SimpleNamespace(returncode=0, stdout="", stderr=""))
    stack = _stack(tmp_path)
    monkeypatch.setattr(stack, "exec_", lambda *a, **k: None)
    monkeypatch.setattr(stack, "copy_in", lambda *a, **k: None)
    stack.start_rexec({"B": "2", "A": "1"})
    assert calls[0] == [
        "docker", "exec", "-d", "-w", "/workspace", "-e", "ALKERA_REXEC_TOKEN=TOKEN",
        "-e", "A=1", "-e", "B=2", "EPISODE", "/usr/local/bin/python",
        "/opt/alkera/rexec.py", "serve", "--port", str(ce.REXEC_PORT),
        "--workdir", "/workspace"]


def test_dynamic_bridge_is_attached_with_clock(tmp_path, monkeypatch):
    popen = []

    class FakeProc:
        def __init__(self, argv, **kw):
            popen.append(argv)
            self.stdout, self.stdin = io.BytesIO(b""), io.BytesIO()

        def poll(self):
            return 0

    monkeypatch.setattr(ce.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(ce, "_run", lambda argv, **kw:
                        SimpleNamespace(returncode=0, stdout="", stderr=""))
    stack = _stack(tmp_path, clock=SimpleNamespace(paused=False))
    monkeypatch.setattr(stack, "exec_", lambda *a, **k: None)
    monkeypatch.setattr(stack, "copy_in", lambda *a, **k: None)
    stack.start_rexec({})
    assert popen[0][:3] == ["docker", "exec", "-i"]
    assert popen[0][-2:] == ["--clock", "stdio"]
    assert "-d" not in popen[0]


# ------------------------------------------------------------ control loop
class _Recorder:
    def __init__(self):
        self.seen, self.errors, self.events = [], [], []

    def on_command(self, seq, inflight):
        self.seen.append((seq, inflight))

    def _event(self, **kw):
        self.events.append(kw)


def test_control_loop_answers_each_line_in_turn():
    out = io.BytesIO(b"tick? 1 0\ntick? 2 1\ntick? 2 0\ngarbage\ntick? 3 0\n")
    back = io.BytesIO()
    rec = _Recorder()
    dynamic.control_loop(out, back, rec)
    assert back.getvalue() == b"go 1\ngo 2\ngo 2\ngo 3\n"
    # The duplicated 2 is never ticked on; the stray line is recorded.
    assert rec.seen == [(1, 0), (2, 1), (3, 0)]
    assert [e["kind"] for e in rec.events] == ["sequence_violation", "unexpected_line"]


# --------------------------------------------------- the driver, on DW-1
@pytest.fixture(scope="module")
def base_world(tmp_path_factory):
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.path.insert(0, str(episode.TRACK_A))
    from world import build as wbuild

    seed = episode.TRACK_A / "world" / "themes" / "revenue_live" / "seed.example.yaml"
    return wbuild.build(seed, tmp_path_factory.mktemp("dw") / "base")


def _dyn(m=3, h=6.0, T=24.0):
    return dynamic.DynamicSpec(m=m, h_hours=h, T_hours=T,
                               damage_path=TASK / "damage.yaml",
                               shocks_path=TASK / "shock_spec.yaml")


def test_stage_then_drive_then_finish(base_world, tmp_path):
    root = Path(shutil.copytree(base_world, tmp_path / "w", symlinks=True))
    ledger = tmp_path / "rollout" / "dynamic"
    dyn = _dyn()
    staged = dynamic.stage(dyn, root, ledger)
    assert staged["fault_seed"] == 7
    assert (root / "var" / "world" / "shock_schedule.json").is_file()
    assert (ledger / "shock_schedule.json").read_bytes() == \
        (root / "var" / "world" / "shock_schedule.json").read_bytes()

    before = episode.tree_manifest(root)
    drv = dynamic.Driver(dyn, root, ledger, tree_before=before,
                         tree_manifest=episode.tree_manifest)
    assert drv.on_command(1) is None                        # not armed: uncounted
    drv.arm()
    drv.paused = True
    assert drv.on_command(2) is None                        # a harness proof
    drv.paused = False
    # The agent edits a workspace file before the first tick.
    edited = root / "workspace" / "README.md"
    edited.write_text(edited.read_text() + "\nagent was here\n")
    fired = [drv.on_command(seq) for seq in range(3, 10)]   # model commands 1..7
    ticks = [r for r in fired if r]
    assert [r["after_command"] for r in ticks] == [3, 6]
    drv.disarm()

    after = episode.tree_manifest(root)
    judged = drv.baseline(before)
    changed = {k for k in set(judged) | set(after) if judged.get(k) != after.get(k)}
    # Only the agent's edit is left for blindness to explain; every file the
    # two ticks wrote (state.json, the warehouse, landing files...) is folded in.
    assert changed == {"workspace/README.md"}
    assert {k for k in set(before) | set(after) if before.get(k) != after.get(k)} \
        > {"workspace/README.md"}

    result = drv.finish()
    assert result["ticks_episode"] == 2 and result["ticks_post"] == 2
    assert result["commands_counted"] == 7 and result["uncounted_commands"] == 2
    assert result["errors"] == []
    sched = json.loads((ledger / "tick_schedule.json").read_text())
    assert [t["phase"] for t in sched["ticks"]] == ["episode"] * 2 + ["post"] * 2
    assert sched["ticks"][-1]["vnow_after"] == "2026-03-04T00:00:00Z"
    rows = [json.loads(x) for x in (ledger / "damage.jsonl").read_text().splitlines()]
    assert [(r["tick"], r["phase"]) for r in rows] == \
        [(0, "episode"), (1, "episode"), (2, "post"), (3, "post")]
    assert all(set(r["V"]) == {"exactly_once", "liveness", "consumer_consistency"}
               for r in rows)
    # After finish nothing counts or ticks any more.
    assert drv.on_command(10) is None


def test_agent_change_overwritten_by_a_tick_is_not_laundered(base_world, tmp_path):
    root = Path(shutil.copytree(base_world, tmp_path / "w", symlinks=True))
    before = episode.tree_manifest(root)
    drv = dynamic.Driver(_dyn(m=1), root, tmp_path / "led", tree_before=before,
                         tree_manifest=episode.tree_manifest)
    drv.arm()
    state = root / "var" / "sched" / "state.json"
    state.write_text(state.read_text().replace('"vnow"', '"vnow"', 1) + " ")
    assert drv.on_command(1)                                # the tick rewrites state.json
    judged = drv.baseline(before)
    assert judged["var/sched/state.json"] == before["var/sched/state.json"]


# ----------------------------------------------------------------- classify
def test_classify_dynamic_is_unjudged_not_failed():
    spec = SimpleNamespace(dynamic={"m": 10}, wall_clock_seconds=10,
                           max_tool_calls=10, pass_requires_checks=[])
    v = classify.classify(spec=spec, score={"J": 1.5, "ticks": 30, "checks": [],
                                            "total": None, "max": None},
                          score_stable=True, grader_crashed=False,
                          harness_failure=None, agent_exit_reason="completed",
                          blindness={}, action_rows=[], surfaces={},
                          artifact_present=False)
    assert v["classification"] == "dynamic"
    assert v["passed"] is None and v["scored"] is False


def test_grade_reads_the_ledger(tmp_path):
    sys.path.insert(0, str(TASK / "tests"))
    import grade as dw_grade                          # noqa: E402

    led = tmp_path / "dynamic"
    led.mkdir()
    (led / "tick_schedule.json").write_text(json.dumps(
        {"m": 10, "h_hours": 6.0, "T_hours": 12.0, "vnow_start": "x", "ticks": []}))
    (led / "damage.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"tick": 0, "phase": "episode", "vnow": "a", "V": {"liveness": 0.5}},
        {"tick": 1, "phase": "post", "vnow": "b", "V": {"liveness": 0.5}}]))
    out = dw_grade.grade(led)
    assert out["J"] == pytest.approx(0.5 * 12 + 0.5)
    assert out["pass"] is None and out["checks"] == [] and out["total"] is None
