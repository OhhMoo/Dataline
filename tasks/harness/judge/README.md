# `tasks/harness/judge` — judge tools, all off the score

## What is in this folder

| file | what |
|---|---|
| `__init__.py` | re-exports `consensus`, `judge_brief`, `scan_brief` from `clarity_gate` |
| `clarity_gate.py` | reject-only clarity gate for a task brief: `reject` or `hold`, never `accept` |
| `model_client.py` | the model behind any judge: `claude --print`, k samples, strict majority with `unknown`, model family recorded on every verdict |
| `grader_audit.py` | audits the GRADER's verdicts check by check against the world; a `grader_wrong` sets `needs_fixer` and exits 2, never awards a point |
| `test_clarity_gate.py`, `test_grader_audit.py` | tests; no model is called |

## How to run

```bash
PY=gateway/.venv/bin/python
$PY tasks/harness/judge/clarity_gate.py --brief tasks/<T>/instruction.md   # deterministic scan, no model: exit 0 hold, 2 reject
$PY tasks/harness/judge/grader_audit.py --run <run-dir> --dry-run   # prompts only
$PY tasks/harness/judge/grader_audit.py --run <run-dir> --k 3 --dedupe --include-passes
$PY -m pytest tasks/harness/judge tasks/harness/test_judge_isolation.py -q
```

## Isolation

Nothing here is a pass@1 signal. `pass@1` stays in `outcome.py` and the
training scalar in `reward.py`; neither they nor any `tests/grade.py` may import
this package, and `test_judge_isolation.py` enforces it. `grader_audit.py` works
on a copy of the rollout directory and writes beside the run, never into it.

Two patterns shape the tools:

- **Reject-only clarity** (SWE-Bench++, arXiv 2512.17419): a model scores brief
  clarity and a low score drops the task. Its reviewers reach precision
  0.93–0.95 against a human near 0.96 with uneven recall, so a model verdict
  here may reject a brief but never accept one. SWE-bench Verified's 0–3 issue
  rubric (any severe flag rejects) is the same idea.
- **Exploit agents** (Terminal-Bench, arXiv 2601.11868, and its
  `rubrics/task-implementation.toml`): a task is attacked before release, and a
  pass without a repair is a grader defect to fix, not a point. The clarity
  gate's deterministic scan takes Terminal-Bench's checks: behaviour the brief
  describes is tested, and no solution or expected answer sits in text the
  agent reads.

The design memo is [`docs/research/LLM_Judge_Research.md`](../../../docs/research/LLM_Judge_Research.md);
the literature notes behind it are in [`docs/research/llm-judge/`](../../../docs/research/llm-judge/).
