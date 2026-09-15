# A-rollout-results — first real model rollouts on GT001

**2026-09-14 · Track A (Kimi), clearing C1's open acceptance item.** Artifacts:
`%TEMP%\alkera-rollouts\GT001-aov-halving\{kimi-k1-validate,kimi-k5,claude-k1-smoke}\`
(rollout.json + transcript.jsonl + report.md per run). Tree: harness kimi driver at
`ce635cd`; rollouts ran pre-control-plane-merge (toolbelt = 28 tools).

## What is now measured (not derived)

The full loop ran for real, on a second model family: world built from seed → fresh
blind subject process with only the 28 MCP tools granted → episode → teardown →
deterministic grading (twice) → failure classification. Blindness: clean across all
rollouts (0 violations, 0 unobserved). The trajectory data is real and stored.

| Campaign | Subject | K | Scored | Result |
|---|---|---|---|---|
| kimi-k1-validate | kimi-code/k3 | 1 | 1 | **pass** (driver validation) |
| kimi-k5 | kimi-code/k3 | 5 | 2 | **1 pass (17/19) · 1 tool_gap (15/19) · 3 quota-killed** |
| claude-k1-smoke | opus | 1 | 0 | harness_error — account spend limit, 0 tool calls |

kimi-k5 per-check (scored rollouts): D1 5/5, D2 3/3, D3 3/3, D5a 2/2, D5b 2/2 all at
100%; **D6 0/2** (secondary-cause reporting) and D7 1/2 (durable loader) are the only
discriminators. Median 77.5 tool calls, ~970s wall.

## Trajectory sketches

- **r4 (pass, 17/19, 76 calls):** oriented via bi_list/sched_list/fs_list, found the
  retry backfill (`retry-20260303-0412`, scheduler run-000363), proved duplicates,
  repaired, and committed `final_report.json` after noticing an interrupted commit had
  actually landed. Full diagnosis, exact damage window.
- **r1 (tool_gap, 15/19, 79 calls):** same correct root cause in its own words — then
  made **9 failed calls to `dbt_compile`, a surface the pre-episode probe already knew
  was dead in this world.** Classified tool_gap: the failure is attributable to the
  dead surface, not the model. Exactly the misattribution the taxonomy exists to catch.
- **r0/r2/r3 (harness_error):** the kimi CLI hit the account's 5-hour usage cap
  mid-episode (403 after 48–92 tool calls). Infrastructure, correctly excluded.

## Findings

1. **GT001 falls at first attempt to frontier models.** kimi 17/19 on its first scored
   try; opus 18/19 in the earlier smoke. C1's prediction ("Pass@1 well above 20%")
   is directionally confirmed. The sub-20% thesis now has two falsifying-direction
   samples; difficulty work should go to GT002/GT003 and harder levers, not GT001.
2. **D6 (honest secondary reporting) is the real discriminator** — both models lost
   their points there, and it is excluded from `pass_requires_checks` (open decision,
   flagged in task.toml).
3. **Dead surfaces are a world gap, not difficulty** — `dbt`, `s3`, `bi_cached` are
   dead in this world and produced the one non-pass. Fixing the world (dbt project
   present in-episode) matters more than hardening the grader.
4. **Quota, not machinery, is the campaign bottleneck.** Both subjects died on account
   limits (kimi 5-hour window; claude monthly spend cap, session resets 02:10 PT).

## Blocked / next

- claude K≥5 on GT001: blocked until the account's 02:10 PT reset.
- kimi clean K=5: blocked until the 5-hour window resets.
- GT002/GT003 rollouts: not yet attempted (GT003 also lacks a [harness] block).

Rerun (from `tasks/harness/`, after quota resets):

```
../../gateway/.venv/Scripts/python.exe run_rollouts.py --task ../GT001-aov-halving --k 5 --model opus --driver mcp --run-id claude-k5
../../gateway/.venv/Scripts/python.exe run_rollouts.py --task ../GT001-aov-halving --k 5 --driver kimi --run-id kimi-k5b
```

Note: post-merge (`2bd6d37`) the toolbelt is 36 tools (ingest_* added); rerun reports
are not toolbelt-comparable with the above.

---

## UPDATE 2026-09-14 (final): campaign complete, GT001 falsified as "hard"

Both reruns above completed after quota resets (36-tool toolbelt). Artifacts:
`%TEMP%\alkera-rollouts\GT001-aov-halving\{claude-k5,kimi-k5b}\`.

| Campaign | Subject | K | Scored | Result |
|---|---|---|---|---|
| claude-k5 | opus | 5 | 5 | **Pass@1 = 1.0** — totals [17,17,18,18,18]/19, median 67 calls / 485s |
| kimi-k5b | kimi-code/k3 | 5 | 4 | **3 pass (17,17,17) · 1 tool_gap (15, 8 dead `dbt_compile` calls) · 1 quarantined** |

The quarantine (r2) is a **harness artifact, not cheating**: the run's `--out` was inside
the repo, a tool error leaked that abs path, and the agent quoting it in thinking fired
the repo_path_reference marker. Fixed by running with `--out` under `%TEMP%`.

Also fixed mid-campaign (`tasks/harness/kimi_subject.py`): provider `turn.step.retrying`
/ `interrupted` wire events are now observed-but-benign instead of fail-closed quarantine
(selftest 59/59). r1/r2/r4 transcripts retranslated and reclassified.

**Combined picture on GT001:** claude 5/5 pass; kimi 4 pass / 2 tool_gap of 6 scored
across k5+k5b — and both tool_gaps are attributable to the dead `dbt` surface (world
gap, not difficulty). **GT001 falls at first attempt to both model families; the
sub-20% Pass@1 thesis is falsified for GT001.** D6 (honest secondary-cause reporting,
pass rate 0.0, mean 0.6/2 on claude) remains the only discriminating check, and it is
excluded from `pass_requires_checks` (open task-owner decision in task.toml).
