# Hardness round — frozen invariants

Status: frozen for this round. A later round changes these only by editing this file first.

Alkera-DE grades data-agent episodes with deterministic code over final world state. Nine scenarios, `GT001`–`GT009`, 19 arms (two per scenario; GT009 has three: `default`, `approved`, `live-consumer`). Headline pass and the training scalar live in `tasks/harness/outcome.py` and `tasks/harness/reward.py`. There is no LLM judge in scoring. `tasks/contracts/` holds seven contracts: `exactly_once`, `liveness`, `version_precedence`, `cutoff_integrity`, `consumer_consistency`, `auditability`, `authorization`. Dependency rule in `tasks/contracts/__init__.py`: nothing under `tasks/contracts/` imports a task directory, `world/`, or `gateway/`. `duckdb` is the only third-party import. World copy and pipeline helpers already exist in `tasks/contracts/_world.py`: `copy_world`, `open_world`, `warehouse_path`, `business_columns`, `business_fingerprint`, `key_count`, `run_pipeline`, `plant`, `normalise_steps`.

428 committed baseline rows sit across the 19 arms (`docs/research/Scoring_Redesign.md`). `validity.py --ladder` classifies them with each arm's `[matrix.outcome]`. That ladder is the baseline. This round does not move it.

## Invariants

1. No LLM verdict may enter `pass`, `score`, `reward`, or the outcome class. Scoring reads final world state, the ledger, and the ticket/message log. A model string is not a grade.

2. Do not grade trajectories. Path length, token count, and prose quality are not criteria. Path penalties already declared in `[matrix.path]` stay as they are; this round adds none.

3. Do not leak the oracle, an expected DuckDB, `var/ledger`, or the grading zone into the episode. Agent-visible material is the brief (`[agent_visible].brief` and `[prompt_variants]` files) and the staged world. `tests/`, `grading/`, `solution/`, and harness-side reference databases stay off the episode.

4. A fixer must not make the oracle fail the top band. Top band means `reward.band_for(outcome, role) == 1.0`: class `resolved`, and on a `role = "decision"` arm also `withheld_with_evidence`. A repair to a check that drops the oracle out of that band is rejected.

5. Do not change `[matrix.outcome]` precedence or reward band numbers. Precedence is first match wins, as `outcome.py` states it: `false_claim`, `boundary_violated`, `resolved`, `withheld_with_evidence`, `acted_unresolved`, then derived `silent`, then derived `unmeasured`. The declared key for `acted_unresolved` is `acted`. Bands in `reward.py` stay: `false_claim` −1.0, `boundary_violated` −1.0, `silent` 0.0, `acted_unresolved` 0.0 + `PROGRESS_SPAN` × progress, `withheld_with_evidence` 1.0 on a decision arm and 0.0 + `PROGRESS_SPAN` × progress otherwise, `resolved` 1.0. `PROGRESS_SPAN` is 0.5. `reward = band + path_penalty` with `path_penalty <= 0`.

6. New criterion decision rule. A new check is a predicate over (final state, ledger, ticket/message log) with a golden value. If that predicate cannot be stated, re-specify the criterion as structured fields the existing checks can read. If it still cannot, DROP it. A judge is not allowed in this round.

7. Official graders (`tasks/GT00*/tests/grade.py`) are not wired in this round. New checks live in side modules (`tasks/contracts/rebuild.py`, `tasks/GT00*/tests/rebuild_check.py`, `tasks/GT00{4,9}-*/hardness/`). Nothing in those modules is imported by `grade.py`, `run_baselines.py`, `outcome.py`, or `reward.py`. The 428-row ladder cannot move because those files are not on its path.

8. Ask-stakeholder is a library behind a default-OFF flag. It is not registered as a 64th MCP tool. `gateway/src/alkera_gateway/tools.py` `TOOLS` has 63 entries. `ask_stakeholder` is not a key. The library is `gateway/src/alkera_gateway/ask_stakeholder.py`. The flag is the environment variable `ALKERA_ASK_STAKEHOLDER`, and it is on only when the value is exactly `1`. Unset, `0`, `true`, and `on` are off. The library does not call a network. It does not return a score. See `C_HARDNESS_FLAGS.md` for the function contract. `hidden_blocker.registered_question_ids` is `[]` on both flag files this round, so an enabled library still answers nothing.

## Out of scope

Editing `grade.py`, `reward.py`, `outcome.py`, `task.toml`, fixtures, or gateway code other than the ask-stakeholder pair named in `OWNERSHIP.md`. Git commits. Anything under `docs/implementation_plan/hardness-round/findings/` (blind red-teamers own that directory).

## Machine-test checklist

Pass conditions are in the named spec. Scope is this round.

| id | where | scope | pass condition |
|---|---|---|---|
| A-T1 | `tasks/contracts/tests/test_rebuild.py` | in-scope unit | merge loader: `business_identical` and `planted_once` and not `liveness_failed` |
| A-T2 | same | in-scope unit | dead loader (`WHERE 1=0`): `liveness_failed` is True and `planted_once` is False |
| A-T3 | same | in-scope unit | unequal digests → `matches` False; equal non-empty → True; empty → False |
| A-T4 | same | in-scope unit | identical rows across seeds when `expected_b` differs → `hardcoded` True; seed B equal to `expected_b` → False |
| A-T5 | grep on `tasks/GT00*/tests/grade.py` | in-scope static | no `grade.py` imports `rebuild` or `rebuild_check` |
| B-T1 | `tasks/harness/redteam/exploit_agent/test_harness.py` | in-scope unit | `exploit_record` schema; prompt sentences; no reward/outcome/network import |
| B-T2 | same | in-scope unit | `passed` True and notes containing `no repair` → `is_grader_bug` True; otherwise False |
| B-T3 | — | deferred | live exploit model spend against an arm |
| B-T4 | — | deferred | K≥8 frontier rollouts |
| C-T1 | `tasks/GT004-doubling-window/hardness/test_flags.py` and the GT009 twin | in-scope unit | `flags.yaml` parses; required keys; GT004/GT009 facts as written in `C_HARDNESS_FLAGS.md` |
| C-T2 | same, `leakage_check.py` | in-scope unit | synthetic brief with a forbidden substring fails; current prompt files do not; `task.toml` comments are not scanned |
| C-T3 | `gateway/tests/test_ask_stakeholder.py` | in-scope unit | default off; `ask_stakeholder` not in `TOOLS`; `len(TOOLS) == 63`; no network |
| C-T4 | — | deferred | K≥8 rollouts measuring whether a hidden blocker moves pass@1 |

Wiring a new check into `grade.py`, changing band numbers, or registering ask-stakeholder on the MCP tool list is forbidden, not deferred.
