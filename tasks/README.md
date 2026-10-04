# tasks/ — the graded content and the machinery that grades it

Task packages (what the agent is asked, the hidden grader, the oracle, the shortcut battery that
certifies the task) and the task-agnostic harness that runs a blind model against one and reports
a pass rate it can defend.

## What is in this folder

| path | what | guide |
|---|---|---|
| `GT001-…` … `GT009-…/`, `BI001-…/` | one task each: `task.toml`, `instruction.md`, `prompts/`, `fixtures/`, `solution/`, `tests/`, `DESIGN.md`. `tests/` holds `grade.py`, `manifest.yaml`, `probes/`, `run_baselines.py` (prints `TASK VALID` or why not) and `VALIDITY.md`. `task.toml [variants]` are the named context arms of one task, selected with `--variant <name>` | each task's own `README.md` |
| `BI001-revenue-disagrees-across-bi/` | the first task on an environment profile (`[environment] profile = "da/laptop"`, `[environment.plugs] bi = ["powerbi", "tableau", "sigma"]`). Its world is `world.build` of `commerce_analytics` with `bi_faults:` on, the seed recording the same profile (`episode.check_world_profile`); its grader re-renders the vendors' published state, staged after teardown by `harness/bistate.py` (`{bi_state}`), with `world.semantic`; `tests/oracle_rollout.py` runs `solution/solve.sh` as the scripted subject of one real rollout on either topology | `BI001-…/README.md`, `tests/VALIDITY.md` |
| `contracts/` | **layer 1**: seven invariants as reusable clause functions, and `DECISION_TEMPLATE.md`, the control/decision arm structure every task declares against | [`contracts/README.md`](contracts/README.md) |
| `stressors/registry.yaml` | **layer 2**: every planted defect or event that exists as fixture code, which contracts it stresses and which state (tables, checkpoints, window, metrics) it touches. The field reference is the comment block at the top of the file. Read by `harness/coupling.py` | [below](#stressors) |
| `stressors/measured/` | coupling-filter records written by `harness/coupling.py --out` (GT004 plain and clocked, GT009 clocked) | [below](#stressors) |
| `harness/` | rollouts, containment, grading, blindness, the surface gate, reports; `coupling.py` (**layer 3**), `ledger.py` (**layer 4** reader), `native_stack.py` (`--driver native`) | [`harness/README.md`](harness/README.md) |
| `harness/judge/` | judge tools that never touch the score (clarity gate, grader audit) | [`harness/judge/README.md`](harness/judge/README.md) |
| `harness/redteam/` | attacks on our own detectors and graders: scripted subject, adversarial suffixes, scripted cheats, exploit agent | [`harness/redteam/README.md`](harness/redteam/README.md) |
| `harness/evidence/` | JSON evidence written by the operator checks (`container_check.py`, `dual_container_check.py`, `reference_copy_check.py`) | [`harness/README.md`](harness/README.md) |

## How to run

From the repo root:

```bash
PY=gateway/.venv/bin/python
$PY tasks/harness/run_rollouts.py --task GT004-doubling-window --k 5        # K rollouts + report
$PY tasks/harness/validity.py --task GT004-doubling-window --lab-card       # static validity + lab card
$PY tasks/GT004-doubling-window/tests/run_baselines.py --variant default    # is the task valid?
(cd tasks/harness && ../../$PY selftest.py)                                 # detector/classifier checks
$PY -m pytest -q tasks/contracts tasks/harness/judge tasks/harness/redteam
```

The default is `--driver native --topology container`; see
[`harness/README.md#drivers-and-how-much-they-are-worth`](harness/README.md#drivers-and-how-much-they-are-worth).

## Do not break these

- **Agent-visible files are measurement inputs, not docs.** `instruction.md`, `prompts/**` and
  `fixtures/**` of every task are what the agent sees; editing them changes the measurement and
  breaks fixture digests. The same holds for `harness/redteam/*.md` and
  `harness/redteam/exploit_agent/prompt.md`, which code reads verbatim.
- **Some task docs are read by code.** `harness/lab_card.py` reads each task's `DESIGN.md` and
  `tests/VALIDITY.md`; `task.toml` names `SEED_SPEC.md` as the seed contract, and the file name
  is a blindness marker. Keep them where they are, under their names.
- **`contracts/DECISION_TEMPLATE.md` is cited by section number** (`§5`, `§9`) from task files,
  graders and fixtures. Do not rename it or renumber its sections.
- **Nothing under `contracts/` imports from a task, `world/` or `gateway/`**
  (`contracts/tests/test_registry.py` checks it).

## Stressors

A stressor is a way to stress a contract, not a task: one planted defect or event can serve
several contracts. `registry.yaml` lists only stressors that exist as fixture code, each with
`implemented_in` (the `file:symbol` that plants it), `kind` (`defect | event | context`),
`stresses` (contract names from a closed set), `state` (direct writes only; propagation lives in
`derived_tables`), `repair` (the oracle step that repairs it alone), `stageable_subset` and
`verified`. The coupling filter (`harness/coupling.py`, described in
[`harness/README.md#the-coupling-filter--layer-3-couplingpy`](harness/README.md#the-coupling-filter--layer-3-couplingpy))
reads it to decide which pairs to measure and writes its results to `measured/`:

```bash
$PY tasks/harness/coupling.py --task GT009-settlement-cutoff --pairs all --clock --out tasks/stressors/measured
```

## History

Request logs, round history and measured findings behind the harness live in
`docs/implementation_plan/harness/` and `docs/implementation_plan/hardness-round/`. The
scoring design is `docs/research/Scoring_Redesign.md`.
