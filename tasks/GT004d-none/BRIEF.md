# GT004 - agent brief and prompt-variant framework

SPEC owns this file, `task.toml` and `prompts/**`. The task itself - what is planted,
why it is hard, who owns which directory - is `DESIGN.md`, and this file does not
restate it. This is about the one thing that is not in DESIGN.md: **how the task is
described to the agent, how that description is chosen, and how a finished rollout is
bound to the description it actually ran.**

## Why the prompt is the difficulty dial

Everything before GT004 graded code correctness, and the code checks are saturated:
D1-D5b pass at 1.0 for every model run so far. The cheapest remaining lever is not the
problem, it is how the problem is stated. Vague prompts make even simple problems hard,
and the questions that break agents are the ones that cannot be answered from the
codebase alone - they have to be answered from the data, the logs and the clock.

So GT004 ships **one world, one rubric, one pass criterion, and three tickets**. Nothing
else differs between variants. A difference in measured Pass@1 across them is a
difference in how much was said, and nothing else.

| variant | what it says | what it withholds |
|---|---|---|
| `vague.md` | Nadia, who owns the pack and the audit deadline, is not willing to sign numbers she cannot defend and cannot say what is wrong with them. No surface, no direction, not even a claim that anything IS wrong. The agent must establish a baseline, find the affected period itself, and decide whether action is warranted at all. | everything |
| `guided.md` | DATA-4412 as CONTEXT wrote it: the symptom (the commercial marts are over the source extract), the affected surface, that it is not every day and not a constant amount, and that everything reports green. | cause, count, window, mechanism |
| `specific.md` | Six acceptance criteria - what must be **true** at the end, checked against the world, not the write-up. The floor of the dial. | cause, count, window, and every step of the repair |

### The leak rules every variant was written against

None of the three names, hints at, or lets an agent infer without querying:

- **the cause** - no append mode, no re-run, no retry, no write-disposition, no schema
  change, no incremental switch, no "the loader", no named table or job;
- **the number of defects** - every variant speaks of "the difference" in the singular
  and never enumerates. `specific.md` was the hard one: an acceptance list that says
  "no duplicate keys" *and* "one column layout" has told the agent there are two
  problems. Criterion 1 therefore states a single reconciliation property - same
  events, counted once, same values, same shape - which is satisfiable only by fixing
  both and reveals neither;
- **the damage window** - no dates anywhere in any variant. `guided.md`'s "not every day
  and not a constant amount" is the reporter's symptom, and is exactly what sends the
  agent to find the boundary;
- **the late arrivals** - all three carry the identical ground-rule line *"This world is
  live. Scheduled work runs and new data arrives while you are working."* It is in all
  three on purpose: it is an operating condition, not a description of the problem, so
  it cannot be the dial, and withholding it entirely would make G3 a gotcha rather than
  a lifecycle test. It does not say that late events land inside the repaired window,
  which is the part the agent has to discover.

This is F202 and F125 applied. F125 landed on A027 - one of this task's three components
- precisely because an archetype's own description named the root cause verbatim and
handed over the gap length; the task is solved on reading. F125 was executed by C2-GT002
and the rule carries here.

**Also held identical across variants:** the deliverable block (`final_report.json` and
its six keys), the note on committing to a single `primary_cause`, the
`additional_causes` warning that feeds the false-cause penalty, the "what you have"
orientation, and the ground rules. If grading is to be identical, everything the grader
reads must be identical. Only the ticket changes.

## Selecting a variant at rollout time

The harness has exactly **one** instruction source. `taskspec.load()` reads
`[agent_visible].brief`, resolves it against the task directory, and `rollout.py` hands
that text to the subject as `spec.instruction`. There is no `--prompt-variant` flag and
no environment override. So selection is a one-line change to that key, and
`prompts/variant.py` is what makes that change atomic, hash-checked and stamped:

```bash
PY=../../gateway/.venv/Scripts/python.exe

$PY prompts/variant.py list                 # which variants exist, which is active, has any drifted
$PY prompts/variant.py select guided        # rewrite [agent_visible].brief + write ACTIVE_VARIANT.json
$PY ../harness/run_rollouts.py --task GT004-doubling-window --k 5 \
      --run-id guided-2026-09-20-k5         # the run-id CARRIES the variant - see below
$PY prompts/variant.py identify %TEMP%/alkera-rollouts/GT004-doubling-window/guided-2026-09-20-k5
```

`select` refuses if the file has drifted from the sha256 locked in `task.toml`
`[prompt_variants]`. That is not bureaucracy: a prompt edited under a frozen lock
silently re-defines every rollout already attributed to that variant. Editing a prompt
means re-locking (`variant.py lock` prints the block), and re-locking means the earlier
rollouts measured a different task.

## Recording it: a pass rate that does not name its variant is meaningless

Three independent places, two of which cannot be forged by whoever writes the report.

**1. The run-id, because it is the only label the report has.** `report.py` renders
`# <task> - rollout report (<run_id>)` and identifies a run by task and run_id and
nothing else - there is no variant field in `report.json`, and `rollout.json` records
`task` but not the prompt. So **every GT004 run must be launched with
`--run-id <variant>-<date>-k<K>`**. Without that convention a GT004 report is a Pass@1
with no statement of what was asked, which is not a result.

**2. The prompt the subject actually received - the evidence of record.**
`agent_under_test.run()` writes the complete prompt to `<rollout>/prompt.txt`
unconditionally, for every driver, before the episode starts, and `build_prompt()`
appends the task instruction verbatim as that prompt's suffix. So the variant a rollout
ran is derivable from the rollout's own stored evidence, with no cooperation from the
person reporting it:

```bash
$PY prompts/variant.py identify <run-dir>
```

It exits non-zero and says why when (a) any `prompt.txt` matches no locked variant - the
prompt was edited between the lock and the run, and those scores cannot be attributed to
anything; (b) one run mixes variants - one pass rate over mixed prompts is not a number
about anything; or (c) `report.json`'s `run_id` does not begin with the variant the
prompts on disk say was used - a report titled `guided-...` over vague prompts is worse
than an unlabelled one. **Run it before quoting any GT004 Pass@1.**

**3. `prompts/ACTIVE_VARIANT.json`**, written by `select`: task, variant, file, sha256,
selection time. The weakest of the three - it records intent at selection time, not what
ran - so it is a convenience for "what is the repo set to right now", never the evidence.

### The harness change that would make this automatic (not SPEC's to make)

Three lines in `tasks/harness/`, owned by whoever owns the harness: a
`--prompt-variant NAME` on `rollout.py` that overrides the brief path, and
`record["prompt_variant"] = NAME` beside `record["task"]`, surfaced by `report.py` in
the report header. Until that exists, convention (1) plus verification (2) is the
mechanism, and (2) is the half that does not depend on anyone remembering.

## Known leak vector, found while writing this and NOT fixed here

Under `--topology container` the subject has `bash` and the audit log is bind-mounted at
`/audit` inside its container. `gateway/src/alkera_gateway/audit.py` writes
`<audit>/manifest.json` containing `episode_id`, and `rollout.py` builds that id as
`f"{spec.task_id}-{run_id}-{rollout_id}"`. So a container-topology subject that reads
`/audit/manifest.json` sees the string `GT004-doubling-window-<run-id>-r0` - a task name
that names the mechanism, and, under the run-id convention above, the prompt variant
too. Under `host` topology the audit dir is `<rollout>/audit`, outside the world root and
unreachable by the workspace-rooted `fs_*` tools, so the leak is container-only.

Nothing in SPEC's ownership fixes it: renaming the task directory would break five other
agents mid-build. The options are for the harness owner - hash the `episode_id`, or omit
it from the in-container manifest - and until one is taken, **GT004 container rollouts
should be treated as carrying a possible task-name leak**, and the transcripts checked
for `/audit`.

## What is not real yet

`task.toml`'s `[handover]` block is the authoritative list; it names every placeholder,
every value adopted from another agent's file, and every file this spec points at that
does not exist yet. Read it before quoting anything out of this package. In particular:

- **No baseline has been measured.** Every `[[validity]].actual` reads `PENDING
  VALIDITY`, and `pass_min_total = 28.0` is GRADER's derivation (31 required points
  minus hygiene's -3 floor, so hygiene can never bind the verdict), not an observation
  against a baseline profile the way GT001's 17.0 and GT002's 21.0 were.
- **`tests/build_world.py` does not exist**, so GT004 loads but does not yet run. It
  must emit both `world_root` and `workspace`, because `grade_argv` now passes both -
  GT004's grader differs from GT001's and GT002's here, since G5 diffs fingerprint
  sections that live outside the workspace.
- **The BI dashboard id is still unknown**, so the `bi_*` tools are deliberately
  unprobed rather than probed with a guessed id.
- **The prompts were reconciled against CONTEXT's workflow corpus on 2026-09-14**, after
  an earlier draft invented its own cast. `guided.md` and `specific.md` quote DATA-4412's
  body as CONTEXT wrote it, and every name is from `fixtures/workflow/users.yaml`. They
  quote **no figure at all**, so nothing in `prompts/` needs CONTEXT's `{{CONTRACT}}`
  placeholder resolution - and nothing in `prompts/` may acquire a figure without going
  through `check_claims.py` first.
