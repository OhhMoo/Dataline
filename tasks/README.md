# tasks/ — the graded content and the machinery that grades it

**Purpose.** Task packages (what the agent is asked, the hidden grader, the oracle,
the shortcut battery that certifies the task) and the task-agnostic harness that runs
a blind model against one and reports a pass rate it can defend.

```
GT001 … GT009/          one task each: task.toml, prompts/, fixtures/, solution/, tests/
                        tests/ = grade.py, manifest.yaml, probes/, run_baselines.py (prints TASK VALID or why not)
                        task.toml [variants] = named context arms of ONE task, `--variant <name>`
contracts/              LAYER 1 — seven invariants as reusable clause functions (contracts/README.md);
                        DECISION_TEMPLATE.md, the control/decision arm structure every task declares
stressors/registry.yaml LAYER 2 — every planted defect/event, which contracts it stresses, what state it touches
stressors/measured/     coupling-filter records (harness/coupling.py --out)
harness/                rollouts, grading, blindness, surface gate, report;
                        coupling.py (LAYER 3), ledger.py (LAYER 4 reader), native_stack.py (--driver native)
```

Run one task (from the repo root):

```bash
gateway/.venv/bin/python tasks/harness/run_rollouts.py --task GT004-doubling-window --k 5
```

The default is `--driver native --topology container`. Read next: `harness/README.md`,
`contracts/README.md`, `contracts/DECISION_TEMPLATE.md`. Request logs and round history
for this folder live in `docs/implementation_plan/harness/`.
