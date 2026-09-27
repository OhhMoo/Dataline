# `tasks/harness` — the parallel rollout and difficulty harness

Puts a fresh, blind model in front of a task, captures the whole episode, grades
it after teardown, and classifies why it failed. Task-agnostic: everything
specific to a task is declared in that task's own `task.toml` under `[harness]`.

```bash
PY=../../gateway/.venv/Scripts/python.exe

$PY selftest.py                                        # 133 detector/classifier/gate checks
$PY surface_gate_check.py                              # live proof the surface gate fires, no model
$PY container_check.py --task GT001-aov-halving        # live containment proof, no model
$PY reference_copy_check.py --task GT001-aov-halving   # the reference_copy baseline, contained
$PY run_rollouts.py --task GT001-aov-halving --k 5     # K rollouts + report (native + container)
$PY run_rollouts.py --task GT001-aov-halving --k 5 --driver mcp --topology host
$PY rollout.py --task GT001-aov-halving --out <dir>    # one rollout
$PY rollout.py --reclassify <rollout-dir>              # re-derive the verdict only
$PY rollout.py --regrade <rollout-dir>                 # re-run everything after teardown
$PY run_rollouts.py --regrade-run <run-dir>            # ...for a whole run, + the report
$PY test_regrade.py                                    # what the regrade may and may not do
```

`--reclassify` re-runs the detectors and the classifier over a completed rollout's stored
transcript, action log and artefact. It never re-runs the episode and never re-grades, so a
detector fix can be applied to rollouts already spent. Use it after changing `blindness.py` or
`classify.py`; a stale verdict is worse than no verdict.

## `--regrade` — the episode already happened

`--reclassify` keeps the stored score verbatim, which is no use when the GRADER is what was
wrong. `--regrade` re-runs every stage that comes after teardown — workspace-edit staging
(native), the action log, the fingerprint, the artefact, `blindness.detect`,
`episode.grade_twice` and the classifier — over the files the episode left behind. The subject
is never re-run and the world is never rebuilt.

It was built for GT009: five episodes ran to completion, and `rollout.py` then died at record
time because that task's `grade.py` emitted `check` where the record indexes `id`. Re-running
them would have spent ~55 minutes of model quota re-doing the one part that had not failed.

**What it refuses.** A directory missing the transcript, the action log, the world, the grading
zone or the final artefact is refused by name — a stage that runs without its input produces a
verdict about nothing. So is a rollout whose task, driver or world manifest can be neither read
from a record nor evidenced on disk: the driver decides the tool allowlist, and the wrong one
accuses the subject of holding a tool the harness handed it.

**What it will not fake.** Everything that describes the world BEFORE the episode — the tree
manifest the off-channel detector diffs against, the fingerprint, the action-log watermark, the
probe results, the toolbelt — comes from what `rollout.py` wrote **at the time**. A "before"
picture taken now is a picture of the world the episode already changed. `before_episode.json`
is written immediately before the subject is handed the terminal and is what makes a regrade
`complete`; without it the record is marked **`regrade_partial`**, the missing piece is named,
and its consequence is written out:

| not saved | what happens |
|---|---|
| `before_episode.json`, but a record exists | the world-diff verdict is carried forward from that record verbatim — the detector *did* run then — and the carry-forward is recorded |
| `before_episode.json` and no record either | `world_before` stays `None`, `blindness.detect`'s evidence gate fires, and the rollout quarantines as **unobserved**. It is still graded, so the score is not lost with the verdict |
| the surface probe results | the surface audit reports `unprobed` and `report.py` withholds Pass@1 — they cannot be re-taken, the world has been worked in since |
| the toolbelt (mcp/kimi) | re-read from the gateway venv and **verified** to cover every tool the transcript's init event records as granted; a gateway that has since lost one of them refuses rather than inventing a violation |
| the action-log watermark | rows the harness cannot name are counted as the subject's — it can only over-count |
| the agent's exit reason | recorded as `unrecorded`, which matches no classifier branch. `not_started` would be a claim, and a false one |

**The grader is two files.** `grade.py` reads `manifest.yaml` out of the grading zone — the
copy `build_world.py` made at episode time, with the canary comment `rollout.py` appended. A
regrade refreshes that copy from the task's current `[grading].manifest` (the canary line is
carried over byte for byte, since `_recover_canaries` reads it back) and
`regrade_grader_sha256` hashes both halves. Measured before this: GT003's first regrade re-ran
a new `grade.py` against the old manifest and reported the old token-list penalty five times.
A task that declares no manifest hashes `grade.py` alone, as before.

Every record carries `regraded_from` (the classification it replaced, or `no_record`),
`regraded_at`, `regrade_grader_sha256` and a `regrade` block with the source of each input.
`report.py` puts `- regraded: true` at the top of the run-conditions block, above the number,
and a run that mixes re-graded and original records — or two grader hashes — is invalidated.

A regrade writes **in place**, so copy a run that must be preserved; a copy's world manifest is
re-rooted onto the copy, and the original is never read or written. `test_regrade.py` executes
all of this against a real `--driver scripted` rollout (no model quota) and, when the machine
has one, diffs a re-graded recorded rollout against its original field by field.

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
required_surfaces    = ["sql", "dbt"]            # dead -> harness error, never a score
dead_by_design       = [{ label = "chat", reason = "this world has no chat" }]
pass_requires_checks = ["D1", "D2", ...]         # all must pass
pass_min_total       = 17.0
```

`required_surfaces` and `dead_by_design` are optional, and the harness invents
neither: a task that declares no requirements cannot fail the surface gate, and
the report names that as a run condition rather than letting it read as "all
surfaces fine". What is not optional is the consequence of not declaring - see
**The surface gate** below.

## Context variants — one task directory, several worlds

**A task directory is ONE context.** An ablation of that context — remove the
ticket, remove the stale definition, stack a second defect — is not a new task.
It is a `[variants]` entry inside the same `task.toml`, selected by
`run_rollouts.py --variant <name>`:

```toml
[variants.default]
note = "the shipped context"                 # mandatory; `default` has NO overlay

[variants.no-ticket]
overlay = "fixtures/variants/no-ticket"      # tree copied OVER fixtures/ at staging
prompt  = "vague"                            # a declared, sha-locked [prompt_variants] name
note    = "T removed: DATA-4402 becomes a neutral closed ticket. R and D stay."

[variants.escalation-credited]
note    = "the default world; G1c pays for the rewrite OR a recorded escalation"
grading = { authorization = true, g1c_credits_escalation = true }
```

Five of these used to be five sibling directories (`GT004a-no-ticket` …
`GT004e-ticket-only`), each a byte-for-byte copy of a 25 MB package with two to
four YAML files changed, and GT006's 2x2 was two more. A copy drifts: the day
the parent's grader is fixed, seven graders disagree and nobody notices, because
`diff -rq` over a 25 MB fixture tree is not something anyone runs twice. One
directory and an overlay of four files cannot drift.

**What each piece does.**

| piece | where | what it decides |
|---|---|---|
| `overlay` | a directory, or a LIST of them, under the task | trees copied over `fixtures/` at staging time in the order declared: base first, then each overlay, last write wins. A list so two arms of a 2x2 can share a base overlay instead of duplicating a 1,100-line generator into both |
| `prompt` | a `[prompt_variants]` NAME | the brief, so replaying a recorded arm takes one flag instead of a flag plus a `variant.py select` |
| `grading` | free-form table | flags the TASK's own `grade.py` reads. The harness carries them and knows what none of them mean. `pass_requires_checks` and `pass_min_total` are the two it does read, as per-arm overrides |
| `note` | mandatory | why the arm exists. An unexplained arm is an unexplained number six weeks later |

**How it reaches the parts of the run the harness does not own.** The world
builder, the surface stager and the grader are subprocesses whose argv the
harness does not write, so `run_rollouts.py` exports **`ALKERA_TASK_VARIANT`**
and `taskspec.load(..., variant=None)` falls back to it. A task-side script
calls `taskspec.fixtures_dir(TASK)` for the overlaid fixtures tree and
`taskspec.variant_of(TASK)` for the arm itself. The arm is then written into the
grading zone as `grading/variant.json` by the task's `build_world.py`, and
`grade.py` reads it **from there** rather than from the environment: the grader
is its own process, and an arm carried by a variable that did not survive a
subprocess boundary would score the wrong rubric while printing a number that
looked exactly like the right one. A grading zone with no `variant.json` is the
shipped context, so zones built before variants existed still grade.

**What carries the arm out of the run.** `variant: <name>` in every rollout
record, `run_conditions.variants` in `report.json`, a `- context variant:` line
at the top of `report.md`'s run-conditions block, and the arm appended to the
**default** run id - `20260917T041215Z-no-ticket` - so two arms measured the
same afternoon cannot land in one directory. An explicit `--run-id` is left
exactly as given, and `default` is NOT appended: a default-arm run directory is
named exactly as every GT004 run directory recorded before variants existed,
which is what keeps those recorded runs comparable to new ones. The record and
the report still say `default` in as many words. A run
whose records name two arms is **invalidated** by `report.py` rather than
averaged — the arms are different worlds, and one rate over two of them is not a
rate for either.

**Refusals.** `--variant` on a task that declares no `[variants]` is an error
naming the task; an unknown name is an error listing the ones that exist; a
`[variants]` table with no `default`, an arm with no `note`, and a `default`
that declares an overlay are all refused at load. `tasks/harness/test_variants.py`
executes all of that, plus: every overlay file wins over the base, every file the
overlay does not name is byte-identical to the package, and a world built with
`--variant default` is byte-identical to one built with no variant at all except
`var/dbt/profiles.yml` (which embeds the build's absolute path) and
`workspace/.git/**` (which records inodes).

**Each arm is separately task-valid.** `tests/run_baselines.py --variant <name>`
stages that arm's fixtures, writes its `variant.json` and applies its rubric —
including the baselines that only exist in that arm. An arm whose oracle cannot
reach max is not an ablation of the task, it is a broken copy of it, so the
suite is run for every declared arm and the table recorded.

## The order of operations, which is the contract

```
build world  ->  plant canaries  ->  fingerprint  ->  probe surfaces (throwaway
copy, own audit dir)  ->  SURFACE GATE  ->  run the subject  ->  TEARDOWN  ->
fingerprint  ->  grade (twice)  ->  detect  ->  classify
```

The grading zone is built as a **sibling** of the world root, the gateway config
never names it, and the grader is not invoked until the subject's process is
dead. Ground rule 1 is structural here, not a convention.

## Drivers, and how much they are worth

| driver | subject | tool surface | blindness |
|---|---|---|---|
| `mcp` | `claude --print` | **only** the gateway tools, over MCP stdio | `tool_surface_revoked` |
| `kimi` | `kimi -p` (Kimi Code CLI) | **only** the gateway tools, over MCP stdio | `tool_surface_revoked` |
| `cursor` | `cursor-agent -p` (Cursor CLI) | the gateway tools over MCP stdio; the client's built-ins are **refused by permission policy** (Shell, Read, Write, WebFetch), not removed, and its search tools cannot be refused at all - a built-in that runs is a hard violation | `detection_only` (the init event carries no tool list; see `cursor_subject.py`) |
| `cli` | `claude --print` | Bash + a `gw.sh` shim | `detection_only` |
| `native` | `claude --print`, or a scripted shell with `--model scripted` | Bash and nothing else - an EMPTY mcp config, vendor-shaped services found through `.envrc` and the environment, PATH-front shims for `duckdb`/`dbt`/`python`/`git` | `detection_only` + `network_destination` (every connection is logged server-side; an absent `destinations.jsonl` quarantines) |
| `scripted` | `redteam/scripted_subject.py` | n/a | detector tests only |

Measurement substrate is `--driver native --topology container` (the default).
`--driver mcp` is the comparison arm: the 63-tool list is a map of intended
moves, which is the leak native removes. Host native is not
`measurement_valid`. Spec: `gateway/NATIVE.md`. Root `README.md` has the
operator table.

## The headline on a `[matrix]` task (2026-09-19)

The report headline is APEX-Agents style, read off the arm's
`[matrix.outcome]` table and its `role`. Nothing is gated; every graded
episode is in the denominator. `docs/research/Scoring_Redesign.md` is the design
note.

- **pass** (per episode, bool): the outcome class is in the arm's **top
  band** — `resolved`, or `withheld_with_evidence` on a `role = "decision"`
  arm; equivalently `reward.band_for(outcome, role) == 1.0`. **pass@1** is
  its rate over the graded episodes (scored, class other than `unmeasured`),
  printed with the Wilson 95 % CI and n, plus the unbiased pass@k / pass^k
  table for k ≤ n.
- **score** (per episode, in [0, 1]; **mean score** aggregated): the
  fraction of **must-have criteria** met — the conditions the top-band
  classes list — taken as the max over those classes on a decision arm. A
  condition `X` holds when check X passed, `!X` when X paid less than its
  ceiling (the same rule `outcome.py` classifies with), and a condition on a
  check absent from the record never holds. `resolved` /
  `withheld_with_evidence` episodes score 1.0 by construction; a
  `false_claim` episode still gets its score ("lied, but 80 % of the work
  was there").
- **criteria** (aggregate): for every check id on record in the run,
  `{n, passed, rate, must_have, required_by, condition}` — `passed` counts
  the criterion met (`X` passed, or for a check the table names only
  negated, X did not charge), `must_have` says whether a top-band class
  requires it. Sorted must-have first, then by rate ascending: the "why did
  it fail" view.

The grader's own all-checks bit is printed under the headline as
`pass_all_required`. The declared training scalar (`reward.py`: band +
progress + path) is printed beside it and is never the headline. The
"library axes" (`pass_axis`, `gate`, `[matrix.map]`) were removed on
2026-09-19; a `task.toml` that still carries those keys loads exactly as one
that does not.

## The layers around the number (2026-09-18)

`docs/research/Scoring_Redesign.md` says what a lab reads off a task before
training on it. These modules print it. None of them changes a score.

| layer | module | what |
|---|---|---|
| 0 | `validity.py` | a per-task **validity card**: every check `[matrix.outcome]` and `pass_requires_checks` name exists in the grader; every `[matrix].accepted` name has a `[[validity]]` row on that arm; oracle / no-op / false-claim / positive-control rows present; with `--run`, the task's `run_baselines.py` verdict parsed. `python validity.py --task <T> [--variant <arm>] [--run] --out DIR`; exit 0 valid, 2 not, 3 refused |
| 1 | `outcome.py` | one **outcome class** per episode from checks alone, declared in task.toml `[matrix.outcome]`: `false_claim` › `boundary_violated` › `resolved` › `withheld_with_evidence` › `acted_unresolved` › `silent` › `unmeasured`. `!X` = "X paid less than its ceiling" (a penalty-only check's `passed` bit is not "did not fire"). Declared for GT009 and GT004 |
| 1 | `reward.py` | the **declared training scalar** per episode, three tiers (`DECISION_TEMPLATE.md §9`): a fixed **band** from the outcome class (`false_claim` = `boundary_violated` = −1, `silent` = 0, `resolved` = 1, `withheld_with_evidence` = 1 on a `decision` arm), plus `0.5·progress` inside the two partial classes, plus **path penalties** in every band. `None` (excluded) when the class is `unmeasured` or undeclared. Printed as such, never as the measurement |
| 1 | `progress.py` | **progress-in-band**: `[matrix.progress]` names product quantities a grader check exposes in `detail[field]` (rows repaired, nights correct, receipts matching — never report keys or step counts); each is normalised `(x − noop)/(oracle − noop)` against anchors read from the arm's baseline JSON and clamped to `[0, 0.999]`, then weighted. `reachability()` says whether the baselines spread the quantity at all |
| 1 | `path.py` | **path penalties** from `audit/actions.jsonl`: four rules (`destructive_command`, `platform_write`, `consumer_write` without a prior approval, `grading_surface_edit`), each ≤ 0, summed and clamped at `cap`. Reads are never charged. What counts as consumer-owned / platform / grading surface comes from `[matrix.path_allow]` over the shared world defaults |
| 0 | `validity.py --ladder` | with a baseline JSON (`run_baselines.py --json`, schema in `baselines_schema.py`): classifies every required baseline with the arm's outcome table, refuses a declared-vs-classified mismatch, a missing canonical baseline, or a scalar that does not order `resolved ≥ withheld > acted_unresolved ≥ silent > damage`; warns when progress takes < 3 distinct values across baselines |
| 0 | `lab_card.py` | `validity.py --lab-card`: the A1–A12 (Anthropic agent-evals) / O1–O8 (OpenAI RFT) requirement rows for a task, each `met` / `unmet` / `needs_run` with the file or run that decides it |
| 2 | `learning_signal.py` | Wilson **CI and n on every rate**; unbiased **pass@k / pass^k estimators** for k ≤ n (the run-level `Pass^k` bit is unchanged and still published); the group's **p(1−p)** and `collapsed` flag; **band** (`core` 0.05–0.7, `frontier`, `eval_only`, `control` > 0.9, `unmeasured`) — all over the headline `pass` bit on a matrix task. `## Learning signal` in every report |
| 2 | `tracked_metrics.py` | per-episode usage beside the score, never in it: `n_turns`, `n_toolcalls`, tokens in/out, `wall_clock_s`, `time_to_first_action_s` (first audit row − transcript start) |
| – | `export.py` | `benchmark.json` per run (schema 2) for the public table: `role`, `pass` {n, passed, rate, ci95, pass_at_k, pass_hat_k, top_band}, `mean_score` {n, mean}, `criteria` {id: {n, passed, rate, must_have}}, `outcome_counts`, the grader's `pass_at_1` / `pass_at_k` / `pass_hat_k`, `saturated`, band, `score_tiers` (band / progress / damage means) and `reward_headline: null` — the scalar is never a headline. No check text, paths, transcripts, per-rollout scores or `why`. `render_row` prints task, arm, role, n, pass@1 [ci], mean score, pass@k, pass^k, collapsed?. `run_rollouts.py --export DIR`, also with `--re-report`. `export.py --task T --variant V --rft DIR` writes an OpenAI RFT `grade(sample, item)` Python grader (stdlib only) and a `multi` grader exposing the three tiers |

Report-side, `report.aggregate(records, spec, baselines_json=...)` /
`run_rollouts.py --baselines PATH` supply the progress anchors; without them
progress is `None` and the caveat says so. Each `matrix.per_rollout` row
carries `outcome`, `pass`, `score`, `band`, `progress`, `path_penalty`,
`path_events`, `reward` and `tracked_metrics`; the block carries `pass`,
`mean_score`, `criteria`, `outcome_counts`, `saturated`, `reward_var`,
`progress_reachability` and `path_event_counts`, every one with its denominator.

If one of the layer modules cannot be imported or is unfinished, `report.py`
prints the block as unavailable and records a caveat rather than failing the
report - a number over a bug is worse than no number.

## Topology: where the WORLD and the TOOLS run

The driver says where the SUBJECT runs. `--topology` says where everything it
touches runs, and that is the axis that decides whether a terminal is safe.

| topology | world + gateway + audit | terminal | blindness guarantee |
|---|---|---|---|
| `host` | a host subprocess, beside the grading zone | none | `tool_surface_revoked` |
| `container` (default) | the `episode` service; native services on `*.internal` | **yes** — subject's bash is `docker exec` into the episode (native) or `mcp__alkera__bash` (mcp) | `filesystem_isolated` |

```bash
$PY run_rollouts.py --task GT001-aov-halving --k 5 --topology container
```

Under `host`, blindness works because the subject has no tool that reads
anything outside the gateway. That is why the 2026-09-12 meeting's ask for
terminal access could not be met: `agent_under_test.py` runs the gateway as a
host subprocess, so a shell there is one `..` from `/grading` and ground rule 1
is gone.

Under `container`, each rollout gets its own compose project. The world and the
audit log are bind-mounted into the episode container; the grading zone is a
SIBLING directory on the host and is not mounted, so it has no path inside the
container at all - not a denied path, an absent one. Both compose networks are
`internal: true`, so there is no egress. The gateway runs *inside* the container
and the subject reaches it as `docker exec -i <container> ... serve` over MCP
stdio: no published port (a port would be a second channel), and exactly one
gateway process (the audit log holds an exclusive writer lock).

The subject process itself stays on the host, because it is a model CLI and
needs an inference API that the episode zone deliberately cannot reach. The
agent loop is outside; the workspace is inside. Its tool surface is unchanged in
shape - still exactly `mcp__alkera__*`, verified per rollout from its own init
event - and now includes `bash`.

`bash` is registered into the gateway's own `TOOLS` table at runtime by
`container_shim.py` and served by the gateway's own `Gateway.call`, so a shell
command produces a contract-9.2.2 action-log row through the same code path as
`sql_query`: same seq, same args/result hashes, same schema, validated by
`alkera-gateway trace`. A second writer appending to the same log cannot work -
the log asserts a contiguous sequence and holds an exclusive lock. A bash row's
`targets` are the world files the command changed, from a before/after walk of
the world tree; without them the off-channel-change detector would fire on every
legitimate shell write. Nothing under `gateway/src/` is edited to do any of it.

### Rebuilding the episode image

The image is pinned so a measured run is never against whatever happens to be
uncommitted. Build it from a clean worktree, never from the working tree:

```bash
git worktree add --detach /tmp/wt HEAD
docker build -t alkera-episode:head -f /tmp/wt/gateway/docker/Dockerfile /tmp/wt
git worktree remove /tmp/wt
```

Every rollout records the image tag and digest. Rebuild when the toolbelt changes -
and say so in the report, because the toolbelt decides the prompt and therefore the
measurement.

**What the shell costs, stated plainly.** An agent with a shell can open the
warehouse file directly and mutate it without any `sql_execute` row naming the
statement. The action log still has a row per tool call and still names the
files that changed, so 9.2.2 holds - but `op_class`, `rows_affected` and the SQL
text are no longer recorded for work done that way, and the process signals that
read them see less than they used to. That is the price of realism and it is
paid knowingly, not discovered later.

Under `mcp` the revocation is real and verified per rollout from the subject's
own session-init event, not from our flags: `--tools ""` plus
`--strict-mcp-config` leaves it with `mcp__alkera__*` and nothing else.

Under `kimi` the same revocation is assembled from different parts (see
`kimi_subject.py` for the full reasoning): a per-rollout isolated
`KIMI_CODE_HOME` whose user-level `mcp.json` names only the gateway (the
subject's config.toml and credentials are hardlinked/junctioned in, so it
authenticates as the same account without the harness reading a secret), an
`--agent-file` whose `tools: [mcp__alkera__*]` allowlist removes every
built-in tool from the model's surface, `subagents: []` so there is no
delegation, and an empty `--skills-dir`. Kimi's stdout stream has no init
event, so the capability evidence comes from the session wire log's
`llm.tools_snapshot` — the exact tool list the subject's client sent to the
model API — translated into the claude-shaped transcript the detectors parse.
The translation fails closed: any unrecognised wire event becomes an
unparsed transcript line, which quarantines the rollout as unobserved.

**Quota.** Every driver's subject can be refused for a usage or spend limit,
and until 2026-09-17 that read as `agent_error` → `harness_error` → "needs
human review", with the cause one `final_text` away. Now `exit_reason_for`
files it as `quota_exhausted` (the result event's subtype, or a failed
process whose final text/stderr reads like a limit message —
`agent_under_test.QUOTA_PATTERN`; a clean exit is never relabelled on prose)
and `classify.py` gives it its own class: unscored, excluded, not
`needs_review`. `--reclassify` relabels records made before the class
existed from their stored `final_text` (`authorized-20260917-k5`: 5 ×
`harness_error` → 5 × `quota_exhausted`). `run_rollouts.py` also runs a
**quota probe** — one text-only turn against the model, recorded as
`quota_probe.json` — before it spends K episodes, and refuses to start (exit
3) if the probe is refused. `--no-quota-probe` skips it. `run_queue.py`
chains several `run_rollouts.py` invocations sequentially and stops at the
first refused probe or `quota_exhausted` episode (`queue-*.json` under
`runs/`). Tests: `test_quota_class.py`.

A tool the task does not probe is a tool whose failures cannot be attributed, so
every rollout records `unprobed_tools` and the report names them. The toolbelt
grew from 17 tools to 63; a probe may declare `covers = [...]` to speak for
siblings that cannot be probed read-only (`dbt_compile` for `dbt_run`/`dbt_test`),
and everything still undeclared is SWEPT - see below.

## The surface gate

GT004 ran five scored Opus rollouts in a world where eight of the agent's
surfaces answered `no_issue_tracker` / `no_chat` / `no_knowledge_base` /
`no_observability` / `no_ingest_state`. The harness printed that at the bottom of
the report, under the number, and did nothing with it. A run measured in a
half-absent world produces a Pass@1 indistinguishable from one measured in a
whole one, so a dead surface is now a run OUTCOME (`surface_audit.py`), on
`blindness.py`'s precedent: a compromised episode is quarantined rather than let
into the number. The grounds differ - there the subject may have seen what it
must not, here the subject was never given the world the task describes - but in
both cases the score is not a measurement of this task.

| the world says | the task says | what happens |
|---|---|---|
| surface DEAD | `required_surfaces` | **the rollout never starts.** `harness_error`, not scored, not graded, subject never handed the terminal |
| surface DEAD | `dead_by_design` (+ reason) | fine. A deliberately minimal world is not flagged forever |
| surface DEAD | nothing | the rollout RUNS - we do not know it mattered - but the report **withholds** Pass@1 until the task says which |
| surface live | anything | nothing |

`required_surfaces` may name a probe label, a probe's tool, or anything in a
probe's `covers`. Naming something no probe reaches is refused at load: a gate
that cannot be evaluated would silently pass. Requiring and declaring
dead-by-design the same surface is refused for the same reason. A
`dead_by_design` entry with no `reason` is refused - "dead on purpose" without
the purpose written down is the flag being switched off.

**The probes are a floor, not a ceiling.** Every gateway tool the task does not
declare is swept as well, called with no arguments, so a dead surface cannot hide
behind a tool nobody thought to declare. It fires: on GT001, whose task declares
14 probes, the sweep finds 10 further DEAD surfaces behind tools the task never
named (`alarms_list`, `chat_channels`, `deploy_list`, `ingest_list`,
`issues_list`, `kb_list`, `metrics_list`, ...). The error CODE decides, not the
fact of an error: `no_chat` is a fact about the world, `invalid_arguments` is a
fact about the probe, and a swept tool that needs arguments is recorded as
`unreachable`, never as dead. `dbt_run` and `dbt_test` are never swept - they
would run. Everything else that accepts `{}` is read-only: measured, 23 of the 63
tools validate against `{}` and only those two mutate. Probes run against a
throwaway copy of the world with their own audit directory, which is what makes
the sweep safe structurally rather than by naming convention.

### The plane paths, and what a "dead surface" usually means

GT004's eight dead surfaces were not a world gap. `GatewayConfig`'s defaults for
`workflow_dir`, `observability` and `ingest` are ABSOLUTE
(`/var/workflow`, `/var/obs/observability.yml`, `/var/ingest/state.json` -
`gateway/src/alkera_gateway/config.py:19-23`) and resolve outside any world root,
and neither config writer here named them. So `issues_*`, `chat_*`, `kb_*`,
`metrics_*`, `alarms_*`, `deploy_*` and `ingest_*` answered "this world has no
..." however richly the world was seeded. The world was not half absent; it was
half addressed. Both writers now pass through `episode.WORLD_PLANE_KEYS` -
`workflow_dir`, `observability`, `obs_samples`, `ingest` - whenever the world
builder publishes them, and pass nothing when it does not, so a world that
genuinely has no workflow plane still reports one absent.

`surface_gate_check.py` keeps that fixed. It stands up a world that seeds all
three planes and drives the real gateway over it twice, with and without those
keys, showing the same bytes answer `no_issue_tracker`/`no_chat`/... in one and
`ok` in the other. Then it proves the gate itself: two fixture task packages
outside the repo, differing in one line of `task.toml`, run through the real
`rollout.py` with `--driver scripted` (no model quota). One is quarantined before
the subject is handed the terminal; the other is left completely alone. Which
surfaces are live and which are dead is measured off the world first, never
hardcoded, so the check cannot decay into "require something that happens to be
live".


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
   accounts for. A `bash` row's targets are read as world-relative (every other
   tool names a workspace path), so a legitimate shell write is attributed
   rather than flagged.
6. **containment** — `--topology container` only. This rollout's own container,
   probed from the inside **before and after** the episode: no path to the
   grading zone, no route off itself, no host path visible, no marker-shaped
   file readable, and no bind mount publishing the source repository's path.
   Same discipline as detector 1 - read out of the subject's environment, never
   out of our compose file. Missing or failing evidence is a hard
   `unverified_containment`, which quarantines as **unobserved**: a statement
   about the harness, never about the subject. `blindness.guarantee()` derives
   the recorded guarantee from that evidence, so a container nobody probed
   carries `tool_surface_revoked+unverified_container` and never
   `filesystem_isolated`.

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

`report.md` and `report.json` per run. Three rates, each labelled with its
denominator AND with the live-surface set it was measured in:

- `pass_at_1` over **scored** rollouts (attempted; not quarantined; no harness
  or grader defect)
- `pass_at_1_reasoning_only` over passes + reasoning failures only - an upper
  bound on what the task measures about reasoning
- `pass_hat_k` (Pass^k): 1.0 iff EVERY one of the k scored rollouts passed. The
  consistency metric, and a cheap one - the world is byte-identical across K by
  construction. It discriminates where Pass@1 does not: on GT001, re-aggregated
  from the stored records, Claude scored Pass@1 1.0 and Pass^5 1.0 while Kimi
  scored Pass@1 0.5 and Pass^k 0.0. `k` is the SCORED count, not K, because
  counting a quarantined rollout as a failure blames the model for the harness;
  when k < K the report says the claim is weaker than the run intended. Broken
  out per model whenever a run has more than one, since a single Pass^k over two
  models says nothing about either.

**Run conditions come first, above the number.** The prompt variant (matched by
sha256 against `[prompt_variants]` - the job `prompts/variant.py identify` does
for a finished run), the live-surface set, the dead set split into *required* /
*by design* / *undeclared*, and the gate verdict. A pass rate whose world is not
described is not a result, and describing it after the reader has taken the
number is the same as not describing it.

The report then **refuses to present a Pass@1 as a measurement** when any of
these holds. The value is still computed, marked provisional, and never dropped:

- a required surface was dead
- a surface was dead that the task neither requires nor declares by design
- the task declares no probes at all, or a rollout carries no surface audit
- the live-surface set differs between rollouts of a byte-identical K
- the run mixes prompt variants, or its brief matches no locked variant
- the run contains red-team detector tests

Plus per-check pass rates, the classification counts, the quarantine count split
into *caught violating* vs *unobservable*, and median tool calls and wall clock.


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
