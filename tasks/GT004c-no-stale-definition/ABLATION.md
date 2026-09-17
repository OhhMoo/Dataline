# GT004c-no-stale-definition - ablation variant of GT004-doubling-window

A byte-for-byte copy of `tasks/GT004-doubling-window/` with ONE change to the
seeded human context under `fixtures/workflow/`, plus the bookkeeping that
change forces. The data, the contract, the rubric, the probes, the prompts,
`chat.yaml` and `users.yaml` are identical to the original. `[task] id` in
`task.toml` differs so the harness can name the run, and the `vague` prompt
variant is selected (the original's 0.0 Pass@1 baseline was measured under it).

Removed: D (stale definition-of-record metric page)
Kept:    T (closed wontfix ticket DATA-4402), R (false full-refresh claim on the changelog page)

| cause | where it lived | what replaced it |
|---|---|---|
| T | issues.yaml `DATA-4402`, closed wontfix with a wrong explanation | a neutral closed ticket, same id/status/people/dates, similar length, unrelated topic |
| R | kb.yaml `de/changelog/materialisation-change`, "History was full-refreshed at the cutover..." | one neutral procedural sentence of the same length |
| D | kb.yaml `de/metrics/net-revenue`, stale definition-of-record (net = gross + tax) | the page corrected to the post-2025-12-01 definition, same length/label/author/date |

`fixtures/workflow/claims.yaml` is adjusted per removed cause so
`check_claims.py` passes; `false_causes.yaml` (grading policy) is untouched.

Known confounds are recorded in the `authoring` notes of the replaced records
and in the ablation report. Do not run a rollout against this package without
reading them.
