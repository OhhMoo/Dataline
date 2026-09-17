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
| `cli` | `claude --print` | Bash + a `gw.sh` shim | `detection_only` |
| `native` | `claude --print`, or a scripted shell with `--model scripted` | Bash and nothing else - an EMPTY mcp config, vendor-shaped services found through `.envrc` and the environment, PATH-front shims for `duckdb`/`dbt`/`python`/`git` | `detection_only` + `network_destination` (every connection is logged server-side; an absent `destinations.jsonl` quarantines) |
| `scripted` | `redteam/scripted_subject.py` | n/a | detector tests only |

`native` under `--topology host` brings its own stack up per rollout
(`native_stack.py`): the nine services on loopback at their own port plus an
offset, the shims on PATH, MinIO in docker only when the task REQUIRES the s3
surface, and teardown in the rollout's `finally`. Pass `--native-endpoints` to
point at a stack you are running yourself instead. `--topology container` is
still refused for every driver but `mcp`. Spec `gateway/NATIVE_SURFACES.md`;
what is executed and what is designed, `gateway/NATIVE_INTEGRATION.md`.

## Topology: where the WORLD and the TOOLS run

The driver says where the SUBJECT runs. `--topology` says where everything it
touches runs, and that is the axis that decides whether a terminal is safe.

| topology | world + gateway + audit | terminal | blindness guarantee |
|---|---|---|---|
| `host` (default) | a host subprocess, beside the grading zone | none | `tool_surface_revoked` |
| `container` | the `episode` service of `gateway/docker/compose.yml` | **yes**, `mcp__alkera__bash` | `filesystem_isolated` |

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
