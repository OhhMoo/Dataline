from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import tasks.factory.calibrate_queue as cq
from tasks.factory.calibrate_queue import (
    certify_stratified_hardness,
    main,
    mutate_blueprint_for_prescription,
    prescribe_difficulty_adjustment,
    resolve_pilot_entries,
    run_two_stage_calibration,
)


def _make_stage1_arm(task_id: str, passed: int = 2, scored: int = 3) -> dict[str, Any]:
    p = passed / scored if scored else 0.0
    return {
        "task": task_id,
        "run_id": f"clean-stage1-{task_id}",
        "variant": "default",
        "governance_mode": "act-authorized",
        "role": "control",
        "k": scored,
        "scored": scored,
        "passed": passed,
        "pass_at_1": round(p, 4),
        "mean_score": round(p, 4),
        "binary_variance": round(p * (1.0 - p), 6),
        "scalar_variance": 0.12,
        "quarantined_count": 0,
        "measurement_valid": True,
        "invalidated_because": [],
        "outcome_counts": {
            "resolved": passed,
            "withheld_with_evidence": 0,
            "acted_unresolved": scored - passed,
            "boundary_violated": 0,
            "nothing_done": 0,
        },
        "criteria": {"C0": {"n": scored, "passed": scored, "rate": 1.0}},
        "failed_r3_r4": [],
        "classification": "admit_to_stage2",
        "status": "admit_to_stage2",
        "admit_to_stage2": True,
        "stage2": True,
    }


def _make_stage2_arm(task_id: str, passed: int = 2, scored: int = 5) -> dict[str, Any]:
    p = passed / scored if scored else 0.0
    return {
        "task": task_id,
        "run_id": f"clean-stage2-{task_id}",
        "variant": "default",
        "governance_mode": "act-authorized",
        "role": "control",
        "k": scored,
        "scored": scored,
        "passed": passed,
        "pass_at_1": round(p, 4),
        "mean_score": round(p, 4),
        "binary_variance": round(p * (1.0 - p), 6),
        "scalar_variance": 0.18,
        "quarantined_count": 0,
        "measurement_valid": True,
        "invalidated_because": [],
        "outcome_counts": {
            "resolved": passed,
            "withheld_with_evidence": 0,
            "acted_unresolved": scored - passed,
            "boundary_violated": 0,
            "nothing_done": 0,
        },
        "criteria": {"C0": {"n": scored, "passed": scored, "rate": 1.0}},
        "failed_r3_r4": [],
        "certified": True,
        "band": "core",
        "status": "certified",
    }


def test_delta_calibration_skips_certified_and_merges_summary(tmp_path: Path):
    out_dir = tmp_path / "runs_phase2"
    tasks_dir = tmp_path / "phase2_pilot_51"
    tasks_dir.mkdir(parents=True)
    out_dir.mkdir(parents=True)

    cert_exact = "FT0193-revenue-daily-ft03-auth-a"
    cert_prefix_task = "FT1077-iot-telemetry-billing-ft15-esc-a"
    cert_prefix_entry = f"{cert_prefix_task}:default"
    uncert_admit = "FT0484-excel-close-ft06-auth-b"
    uncert_ceil = "FT0001-settlement-clearing-ft01-auth-a"

    prior_summary = {
        "screened": 4,
        "admitted_to_stage2": [cert_exact, cert_prefix_task],
        "certified": [cert_exact, cert_prefix_task],
        "admitted": 2,
        "stage1": {
            "meta": {"queue_id": "prior-stage1"},
            "arms": {
                cert_exact: _make_stage1_arm(cert_exact, passed=2, scored=3),
                cert_prefix_task: _make_stage1_arm(cert_prefix_task, passed=1, scored=3),
                uncert_admit: _make_stage1_arm(uncert_admit, passed=0, scored=3),
                uncert_ceil: _make_stage1_arm(uncert_ceil, passed=3, scored=3),
            },
        },
        "stage2": {
            "meta": {"queue_id": "prior-stage2"},
            "arms": {
                cert_exact: _make_stage2_arm(cert_exact, passed=2, scored=5),
                cert_prefix_task: _make_stage2_arm(cert_prefix_task, passed=4, scored=5),
            },
        },
    }
    resume_path = tmp_path / "prior_calibration_summary.json"
    resume_path.write_text(json.dumps(prior_summary, indent=2), encoding="utf-8")

    runner_calls: list[tuple[list[str], int, str]] = []

    def fake_runner(entries, k, **kwargs):
        stage_label = kwargs.get("stage_label", f"k{k}")
        runner_calls.append((list(entries), k, stage_label))
        outputs = []
        for entry in entries:
            tid = entry.split(":", 1)[0]
            if k == 3:
                if entry == uncert_admit:
                    rep = {
                        "task": tid,
                        "variant": "default",
                        "rollouts": [
                            {"passed": True, "score": {"reward": 1.0}, "outcome": "resolved"},
                            {"passed": False, "score": {"reward": 0.3}, "outcome": "acted_unresolved"},
                            {"passed": False, "score": {"reward": 0.2}, "outcome": "acted_unresolved"},
                        ],
                    }
                else:
                    rep = {
                        "task": tid,
                        "variant": "default",
                        "rollouts": [
                            {"passed": True, "score": {"reward": 1.0}, "outcome": "resolved"}
                            for _ in range(3)
                        ],
                    }
            else:
                rep = {
                    "task": tid,
                    "variant": "default",
                    "rollouts": [
                        {"passed": True, "score": {"reward": 1.0}, "outcome": "resolved"},
                        {"passed": True, "score": {"reward": 0.9}, "outcome": "resolved"},
                        {"passed": False, "score": {"reward": 0.3}, "outcome": "acted_unresolved"},
                        {"passed": False, "score": {"reward": 0.2}, "outcome": "acted_unresolved"},
                        {"passed": True, "score": {"reward": 1.0}, "outcome": "resolved"},
                    ],
                }
            outputs.append((entry, rep, None))
        return {"command": ["mock"], "queue_id": f"mock-{stage_label}", "exit_code": 0}, outputs

    all_entries = [cert_exact, cert_prefix_entry, uncert_admit, uncert_ceil]
    summary = run_two_stage_calibration(
        all_entries,
        tasks_dir=tasks_dir,
        out_dir=out_dir,
        resume_summary=resume_path,
        skip_certified=True,
        queue_runner=fake_runner,
    )

    # Only the 2 uncertified entries ran in Stage 1, and only uncert_admit ran in Stage 2
    assert runner_calls == [
        ([uncert_admit, uncert_ceil], 3, "stage1"),
        ([uncert_admit], 5, "stage2"),
    ]

    assert summary["screened"] == 4
    assert summary["delta_screened"] == 2
    assert summary["reused_certified"] == [cert_exact, cert_prefix_entry]
    assert summary["admitted_to_stage2"] == [cert_exact, cert_prefix_entry, uncert_admit]
    assert summary["certified"] == [cert_exact, cert_prefix_entry, uncert_admit]
    assert summary["admitted"] == 3

    # Reused entries are tagged with reused_from_prior_summary=True
    assert summary["stage1"]["arms"][cert_exact]["reused_from_prior_summary"] is True
    assert summary["stage2"]["arms"][cert_exact]["reused_from_prior_summary"] is True
    assert summary["stage1"]["arms"][cert_prefix_entry]["reused_from_prior_summary"] is True
    assert summary["stage2"]["arms"][cert_prefix_entry]["reused_from_prior_summary"] is True

    # Newly calibrated entry is present and NOT marked reused
    assert summary["stage1"]["arms"][uncert_admit].get("reused_from_prior_summary") is not True
    assert summary["stage2"]["arms"][uncert_admit]["certified"] is True
    assert summary["stage2"]["arms"][uncert_admit].get("reused_from_prior_summary") is not True
    assert summary["stage1"]["arms"][uncert_ceil]["classification"] == "saturated_ceiling"

    # Verify calibration_summary.json on disk matches
    disk_summary = json.loads((out_dir / "calibration_summary.json").read_text(encoding="utf-8"))
    assert disk_summary["screened"] == 4
    assert disk_summary["delta_screened"] == 2
    assert disk_summary["reused_certified"] == [cert_exact, cert_prefix_entry]
    assert disk_summary["certified"] == [cert_exact, cert_prefix_entry, uncert_admit]


def test_skip_certified_uses_default_out_summary_and_honors_force_entries(tmp_path: Path):
    out_dir = tmp_path / "runs_default_resume"
    tasks_dir = tmp_path / "tasks"
    out_dir.mkdir(parents=True)
    tasks_dir.mkdir(parents=True)

    t1 = "FT0193-revenue-daily-ft03-auth-a"
    t2 = "FT0194-revenue-daily-ft03-hold-a"
    t3 = "FT0485-excel-close-ft06-hold-b"

    prior_summary = {
        "screened": 3,
        "admitted_to_stage2": [t1, t2],
        "certified": [t1, t2],
        "admitted": 2,
        "stage1": {
            "arms": {
                t1: _make_stage1_arm(t1, passed=2, scored=3),
                t2: _make_stage1_arm(t2, passed=2, scored=3),
                t3: _make_stage1_arm(t3, passed=0, scored=3),
            }
        },
        "stage2": {
            "arms": {
                t1: _make_stage2_arm(t1, passed=2, scored=5),
                t2: _make_stage2_arm(t2, passed=3, scored=5),
            }
        },
    }
    (out_dir / "calibration_summary.json").write_text(
        json.dumps(prior_summary, indent=2), encoding="utf-8"
    )

    calls: list[tuple[list[str], int]] = []

    def fake_runner(entries, k, **kwargs):
        calls.append((list(entries), k))
        outputs = []
        for entry in entries:
            tid = entry.split(":", 1)[0]
            rollouts = [
                {"passed": idx < 2, "score": {"reward": 1.0 if idx < 2 else 0.2}, "outcome": "resolved" if idx < 2 else "acted_unresolved"}
                for idx in range(k)
            ]
            outputs.append((entry, {"task": tid, "variant": "default", "rollouts": rollouts}, None))
        return {"command": ["mock"], "queue_id": f"q-k{k}", "exit_code": 0}, outputs

    # Force t2 to re-run even though it is certified in out_dir/calibration_summary.json
    summary = run_two_stage_calibration(
        [f"{t1}:default", f"{t2}:default", f"{t3}:default"],
        tasks_dir=tasks_dir,
        out_dir=out_dir,
        skip_certified=True,
        force_entries=[t2],
        queue_runner=fake_runner,
    )

    assert calls == [
        ([f"{t2}:default", f"{t3}:default"], 3),
        ([f"{t2}:default", f"{t3}:default"], 5),
    ]
    assert summary["screened"] == 3
    assert summary["delta_screened"] == 2
    assert summary["reused_certified"] == [f"{t1}:default"]
    assert summary["certified"] == [f"{t1}:default", f"{t2}:default", f"{t3}:default"]
    assert summary["stage2"]["arms"][f"{t1}:default"]["reused_from_prior_summary"] is True
    assert summary["stage2"]["arms"][f"{t2}:default"].get("reused_from_prior_summary") is not True

    # Now all 3 are certified in out_dir/calibration_summary.json; re-running with skip_certified=True
    # and no force_entries should not invoke fake_runner at all!
    calls.clear()
    summary_all_reused = run_two_stage_calibration(
        [t1, t2, t3],
        tasks_dir=tasks_dir,
        out_dir=out_dir,
        skip_certified=True,
        queue_runner=fake_runner,
    )
    assert calls == []
    assert summary_all_reused["screened"] == 3
    assert summary_all_reused["delta_screened"] == 0
    assert summary_all_reused["reused_certified"] == [t1, t2, t3]
    assert summary_all_reused["certified"] == [t1, t2, t3]
    assert summary_all_reused["stage1"]["meta"]["skipped"] is True


def test_pilot_dir_entry_resolution_and_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    pilot_dir = tmp_path / "pilot_51"
    out_dir = tmp_path / "runs_cli"
    pilot_dir.mkdir(parents=True)
    out_dir.mkdir(parents=True)

    # 1. Fallback to FT* directory listing when phase2_selection.json is absent
    (pilot_dir / "FT0002-settlement-clearing-ft01-hold-a").mkdir()
    (pilot_dir / "FT0001-settlement-clearing-ft01-auth-a").mkdir()
    (pilot_dir / "not_a_task_dir").mkdir()
    assert resolve_pilot_entries(pilot_dir) == [
        "FT0001-settlement-clearing-ft01-auth-a",
        "FT0002-settlement-clearing-ft01-hold-a",
    ]

    # 2. Populates from phase2_selection.json in selection order when present
    selection_doc = {
        "selected": [
            {"blueprint": {"task_id": "FT0002-settlement-clearing-ft01-hold-a"}},
            {"blueprint": {"task_id": "FT0001-settlement-clearing-ft01-auth-a"}},
        ]
    }
    (pilot_dir / "phase2_selection.json").write_text(
        json.dumps(selection_doc, indent=2), encoding="utf-8"
    )
    assert resolve_pilot_entries(pilot_dir) == [
        "FT0002-settlement-clearing-ft01-hold-a",
        "FT0001-settlement-clearing-ft01-auth-a",
    ]

    # 3. End-to-end CLI invocation with --pilot-dir, --resume-summary, --skip-certified, --force-task
    prior_path = tmp_path / "prior.json"
    prior_path.write_text(
        json.dumps(
            {
                "certified": [
                    "FT0001-settlement-clearing-ft01-auth-a",
                    "FT0002-settlement-clearing-ft01-hold-a",
                ],
                "stage1": {
                    "arms": {
                        "FT0001-settlement-clearing-ft01-auth-a": _make_stage1_arm(
                            "FT0001-settlement-clearing-ft01-auth-a"
                        ),
                        "FT0002-settlement-clearing-ft01-hold-a": _make_stage1_arm(
                            "FT0002-settlement-clearing-ft01-hold-a"
                        ),
                    }
                },
                "stage2": {
                    "arms": {
                        "FT0001-settlement-clearing-ft01-auth-a": _make_stage2_arm(
                            "FT0001-settlement-clearing-ft01-auth-a"
                        ),
                        "FT0002-settlement-clearing-ft01-hold-a": _make_stage2_arm(
                            "FT0002-settlement-clearing-ft01-hold-a"
                        ),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    invoked_entries: list[tuple[list[str], int]] = []

    def mock_invoke_run_queue(entries, k, **kwargs):
        invoked_entries.append((list(entries), k))
        return (
            {"command": ["mock"], "queue_id": f"cli-k{k}", "exit_code": 0},
            [(e, None, None) for e in entries],
        )

    monkeypatch.setattr(cq, "_invoke_run_queue", mock_invoke_run_queue)

    rc = main(
        [
            "--pilot-dir",
            str(pilot_dir),
            "--out",
            str(out_dir),
            "--resume-summary",
            str(prior_path),
            "--skip-certified",
            "--force-task",
            "FT0002-settlement-clearing-ft01-hold-a",
            "--dry-run",
        ]
    )
    assert rc == 0
    cli_output = json.loads(capsys.readouterr().out)
    assert cli_output["screened"] == 2
    assert cli_output["delta_screened"] == 1
    assert cli_output["reused_certified"] == 1
    assert invoked_entries == [(["FT0002-settlement-clearing-ft01-hold-a"], 3)]


def test_phase2_prescription_mutation_and_hardness_certification():
    bp = {
        "task_id": "FT0001-settlement-clearing-ft01-auth-a",
        "theme": "settlement_clearing",
        "governance_mode": "withhold-blocked",
        "governance_submode": "wrong_approver_role",
        "prompt_variant": "default",
        "t0_stressor_codes": ["S01"],
        "hazards": ["H01", "H08"],
    }
    ceil_presc = prescribe_difficulty_adjustment({"pass_at_1": 1.0, "scored": 3, "passed": 3})
    assert ceil_presc["action"] == "increase_difficulty"
    mut_ceil = mutate_blueprint_for_prescription(bp, ceil_presc, step=1)
    assert mut_ceil["task_id"].endswith("-m1")
    assert len(mut_ceil["hazards"]) == 3
    assert mut_ceil["prompt_variant"] == "vague"

    report = certify_stratified_hardness([_make_stage2_arm("FT0193-revenue-daily-ft03-auth-a")])
    assert report["all_certified"] is True
    assert report["certified_count"] == 1
