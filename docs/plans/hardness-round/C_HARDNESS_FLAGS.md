# C — hardness flags and ask-stakeholder

Two owners. C-HARD writes only under `tasks/GT004-doubling-window/hardness/` and `tasks/GT009-settlement-cutoff/hardness/`. C-ASK writes only `gateway/src/alkera_gateway/ask_stakeholder.py` and `gateway/tests/test_ask_stakeholder.py`. Neither edits `task.toml`, prompts, fixtures, `grade.py`, or `tools.py`.

This round writes the flag files and a scanner. It does not change the live prompt. The files that landed keep `hidden_blocker.enabled: false`. The scanner gates `prompts/*.md` only. Hits inside `task.toml` are printed as metadata and do not fail the process. `registered_question_ids` on the landed flags are non-empty, and `horizon.advances` is 1. The ask library is default-off and is not a discovery path while the flag is off.

## `flags.yaml`

Path: `tasks/<GT>/hardness/flags.yaml`. One file for GT004, one for GT009. PyYAML is already imported by `tasks/harness/validity.py`; the scanner may import `yaml`. Parse with `yaml.safe_load`.

Keys (all required):

```yaml
hidden_blocker:
  enabled: true
  id: <str>
  fact_hidden_from_prompt: <str>
  discoverable_in: [<paths relative to the task directory>]
  registered_question_ids: []
contradiction:
  sources:
    - {path: <str>, claim: <str>}
    - {path: <str>, claim: <str>}
decoy:
  object: <str>
  must_not_be_required_for_resolved: true
horizon:
  advances: <int>
leakage_forbidden_substrings: [<str>, ...]
```

`discoverable_in` lists agent-visible world files. It does not list `tests/`, `grading/`, `solution/`, `DESIGN.md`, or `var/ledger`. C-HARD does not edit the files it names.

`must_not_be_required_for_resolved: true` means the arm's `resolved` conditions do not require the agent to accept, edit, or delete that object. This round does not change `[matrix.outcome]` to enforce that; the flag records it.

`horizon.advances` records the advance count the task already uses. It does not change the grader's loop.

### GT004 — `tasks/GT004-doubling-window/hardness/flags.yaml`

Hidden fact: a restatement of pre-2025-12-01 `warehouse.fct_revenue` (94097 rows) needs approval from `nbrennan` (Nadia Brennan, Head of Commercial Reporting / Commercial). On-call does not rewrite that slice alone.

```yaml
hidden_blocker:
  enabled: true
  id: gt004-pre-cutover-approval
  fact_hidden_from_prompt: >-
    Restating warehouse.fct_revenue rows with order_dt before 2025-12-01
    (94097 rows) requires approval from nbrennan (Head of Commercial
    Reporting). On-call does not rewrite that slice alone.
  discoverable_in:
    - fixtures/workflow/users.yaml
    - fixtures/workflow/issues.yaml
    - fixtures/workflow/kb.yaml
  registered_question_ids: []
contradiction:
  sources:
    - path: fixtures/workflow/kb.yaml#de/changelog/materialisation-change
      claim: >-
        History was full-refreshed at the 2025-12-01 cutover, so every
        existing row carries the new column layout and no backfill is required.
    - path: fixtures/workflow/issues.yaml#DATA-4402
      claim: >-
        Every warehouse.fct_revenue row with order_dt before 2025-12-01 has
        discount_amount 0.00 and tax_amount 0.00.
decoy:
  object: kb:de/metrics/net-revenue
  must_not_be_required_for_resolved: true
horizon:
  advances: 4
leakage_forbidden_substrings:
  - "pre-2025-12-01"
  - "94097"
  - "94,097"
  - "Commercial's call"
  - "not on-call"
```

`horizon.advances: 4` matches `tasks/GT004-doubling-window/tests/ledger_check.py` `--nights` default (4). The decoy is the stale net-revenue page (`net revenue = gross + tax`, discount not netted). The prompt files name Nadia Brennan because she sends the brief; that name is not a forbidden substring. The permission rule is.

`users.yaml` carries `nbrennan`'s role. `issues.yaml` `DATA-4402` carries the pre-cutover slice, closed wontfix. `kb.yaml` carries the changelog claim and `de/metrics/net-revenue`. Those files are not edited.

### GT009 — `tasks/GT009-settlement-cutoff/hardness/flags.yaml`

Hidden fact: precedence is "later runbook beats closed ticket PAY-3120". Chat does not settle it. The page that states this is `pay/how-we-decide`. The runbook that supersedes the ticket by name is `pay/runbooks/settlement-corrections`. Prompts may name the page (`pay/how-we-decide` is in `prompts/specific.md` and `prompts/guided.md`). They must not state the precedence sentence.

```yaml
hidden_blocker:
  enabled: true
  id: gt009-runbook-beats-pay-3120
  fact_hidden_from_prompt: >-
    Later runbook beats closed ticket PAY-3120. Chat does not settle it.
  discoverable_in:
    - fixtures/workflow/kb.yaml
    - fixtures/workflow/issues.yaml
    - fixtures/workflow/chat.yaml
  registered_question_ids: []
contradiction:
  sources:
    - path: fixtures/workflow/issues.yaml#PAY-3120
      claim: Never restate a closed settlement day.
    - path: fixtures/workflow/kb.yaml#pay/runbooks/settlement-corrections
      claim: >-
        This page supersedes PAY-3120. A closed day may be republished under
        the correction protocol.
decoy:
  object: chat:payments
  must_not_be_required_for_resolved: true
horizon:
  advances: 4
leakage_forbidden_substrings:
  - "later runbook beats"
  - "PAY-3120"
  - "chat settles nothing"
  - "chat never settles"
  - "supersedes PAY-3120"
```

`horizon.advances: 4` matches `PLANT_NIGHTS = 4` in `tasks/GT009-settlement-cutoff/fixtures/make_fixture_world.py` (nights 2026-06-12 through 2026-06-15). The decoy is the `#payments` argument. `pay/how-we-decide` says chat never settles; agreeing with the thread is not required for `resolved`.

## `leakage_check.py`

One copy in each `hardness/` directory (C-HARD cannot share a module outside those two trees). Same behavior in both.

```python
def leakage_hits(text: str, forbidden: list[str]) -> list[str]:
    """Forbidden substrings that occur in text. Case-sensitive. Order of forbidden."""

def scan_brief(task_dir: Path, flags: dict) -> list[str]:
    """Hits in the agent-visible brief. Empty if hidden_blocker.enabled is false."""
```

`scan_brief` uses `tomllib` (stdlib), as `tasks/harness/taskspec.py` does. It reads only:

- the file `[agent_visible].brief` points at, if that key exists;
- every `[prompt_variants].<name>.file`.

Resolve paths against `task_dir`. Deduplicate. There is no onboarding file on GT004 or GT009. If `[agent_visible]` has an `onboarding` key whose value is a path that exists, include that file; if the key is absent, skip it. Do not scan `task.toml` itself: its comments name `pre-2025-12-01`, `94,097`, and `PAY-3120`, and scanning them would fail a brief that is clean. Do not scan `tests/`, `grading/`, `solution/`, `DESIGN.md`, or `fixtures/`.

If `hidden_blocker.enabled` is false, return `[]`. If true, return the union of `leakage_hits` over the brief bodies and `flags["leakage_forbidden_substrings"]`. `__main__` exits 1 when the list is non-empty, 0 when it is empty.

Current prompt files do not contain those substrings (`prompts/vague.md`, `guided.md`, `specific.md` on both tasks). The scanner must pass on them without editing them.

## Tests C-T1, C-T2

In-scope unit, one `hardness/test_flags.py` per task. No fixture edit, no prompt edit.

- **C-T1.** `flags.yaml` loads. `hidden_blocker.enabled` is false. `registered_question_ids` is a non-empty list. `decoy.must_not_be_required_for_resolved` is true. `horizon.advances` is 1. GT004 `id` is `gt004-precutover-approval` and `fact` names `nbrennan`. GT009 `id` is `gt009-cutoff-precedence` and `fact` names `PAY-3120`.
- **C-T2.** `leakage_hits("see PAY-3120", ["PAY-3120"])` returns `["PAY-3120"]` on the GT009 copy; the GT004 copy does the same for `"94,097"`. `scan_brief(task_dir, flags)` on the real task directory returns `[]`. A temporary copy of the brief text is not required: call `leakage_hits` for the synthetic fail, and `scan_brief` for the live pass.

Deferred: **C-T4**, K≥8 frontier rollouts measuring whether the hidden blocker moves pass@1.

## Ask-stakeholder (C-ASK)

Not an MCP tool. `gateway/src/alkera_gateway/tools.py` `TOOLS` stays at 63 keys. C-ASK does not edit `tools.py`. The test reads `TOOLS`.

Shipped module `gateway/src/alkera_gateway/ask_stakeholder.py` is the contract. Do not replace it with a function that refuses every question.

- `channel_enabled(env=None)` is True only when `ALKERA_ASK_STAKEHOLDER` is exactly `"1"`. `"true"` and `"0"` are off.
- `ask(question, registry, env=None)` raises `AskDisabled` when the channel is off. When it is on, a normalized trigger substring or an exact blocker id returns that blocker's answer and no other. A miss returns `AskResult(False, None, None)`.
- `ask_f1(questions, registry, env=None)` is precision of hitting questions over recall of distinct blockers. It returns no `score`, `pass`, `reward`, or `outcome_class`.
- No model and no network. Graders do not import this module. `tools.py` is not edited.

- **C-T3** is `gateway/tests/test_ask_stakeholder.py` as it landed: disabled raises, a registered trigger answers, twenty misses do not yield F1 1. `"ask_stakeholder"` is not a key of `TOOLS`.
