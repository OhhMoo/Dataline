"""Tests for the leak-free triplet split and the open-weight SFT/RL loss verifier."""
from __future__ import annotations

import copy
import json
import math
import os
import py_compile
import subprocess
import sys
from pathlib import Path

import pytest

from tasks.factory import export_rl_dataset as exporter
from tasks.factory import train_open_weight_rl as trainer
from tasks.factory.export_rl_dataset import (
    CARD_FILE,
    EVAL_GRPO_FILE,
    EVAL_HOLDOUT_TASKS,
    GRPO_FILE,
    SFT_FILE,
    TRAIN_DPO_FILE,
    TRAIN_GRPO_FILE,
    TRAIN_SFT_FILE,
    export_rl_dataset,
    pair_tool_results,
)

LIVE_DATA = Path("/home/ubuntu/alkera_factory_1017/rl_dataset_v1")
CHECK_IDS = ("A1", "W1", "C0", "R1", "R2", "R3", "R4", "D1", "D2", "D3", "S1", "H2")
OUTCOME_TOML = """
[matrix.outcome]
resolved = ["A1", "C0", "R1", "R2", "R3", "R4", "D1", "D2", "D3"]
withheld_with_evidence = ["W1", "C0", "R1", "R2", "R3", "R4", "D1", "D2", "D3", "S1"]
acted = ["C0"]
boundary_violated = ["!H2"]
"""
AUTH_HOLDOUT, HOLD_HOLDOUT = EVAL_HOLDOUT_TASKS[0], EVAL_HOLDOUT_TASKS[1]


def _transcript(orphan: bool) -> list[dict]:
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "user", "message": {"role": "user", "content": "Repair the feed."}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "Check the tracker first."},
            {"type": "tool_use", "id": "tu_1", "name": "mcp__alkera__issues_list", "input": {}},
        ]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_1", "content": "OPS-1 open"},
        ]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "tu_2", "name": "mcp__alkera__fs_write",
             "input": {"path": "/workspace/final_report.json", "content": "{}"}},
        ]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_2", "content": "ok"},
        ]}},
    ]
    if orphan:
        events.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_lost", "content": "bad args", "is_error": True},
        ]}})
    events.append({"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "text", "text": "Done."},
    ]}})
    return events


def _write_task(pilot: Path, runs: Path, task_id: str, *, mode: str, twin: str, theme: str, topo: str,
                rollouts: list[tuple[set, bool, float]], orphan_in: str | None = None) -> Path:
    task_dir = pilot / task_id
    (task_dir / "fixtures").mkdir(parents=True)
    (task_dir / "fixtures/blueprint.json").write_text(json.dumps({
        "theme": theme, "fault_topology_id": topo, "governance_mode": mode, "twin_group_id": twin,
    }), encoding="utf-8")
    role = "control" if mode == "act-authorized" else "decision"
    (task_dir / "task.toml").write_text(f'[matrix]\nrole = "{role}"\n{OUTCOME_TOML}', encoding="utf-8")
    run_dir = runs / task_id / "run-k5"
    for i, (passing, passed, total) in enumerate(rollouts):
        rdir = run_dir / f"r{i}"
        rdir.mkdir(parents=True)
        checks = [{"id": c, "passed": c in passing, "points": 1.0, "detail": {}} for c in CHECK_IDS]
        (rdir / "rollout.json").write_text(json.dumps({
            "rollout_id": rdir.name, "episode_id": f"{task_id}-{i}", "passed": passed, "scored": True,
            "score": {"checks": checks, "total": total, "max_total": 20.0},
        }), encoding="utf-8")
        (rdir / "prompt.txt").write_text("Repair the feed.\n", encoding="utf-8")
        events = _transcript(orphan=orphan_in == rdir.name)
        (rdir / "transcript.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return run_dir


def _export(tmp_path: Path) -> tuple[Path, dict]:
    pilot, runs = tmp_path / "pilot", tmp_path / "runs"
    full = set(CHECK_IDS) - {"W1"}
    withheld = set(CHECK_IDS) - {"A1"}
    specs = {
        # Holdout triplet arms plus a non-holdout task sharing its twin group (a relative).
        AUTH_HOLDOUT: dict(mode="act-authorized", twin="TG-H", theme="commerce_analytics", topo="FT02",
                           rollouts=[(full, True, 20.0), (full - {"R3"}, False, 12.0), (full, True, 20.0)]),
        HOLD_HOLDOUT: dict(mode="withhold-blocked", twin="TG-H", theme="commerce_analytics", topo="FT02",
                           rollouts=[(withheld, True, 20.0), ({"C0", "H2"}, False, 4.0)]),
        "FT9001-commerce-analytics-ft02-esc-z": dict(mode="escalate-ambiguous", twin="TG-H",
                                                     theme="commerce_analytics", topo="FT02",
                                                     rollouts=[(withheld, True, 20.0), (withheld, True, 20.0)]),
        "FT9002-revenue-daily-ft05-auth-z": dict(mode="act-authorized", twin="TG-T", theme="revenue_daily",
                                                 topo="FT05", orphan_in="r1",
                                                 rollouts=[(full, True, 20.0), (full - {"R2"}, False, 14.0),
                                                           (full, True, 20.0)]),
        "FT9003-revenue-daily-ft05-hold-z": dict(mode="withhold-blocked", twin="TG-T", theme="revenue_daily",
                                                 topo="FT05",
                                                 rollouts=[(withheld, True, 20.0), (withheld, True, 20.0)]),
    }
    index = {"model": "m", "driver": "d", "topology": "container", "runs": {}}
    for task_id, spec in specs.items():
        run_dir = _write_task(pilot, runs, task_id, **spec)
        index["runs"][task_id] = {"k3": str(run_dir), "k5": str(run_dir)}
    run_index = tmp_path / "run_index.json"
    run_index.write_text(json.dumps(index), encoding="utf-8")
    out = tmp_path / "dataset"
    card = export_rl_dataset(run_index, pilot, out, include_all_pilot=True)
    return out, card


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_split_holds_out_whole_twin_groups(tmp_path: Path) -> None:
    out, card = _export(tmp_path)
    rows = _jsonl(out / GRPO_FILE)
    split = {r["task_id"]: r["split"] for r in rows}
    assert split[AUTH_HOLDOUT] == split[HOLD_HOLDOUT] == "eval_holdout"
    assert split["FT9001-commerce-analytics-ft02-esc-z"] == "eval_holdout"
    assert split["FT9002-revenue-daily-ft05-auth-z"] == split["FT9003-revenue-daily-ft05-hold-z"] == "train"

    train, held = _jsonl(out / TRAIN_GRPO_FILE), _jsonl(out / EVAL_GRPO_FILE)
    train_sft, all_sft = _jsonl(out / TRAIN_SFT_FILE), _jsonl(out / SFT_FILE)
    assert len(train) + len(held) == len(rows) == 12
    assert {r["task_id"] for r in train}.isdisjoint({r["task_id"] for r in held})
    assert {r["twin_group_id"] for r in train} == {"TG-T"}
    assert all(r["split"] in ("train", "eval_holdout") for r in all_sft)
    assert [r["id"] for r in train_sft] == [r["id"] for r in all_sft if r["split"] == "train"]
    assert card["counts"]["by_split"] == {"eval_holdout": 7, "train": 5}
    assert card["invariants"]["exporter"]["split_twin_disjoint"] is True
    assert trainer.check_split(train, held, train_sft)["passed"] is True


def test_export_drops_orphan_tool_results(tmp_path: Path) -> None:
    out, card = _export(tmp_path)
    assert card["dropped_orphan_tool_results"] == 1
    assert card["dropped_orphan_tool_result_ids"] == [
        "FT9002-revenue-daily-ft05-auth-z/run-k5/r1#tu_lost"]
    assert card["orphan_tool_results"] == []
    assert card["invariants"]["exporter"]["tool_results_paired"] is True
    for name in (GRPO_FILE, SFT_FILE, TRAIN_GRPO_FILE, TRAIN_SFT_FILE, EVAL_GRPO_FILE):
        for row in _jsonl(out / name):
            issued: set[str] = set()
            for m in row["messages"]:
                if m["role"] == "assistant":
                    issued.update(c["id"] for c in m.get("tool_calls") or [])
                elif m["role"] == "tool":
                    assert m["tool_call_id"] in issued


def test_pair_tool_results_keeps_answered_and_drops_unanswered() -> None:
    messages = [
        {"role": "tool", "tool_call_id": "early"},
        {"role": "assistant", "tool_calls": [{"id": "a"}]},
        {"role": "tool", "tool_call_id": "a"},
        {"role": "tool", "tool_call_id": "b"},
    ]
    kept, dropped = pair_tool_results(messages)
    assert [m.get("tool_call_id") for m in kept] == [None, "a"]
    assert dropped == ["early", "b"]


def test_verify_cpu_passes_on_exported_split(tmp_path: Path) -> None:
    out, _ = _export(tmp_path)
    report = trainer.verify_cpu(out)
    assert report["all_passed"] is True
    live = report["live_checks"]
    assert live["split_disjoint"]["shared_keys"] == []
    assert live["sft_loss_gradient"]["masked_positions"] > 0
    assert live["sft_loss_gradient"]["masked_positions_with_gradient"] == 0
    assert live["dapo_surrogate_gradient"]["rows_dropped_collapsed"] == 2
    assert live["dapo_surrogate_gradient"]["masked_positions_with_gradient"] == 0


def test_check_split_detects_twin_leak(tmp_path: Path) -> None:
    out, _ = _export(tmp_path)
    train, held = _jsonl(out / TRAIN_GRPO_FILE), _jsonl(out / EVAL_GRPO_FILE)
    leaked = copy.deepcopy(held[0])
    leaked["split"] = "train"
    leaked["task_id"] = "FT9999-other"
    result = trainer.check_split(train + [leaked], held, [])
    assert result["passed"] is False
    assert "twin:TG-H" in result["shared_keys"]


def test_check_masking_detects_label_and_pairing_errors(tmp_path: Path) -> None:
    out, _ = _export(tmp_path)
    row = _jsonl(out / TRAIN_GRPO_FILE)[0]
    assert trainer.check_masking([row])["passed"] is True
    tool_labelled = copy.deepcopy(row)
    idx = next(i for i, m in enumerate(tool_labelled["messages"]) if m["role"] == "tool")
    tool_labelled["messages"][idx]["loss_mask"] = 1
    tool_labelled["loss_mask"][idx] = 1
    orphan = copy.deepcopy(row)
    orphan["messages"].insert(1, {"role": "tool", "tool_call_id": "ghost", "content": "", "loss_mask": 0,
                                  "causal_phase": "discovery", "approx_tokens": 1})
    orphan["loss_mask"].insert(1, 0)
    orphan["token_loss_mask_rle"].append([0, 1])
    assert trainer.check_masking([tool_labelled])["passed"] is False
    assert trainer.check_masking([orphan])["passed"] is False


def test_check_live_advantages_detects_corruption(tmp_path: Path) -> None:
    out, _ = _export(tmp_path)
    rows = _jsonl(out / GRPO_FILE)
    assert trainer.check_live_advantages(rows, 0.5)["passed"] is True
    bad = copy.deepcopy(rows)
    bad[0]["grpo_advantage"] += 0.01
    assert trainer.check_live_advantages(bad, 0.5)["advantage_mismatches"] == [f"{bad[0]['id']}:grpo_advantage"]


def test_reference_loss_and_gradient_checks_all_pass() -> None:
    checks = trainer.reference_checks()
    assert checks and all(c["passed"] for c in checks.values()), {k for k, v in checks.items() if not v["passed"]}


def test_sft_masked_positions_have_zero_gradient_and_match_finite_differences() -> None:
    logits = [[0.2, -1.0, 0.5], [1.5, 0.0, -0.5], [0.0, 0.3, 0.1]]
    labels = [2, trainer.IGNORE_INDEX, 0]
    loss, grad = trainer.sft_loss_and_grad(logits, labels, weight=2.0)
    expected = 2.0 * -(trainer.log_softmax(logits[0])[2] + trainer.log_softmax(logits[2])[0]) / 2
    assert loss == pytest.approx(expected)
    assert grad[1] == [0.0, 0.0, 0.0]
    for t in (0, 2):
        for v in range(3):
            def f(row: list[float], t=t) -> float:
                patched = [list(r) for r in logits]
                patched[t] = row
                return trainer.sft_loss_and_grad(patched, labels, weight=2.0)[0]
            assert grad[t][v] == pytest.approx(trainer.finite_difference(f, logits[t], v), rel=1e-5, abs=1e-8)


def test_dapo_asymmetric_clip_and_collapsed_group_filtering() -> None:
    new = [math.log(1.5), math.log(1.5), math.log(0.5), math.log(0.5)]
    adv = [1.0, -1.0, 1.0, -1.0]
    loss, grad = trainer.dapo_loss_and_grad(new, [0.0] * 4, adv, [1, 1, 1, 1])
    # Positive advantage clips above 1.28, negative advantage clips below 0.80.
    assert grad[0] == 0.0 and grad[3] == 0.0
    assert grad[1] == pytest.approx(1.5 / 4) and grad[2] == pytest.approx(-0.5 / 4)
    assert loss == pytest.approx(-(1.28 * 1.0 + 1.5 * -1.0 + 0.5 * 1.0 + 0.8 * -1.0) / 4)
    adv_c, collapsed = trainer.grpo_advantages([0.6, 0.6, 0.6])
    assert collapsed and adv_c == [0.0, 0.0, 0.0]
    assert trainer.dapo_loss_and_grad([0.1] * 3, [0.0] * 3, adv_c, [1, 1, 1])[1] == [0.0, 0.0, 0.0]


def test_grpo_rloo_twin_and_phase_closed_forms() -> None:
    rewards = [1.0, 0.0, 0.5]
    grpo, collapsed = trainer.grpo_advantages(rewards)
    std = math.sqrt(sum((r - 0.5) ** 2 for r in rewards) / 3)
    assert not collapsed
    assert grpo == pytest.approx([(r - 0.5) / (std + trainer.GRPO_EPS) for r in rewards])
    assert trainer.rloo_advantages(rewards) == pytest.approx([0.75, -0.75, 0.0])
    twin = trainer.twin_advantages(grpo, [0.0, 1.0, 0.0], 0.5)
    assert sum(twin) == pytest.approx(0.0, abs=1e-12)
    assert twin[1] == pytest.approx(grpo[1] - 0.5 * (1 - 1 / 3))
    assert trainer.phase_step_advantage(2.0, 1.0, alpha=0.7) == pytest.approx(1.7)


def test_generate_gpu_bundle_writes_compilable_entrypoints(tmp_path: Path) -> None:
    out, _ = _export(tmp_path)
    bundle = tmp_path / "bundle"
    result = trainer.generate_gpu_bundle(out, bundle)
    assert result["report"]["all_passed"] is True
    for name in ("run_sft_lora.py", "run_offline_rloo_dpo.py", "run_online_grpo_verl.py",
                 "launch_gpu.sh", trainer.VERIFICATION_FILE, "manifest.json"):
        assert (bundle / name).is_file(), name
    for script in bundle.glob("*.py"):
        py_compile.compile(str(script), cfile=str(tmp_path / "pyc" / f"{script.name}c"), doraise=True)
    assert (bundle / "launch_gpu.sh").stat().st_mode & 0o111
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["verification_all_passed"] is True
    assert set(manifest["files"]) == {
        p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file() and p.name != "manifest.json"
    }
    assert (bundle / trainer.FSDP_24K_CONFIG).is_file()
    assert (bundle / trainer.DEEPSPEED_24K_CONFIG).is_file()
    assert json.loads((bundle / trainer.VERIFICATION_FILE).read_text(encoding="utf-8"))["all_passed"] is True


def test_cli_verify_cpu_exit_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out, _ = _export(tmp_path)
    assert trainer.main(["--mode", "verify-cpu", "--data-dir", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["all_passed"] is True
    (out / TRAIN_SFT_FILE).write_text(
        (out / TRAIN_SFT_FILE).read_text(encoding="utf-8") + (out / EVAL_GRPO_FILE).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    assert trainer.main(["--mode", "verify-cpu", "--data-dir", str(out)]) == 1


@pytest.mark.skipif(not (LIVE_DATA / TRAIN_GRPO_FILE).is_file(), reason="live rl_dataset_v1 split not exported")
def test_live_dataset_split_and_losses_verify() -> None:
    card = json.loads((LIVE_DATA / CARD_FILE).read_text(encoding="utf-8"))
    assert card["orphan_tool_results"] == []
    assert sorted(card["split"]["eval_holdout_tasks_present"]) == sorted(EVAL_HOLDOUT_TASKS)
    report = trainer.verify_cpu(LIVE_DATA)
    assert report["all_passed"] is True
    assert report["live_checks"]["split_disjoint"]["eval_tasks"] == len(EVAL_HOLDOUT_TASKS)


# ---------------------------------------------------------------------------
# Gap 1B: DPO pairs, 24k observation compaction, extra roots, cluster configs
# ---------------------------------------------------------------------------

def test_export_writes_train_dpo_pairs_only_from_train_reward_1(tmp_path: Path) -> None:
    out, card = _export(tmp_path)
    pairs = _jsonl(out / TRAIN_DPO_FILE)
    # Only FT9002 (train) mixes a reward-1.0 passing rollout with a lower one;
    # FT9003 is all-pass train and the holdout triplet is mixed but excluded.
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair["task_id"] == "FT9002-revenue-daily-ft05-auth-z"
    assert pair["task_id"] not in EVAL_HOLDOUT_TASKS
    assert pair["split"] == "train"
    assert pair["reward_chosen"] == 1.0
    assert pair["reward_rejected"] < 1.0
    assert pair["reward_margin"] == pytest.approx(pair["reward_chosen"] - pair["reward_rejected"])
    assert pair["reward_margin"] > 0
    assert pair["progress_margin"] == pytest.approx(pair["progress_chosen"] - pair["progress_rejected"])
    assert pair["chosen"]["id"] == pair["chosen_id"] and pair["rejected"]["id"] == pair["rejected_id"]
    assert pair["chosen"]["task_id"] == pair["rejected"]["task_id"] == pair["task_id"]
    assert pair["chosen"]["messages"] and pair["rejected"]["messages"]
    assert {"chosen", "rejected", "reward_chosen", "reward_rejected", "reward_margin",
            "progress_margin", "split"} <= set(pair)
    assert card["counts"]["dpo_pairs"] == 1
    assert card["dpo_pairs"] == {
        "rows": 1,
        "tasks": ["FT9002-revenue-daily-ft05-auth-z"],
        "min_reward_margin": pair["reward_margin"],
        "all_chosen_reward_1": True,
        "all_margins_positive": True,
        "all_split_train": True,
    }
    assert card["files"][TRAIN_DPO_FILE]["rows"] == 1
    assert trainer.verify_cpu(out)["live_checks"]["dpo_pairs"]["passed"] is True


def _long_tool_messages() -> list[dict]:
    huge = "row_count=42 passed=true ticket=OPS-1\n" + ("observation payload line\n" * 12000) + "business_day=2026-10-24 approved_by=ops\n"
    return [
        {"role": "user", "content": "Repair the feed.", "loss_mask": 0, "causal_phase": "discovery", "approx_tokens": 5},
        {"role": "assistant", "content": "Inspecting.", "reasoning_content": "check first",
         "tool_calls": [{"id": "tu_1", "type": "function", "function": {"name": "sql_query", "arguments": "{}"}}],
         "loss_mask": 1, "causal_phase": "discovery", "approx_tokens": 8},
        {"role": "tool", "tool_call_id": "tu_1", "content": huge, "loss_mask": 0,
         "causal_phase": "discovery", "approx_tokens": max(1, math.ceil(len(huge) / 4))},
        {"role": "assistant", "content": "Repairing and writing the final report.",
         "tool_calls": [{"id": "tu_2", "type": "function",
                         "function": {"name": "fs_write", "arguments": json.dumps({"path": "/workspace/final_report.json", "content": "{}"})}}],
         "loss_mask": 1, "causal_phase": "governance_and_reporting", "approx_tokens": 30},
        {"role": "tool", "tool_call_id": "tu_2", "content": "ok", "loss_mask": 0,
         "causal_phase": "governance_and_reporting", "approx_tokens": 1},
        {"role": "assistant", "content": "Done.", "reasoning_content": None,
         "loss_mask": 1, "causal_phase": "governance_and_reporting", "approx_tokens": 2},
    ]


def test_zero_assistant_tokens_truncated_at_24576_after_compaction() -> None:
    messages = _long_tool_messages()
    assert sum(m["approx_tokens"] for m in messages) > 24576
    assistants_before = [copy.deepcopy(m) for m in messages if m["role"] == "assistant"]

    for result in (
        exporter.compact_messages_fallback(messages, max_seq_tokens=24576),
        exporter.apply_observation_compaction(messages, max_seq_tokens=24576),
        trainer.compact_messages_for_training(messages, max_seq_tokens=24576),
    ):
        assert result["fits_max_seq_tokens"] is True
        assert result["compacted_tokens"] <= 24576
        assert result["assistant_tokens_before"] == result["assistant_tokens_after"]
        assert [m for m in result["messages"] if m["role"] == "assistant"] == assistants_before
        # The tail governance/reporting assistant turn survives byte-identically.
        assert result["messages"][-1]["content"] == "Done."
        assert "final_report.json" in result["messages"][3]["tool_calls"][0]["function"]["arguments"]

    fitted = trainer.apply_left_obs_first_truncation(messages, 24576)
    assert fitted["truncated_assistant_tokens"] == 0
    assert fitted["fits_max_seq_tokens"] is True
    assert [m for m in fitted["messages"] if m["role"] == "assistant"] == assistants_before

    row = {"id": "synthetic/r0", "messages": copy.deepcopy(messages),
           "loss_mask": [m["loss_mask"] for m in messages],
           "token_loss_mask_rle": exporter.token_loss_mask_rle(messages)}
    fitted_rows, stats = trainer.load_rows_within_token_budget([row], max_seq_tokens=24576)
    assert stats["truncated_assistant_tokens"] == 0
    assert stats["assistant_tokens_preserved"] is True
    assert stats["rows_fitting"] == 1 and stats["max_compacted_tokens"] <= 24576
    assert [m for m in fitted_rows[0]["messages"] if m["role"] == "assistant"] == assistants_before
    assert sum(n for _, n in fitted_rows[0]["token_loss_mask_rle"]) == sum(
        m["approx_tokens"] for m in fitted_rows[0]["messages"])


def test_export_compact_max_tokens_preserves_assistant_and_key_sets(tmp_path: Path) -> None:
    out, _ = _export(tmp_path)
    compact_out = tmp_path / "dataset_compact"
    card = export_rl_dataset(
        tmp_path / "run_index.json", tmp_path / "pilot", compact_out,
        include_all_pilot=True, compact_max_tokens=24576,
    )
    assert card["compaction"]["enabled"] is True
    assert card["compaction"]["max_seq_tokens"] == 24576
    assert card["compaction"]["assistant_tokens_preserved"] is True
    assert card["compaction"]["rows_fitting"] == card["compaction"]["rows"]
    plain_rows = {r["id"]: r for r in _jsonl(out / GRPO_FILE)}
    for row in _jsonl(compact_out / GRPO_FILE):
        plain = plain_rows[row["id"]]
        assert [m for m in row["messages"] if m["role"] == "assistant"] == [
            m for m in plain["messages"] if m["role"] == "assistant"]
        assert [set(m) for m in row["messages"]] == [set(m) for m in plain["messages"]]
        assert sum(m["approx_tokens"] for m in row["messages"]) <= 24576
    plain_sft, compact_sft = _jsonl(out / TRAIN_SFT_FILE), _jsonl(compact_out / TRAIN_SFT_FILE)
    assert set(compact_sft[0]) == set(plain_sft[0]) == {
        "id", "task_id", "governance_mode", "role", "split", "prompt", "messages", "loss_mask",
        "token_loss_mask_rle", "sft_trajectory_weight", "reward"}
    assert [set(m) for m in compact_sft[0]["messages"]] == [set(m) for m in plain_sft[0]["messages"]]
    # Default (no compaction) export is byte-identical to the first export.
    assert (tmp_path / "dataset" / GRPO_FILE).read_bytes() == (out / GRPO_FILE).read_bytes()


def test_export_extra_runs_roots_merges_cross_model_trajectories(tmp_path: Path) -> None:
    out, _ = _export(tmp_path)
    train_row = _jsonl(out / TRAIN_GRPO_FILE)[0]
    extra_dir = tmp_path / "extra_runs"
    extra_dir.mkdir()
    cross = copy.deepcopy(train_row)
    cross.update({"id": f"{train_row['task_id']}/cross-model/rX", "rollout_id": "rX",
                  "reward": 0.0, "passed": False, "progress_scalar": 0.1})
    holdout_cross = copy.deepcopy(train_row)
    holdout_cross.update({"id": f"{AUTH_HOLDOUT}/cross-model/rX", "task_id": AUTH_HOLDOUT,
                          "rollout_id": "rX", "reward": 0.0, "passed": False})
    (extra_dir / "cross.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in (cross, holdout_cross)), encoding="utf-8")

    merged_out = tmp_path / "dataset_merged"
    card = export_rl_dataset(
        tmp_path / "run_index.json", tmp_path / "pilot", merged_out,
        include_all_pilot=True, extra_runs_roots=[extra_dir],
    )
    merged = _jsonl(merged_out / TRAIN_GRPO_FILE)
    assert cross["id"] in {r["id"] for r in merged}
    assert holdout_cross["id"] not in {r["id"] for r in merged}
    assert all(r["task_id"] not in EVAL_HOLDOUT_TASKS for r in merged)
    assert card["counts"]["extra_rows_merged"] == 1
    assert card["extra_runs"]["tasks"] == [train_row["task_id"]]
    # Group advantages were recomputed over the merged group (zero-sum holds).
    assert trainer.verify_cpu(merged_out)["all_passed"] is True
    pairs = _jsonl(merged_out / TRAIN_DPO_FILE)
    assert any(p["rejected_id"] == cross["id"] for p in pairs)
    with pytest.raises(FileNotFoundError):
        export_rl_dataset(
            tmp_path / "run_index.json", tmp_path / "pilot", tmp_path / "nope",
            include_all_pilot=True, extra_runs_roots=[tmp_path / "missing-root"],
        )


def test_dpo_loss_exact_gradient_and_descent_direction() -> None:
    beta = 0.5
    # Masked tokens (logp 99.0) must not change the loss or receive gradient.
    loss, grad_c, grad_r = trainer.dpo_loss_and_grad(
        [-1.0, -2.0, 99.0], [-0.5, 99.0], [-1.0, -1.0, 0.0], [-1.5, 0.0],
        beta=beta, chosen_mask=[1, 1, 0], rejected_mask=[1, 0],
    )
    # delta = ((-3) - (-2)) - ((-0.5) - (-1.5)) = -2; L = -log sigmoid(-1).
    assert loss == pytest.approx(math.log1p(math.exp(1.0)), rel=1e-12)
    assert loss == pytest.approx(trainer.dpo_sequence_loss(-3.0, -0.5, -2.0, -1.5, beta=beta), rel=1e-12)
    coeff = beta / (1.0 + math.exp(-1.0))  # beta * sigmoid(-beta * delta) = beta * sigmoid(1)
    # Exact analytic gradient of L w.r.t. policy logps: negative on chosen,
    # positive on rejected -- so the descent *step* (-grad) is positive on
    # chosen tokens and negative on rejected tokens.
    assert grad_c[:2] == pytest.approx([-coeff, -coeff], rel=1e-12)
    assert grad_r[0] == pytest.approx(coeff, rel=1e-12)
    assert all(step > 0 for step in (-grad_c[0], -grad_c[1]))
    assert -grad_r[0] < 0
    assert grad_c[2] == 0.0 and grad_r[1] == 0.0  # loss_mask == 0: exactly zero
    # Central finite differences agree on every unmasked position.
    chosen, rejected, ref_c, ref_r = [-1.0, -2.0], [-0.5], [-1.0, -1.0], [-1.5]
    _, gc, gr = trainer.dpo_loss_and_grad(chosen, rejected, ref_c, ref_r, beta=beta)
    for t in range(len(chosen)):
        fd = trainer.finite_difference(
            lambda x, t=t: trainer.dpo_loss_and_grad(x, rejected, ref_c, ref_r, beta=beta)[0], chosen, t)
        assert gc[t] == pytest.approx(fd, rel=1e-5, abs=1e-8)
    fd = trainer.finite_difference(
        lambda x: trainer.dpo_loss_and_grad(chosen, x, ref_c, ref_r, beta=beta)[0], rejected, 0)
    assert gr[0] == pytest.approx(fd, rel=1e-5, abs=1e-8)
    checks = trainer.reference_checks()
    assert {"dpo_margin_loss_closed_form", "dpo_gradient_closed_form", "dpo_finite_difference"} <= set(checks)
    assert all(checks[k]["passed"] for k in checks if k.startswith("dpo_"))


def _run_bundle_script(bundle: Path, script: str, *argv: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(bundle / script), *argv],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"{script} {argv}: {proc.stderr[-2000:]}"
    return json.loads(proc.stdout)


def test_cluster_configs_and_bundle_dry_runs(tmp_path: Path) -> None:
    out, _ = _export(tmp_path)
    bundle = tmp_path / "bundle"
    trainer.generate_gpu_bundle(out, bundle)

    fsdp_path, ds_path = bundle / trainer.FSDP_24K_CONFIG, bundle / trainer.DEEPSPEED_24K_CONFIG
    assert fsdp_path.is_file() and ds_path.is_file()
    fsdp_text = fsdp_path.read_text(encoding="utf-8")
    for needle in ("gpus_per_node: 8", "max_seq_len: 24576", "truncation_side: left_obs_first",
                   "Qwen/Qwen2.5-Coder-7B-Instruct", "Qwen/Qwen2.5-Coder-14B-Instruct",
                   "Qwen/Qwen2.5-Coder-32B-Instruct", "FULL_SHARD"):
        assert needle in fsdp_text, needle
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None  # noqa: F841
    if yaml is not None:
        fsdp = yaml.safe_load(fsdp_text)
        assert fsdp["cluster"]["gpus_per_node"] == 8
        assert fsdp["data"]["max_seq_len"] == 24576
        assert fsdp["data"]["truncation_side"] == "left_obs_first"
    ds = json.loads(ds_path.read_text(encoding="utf-8"))
    assert ds["cluster"]["cluster_gpus"] == 4
    assert ds["data"]["max_seq_len"] == 24576 and ds["data"]["truncation_side"] == "left_obs_first"
    assert ds["zero_optimization"]["stage"] == 3 and ds["bf16"]["enabled"] is True
    assert ds["model"]["supported_models"] == list(trainer.SUPPORTED_MODELS)

    launch = (bundle / "launch_gpu.sh").read_text(encoding="utf-8")
    for needle in ("CLUSTER_GPUS", "4|8", "Qwen/Qwen2.5-Coder-7B-Instruct",
                   "Qwen/Qwen2.5-Coder-32B-Instruct", "rloo) run_offline rloo", "left_obs_first"):
        assert needle in launch, needle

    sft_plan = _run_bundle_script(bundle, "run_sft_lora.py", "--data-dir", str(out), "--dry-run")
    assert sft_plan["hyperparams"]["max_seq_len"] == 24576
    assert sft_plan["hyperparams"]["truncation_side"] == "left_obs_first"
    assert sft_plan["assistant_tokens_truncated"] == 0
    assert sft_plan["tokens"]["rows_over_max_seq_len_approx"] == 0

    dpo_plan = _run_bundle_script(bundle, "run_offline_rloo_dpo.py", "--algo", "dpo", "--data-dir", str(out), "--dry-run")
    assert dpo_plan["algo"] == "dpo" and dpo_plan["dpo"]["pairs"] >= 1
    assert dpo_plan["dpo"]["source"].endswith(TRAIN_DPO_FILE)
    rloo_plan = _run_bundle_script(bundle, "run_offline_rloo_dpo.py", "--algo", "rloo", "--data-dir", str(out), "--dry-run")
    assert rloo_plan["algo"] == "rloo" and "rloo" in rloo_plan
    awr_plan = _run_bundle_script(bundle, "run_offline_rloo_dpo.py", "--algo", "awr", "--data-dir", str(out), "--dry-run")
    assert awr_plan["algo"] == "awr"

    grpo_plan = _run_bundle_script(
        bundle, "run_online_grpo_verl.py", "--data-dir", str(out), "--dry-run",
        "--cluster-gpus", "4", "--output-dir", str(tmp_path / "grpo_out"))
    assert grpo_plan["cluster_gpus"] == 4
    assert grpo_plan["truncation_side"] == "left_obs_first"

    env = {**os.environ, "DRY_RUN": "1", "CLUSTER_GPUS": "4", "MODEL": "7B",
           "DATA_DIR": str(out), "PYTHON": sys.executable, "OUT_ROOT": str(tmp_path / "launch_out")}
    for stage in ("sft", "dpo", "rloo"):
        proc = subprocess.run(["bash", str(bundle / "launch_gpu.sh"), stage],
                              capture_output=True, text=True, timeout=120, env=env)
        assert proc.returncode == 0, f"launch {stage}: {proc.stderr[-2000:]}"
    env["CLUSTER_GPUS"] = "8"
    env["MODEL"] = "Qwen/Qwen2.5-Coder-32B-Instruct"
    proc = subprocess.run(["bash", str(bundle / "launch_gpu.sh"), "sft"],
                          capture_output=True, text=True, timeout=120, env=env)
    assert proc.returncode == 0, proc.stderr[-2000:]
    env["CLUSTER_GPUS"] = "3"
    proc = subprocess.run(["bash", str(bundle / "launch_gpu.sh"), "sft"],
                          capture_output=True, text=True, timeout=120, env=env)
    assert proc.returncode == 64


@pytest.mark.skipif(not (LIVE_DATA / TRAIN_GRPO_FILE).is_file(), reason="live rl_dataset_v1 split not exported")
def test_live_dpo_pairs_and_24k_loader() -> None:
    train_rows = trainer.load_jsonl(LIVE_DATA / TRAIN_GRPO_FILE)
    pairs = exporter.build_dpo_pairs(train_rows)
    assert pairs and all(
        p["split"] == "train" and p["reward_chosen"] == 1.0 and p["reward_margin"] > 0
        and p["task_id"] not in EVAL_HOLDOUT_TASKS
        for p in pairs)
    # 8 mixed train groups in rl_dataset_v1 contribute one pair per rejected row.
    assert len(pairs) == 20 and len({p["task_id"] for p in pairs}) == 8

    sft_rows = trainer.load_jsonl(LIVE_DATA / TRAIN_SFT_FILE)
    fitted, stats = trainer.load_rows_within_token_budget(sft_rows, max_seq_tokens=24576)
    assert stats["truncated_assistant_tokens"] == 0
    assert stats["assistant_tokens_preserved"] is True
    assert stats["rows_fitting"] == len(sft_rows)
    assert stats["max_compacted_tokens"] <= 24576
    for before, after in zip(sft_rows, fitted):
        assert [m for m in after["messages"] if m["role"] == "assistant"] == [
            m for m in before["messages"] if m["role"] == "assistant"]
