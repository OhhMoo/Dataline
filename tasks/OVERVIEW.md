# tasks/ — the graded content and the machinery that grades it

**Purpose.** Task packages (what the agent is asked, the hidden grader, the oracle,
the shortcut battery that certifies the task) and the task-agnostic harness that runs
a blind model against one and reports a pass rate it can defend.

```
GT001 … GT008/          one task each: task.toml, prompts/, fixtures/, solution/, tests/
                        tests/ = grade.py, manifest.yaml, probes/, run_baselines.py (prints TASK VALID or why not)
GT004a-e, GT006c-d      ablation and 2x2 arms of GT004 / GT006 (context-only variants)
contracts/              LAYER 1 — six invariants as reusable clause functions (CONTRACTS.md)
stressors/registry.yaml LAYER 2 — every planted defect/event, which contracts it stresses, what state it touches
harness/                rollouts, grading, blindness, surface gate, Pass@1/Pass^k report;
                        coupling.py (LAYER 3), ledger.py (LAYER 4 reader), native_stack.py (--driver native)
```

Run one task: `gateway/.venv/Scripts/python.exe tasks/harness/run_rollouts.py --task GT004-doubling-window --k 5`
(add `--driver native` for no-tool-list mode). Measured results: `agent.md` §8.10–8.11.
Read next: `harness/README.md`, `harness/NATIVE_DRIVER.md`, `contracts/CONTRACTS.md`, `harness/COUPLING.md`.
