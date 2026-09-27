#!/usr/bin/env python3
"""Grader audit: a judge that checks the GRADER's verdicts, off the score.

    python tasks/harness/judge/grader_audit.py --run ~/alkera-rollouts/GT006-quality-gate-drops-rows/<run>
    python tasks/harness/judge/grader_audit.py --run <run-dir> --k 3 --dedupe --include-passes
    python tasks/harness/judge/grader_audit.py --run <run-dir> --dry-run     # prompts only

This is job 2 of docs/research/LLM_Judge_Research.md ("attack the grader"), in its audit
form. The exploit loop asks "can a pass be had without the repair?"; this asks
"was each verdict the grader actually gave true of the world?". Both treat what
they find the same way: a grader bug is a defect to fix and a baseline to add,
never a point to award.

Isolation, which test_judge_isolation.py holds this module to:
  * Nothing here writes rollout.json, the report or the grade. Output goes to
    `<out>/<task>/<run>/<rollout>.json`, beside the run, not in it.
  * The judge works on a COPY of the rollout directory, so its tools cannot
    change the record it is auditing.
  * A `grader_wrong` verdict does not change pass@1. It sets `needs_fixer` and
    a baseline stub, exactly as exploit_loop.py does for an exploit pass, and the
    script exits 2 so a batch cannot end silently green.

Design rules taken from the memo (section "What a judge is for"):
  * One criterion per call: each failed check is its own judgement.
  * k samples with a strict majority, `unknown` otherwise.
  * Tools on the world, not transcript-only, and never the agent's own claim:
    the prompt tells the judge to establish the truth from the warehouse, the
    workflow state and the reference in `grading/`.
  * Provenance on every verdict, including whether the judge shares the
    subject's model family.

Measured before this module existed (2026-09-26, run
opus-decision-k1-20260926T054914Z, one subagent per task under the same
protocol): 5 of 5 failed decision-arm episodes carried at least one
`grader_wrong` check (GT003 F1 negation window, GT006 X1 and GT007/GT008 D2 obs
digest vs. the native sanitiser, GT002 K1 double count, GT006 E1 owner token,
GT008 H1 cascading from D2). That is the case for running it on every batch.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

try:  # package import (tests) and script import (CLI) both work
    from .model_client import ClaudeClient, extract_json, majority
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from model_client import ClaudeClient, extract_json, majority

REPO = Path(__file__).resolve().parents[3]
VERDICTS = ("grader_correct", "grader_wrong", "task_ambiguous",
            "environment_fault", "unknown")
#: What each verdict means for the episode, most serious first when combined.
_EPISODE = {"grader_wrong": "grader_defect", "task_ambiguous": "task_defect",
            "environment_fault": "environment_defect",
            "grader_correct": "agent_failure", "unknown": "unknown"}
#: Copied for the judge; big derived trees it never needs are skipped.
_SKIP_COPY = {"__pycache__", ".git", "target", "dbt_packages", "logs"}

PROTOCOL = """\
You are auditing a deterministic GRADER in an AI-agent benchmark, not grading the agent.
Your verdict never changes the score; it tells a researcher whether this failure belongs to
the agent or to a defect in the grader, the task or the environment.

Work in the current directory, a private copy of one rollout:
  prompt.txt           what the agent was told
  transcript.jsonl     the whole episode (tool calls and results)
  rollout.json         the record; score.checks[] holds each check's verdict and detail
  world/               the world the agent left: world/workspace (its repo),
                       world/var/warehouse/*.duckdb, world/var/workflow (tracker/chat/wiki
                       state, where its posts land), world/var/ledger (receipts)
  grading/             hidden from the agent: expected.duckdb (reference),
                       pre_repair_world/, manifest.yaml, REFERENCE_ANSWER_*.txt,
                       decision.json / variant.json (who owns what, what needs sign-off)
  audit/actions.jsonl  every action the environment observed
  before_episode.json  the world as the agent received it (after harness staging)
The task's grader and definition are at {task_dir} (tests/grade.py, tests/manifest.yaml,
task.toml, DESIGN.md). Read them; do not modify anything outside this directory.
Python with duckdb: {python}. Open .duckdb files with read_only=True.

Never trust the transcript's or the agent's report's claims: establish the truth in the
world (query the warehouse, diff against grading/, read the workflow JSON). Also check
whether the harness itself changed a file before the episode (compare before_episode.json
and rollout.json native_world_sanitised with grading/pre_repair_world).

The ONE check to judge: {check_id}
The grader said: passed={passed}, points={points}/{max_points}
Grader detail: {detail}
Episode outcome class: {outcome}

Decide exactly one verdict for this check:
  grader_correct     the agent really did / failed to do what the check says (agent's fault)
  grader_wrong       the world shows the check's verdict is false (a grader/harness defect)
  task_ambiguous     the check is internally right but nothing the agent could read made
                     the requirement knowable
  environment_fault  infrastructure blocked the agent (a service refused, data missing)
  unknown            you could not establish it; prefer this to a guess

End your reply with one fenced JSON block, exactly:
```json
{{"check": "{check_id}", "verdict": "...", "confidence": "high|medium|low",
  "evidence": ["file + query/result, or transcript event index + quote"],
  "explanation": "2-4 sentences",
  "suspected_bug": {{"file": "", "line": 0, "what": "", "fix_hint": ""}}}}
```
"""


def _checks(rollout: dict) -> list[dict]:
    score = rollout.get("score") or {}
    return [c for c in (score.get("checks") or []) if isinstance(c, dict)]


def _detail(check: dict) -> str:
    for key in ("reason", "detail", "why", "summary", "note"):
        value = check.get(key)
        if value not in (None, "", {}):
            return json.dumps(value, default=str)[:1500]
    rest = {k: v for k, v in check.items() if k not in ("id", "passed", "points", "max")}
    return json.dumps(rest, default=str)[:1500]


#: Verdicts about the GRADER are systemic: the same check failing the same way
#: is the same defect, so one representative can speak for the rest. Verdicts
#: about the EPISODE are not - measured 2026-09-27: GT005 r6 inherited r5's
#: `environment_fault` (a budget kill) because both failed D1 with the same
#: wording, while r6 had simply never told the owner.
INHERITABLE = frozenset({"grader_wrong", "grader_correct"})


def _signature(task: str, check: dict, rollout: dict | None = None) -> str:
    """Same task, same check, same reason with the numbers blanked, and the same
    way the episode ended: one representative can speak for the rest, but only
    for a verdict in INHERITABLE (see audit_rollout)."""
    text = re.sub(r"\d[\d,._]*", "#", _detail(check).lower())
    agent = (rollout or {}).get("agent") or {}
    ended = f"{agent.get('exit_reason')}|{(rollout or {}).get('classification')}"
    return f"{task}|{check.get('id')}|{ended}|{text[:240]}"


def targets(rollout: dict, *, include_passes: bool = False) -> list[dict]:
    """The checks to judge: every failed one; with include_passes, every
    must-have that passed too (a pass the world does not support is a bug)."""
    out = [c for c in _checks(rollout) if not c.get("passed")]
    if include_passes and rollout.get("passed"):
        required = set((rollout.get("score") or {}).get("pass_requires_checks") or [])
        out += [c for c in _checks(rollout)
                if c.get("passed") and (not required or c.get("id") in required)]
    return out


def prompt_for(rollout: dict, check: dict, *, task_dir: Path, python: str) -> str:
    return PROTOCOL.format(
        task_dir=task_dir, python=python, check_id=check.get("id"),
        passed=check.get("passed"), points=check.get("points"),
        max_points=check.get("max"), detail=_detail(check),
        outcome=rollout.get("classification") or rollout.get("why") or "?")


def judge_check(client: ClaudeClient, prompt: str) -> dict:
    """k samples -> one verdict, with the votes kept."""
    votes, parsed = [], []
    for text in client.samples(prompt):
        data = extract_json(text) or {}
        verdict = data.get("verdict") if data.get("verdict") in VERDICTS else ""
        votes.append(verdict)
        if verdict:
            parsed.append(data)
    verdict, agreement = majority(votes)
    chosen = next((p for p in parsed if p.get("verdict") == verdict), {})
    return {"verdict": verdict, "agreement": agreement, "votes": votes,
            "confidence": chosen.get("confidence"),
            "evidence": chosen.get("evidence") or [],
            "explanation": chosen.get("explanation") or "",
            "suspected_bug": chosen.get("suspected_bug") or {}}


def episode_verdict(check_verdicts: list[str], *, grader_passed: bool = False,
                    wrong_on_passed_check: bool = False) -> str:
    """For a failed episode, whose failure it is. For a passed one:
    `false_pass` when a check the grader PASSED is wrong (a pass the world does
    not support), else `pass_confirmed` - a grader mistake on a check it failed
    cannot undermine a pass, and is still listed in needs_fixer."""
    if grader_passed:
        return "false_pass" if wrong_on_passed_check else "pass_confirmed"
    kinds = {_EPISODE.get(v, "unknown") for v in check_verdicts}
    if not kinds:
        return "no_failures"
    serious = kinds - {"unknown"}
    if len(serious) > 1:
        return "mixed"
    return next(iter(serious)) if serious else "unknown"


def baseline_stub(task: str, rollout_id: str, check: dict, result: dict) -> dict:
    """Shaped like exploit_loop.promote_baseline_stub: the ladder row a fix must
    add so the defect cannot reopen silently."""
    return {"task": task, "baseline_id": f"audit_{str(check.get('id')).lower()}_{rollout_id}",
            "declared_class": "see audit", "check": check.get("id"),
            "why": result.get("explanation", "")[:400],
            "suspected_bug": result.get("suspected_bug") or {}}


def _copy_rollout(src: Path) -> Path:
    scratch = Path(tempfile.mkdtemp(prefix="grader-audit-"))
    dst = scratch / src.name
    shutil.copytree(src, dst, symlinks=True,
                    ignore=lambda d, names: [n for n in names if n in _SKIP_COPY])
    return dst


def audit_rollout(rollout_dir: Path, *, client: ClaudeClient | None, task: str,
                  run: str, include_passes: bool = False, dry_run: bool = False,
                  seen: dict | None = None) -> dict:
    record = json.loads((rollout_dir / "rollout.json").read_text(encoding="utf-8"))
    task_dir = REPO / "tasks" / task
    python = str(REPO / "gateway" / ".venv" / "bin" / "python")
    out = {"task": task, "run": run, "rollout": rollout_dir.name,
           "outcome_class": record.get("classification"),
           "grader_passed": record.get("passed"), "checks": [],
           "provenance": client.provenance(record.get("model")) if client else {},
           "audited_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "off_the_score": True}
    work = None if dry_run else _copy_rollout(rollout_dir)
    try:
        for check in targets(record, include_passes=include_passes):
            sig = _signature(task, check, record)
            prompt = prompt_for(record, check, task_dir=task_dir, python=python)
            if dry_run:
                out["checks"].append({"id": check.get("id"), "prompt": prompt})
                continue
            if (seen is not None and sig in seen
                    and seen[sig].get("verdict") in INHERITABLE):
                result = {**seen[sig], "inherited_from": seen[sig]["rollout"]}
            else:
                client.cwd = work
                result = judge_check(client, prompt)
                result["rollout"] = rollout_dir.name
                if seen is not None:
                    seen[sig] = result
            out["checks"].append({"id": check.get("id"),
                                  "grader_said": f"passed={check.get('passed')}: "
                                                 f"{_detail(check)[:300]}",
                                  **{k: v for k, v in result.items() if k != "rollout"}})
    finally:
        if work is not None:
            shutil.rmtree(work.parent, ignore_errors=True)
    verdicts = [c.get("verdict") for c in out["checks"] if c.get("verdict")]
    out["episode_verdict"] = episode_verdict(
        verdicts, grader_passed=bool(record.get("passed")),
        wrong_on_passed_check=any(c.get("verdict") == "grader_wrong"
                                  and c.get("grader_said", "").startswith("passed=True")
                                  for c in out["checks"]))
    out["needs_fixer"] = [baseline_stub(task, rollout_dir.name, c, c)
                          for c in out["checks"] if c.get("verdict") == "grader_wrong"]
    return out


def run_audit(run_dir: Path, out_root: Path, *, client: ClaudeClient | None,
              include_passes: bool, dedupe: bool, dry_run: bool) -> dict:
    task, run = run_dir.parent.name, run_dir.name
    seen: dict | None = {} if dedupe else None
    rows = []
    for rollout_dir in sorted(p for p in run_dir.glob("r*") if (p / "rollout.json").is_file()):
        result = audit_rollout(rollout_dir, client=client, task=task, run=run,
                               include_passes=include_passes, dry_run=dry_run, seen=seen)
        dest = out_root / task / run / f"{rollout_dir.name}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
        rows.append(result)
    summary = {"task": task, "run": run, "rollouts": len(rows),
               "grader_passed": sum(1 for r in rows if r.get("grader_passed")),
               "episode_verdicts": {v: sum(1 for r in rows if r.get("episode_verdict") == v)
                                    for v in sorted({r.get("episode_verdict") for r in rows})},
               "needs_fixer": [s for r in rows for s in r.get("needs_fixer", [])],
               "off_the_score": True}
    (out_root / task / run).mkdir(parents=True, exist_ok=True)
    (out_root / task / run / "summary.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--run", required=True, action="append",
                    help="a run directory <out>/<task>/<run-id>; repeatable")
    ap.add_argument("--out", default=None,
                    help="audit root (default: <run's out>/_audit)")
    ap.add_argument("--model", default="opus")
    ap.add_argument("--k", type=int, default=3, help="samples per check (memo: 3-5)")
    ap.add_argument("--include-passes", action="store_true",
                    help="also audit the must-have checks of passing rollouts")
    ap.add_argument("--dedupe", action="store_true",
                    help="judge one rollout per (task, check, reason) signature")
    ap.add_argument("--dry-run", action="store_true", help="write prompts, call no model")
    a = ap.parse_args(argv)

    client = None if a.dry_run else ClaudeClient(model=a.model, k=a.k, tools=True)
    exit_code = 0
    for run in a.run:
        run_dir = Path(run).expanduser().resolve()
        out_root = Path(a.out).expanduser() if a.out else run_dir.parents[1] / "_audit"
        summary = run_audit(run_dir, out_root, client=client,
                            include_passes=a.include_passes, dedupe=a.dedupe,
                            dry_run=a.dry_run)
        print(json.dumps(summary, indent=2, default=str))
        if summary["needs_fixer"]:
            exit_code = 2
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
