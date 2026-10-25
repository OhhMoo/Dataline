"""Tests for frontier-model trajectory decomposition and RL/SFT loss modeling."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tasks.factory.export_rl_dataset import (
    CARD_FILE,
    GRPO_FILE,
    SFT_FILE,
    export_rl_dataset,
    main,
    token_loss_mask_rle,
    transcript_messages,
)
from tasks.factory.trajectory_loss_model import (
    CAUSAL_PHASES,
    classify_tool_phase,
    compute_twin_contrastive_advantages,
    enrich_checks_with_progress_quantities,
    governance_signals,
    model_certified_suite,
    model_task_group,
    parse_transcript,
)

RUN_INDEX = Path("/home/ubuntu/alkera_factory_1017/phase2_run_index.json")
PILOT_DIR = Path("/home/ubuntu/alkera_factory_1017/phase2_pilot_51")


def test_classify_tool_phase_covers_all_five_phases() -> None:
    assert classify_tool_phase("ToolSearch", {}) == "discovery"
    assert classify_tool_phase("mcp__alkera__issues_list", {}) == "discovery"
    assert classify_tool_phase("mcp__alkera__sql_query", {"sql": "SELECT 1"}) == "epistemic_investigation"
    assert classify_tool_phase("mcp__alkera__fs_write", {"path": "pipelines/fold_ledger.py"}) == "pipeline_repair"
    assert classify_tool_phase("mcp__alkera__sched_trigger", {"job_id": "factory_load"}) == "verification_and_execution"
    assert classify_tool_phase("mcp__alkera__fs_write", {"path": "/workspace/final_report.json"}) == "governance_and_reporting"
    assert classify_tool_phase("mcp__alkera__issues_comment", {"issue": "INV-1", "body": "hold"}) == "governance_and_reporting"


def test_enrich_checks_backfills_night_progress_quantities() -> None:
    checks = {
        "C0": {"passed": True, "detail": {"q0_raw_loaded": 1.0, "q1_t0_dedup_and_precedence": 1.0, "q2_t0_handoff_parity": 1.0}},
        "R1": {"passed": True, "detail": {"fraction": 1.0}},
        "R2": {"passed": False, "detail": {"fraction": 0.666667}},
        "R3": {"passed": False, "detail": {"fraction": 0.666667}},
        "R4": {"passed": False, "detail": {"fraction": 0.666667}},
    }
    pq = {
        "q0_raw_loaded": 1.0,
        "q1_t0_dedup_and_precedence": 1.0,
        "q2_t0_handoff_parity": 1.0,
        "q3_r1_parity": 1.0,
        "q4_r2_parity": 0.666667,
        "q5_r3_parity": 0.666667,
        "q6_r4_parity": 0.666667,
    }
    enriched = enrich_checks_with_progress_quantities(checks, pq)
    assert enriched["R1"]["detail"]["q3_r1_parity"] == 1.0
    assert enriched["R2"]["detail"]["q4_r2_parity"] == pytest.approx(0.666667)
    assert enriched["R3"]["detail"]["q5_r3_parity"] == pytest.approx(0.666667)
    assert enriched["R4"]["detail"]["q6_r4_parity"] == pytest.approx(0.666667)


def test_parse_transcript_enforces_observation_masking(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    lines = [
        {"type": "system", "subtype": "init", "model": "claude-opus-5-5-high"},
        {"type": "user", "message": {"role": "user", "content": "Fix the pipeline."}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "thinking", "thinking": "Let me list files."}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "mcp__alkera__fs_list", "input": {"path": "."}}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "pipelines/fold_ledger.py"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "mcp__alkera__fs_write", "input": {"path": "pipelines/fold_ledger.py", "content": "pass"}}]}},
        {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": "ok"}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t3", "name": "mcp__alkera__fs_write", "input": {"path": "/workspace/final_report.json", "content": "{}"}}]}},
        {"type": "result", "subtype": "success", "result": "Done."},
    ]
    transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    parsed = parse_transcript(transcript)
    assert parsed["observation_mask_pure"] is True
    assert parsed["n_assistant_blocks"] == 4
    assert parsed["n_masked_blocks"] == 5
    assert parsed["phase_block_counts"]["discovery"] == 2
    assert parsed["phase_block_counts"]["pipeline_repair"] == 1
    assert parsed["phase_block_counts"]["governance_and_reporting"] == 1


@pytest.mark.skipif(not RUN_INDEX.is_file(), reason="Live Phase 5 run index not present on disk")
def test_live_certified_frontier_suite_trajectory_and_loss_invariants() -> None:
    suite = model_certified_suite(RUN_INDEX, PILOT_DIR, only_certified=True)
    assert suite["task_count"] == 16
    assert suite["total_rollouts"] == 80
    assert suite["all_invariants_hold"] is True
    assert suite["non_collapsed_groups"] == 16
    for task in suite["tasks"]:
        assert 0.20 <= task["pass_at_1"] <= 0.80
        assert task["invariants"]["observation_mask_purity"] is True
        assert task["invariants"]["grpo_zero_mean"] is True
        assert task["invariants"]["grpo_unit_variance"] is True
        assert task["invariants"]["rloo_zero_mean"] is True
        assert task["invariants"]["corpo_reward_ordering"] is True
        assert task["invariants"]["non_collapsed_reward_signal"] is True
        assert set(task["episodes"][0]["phase_step_advantages"]) == set(CAUSAL_PHASES)


def _episode(rid: str, grpo: float, *, acted: bool, withheld: bool, boundary: bool = True,
             outcome: str = "acted_unresolved") -> dict:
    return {
        "rollout_id": rid,
        "grpo_advantage": grpo,
        "outcome_class": outcome,
        "passed": outcome in ("resolved", "withheld_with_evidence"),
        "governance_signals": {"acted_report": acted, "withheld_report": withheld, "boundary_respected": boundary},
    }


def _group(task_id: str, mode: str, episodes: list[dict], twin: str = "TG-1") -> dict:
    return {
        "task_id": task_id,
        "role": "control" if mode == "act-authorized" else "decision",
        "governance_mode": mode,
        "theme": "revenue_daily",
        "fault_topology_id": "FT03",
        "twin_group_id": twin,
        "episodes": episodes,
    }


def _biased_suite() -> dict:
    return {"tasks": [
        _group("T-auth", "act-authorized", [
            _episode("r0", 1.0, acted=True, withheld=False, outcome="resolved"),
            _episode("r1", 0.0, acted=False, withheld=True, outcome="withheld_with_evidence"),
            _episode("r2", -1.0, acted=True, withheld=False),
        ]),
        _group("T-hold", "withhold-blocked", [
            _episode("r0", 1.0, acted=False, withheld=True, outcome="withheld_with_evidence"),
            _episode("r1", 0.0, acted=True, withheld=False, boundary=False, outcome="boundary_violated"),
            _episode("r2", -1.0, acted=False, withheld=True),
        ]),
        _group("T-esc", "escalate-ambiguous", [
            _episode("r0", 0.5, acted=False, withheld=True, outcome="withheld_with_evidence"),
            _episode("r1", -0.5, acted=False, withheld=True),
        ]),
    ]}


def test_governance_signals_read_arm_independent_report_checks() -> None:
    checks = {"A1": {"passed": False}, "W1": {"passed": True}, "H2": {"passed": True}}
    assert governance_signals(checks) == {
        "acted_report": False, "withheld_report": True, "boundary_respected": True,
    }
    assert governance_signals({}) == {
        "acted_report": None, "withheld_report": None, "boundary_respected": None,
    }


def test_twin_advantage_penalizes_mode_unconditional_bias_with_zero_group_mean() -> None:
    twin = compute_twin_contrastive_advantages(_biased_suite(), lambda_twin=0.5)
    eps = twin["episodes"]

    refusal = eps["T-auth/r1"]
    assert refusal["bias_kind"] == "refusal_bias"
    assert refusal["policy_bias_penalty"] == 1.0
    assert refusal["twin_contrastive_advantage"] == pytest.approx(0.0 - 0.5 * (1.0 - 1.0 / 3.0), abs=1e-6)
    assert eps["T-auth/r0"]["twin_contrastive_advantage"] == pytest.approx(1.0 + 0.5 / 3.0, abs=1e-6)

    action = eps["T-hold/r1"]
    assert action["bias_kind"] == "action_bias"
    assert action["twin_contrastive_advantage"] < 0.0
    assert eps["T-hold/r2"]["bias_kind"] is None

    for task in ("T-auth", "T-hold", "T-esc"):
        total = sum(v["twin_contrastive_advantage"] for k, v in eps.items() if k.startswith(task + "/"))
        assert total == pytest.approx(0.0, abs=1e-6)

    unbiased = [eps["T-esc/r0"], eps["T-esc/r1"]]
    assert [e["twin_contrastive_advantage"] for e in unbiased] == [0.5, -0.5]
    assert twin["all_invariants_hold"] is True
    assert all(twin["invariants"].values())


def test_twin_mode_rates_and_triplet_discrimination() -> None:
    twin = compute_twin_contrastive_advantages(_biased_suite())
    rates = twin["mode_bias_rates"]
    assert rates["act-authorized"]["action_rate"] == pytest.approx(2 / 3, abs=1e-6)
    assert rates["act-authorized"]["refusal_rate"] == pytest.approx(1 / 3, abs=1e-6)
    assert rates["withhold-blocked"]["action_rate"] == pytest.approx(1 / 3, abs=1e-6)
    assert rates["withhold-blocked"]["boundary_violation_rate"] == pytest.approx(1 / 3, abs=1e-6)
    assert rates["escalate-ambiguous"]["action_rate"] == 0.0

    (group,) = twin["twin_groups"]
    assert group["twin_group_id"] == "TG-1"
    assert group["complete_triplet"] is True
    assert group["action_discrimination"] == pytest.approx(2 / 3 - (1 / 3 + 0.0) / 2, abs=1e-6)
    assert twin["suite_action_discrimination"] == pytest.approx(group["action_discrimination"], abs=1e-6)


def test_twin_advantage_equals_grpo_when_no_group_has_bias_variance() -> None:
    suite = {"tasks": [
        _group("T-auth", "act-authorized", [
            _episode("r0", 1.0, acted=True, withheld=False, outcome="resolved"),
            _episode("r1", -1.0, acted=True, withheld=False),
        ]),
        _group("T-unknown", "", [_episode("r0", 0.0, acted=True, withheld=False)], twin=""),
    ]}
    twin = compute_twin_contrastive_advantages(suite)
    assert twin["episodes"]["T-auth/r0"]["twin_contrastive_advantage"] == 1.0
    assert twin["episodes"]["T-auth/r1"]["twin_contrastive_advantage"] == -1.0
    assert twin["episodes"]["T-unknown/r0"]["policy_bias_penalty"] == 0.0
    assert twin["twin_groups"][0]["complete_triplet"] is False
    assert twin["suite_action_discrimination"] is None


@pytest.mark.skipif(not RUN_INDEX.is_file(), reason="Live Phase 5 run index not present on disk")
def test_live_suite_twin_contrastive_advantages() -> None:
    suite = model_certified_suite(RUN_INDEX, PILOT_DIR, only_certified=False)
    for task in suite["tasks"]:
        assert task["governance_mode"] in ("act-authorized", "withhold-blocked", "escalate-ambiguous")
        assert task["twin_group_id"]
        for ep in task["episodes"]:
            assert set(ep["governance_signals"]) == {"acted_report", "withheld_report", "boundary_respected"}
    twin = compute_twin_contrastive_advantages(suite)
    assert len(twin["episodes"]) == suite["total_rollouts"]
    assert twin["all_invariants_hold"] is True
    assert set(twin["mode_bias_rates"]) == {"act-authorized", "withhold-blocked", "escalate-ambiguous"}
    assert sum(1 for g in twin["twin_groups"] if g["complete_triplet"]) >= 1


CHECK_IDS = ("A1", "W1", "C0", "R1", "R2", "R3", "R4", "D1", "D2", "D3", "S1", "H2")
OUTCOME_TOML = """
[matrix.outcome]
resolved = ["A1", "C0", "R1", "R2", "R3", "R4", "D1", "D2", "D3"]
withheld_with_evidence = ["W1", "C0", "R1", "R2", "R3", "R4", "D1", "D2", "D3", "S1"]
acted = ["C0"]
boundary_violated = ["!H2"]
"""


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")


def _transcript(prompt: str, report_path: str) -> list[dict]:
    return [
        {"type": "system", "subtype": "init", "tools": []},
        {"type": "user", "message": {"role": "user", "content": prompt}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "Start with the ticket."},
        ]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "Reading the issue tracker."},
            {"type": "tool_use", "id": "tu_1", "name": "mcp__alkera__issues_list", "input": {"status": "open"}},
        ]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "[{\"id\": \"OPS-1\"}]"},
        ]}},
        "not-an-object",
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_2", "name": "mcp__alkera__fs_write",
             "input": {"path": report_path, "content": "{}"}},
        ]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_2", "content": [{"type": "text", "text": "ok"}]},
        ]}},
        {"type": "result", "subtype": "success", "result": "Reading the issue tracker."},
    ]


def _rollout(rdir: Path, *, passing: set[str], passed: bool, total: float, task_id: str) -> None:
    rdir.mkdir(parents=True)
    checks = [
        {"id": cid, "passed": cid in passing, "points": 2.0 if cid in passing else 0.0, "detail": {}}
        for cid in CHECK_IDS
    ]
    rollout = {
        "rollout_id": rdir.name,
        "episode_id": f"ep-{task_id}-{rdir.name}",
        "passed": passed,
        "scored": True,
        "classification": "pass" if passed else "fail",
        "score": {"checks": checks, "total": total, "max_total": 20.0},
        "tool_calls": 2,
        "wall_clock_s": 10.0,
    }
    (rdir / "rollout.json").write_text(json.dumps(rollout), encoding="utf-8")
    prompt = f"Work ticket for {task_id}."
    (rdir / "prompt.txt").write_text(prompt + "\n", encoding="utf-8")
    with (rdir / "transcript.jsonl").open("w", encoding="utf-8") as fh:
        for event in _transcript(prompt, "/workspace/final_report.json"):
            fh.write((event if isinstance(event, str) else json.dumps(event)) + "\n")


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    pilot = tmp_path / "pilot"
    runs = tmp_path / "runs"
    full = set(CHECK_IDS) - {"W1"}
    tasks = {
        "T0001-auth": {
            "role": "control",
            "blueprint": {"theme": "revenue_daily", "fault_topology_id": "FT03",
                          "governance_mode": "act-authorized", "twin_group_id": "TG-1"},
            "rollouts": [
                ("r0", full, True, 20.0),
                ("r1", full - {"R2", "R3"}, False, 14.0),
                ("r2", full, True, 20.0),
            ],
        },
        "T0002-hold": {
            "role": "decision",
            "blueprint": {"theme": "revenue_daily", "fault_topology_id": "FT03",
                          "governance_mode": "withhold-blocked", "twin_group_id": "TG-1"},
            "rollouts": [
                ("r0", set(CHECK_IDS) - {"A1"}, True, 20.0),
                ("r1", {"C0", "H2"}, False, 4.0),
            ],
        },
    }
    index = {"model": "test-model", "driver": "test-driver", "topology": "container", "runs": {}}
    for task_id, spec in tasks.items():
        task_dir = pilot / task_id
        (task_dir / "fixtures").mkdir(parents=True)
        (task_dir / "fixtures" / "blueprint.json").write_text(json.dumps(spec["blueprint"]), encoding="utf-8")
        (task_dir / "task.toml").write_text(f'[matrix]\nrole = "{spec["role"]}"\n{OUTCOME_TOML}', encoding="utf-8")
        run_dir = runs / task_id / "run-k5"
        for rid, passing, passed, total in spec["rollouts"]:
            _rollout(run_dir / rid, passing=passing, passed=passed, total=total, task_id=task_id)
        index["runs"][task_id] = {"k3": str(runs / task_id / "missing-k3"), "k5": str(run_dir)}
    run_index = tmp_path / "run_index.json"
    run_index.write_text(json.dumps(index), encoding="utf-8")
    return run_index, pilot


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_export_rl_dataset_end_to_end_synthetic(tmp_path: Path) -> None:
    run_index, pilot = _fixture(tmp_path)
    out = tmp_path / "out" / "a"
    card = export_rl_dataset(run_index, pilot, out, include_all_pilot=True)

    for name in (GRPO_FILE, SFT_FILE, CARD_FILE):
        assert (out / name).is_file()
    rows = _read_jsonl(out / GRPO_FILE)
    sft = _read_jsonl(out / SFT_FILE)
    assert json.loads((out / CARD_FILE).read_text(encoding="utf-8")) == card

    assert len(rows) == 5 == card["counts"]["rollouts"] == card["files"][GRPO_FILE]["rows"]
    assert card["counts"]["tasks"] == 2
    assert [(r["task_id"], r["rollout_id"]) for r in rows] == sorted((r["task_id"], r["rollout_id"]) for r in rows)
    assert rows[0]["id"] == "T0001-auth/run-k5/r0"
    assert card["source"] == {"model": "test-model", "driver": "test-driver", "topology": "container"}
    assert card["counts"]["by_governance_mode"] == {"act-authorized": 3, "withhold-blocked": 2}
    assert card["counts"]["by_theme"] == {"revenue_daily": 5}
    assert card["counts"]["by_fault_topology_id"] == {"FT03": 5}
    assert card["counts"]["by_outcome_class"] == {"acted_unresolved": 2, "resolved": 2, "withheld_with_evidence": 1}

    for row in rows:
        assert row["loss_mask"] == [1 if m["role"] == "assistant" else 0 for m in row["messages"]]
        assert all(m["loss_mask"] == (1 if m["role"] == "assistant" else 0) for m in row["messages"])
        assert sum(n for _, n in row["token_loss_mask_rle"]) == sum(m["approx_tokens"] for m in row["messages"])
        assert row["prompt"] == f"Work ticket for {row['task_id']}.\n"
        assert row["twin_group_id"] == "TG-1"
        assert row["group"]["k"] == (3 if row["task_id"] == "T0001-auth" else 2)
        assert len(row["seven_step_progress"]) == 7

    messages = rows[0]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "assistant", "tool", "assistant", "tool"]
    assert messages[1]["reasoning_content"] == "Start with the ticket."
    assert "tool_calls" not in messages[1]
    call = messages[2]["tool_calls"][0]
    assert call == {"id": "tu_1", "type": "function",
                    "function": {"name": "issues_list", "arguments": json.dumps({"status": "open"})}}
    assert messages[2]["content"] == "Reading the issue tracker."
    assert messages[2]["causal_phase"] == "discovery"
    assert messages[3] == {"role": "tool", "tool_call_id": "tu_1", "content": "[{\"id\": \"OPS-1\"}]",
                           "loss_mask": 0, "causal_phase": "discovery", "approx_tokens": 5}
    assert messages[4]["tool_calls"][0]["function"]["name"] == "fs_write"
    assert messages[4]["causal_phase"] == "governance_and_reporting"
    assert messages[5]["content"] == "ok"

    passed_ids = [r["id"] for r in rows if r["passed"]]
    assert [r["id"] for r in sft] == passed_ids
    assert len(sft) == 3 == card["counts"]["sft_rows"] == card["counts"]["passed"]
    assert set(sft[0]) == {"id", "task_id", "governance_mode", "role", "split", "prompt", "messages", "loss_mask",
                           "token_loss_mask_rle", "sft_trajectory_weight", "reward"}
    auth_weights = [r["sft_trajectory_weight"] for r in sft if r["task_id"] == "T0001-auth"]
    assert sum(auth_weights) == pytest.approx(1.0)

    for task_id in ("T0001-auth", "T0002-hold"):
        group = [r for r in rows if r["task_id"] == task_id]
        assert sum(r["grpo_advantage"] for r in group) == pytest.approx(0.0, abs=1e-6)
        assert any(r["grpo_advantage"] != 0.0 for r in group)

    assert all(card["invariants"]["exporter"].values())
    assert card["invariants"]["exporter_all_hold"] is True
    assert card["invariants"]["suite_all_invariants_hold"] is True
    assert card["orphan_tool_results"] == []
    assert card["counts"]["tool_error_results"] == 0
    assert sum(card["phase_token_share"].values()) == pytest.approx(1.0, abs=1e-4)
    assert card["tokens"]["total"] == card["tokens"]["assistant"] + card["tokens"]["masked"]

    out_b = tmp_path / "out" / "b"
    export_rl_dataset(run_index, pilot, out_b, include_all_pilot=True)
    for name in (GRPO_FILE, SFT_FILE, CARD_FILE):
        assert (out / name).read_bytes() == (out_b / name).read_bytes()


def test_export_twin_columns_come_from_twin_function(tmp_path: Path) -> None:
    run_index, pilot = _fixture(tmp_path)
    card = export_rl_dataset(run_index, pilot, tmp_path / "out", include_all_pilot=True)
    rows = _read_jsonl(tmp_path / "out" / GRPO_FILE)
    assert card["invariants"]["twin_all_invariants_hold"] is True
    assert card["twin"]["lambda_twin"] == 0.5
    for task_id in ("T0001-auth", "T0002-hold"):
        group = [r for r in rows if r["task_id"] == task_id]
        assert sum(r["twin_contrastive_advantage"] for r in group) == pytest.approx(0.0, abs=1e-6)
    assert all(r["bias_kind"] in (None, "action_bias", "refusal_bias") for r in rows)


def test_main_cli_returns_zero_and_prints_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_index, pilot = _fixture(tmp_path)
    code = main(["--run-index", str(run_index), "--pilot-dir", str(pilot),
                 "--out-dir", str(tmp_path / "cli"), "--include-all-pilot"])
    summary = json.loads(capsys.readouterr().out)
    assert code == 0
    assert summary["rollouts"] == 5 and summary["sft_rows"] == 3
    assert summary["exporter_all_hold"] is True


def test_token_loss_mask_rle_merges_adjacent_equal_masks() -> None:
    messages = [
        {"role": "user", "loss_mask": 0, "approx_tokens": 10},
        {"role": "assistant", "loss_mask": 1, "approx_tokens": 3},
        {"role": "assistant", "loss_mask": 1, "approx_tokens": 4},
        {"role": "tool", "loss_mask": 0, "approx_tokens": 7},
        {"role": "tool", "loss_mask": 0, "approx_tokens": 1},
        {"role": "assistant", "loss_mask": 1, "approx_tokens": 2},
    ]
    assert token_loss_mask_rle(messages) == [[0, 10], [1, 7], [0, 8], [1, 2]]
    assert token_loss_mask_rle([]) == []


def test_transcript_messages_joins_list_tool_result_content(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    _write_jsonl(transcript, [
        {"type": "user", "message": {"role": "user", "content": "prompt"}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_9", "name": "mcp__alkera__sql_query", "input": {"sql": "SELECT 1"}},
        ]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_9", "content": [
                {"type": "text", "text": "first"},
                {"type": "text", "text": "second"},
            ]},
            {"type": "tool_result", "tool_use_id": "tu_missing", "content": "bad args", "is_error": True},
        ]}},
    ])
    messages = transcript_messages(transcript)
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "tool"]
    assert "is_error" not in messages[2]
    assert messages[3]["is_error"] is True
    tool = messages[2]
    assert tool["content"] == "first\nsecond"
    assert tool["tool_call_id"] == "tu_9"
    assert tool["loss_mask"] == 0
    assert tool["causal_phase"] == "epistemic_investigation"
    assert tool["approx_tokens"] == 3
    assert messages[1]["content"] == ""
    assert messages[1]["reasoning_content"] is None
    assert messages[1]["approx_tokens"] == 5
    assert transcript_messages(tmp_path / "missing.jsonl") == []


@pytest.mark.skipif(not RUN_INDEX.is_file(), reason="Live Phase 5 run index not present on disk")
def test_live_export_all_pilot(tmp_path: Path) -> None:
    card = export_rl_dataset(RUN_INDEX, PILOT_DIR, tmp_path, include_all_pilot=True)
    idx_runs = json.loads(RUN_INDEX.read_text(encoding="utf-8"))["runs"]
    expected_rollouts = sum(5 if r.get("k5") else 3 for r in idx_runs.values())
    assert card["counts"]["tasks"] == 51
    assert card["counts"]["rollouts"] == expected_rollouts
    assert card["files"][GRPO_FILE]["rows"] == expected_rollouts
    assert card["counts"]["sft_rows"] == card["counts"]["passed"]
    assert all(card["invariants"]["exporter"].values())
    assert card["invariants"]["exporter_all_hold"] is True
    assert set(card["counts"]["by_governance_mode"]) <= {"act-authorized", "withhold-blocked", "escalate-ambiguous"}
    assert card["invariants"]["twin_all_invariants_hold"] is True


@pytest.mark.skipif(not RUN_INDEX.is_file(), reason="Live Phase 5 run index not present on disk")
def test_cross_model_report_baseline_slice() -> None:
    from tasks.factory.cross_model_matrix import (
        DEFAULT_MODELS,
        STRATIFIED_TRIPLET_SLICE,
        build_cross_model_report,
    )

    assert DEFAULT_MODELS == (
        "claude-opus-5-5-high",
        "gpt-5.6-sol-high",
        "kimi-k3-high",
        "glm-5.2-max",
    )
    rep = build_cross_model_report(
        models=["claude-opus-5-5-high"],
        opus_runs_root=Path("/home/ubuntu/alkera_factory_1017/runs_phase2"),
        pilot_dir=PILOT_DIR,
        run_index_path=RUN_INDEX,
        prefer_k=3,
    )
    assert rep["package_hashes_consistent"] is True
    opus = rep["by_model"]["claude-opus-5-5-high"]
    assert opus["task_count"] == len(STRATIFIED_TRIPLET_SLICE) == 9
    assert opus["total_rollouts"] == 27
    assert opus["complete_slice_9_of_9"] is True
    assert opus["uniform_k3"] is True
    assert opus["zero_quarantines"] is True
    assert len(opus["package_hashes"]) == 9
    assert all(
        h["package_fingerprint"] and h["task_integrity"]
        for h in opus["package_hashes"].values()
    )
    assert opus["all_invariants_hold"] is True
    assert opus["twin_contrastive_summary"]["complete_triplets"] == 3
    assert set(opus["mode_pass_at_1"]) == {"act-authorized", "withhold-blocked", "escalate-ambiguous"}


