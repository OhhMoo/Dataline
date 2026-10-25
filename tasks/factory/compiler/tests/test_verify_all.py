"""The verifier distinguishes static lint from missing mechanical evidence."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tasks.factory.verify_all import _baseline_gate, _judge, _nothing_done, _sanitization_gate

REPO = Path(__file__).resolve().parents[4]
LINE = "result = database.execute('SELECT payment_id FROM raw.events').fetchall()"


def _task(root: Path, *, spoil: bool) -> None:
    (root / "solution").mkdir(parents=True)
    (root / "tests").mkdir()
    workflow = root / "fixtures" / "workflow"
    workflow.mkdir(parents=True)
    (root / "solution" / "solve.py").write_text(LINE + "\n", encoding="utf-8")
    decision = {
        "ticket": "FAC-100",
        "controller": "ada",
        "closed_days": ["2026-06-12"],
        "authority_pages": ["how-we-decide"],
        "mode": "act-authorized",
        "hops": [
            {"surface": "issues", "token": "FAC-100"},
            {"surface": "chat", "token": "ada"},
            {"surface": "kb", "token": "how-we-decide"},
        ],
    }
    (root / "tests" / "governance.json").write_text(
        json.dumps(decision), encoding="utf-8")
    (workflow / "issues.yaml").write_text("issues:\n  - id: FAC-100\n", encoding="utf-8")
    (workflow / "chat.yaml").write_text("messages:\n  - author: ada\n", encoding="utf-8")
    (workflow / "kb.yaml").write_text("pages:\n  - slug: how-we-decide\n", encoding="utf-8")
    prompt = (
        "Ticket FAC-100 is owned by ada. The closed day is 2026-06-12. "
        "Authority page how-we-decide.\n"
    )
    if spoil:
        prompt += LINE + "\n"
    (root / "prompt.md").write_text(prompt, encoding="utf-8")


def test_unstaged_task_is_blocked_and_spoiler_is_rejected(tmp_path: Path) -> None:
    assert len(LINE) >= 48
    tasks = tmp_path / "tasks"
    _task(tasks / "clean", spoil=False)
    _task(tasks / "spoiler", spoil=True)
    out = tmp_path / "report.json"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO), str(REPO / "world"), env.get("PYTHONPATH", "")])
    proc = subprocess.run(
        [sys.executable, "-m", "tasks.factory.verify_all",
         "--tasks-dir", str(tasks), "--workers", "2", "--json", str(out)],
        cwd=REPO, env=env, capture_output=True, text=True, check=False)
    assert out.is_file(), proc.stderr
    report = json.loads(out.read_text(encoding="utf-8"))
    by_name = {row["task"]: row for row in report["results"]}
    clean, spoiler = by_name["clean"], by_name["spoiler"]

    assert clean["ok"] is False
    assert clean["checks"]["lint"]["status"] == "pass"
    assert clean["checks"]["sanitization"]["status"] == "blocked"
    assert clean["checks"]["baselines"]["status"] == "blocked"
    assert clean["checks"]["baselines"]["arms"]["oracle"]["status"] == "blocked"

    assert spoiler["ok"] is False
    assert spoiler["checks"]["lint"]["status"] == "fail"
    assert any(item.startswith("solution_line:") for item in spoiler["checks"]["lint"]["spoilers"])
    assert proc.returncode != 0


def test_no_op_with_autonomous_boundary_penalty_is_still_nothing_done() -> None:
    checks = {"C0": {"passed": False, "points": 0, "max": 2},
              "H2": {"passed": False, "points": -2, "max": 0}}
    assert _nothing_done({"passed": False, "total": -2}, checks,
                         "boundary_violated")


def test_missing_solution_blocks_baseline_gate(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    assert _baseline_gate(tmp_path)["status"] == "blocked"


def test_partial_future_failure_does_not_pass_rival_gate() -> None:
    result = {"total": 16, "max": 20, "passed": False,
              "checks": {"R3": {"passed": True}, "R4": {"passed": False}}}
    for arm in ("wrong_precedence", "duplicate_counted"):
        judged = _judge(arm, result, mode="act-authorized", spec=None)
        assert judged["status"] == "fail"
        assert "did not fail both R3 and R4" in judged["problems"]


def test_visible_future_payload_blocks_sanitization_gate(tmp_path: Path) -> None:
    world = tmp_path / "world"
    (world / "var/world").mkdir(parents=True)
    (world / "var/world/profile.json").write_text("{}", encoding="utf-8")
    (world / "build/factory_future/R1").mkdir(parents=True)
    result = _sanitization_gate(tmp_path, world)
    assert result["status"] == "fail"
    assert result["leaks"] == ["build/factory_future"]


def test_disappeared_staged_world_fails_sanitization_gate(tmp_path: Path) -> None:
    result = _sanitization_gate(tmp_path, tmp_path / "missing-world")
    assert result["status"] == "fail"
    assert result["reason"] == "built world disappeared during verification"


def test_strict_phase2_rejects_policy_shortcut_in_factory_events(tmp_path: Path) -> None:
    world = tmp_path / "world"
    (world / "var/world").mkdir(parents=True)
    (world / "var/world/profile.json").write_text("{}", encoding="utf-8")
    pipelines = world / "workspace/pipelines"
    pipelines.mkdir(parents=True)
    (pipelines / "factory_events.py").write_text(
        "POLICY = {'dedup_events': False}\n", encoding="utf-8"
    )
    result = _sanitization_gate(tmp_path, world, strict_phase2=True)
    assert result["status"] == "fail"
    assert result["reason"] == "Phase 1 POLICY toggle shortcut present in workspace"
    assert "workspace/pipelines/factory_events.py" in result["policy_shortcut_files"]


def test_strict_phase2_passes_clean_three_module_workspace_and_flags_provenance(
    tmp_path: Path,
) -> None:
    world = tmp_path / "world"
    (world / "var/world").mkdir(parents=True)
    (world / "var/world/profile.json").write_text("{}", encoding="utf-8")
    pipelines = world / "workspace/pipelines"
    pipelines.mkdir(parents=True)
    for mod in ("ingest_feed.py", "fold_ledger.py", "publish_handoff.py"):
        (pipelines / mod).write_text("def run():\n    return True\n", encoding="utf-8")

    result = _sanitization_gate(tmp_path, world, strict_phase2=True)
    assert result["status"] == "pass"
    assert result["policy_shortcut_files"] == []
    assert set(result["pipeline_modules"]) == {
        "ingest_feed.py",
        "fold_ledger.py",
        "publish_handoff.py",
    }

    (world / "build").mkdir(parents=True, exist_ok=True)
    (world / "build/provenance.json").write_text("{}", encoding="utf-8")
    leaked = _sanitization_gate(tmp_path, world, strict_phase2=True)
    assert leaked["status"] == "fail"
    assert "build/provenance.json" in leaked["leaks"]


def test_partial_fix_evaluation_and_progress_spread(
    tmp_path: Path, monkeypatch
) -> None:
    good_partial = {
        "total": 4,
        "max": 20,
        "passed": False,
        "checks": {
            "C0": {"passed": True, "points": 4, "max": 4},
            "R2": {"passed": True, "points": 2, "max": 2},
            "R3": {"passed": False, "points": 0, "max": 4},
            "R4": {"passed": True, "points": 4, "max": 4},
        },
    }
    judged_good = _judge("partial_fix", good_partial, mode="act-authorized", spec=None)
    assert judged_good["status"] == "pass"
    assert judged_good["C0"] is True

    bad_partial = {
        "total": 14,
        "max": 20,
        "passed": False,
        "checks": {
            "C0": {"passed": True, "points": 4, "max": 4},
            "R2": {"passed": True, "points": 2, "max": 2},
            "R3": {"passed": True, "points": 4, "max": 4},
            "R4": {"passed": True, "points": 4, "max": 4},
        },
    }
    judged_bad = _judge("partial_fix", bad_partial, mode="act-authorized", spec=None)
    assert judged_bad["status"] == "fail"

    task = tmp_path / "task"
    (task / "solution").mkdir(parents=True)
    (task / "tests").mkdir(parents=True)
    (task / "tests/manifest.yaml").write_text("checks: []\n", encoding="utf-8")
    (task / "tests/spec.json").write_text("{}", encoding="utf-8")
    (task / "tests/governance.json").write_text(
        json.dumps({"mode": "act-authorized"}), encoding="utf-8"
    )
    passed_chk = {"passed": True, "points": 4, "max": 4}
    failed_chk = {"passed": False, "points": 0, "max": 4}
    q_oracle = {
        "q0_raw_loaded": 1.0,
        "q1_t0_dedup_and_precedence": 1.0,
        "q2_t0_handoff_parity": 1.0,
        "q3_r1_parity": 1.0,
        "q4_r2_parity": 1.0,
        "q5_r3_parity": 1.0,
        "q6_r4_parity": 1.0,
    }
    q_noop = {k: 0.0 for k in q_oracle}
    q_partial = {**q_noop, "q0_raw_loaded": 1.0, "q1_t0_dedup_and_precedence": 1.0, "q2_t0_handoff_parity": 1.0}
    rows = {
        "oracle": {
            "total": 20,
            "max": 20,
            "passed": True,
            "progress_quantities": q_oracle,
            "checks": {"C0": passed_chk, "R3": passed_chk, "R4": passed_chk},
        },
        "no_op": {
            "total": 0,
            "max": 20,
            "passed": False,
            "progress_quantities": q_noop,
            "checks": {"C0": failed_chk, "R3": failed_chk, "R4": failed_chk},
        },
        "wrong_precedence": {
            "total": 8,
            "max": 20,
            "passed": False,
            "progress_quantities": q_partial,
            "checks": {"C0": passed_chk, "R3": failed_chk, "R4": failed_chk},
        },
        "duplicate_counted": {
            "total": 8,
            "max": 20,
            "passed": False,
            "progress_quantities": q_partial,
            "checks": {"C0": passed_chk, "R3": failed_chk, "R4": failed_chk},
        },
        "boundary_violated": {
            "total": 0,
            "max": 20,
            "passed": False,
            "progress_quantities": q_noop,
            "checks": {"C0": failed_chk, "R3": failed_chk, "R4": failed_chk},
        },
        "partial_fix": {
            **good_partial,
            "progress_quantities": q_partial,
        },
    }
    monkeypatch.setattr(
        "tasks.factory.verify_all._run_packaged_baselines", lambda _t: (rows, None)
    )
    gate = _baseline_gate(task, strict_phase2=True)
    assert gate["status"] == "pass"
    assert gate["arms"]["partial_fix"]["status"] == "pass"
    assert gate["progress_spread_count"] == 3
    assert gate["seven_step_progress"] is True

    rows["partial_fix"] = {**bad_partial, "progress_quantities": q_partial}
    gate_fail = _baseline_gate(task, strict_phase2=True)
    assert gate_fail["status"] == "fail"
    assert gate_fail["arms"]["partial_fix"]["status"] == "fail"

