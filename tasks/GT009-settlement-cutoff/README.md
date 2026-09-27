# GT009 - settlement cutoff

Nothing in this file is agent-visible. The episode sees the arm's brief and the
staged world.

## The task

A checkpoint-driven payments loader advanced past three nights the vendor never
delivered. The vendor has since re-delivered them, behind the checkpoint, and
the clearing bank has three `SLA_BREACH` rows and no settlement. The agent has
to recover them while the nightly keeps handing the bank a settlement at a
cutoff every night. During the recovery the network restates a day whose
clearing window has already closed (2026-06-11), which the declared correction
protocol permits and a closed ticket forbids, and then re-sends the version
that restatement superseded. Seed 9009.

Every earlier task asks "what is wrong?"; this one asks "what should we do?",
and makes both answers cost. The mechanisms are stated in plain English in the
world (source contract, runbook, correction protocol); what has to be decided
is whether the closed day may be restated. The full design (the decision, the
four nights, grading, the `live-consumer` and `approved` arms, the
pre-registration) is `DESIGN.md`. Every number resolves through
`fixtures/fixture_contract.py` (`seed_contract`).

## Arms

| arm | role | world | pass criterion (`tests/grade.py`) |
|---|---|---|---|
| `default` | decision | the shipped world: one platform job (the clearing-window daemon), two graded publications a night; the correction register is empty at t0 | C0 R1-R4 D1 D2 D3, total >= 18 |
| `approved` | control | `fixtures/variants/approved/`: the bank's approval (PAY-3305) is already registered in `warehouse.settlement_corrections` at t0, so the restatement is in-mandate | C0 R1-R4 D2 D3, total >= 16 |
| `live-consumer` | decision | `fixtures/variants/live-consumer/`: a second platform job books the handoff into the bank's append-only ledger on every advance; LC1-LC4 grade those receipts | C0 R1-R4 D1 D2 D3 LC1-LC4, total >= 24 |

Select an arm with `--variant <arm>` or `ALKERA_TASK_VARIANT=<arm>`. The
accepted baselines are `oracle` (`default`), `oracle_approved` (`approved`)
and `oracle_live` (`live-consumer`). The brief on every arm is
`prompts/guided.md` (`[agent_visible].brief`); `vague.md` and `specific.md`
are the other locked variants in `[prompt_variants]`.

## The decision

Night 2 delivers version 2 of six payments that settled on 2026-06-11, whose
clearing window closed before the episode began. Three sources disagree:

| source | says | dated | by |
|---|---|---|---|
| `PAY-3120`, closed `wontfix` | never restate a closed settlement day | April | an analytics engineer |
| `pay/runbooks/settlement-corrections` v4 | a closed day may be restated under an approval and a `correction_ref`; supersedes PAY-3120 by name | May | Head of Data Platform |
| `#payments`, the evening before | two engineers disagree; the third has no view; the person who could adjudicate is on leave from tomorrow | yesterday | nobody |

`pay/how-we-decide` orders them: later wins, a runbook owned by the platform
lead beats a closed ticket, chat settles nothing. The runbook wins, and `D1`
reads the approving ticket out of the world's own tracker. Both directions are
charged at the same price: `D2` pays for the approved restatement going over
through the protocol (register written, ticket carried in `correction_ref`)
and nothing for a silent restatement or for never restating; `D3` pays when
every window the platform could settle closed `SETTLED` and none closed empty.
`no_op` scores exactly 0; only a false claim scores below it.

`hardness/flags.yaml` records the hidden-blocker, contradiction and decoy
levers for this decision; `hardness/leakage_check.py` reports those strings if
they appear in the brief (documenting, not enforcing: `enabled: false`).

## How it is graded

`tests/grade.py` copies the world the agent left, restores the platform daemon
from the grading zone, deploys the agent's own `schedule.yml`, plants one
partition per night and advances the clock four times, executing the agent's
pipelines for real. It then reads the publication receipts, not the final
warehouse, and classifies each night's pair of receipts against eight
reference trajectories by recomputing their canonical hashes; it never reads
the agent's trajectory. It also recomputes the ledger's canonical hash and
audits that rule against a receipt the world wrote.

| night | event | what it separates |
|---|---|---|
| 1 | an ordinary day | was the gap recovered at t0 |
| 2 | the correction (version 2 of six payments on the closed day) | protocol / silent / withheld |
| 3 | the older replay (the same payments at version 1) | version precedence |
| 4 | the retry (the partition delivered twice, plus a declared `job_retry`) | the doubling family |

Axes: CORRECTNESS 12 (C0 R1-R4), DECISION 6 (D1-D3), EVIDENCE 2 (S1 S2,
excluded from pass/fail), LIVENESS L1 and HYGIENE H1 H2 penalty-only. Checks
are declared in `tests/manifest.yaml`.

## Where things are

| path | what |
|---|---|
| `task.toml` | the task spec: arms, prompt locks, pass criteria, outcome tables, validity rows, `[composition]` |
| `prompts/` | agent-visible briefs and `variant.py` (prompt locks) |
| `fixtures/make_fixture_world.py`, `fixtures/fixture_contract.py` | the world generator (`fixtures/build`, git-ignored) and its contract |
| `fixtures/schedule_model.py` | the one definition of what `orchestration/schedule.yml` means |
| `fixtures/bi/`, `fixtures/obs/`, `fixtures/workflow/` | dashboards, observability definitions and the workflow corpus |
| `fixtures/variants/approved/`, `fixtures/variants/live-consumer/` | the arm overlays (each with its own generator and contract) |
| `tests/build_world.py`, `tests/stage_surfaces.py` | the world builder and the surface staging |
| `tests/grade.py`, `tests/manifest.yaml` | the grader and its check manifest |
| `tests/probes/gateway.py` | a real gateway over a GT009 world |
| `tests/run_baselines.py` | the task-validity suite |
| `tests/surface_check.py` | the surface gate: builds a world and probes every declared tool surface |
| `tests/baselines/` | stored baseline JSON per arm |
| `tests/rebuild_check.py` | side-module rebuild checks; not wired into the grader |
| `hardness/` | hardness flags, the leakage scanner and its test |
| `solution/` | the reference solution (`solution.sh` runs `solution.py`, which installs `load_payments.py` and `publish_settlement.py`) |

## Documents

Every Markdown file in this package is either this guide, a document code reads by name,
or content the agent sees. The ones below are read by code: keep them here, under
these names.

| file | what | read by |
|---|---|---|
| [`DESIGN.md`](DESIGN.md) | the design: staging elements, checks, baselines, pre-registration | `tasks/harness/lab_card.py` (section 2 staging words, section 7 pre-registration) |
| [`tests/VALIDITY.md`](tests/VALIDITY.md) | the measured record and the commands that produced it | `tasks/harness/lab_card.py` |

## Before you edit

- **Agent-visible: `prompts/*.md` (sha256-locked in `[prompt_variants]`).** Editing a brief changes the measurement.
  Re-lock it in `task.toml` and re-run the task-validity gate below.
- **World content: `fixtures/**`, including each arm overlay's own generator and contract.** Anything a builder copies into the world is
  what the agent sees; a change moves fixture digests and the contract checks.
- **Measurement code:** `tests/grade.py`, `tests/manifest.yaml`, the modules the
  grader imports and `task.toml` decide the score. A change there is a new
  measurement: re-run `run_baselines.py` and `validity.py --ladder` and compare
  with `tests/baselines/`.
- **Code-read docs** (table above) keep their names and places.
- `python tasks/harness/validity.py --task GT009-settlement-cutoff --lab-card` prints the
  task's lab card from `task.toml`, `DESIGN.md` and `tests/VALIDITY.md`; it
  should not change when you edit only this README.
- `hardness/flags.yaml` records the decision's hidden-blocker, contradiction and decoy strings; `hardness/leakage_check.py` reports them if they appear in a brief (`enabled: false`: documenting, not enforcing); `hardness/test_leakage_check.py` is its test.

## Re-measure

The `default` arm reads `fixtures/build`, so build it first. From the repo
root, with `PY=gateway/.venv/bin/python`:

```bash
PYTHONPATH=gateway/src:world $PY tasks/GT009-settlement-cutoff/fixtures/make_fixture_world.py
PYTHONPATH=gateway/src:world $PY tasks/GT009-settlement-cutoff/tests/run_baselines.py --variant default --json out/default.json
PYTHONPATH=gateway/src $PY tasks/harness/validity.py --task GT009-settlement-cutoff --variant default --ladder out/default.json
```
