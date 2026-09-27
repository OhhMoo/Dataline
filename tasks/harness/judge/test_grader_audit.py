"""grader_audit.py and model_client.py, with a scripted model: no network.

    python -m pytest -q tasks/harness/judge/test_grader_audit.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import grader_audit as ga  # noqa: E402
import model_client as mc  # noqa: E402


def reply(verdict: str, **extra) -> str:
    body = {"check": "X1", "verdict": verdict, "confidence": "high",
            "evidence": ["e"], "explanation": "because", **extra}
    return f"some reasoning\n```json\n{json.dumps(body)}\n```\n"


def scripted(*texts):
    queue = list(texts)
    return lambda prompt: queue.pop(0) if queue else ""


# ---------------------------------------------------------------- model_client
def test_extract_json_prefers_the_last_fenced_block():
    text = '```json\n{"a": 1}\n```\nthen\n```json\n{"a": 2}\n```'
    assert mc.extract_json(text) == {"a": 2}
    assert mc.extract_json('prose {"b": 3} prose') == {"b": 3}
    assert mc.extract_json("no json here") is None


def test_majority_needs_a_strict_majority():
    assert mc.majority(["a", "a", "b"]) == ("a", 0.667)
    assert mc.majority(["a", "b"])[0] == "unknown"
    assert mc.majority(["", ""]) == ("unknown", 0.0)
    assert mc.majority(["a", "", ""])[0] == "a"          # failed samples do not vote


def test_family_and_same_family_flag():
    assert mc.family("opus") == mc.family("claude-opus-5-5") == "claude"
    assert mc.family("gpt-5") == "openai"
    prov = mc.ClaudeClient(model="opus").provenance("claude-opus-5-5")
    assert prov["same_family_as_subject"] is True


def test_client_argv_tools_and_isolation():
    no_tools = mc.ClaudeClient(claude="claude")._argv()
    assert no_tools[no_tools.index("--tools") + 1] == ""
    with_tools = mc.ClaudeClient(claude="claude", tools=True)._argv()
    assert with_tools[with_tools.index("--tools") + 1] == mc.TOOLS
    assert "--setting-sources" in with_tools and "--strict-mcp-config" in with_tools


def test_client_satisfies_the_clarity_gate_protocol():
    from clarity_gate import judge_brief
    client = mc.ClaudeClient(runner=lambda p: "reject\nbecause the brief leaks")
    assert judge_brief("Fix the orders table.", client=client) == "reject"


def test_a_failing_sample_does_not_vote():
    def flaky(prompt, state={"n": 0}):
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("boom")
        return reply("grader_wrong")
    client = mc.ClaudeClient(k=3, runner=flaky)
    assert ga.judge_check(client, "p")["verdict"] == "grader_wrong"


# ---------------------------------------------------------------- grader_audit
def _rollout(tmp: Path, checks, passed=False, name="r0") -> Path:
    d = tmp / "GT006-quality-gate-drops-rows" / "run-1" / name
    (d / "world").mkdir(parents=True)
    (d / "world" / "marker.txt").write_text("untouched")
    (d / "rollout.json").write_text(json.dumps({
        "passed": passed, "classification": "reasoning", "model": "opus",
        "score": {"checks": checks, "pass_requires_checks": ["G1a", "X1"]}}))
    return d


def test_targets_are_failed_checks_plus_must_haves_of_a_pass():
    record = {"passed": True, "score": {"pass_requires_checks": ["A"], "checks": [
        {"id": "A", "passed": True}, {"id": "B", "passed": True},
        {"id": "C", "passed": False}]}}
    assert [c["id"] for c in ga.targets(record)] == ["C"]
    assert [c["id"] for c in ga.targets(record, include_passes=True)] == ["C", "A"]


def test_votes_become_one_verdict_with_the_evidence_of_the_winner():
    client = mc.ClaudeClient(k=3, runner=scripted(
        reply("grader_wrong"), reply("grader_correct"), reply("grader_wrong")))
    out = ga.judge_check(client, "p")
    assert out["verdict"] == "grader_wrong" and out["agreement"] == 0.667
    assert out["votes"] == ["grader_wrong", "grader_correct", "grader_wrong"]


def test_an_invalid_verdict_is_not_a_vote():
    client = mc.ClaudeClient(k=1, runner=scripted(reply("definitely_the_agent")))
    assert ga.judge_check(client, "p")["verdict"] == "unknown"


def test_episode_verdicts():
    assert ga.episode_verdict(["grader_correct"]) == "agent_failure"
    assert ga.episode_verdict(["grader_wrong", "grader_wrong"]) == "grader_defect"
    assert ga.episode_verdict(["grader_wrong", "grader_correct"]) == "mixed"
    assert ga.episode_verdict(["grader_correct"], grader_passed=True) == "pass_confirmed"
    # a wrong verdict on a check the grader FAILED does not undermine a pass
    assert ga.episode_verdict(["grader_wrong"], grader_passed=True) == "pass_confirmed"
    assert ga.episode_verdict(["grader_wrong"], grader_passed=True,
                              wrong_on_passed_check=True) == "false_pass"


def test_audit_never_touches_the_record_and_flags_a_fixer(tmp_path):
    d = _rollout(tmp_path, [{"id": "X1", "passed": False, "points": -1, "max": 0,
                             "reason": "obs definitions rewritten"}])
    before = (d / "rollout.json").read_bytes()
    seen_cwd = []

    def runner(prompt):
        seen_cwd.append(str(client.cwd))
        return reply("grader_wrong", suspected_bug={"file": "native_stack.py", "line": 226,
                                                     "what": "strips comments", "fix_hint": ""})
    client = mc.ClaudeClient(k=1, runner=runner)
    out = tmp_path / "_audit"
    summary = ga.run_audit(d.parent, out, client=client, include_passes=False,
                           dedupe=False, dry_run=False)
    assert (d / "rollout.json").read_bytes() == before
    assert seen_cwd and str(d) not in seen_cwd[0]           # the judge worked on a copy
    assert not Path(seen_cwd[0]).exists()                    # and the copy is gone
    result = json.loads((out / d.parent.parent.name / "run-1" / "r0.json").read_text())
    assert result["off_the_score"] and result["episode_verdict"] == "grader_defect"
    assert summary["needs_fixer"][0]["check"] == "X1"


def test_dedupe_judges_one_representative_per_signature(tmp_path):
    for name in ("r0", "r1", "r2"):
        _rollout(tmp_path, [{"id": "X1", "passed": False,
                             "reason": f"obs definitions rewritten ({name[-1]}2 lines)"}], name=name)
    calls = []
    client = mc.ClaudeClient(k=1, runner=lambda p: calls.append(1) or reply("grader_wrong"))
    run = tmp_path / "GT006-quality-gate-drops-rows" / "run-1"
    ga.run_audit(run, tmp_path / "_audit", client=client, include_passes=False,
                 dedupe=True, dry_run=False)
    assert len(calls) == 1
    r2 = json.loads((tmp_path / "_audit" / run.parent.name / "run-1" / "r2.json").read_text())
    assert r2["checks"][0]["inherited_from"] == "r0"


def test_dry_run_calls_no_model(tmp_path):
    d = _rollout(tmp_path, [{"id": "E1", "passed": False, "reason": "owner not named"}])
    summary = ga.run_audit(d.parent, tmp_path / "_audit", client=None,
                           include_passes=False, dedupe=False, dry_run=True)
    result = json.loads((tmp_path / "_audit" / d.parent.parent.name / "run-1" / "r0.json").read_text())
    assert "E1" in result["checks"][0]["prompt"] and summary["needs_fixer"] == []


# ---------------------------------------------------------------- shadow judge
def test_shadow_judge_model_backend_votes_per_axis():
    sys.path.insert(0, str(HERE.parents[1] / "GT001-aov-halving" / "tests" / "judge"))
    import shadow_judge
    answers = {"J1": 2, "J2": 1, "J3": "unknown"}
    client = mc.ClaudeClient(k=3, runner=lambda p: "```json\n" + json.dumps(
        {"score": answers[p.split("Axis ")[1][:2]], "rationale": "r"}) + "\n```")
    res = shadow_judge.model_judge(shadow_judge.SEED_PROMPT, {"primary_cause": "x"}, client)
    assert res["J1"]["score"] == 2 and res["J2"]["score"] == 1
    assert res["J3"]["score"] is None                        # unknown is an answer, not a 0


def test_episode_specific_verdicts_are_never_inherited(tmp_path):
    for name in ("r0", "r1"):
        _rollout(tmp_path, [{"id": "D1", "passed": False, "reason": "owner not told"}], name=name)
    calls = []
    client = mc.ClaudeClient(k=1, runner=lambda p: calls.append(1) or reply("environment_fault"))
    run = tmp_path / "GT006-quality-gate-drops-rows" / "run-1"
    ga.run_audit(run, tmp_path / "_audit", client=client, include_passes=False,
                 dedupe=True, dry_run=False)
    assert len(calls) == 2                                   # each episode judged on its own
    r1 = json.loads((tmp_path / "_audit" / run.parent.name / "run-1" / "r1.json").read_text())
    assert "inherited_from" not in r1["checks"][0]


def test_signature_separates_different_endings():
    check = {"id": "D1", "passed": False, "reason": "owner not told"}
    killed = {"agent": {"exit_reason": "budget"}, "classification": "budget"}
    finished = {"agent": {"exit_reason": "completed"}, "classification": "reasoning"}
    assert ga._signature("T", check, killed) != ga._signature("T", check, finished)

