# `tasks/harness` — the parallel rollout and difficulty harness

Puts a fresh, blind model in front of a task, captures the whole episode, grades
it after teardown, and classifies why it failed. Task-agnostic: everything
specific to a task is declared in that task's own `task.toml` under `[harness]`.

```bash
PY=../../gateway/.venv/Scripts/python.exe

$PY selftest.py                                        # 51 detector/classifier checks
$PY run_rollouts.py --task GT001-aov-halving --k 5     # K rollouts + report
$PY rollout.py --task GT001-aov-halving --out <dir>    # one rollout
$PY rollout.py --reclassify <rollout-dir>              # re-derive the verdict only
```

`--reclassify` re-runs the detectors and the classifier over a completed rollout's stored
transcript, action log and artefact. It never re-runs the episode and never re-grades, so a
detector fix can be applied to rollouts already spent. Use it after changing `blindness.py` or
`classify.py`; a stale verdict is worse than no verdict.

Runs land in `%TEMP%/alkera-rollouts/<task>/<run-id>/` — outside the repo on
purpose. A world built inside the source tree puts the subject one `..` from the
grader, and "it cannot get there anyway" is the assumption this harness exists
to refuse.

## What a task must declare

The harness refuses to run a task that has not declared these. It will not
invent a world builder, a grader argv or a pass threshold: each of them decides
the measured number, and none of them is the harness's to choose.

```toml
[harness]
world_builder        = "tests/build_world.py"    # --root --grading --seed -> json on stdout
grade_argv           = ["--world", "{warehouse}", ...]   # {} filled from that json
surface_probes       = [{ label = "dbt", tool = "dbt_compile", args = {...} }, ...]
pass_requires_checks = ["D1", "D2", ...]         # all must pass
pass_min_total       = 17.0
```

## The order of operations, which is the contract

```
build world  ->  plant canaries  ->  fingerprint  ->  probe surfaces (throwaway
copy, own audit dir)  ->  run the subject  ->  TEARDOWN  ->  fingerprint  ->
grade (twice)  ->  detect  ->  classify
```

The grading zone is built as a **sibling** of the world root, the gateway config
never names it, and the grader is not invoked until the subject's process is
dead. Ground rule 1 is structural here, not a convention.

## Drivers, and how much they are worth

| driver | subject | tool surface | blindness |
|---|---|---|---|
| `mcp` | `claude --print` | **only** the gateway tools, over MCP stdio | `tool_surface_revoked` |
| `cli` | `claude --print` | Bash + a `gw.sh` shim | `detection_only` |
| `scripted` | `redteam/scripted_subject.py` | n/a | detector tests only |

Under `mcp` the revocation is real and verified per rollout from the subject's
own session-init event, not from our flags: `--tools ""` plus
`--strict-mcp-config` leaves it with `mcp__alkera__*` and nothing else.

A tool the task does not probe is a tool whose failures cannot be attributed, so
every rollout records `unprobed_tools` and the report names them. The toolbelt
grew from 17 to 28 mid-round; a probe may declare `covers = [...]` to speak for
siblings that cannot be probed read-only (`dbt_compile` for `dbt_run`/`dbt_test`).

Not `--safe-mode`. It disables MCP servers along with every other
customisation, which leaves the subject with no toolbelt at all and an episode
that looks like a very fast failure. Measured, not assumed.

## Blindness is detected, never assumed

Six detectors, deliberately overlapping, in `blindness.py`. Any **hard**
violation quarantines the rollout: it is recorded in full and excluded from
Pass@1.

0. **evidence** — no transcript, an unparsed line, an un-inventoried world.
   Absence of evidence is not evidence of blindness: a detector that never ran
   answers "no violation" for the same reason a switched-off camera reports no
   burglary. Unobserved is its own hard state and its own classification.
1. **capability** — what the subject's init event says it was *given*, including
   tools it never used.
2. **tool use** — every `tool_use` block against the gateway allowlist. Fails
   closed: an unrecognised tool is a violation.
3. **canary** — unique tokens planted in the grading zone and beside the world.
   Nothing the toolbelt can do reaches them, so one appearing in a transcript is
   proof rather than inference.
4. **markers** — filename-shaped strings and path-segment patterns
   (`expected.duckdb`, `/grading`, the repo path). Filename-shaped on purpose so
   that prose about "the grader" does not trip it — and deliberately NOT
   `/tests`, because the ticket asks the agent to write one and a detector that
   fires hardest on the best rollouts is worse than none.
5. **out of band** — world files that changed and that no action-log row
   accounts for.

Under the `cli` driver every shell command is split on `&&  ||  ;  |  newline
$(  backtick` and each segment must be the shim or a strictly local no-op. `cd
..` is not a local no-op: moving toward the grading zone is itself the signal.

## Failure classification

`pass` · `blindness_violation` · `unobserved` · `harness_error` ·
`grader_defect` · `tool_gap` · `timeout` · `budget` · `reasoning`

Order matters. `tool_gap` sits above `timeout` and `budget`, because an episode
that burned its clock on a surface the pre-episode probe already found dead is a
tool gap that presented as a timeout, and calling it a timeout hides the thing
worth fixing. Every heuristic class carries `needs_review`.

`grader_defect` is assigned only on hard evidence: the grader crashed, or it
scored the same unchanged world differently on two consecutive runs (every
rollout is graded twice for exactly this). "It did the right thing and the
grader said no" is **not** detected automatically and cannot be — see the
weakness section of `rounds/R003/C1-harness.md`.

## Reports

`report.md` and `report.json` per run. Two denominators, both labelled:

- `pass_at_1` over **scored** rollouts (attempted; not quarantined; no harness
  or grader defect)
- `pass_at_1_reasoning_only` over passes + reasoning failures only — an upper
  bound on what the task measures about reasoning

Plus per-check pass rates, the classification counts, the quarantine count split
into *caught violating* vs *unobservable*, median tool calls and wall clock, and
the dead surfaces found by the pre-episode probe.

## `redteam/`

Detector tests, never measurements. Any rollout using one is stamped
`redteam_detector_test: true` and the report says so at the top.

## Read every quarantine before you believe the number

Detection fails closed, which is right when the alternative is certifying a
cheat — but it means the errors point at the good rollouts. The first real
rollout this harness ran was a pass that got quarantined for writing the
regression test the ticket asked for, and the verdict read as entirely
plausible. The report prints each quarantined rollout's violation text so that a
human can check it. Treat the quarantine list as a worklist, not a result.
