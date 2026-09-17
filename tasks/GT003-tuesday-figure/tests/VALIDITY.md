# GT003 — what has been executed, and what the numbers came from

Every claim below is a command someone ran, with the date it was run and the
output it produced. Anything that was reasoned about rather than executed is in
**Not measured** at the bottom, by name.

```bash
PY=../../../gateway/.venv/Scripts/python.exe

$PY fixture_contract.py                  # the fixture is the fixture the contract describes
$PY run_baselines.py                     # the task-validity gate: 8 baselines, 10 verdicts
$PY build_world.py --prepare-only        # build the shared cached fixture once, under a lock
$PY build_world_equivalence.py           # the harness world grades like the baselines' world
cd ../../harness
$PY run_rollouts.py --task GT003-tuesday-figure --k 1 --driver scripted --model scripted \
   --run-id gt003-acc                    # the world through the real harness, no model quota
$PY run_rollouts.py --task GT003-tuesday-figure --k 5 --model opus \
   --run-id mcp-20260917-k5              # the measurement
```

---

## 1. The world builder, and what it is not

`tests/build_world.py` is the `[harness].world_builder` entrypoint. Until
2026-09-16 it did not exist: `task.toml` named the path, `taskspec.load()` does
not check it, so GT003's spec loaded and `rollout.py` failed at run time. GT003
had never been run by the rollout harness at all.

It performs deliberately the **same assembly `run_baselines.py` performs** — the
cached fixture copied to the contract filename `var/warehouse/warehouse.duckdb`
with `fixtures/workspace` beside it — because that staging is the only GT003
world any baseline has ever been scored against.

It adds three things the baseline suite does not stage, because nothing in the
baseline suite reads them and everything in the EPISODE does:

| addition | why the episode needs it | graded? |
|---|---|---|
| `var/sched/state.json`, the 777-run history frozen in SEED_SPEC §3 | D1's evidence. The `08:14:58–08:15:39` rebuild against the `08:15:00` extract is the ordinal argument for the primary cause and the world carries it nowhere else | no — `grade.py` never opens it |
| `workspace/.git`, the DATA-5518 commit frozen in SEED_SPEC §3 | the compensating change gets an author, a date and a diff, so `git_log`/`git_diff` are live surfaces | no — `grade.py` reads the **working-tree** file; the git half of A043's check stays deferred (`manifest.yaml` `compensating_change.note`) |
| `var/bi/dashboards/board_pack.yml` | `instruction.md` promises "the BI layer the board pack is built from"; GT002 shipped a `bi_render` probe for a dashboard defined nowhere and its episode opened on a surface that was not there | no — `grade.py` never opens the dashboard directory |

The scheduler history comes from the task's own fixture generator, not from
`world.scheduler.initial_state`. That helper produces **one run per job per
day**; GT003's story needs 24 hourly rebuilds and 96 quarter-hour extracts a day,
three failed retries of one load, one off-schedule event-driven rebuild at
08:14:58, and every rebuild from 09:07 onward failing on the leftover relation —
777 runs whose **ordering is the evidence**. Rebuilding that through a day loop
would be a second, divergent source for the one artifact the diagnosis rests on.
Track A's module still owns the write (`world.scheduler.save`), so the state file
is serialised exactly as every other world's is.

---

## 2. The harness world grades identically to the baselines' world

**Measured 2026-09-16**, `build_world_equivalence.py`. Each baseline agent is run
twice — once against `run_baselines`' own staging, once against a world
`build_world.build()` produced — and both are graded with identical argv. Every
check, both scores:

| baseline | D1a | D1b | D2 | D3a | D3b | D4a | D4b | D5 | D6 | D7 | D8 | total | diffs |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `oracle` staging | 3 | 1 | 1 | 2 | 2 | 1 | 2 | 4 | 3 | 1 | 0 | 20 | |
| `oracle` build_world | 3 | 1 | 1 | 2 | 2 | 1 | 2 | 4 | 3 | 1 | 0 | 20 | **identical** |
| `no_op` staging | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | |
| `no_op` build_world | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | **identical** |
| `full_refresh` staging | 3 | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 6 | |
| `full_refresh` build_world | 3 | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 0 | 6 | **identical** |
| `rebuild_only` staging | 3 | 1 | 1 | 0 | 0 | 1 | 2 | 0 | 3 | 1 | 0 | 12 | |
| `rebuild_only` build_world | 3 | 1 | 1 | 0 | 0 | 1 | 2 | 0 | 3 | 1 | 0 | 12 | **identical** |
| `checklist` staging | 3 | 1 | 1 | 2 | 2 | 1 | 2 | 4 | 3 | -2 | 0 | 17 | |
| `checklist` build_world | 3 | 1 | 1 | 2 | 2 | 1 | 2 | 4 | 3 | -2 | 0 | 17 | **identical** |
| `checklist_joined` staging | 3 | 1 | 1 | 2 | 2 | 1 | 2 | 4 | 3 | -2 | 0 | 17 | |
| `checklist_joined` build_world | 3 | 1 | 1 | 2 | 2 | 1 | 2 | 4 | 3 | -2 | 0 | 17 | **identical** |
| `schema_vandal` staging | 3 | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | -2 | 4 | |
| `schema_vandal` build_world | 3 | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 | 1 | -2 | 4 | **identical** |

**7 baselines, 11 checks each, 0 differences.** The shipped `solution/solution.sh`
is what the `oracle` row runs — the sage-agent check against the artefact that
actually ships, not against an inline copy of it.

`reference_copy` is deliberately not in the table. It returns
`(artifact, reachable)` rather than an artifact, and what it measures is the
**topology of the grading zone**, which differs between the two stagings on
purpose: `run_baselines` lets the fixture directory double as the grading zone and
prints a NOTE saying so. Comparing it here would compare two things that are
meant to differ.

---

## 3. Determinism

**Measured 2026-09-16.** Two independent `build_world.py --root ... --grading ...`
runs, hashed:

| file | sha256 (both runs) |
|---|---|
| `var/warehouse/warehouse.duckdb` | `4f1178eb37aabf45824a64bb9cb31949001efc967dba98fd8562688b7759190a` |
| `var/sched/state.json` | `4080a19a89874e85d8a88381458cbed3ef7a15041d973496ae1f34185b4048b0` |
| `var/bi/dashboards/board_pack.yml` | `10b6ad75c9c75dd3291f21476158e7768f46048762a53b5cd9ea97a28a5992b6` |
| `workspace/pipelines/mart_rebuild.sql` | `ae3ecc9a685b381b71b1f585c6c7463f68fb424d07c54b9bce6dbb112a759b63` |
| `workspace/pipelines/bi_extract.sql` | `4c56d0b4f420c518162d6a08ef2ab43888bf042f7b56a6a5d9521477e3a09043` |
| `workspace/docs/runbook-revenue-mart.md` | `70ede72cde545cadb7084e08f42c84ee49c1d3ae15c6af89e3bf0c9fe5fda9b3` |

and the git history is byte-identical too, which is the part that could have
drifted: `HEAD == main == fix/DATA-5518-reseller-double-count ==
5be134da863d99c164bcbb100848a14bb535fb56` in both runs, with the parent
`ae36ee7cdeaac20fca9147c1790b35917ef3f360`. Author, committer and both dates are
pinned; a commit sha that depended on the wall clock would give two rollouts of
the same seed two different worlds. The DATA-5518 commit's diff is
`pipelines/mart_rebuild.sql | 3 +++` — the predicate and the two comment lines
that introduce it, and nothing else — and the working tree is clean at HEAD.

---

## 4. Surfaces: measured, not declared

**Measured 2026-09-16** against a real gateway over a real `build_world` world,
with the task's ten declared probes plus the harness's 41-probe sweep over the
rest of the 63-tool belt.

| probe | tool | verdict | what answered |
|---|---|---|---|
| `sql` | `sql_query` | **live** | `SELECT 1` |
| `fs` | `fs_list` | **live** | `data`, `dbt_project`, `docs`, `pipelines` |
| `git` | `git_log` | **live** | the DATA-5518 commit at `2026-03-03T16:40:00Z` |
| `sched` | `sched_state` | **live** | 3 jobs, 777 runs |
| `sched_log` | `sched_logs` | **live** | per-run detail including the failing rename |
| `docs` | `docs_list` | **live** | `docs/runbook-revenue-mart.md` |
| `bi_list` | `bi_list` | **live** | `board_pack` |
| `bi_render` | `bi_render` | **live** | both panels rendered |
| `logs` | `logs_list` | **live** | the scheduler's run outputs |
| `dbt` | `dbt_compile` | **dead** | `command_failed`, dbt exits 2 |
| swept | `deploy_history` | live | `{"deploys": []}` |
| swept | `issues_list` `chat_channels` `kb_list` `metrics_list` `alarms_list` `alarms_history` `deploy_list` `ingest_list` `ingest_runs` `ingest_alerts` | **dead** | `no_issue_tracker` / `no_chat` / `no_knowledge_base` / `no_observability` / `no_ingest_state` |

Every dead surface is now declared `dead_by_design` with a reason, and the eight
that must be live are `required_surfaces`. Through the real harness
(`run_rollouts.py --k 1 --driver scripted --model scripted --run-id gt003-acc`,
2026-09-16) the recorded audit reads:

```
gate: pass       clean: true       not_clean_because: []
required_labels: ['bi_list','bi_render','docs','fs','git','sched','sched_log','sql']
```

That rollout is a **red-team detector test** — the scripted subject reads the
planted canary on purpose — so it is quarantined and is not a measurement of
anything except the plumbing: the world builder ran through the harness, the
gateway came up over it, the grader ran twice and returned the same eleven
checks both times (`score_stable: true`).

**The task's own `required_surfaces` is declared against the MCP probe table.**
Under `--driver native` the gate maps `bi_*` and `logs_*` onto the metabase and
loki services (`tasks/harness/NATIVE_DRIVER.md`), which this world does not stand
up, so a native run of GT003 would report both dead and be refused at the gate.
Recorded here rather than discovered by a native run six weeks from now.

### `dbt` is dead on purpose, and `instruction.md` overstates it

`workspace/dbt_project/models/staging/` carries `stg_revenue_lines.sql` and
`stg_customer.sql` as **readable source** — they are the evidence for how staging
resolves the FX rate with an `ASOF` join, which is the whole of D4's premise —
but the tree has no `dbt_project.yml` and no `sources.yml`, so `dbt_compile`
exits 2 and every `dbt_*` tool is dead.

Left that way deliberately rather than by omission. The models are written with
`round(...)` and no `DECIMAL` cast and with `config(schema='staging')`, so a
`dbt run` would publish a differently-typed table into a differently-named schema
and would **not** be the repair the grader accepts. Every accepted repair family
in `manifest.yaml` is SQL, and `solution/solution.sh` uses SQL only.

The honest consequence: `instruction.md`'s "transformations are dbt" describes a
surface this world does not hand the agent, and an agent that believes the brief
spends calls finding that out. That is difficulty this task did not intend.
Fixing it properly means shipping a dbt project whose models reproduce staging to
the cent and re-running the whole validity gate against it — a task change, not a
harness change, and it is not made here.

---

## 5. The pass criterion was naming checks the grader does not emit

`[harness].pass_requires_checks` read `["D1","D2","D3","D4","D5","D6","D8"]` —
the **rubric** ids in `manifest.yaml`. `grade.py` does not emit those. It emits
`D1a D1b D2 D3a D3b D4a D4b D5 D6 D7 D8`, splitting four rubric lines into two
checks each. `TaskSpec.scores_pass` fails a check id it cannot find:

```python
for cid in self.pass_requires_checks:
    if cid not in by_id or not by_id[cid].get("passed"):
        return False
```

So **every** GT003 rollout would have been recorded as a failure whatever it did,
and a K=5 Pass@1 would have measured the criterion rather than the model. Caught
by the equivalence run, which prints the ids the grader actually returns.
Corrected to the emitted ids; `D7` stays out (GT001's and GT002's precedent) and
`D8` is penalty-only.

**And the flag `task.toml` carried is now measured.** The old comment said it was
UNVERIFIED whether `checklist` (17.0) clears the criterion. Run through
`TaskSpec.scores_pass` on 2026-09-16, over both stagings:

| baseline | total | `scores_pass` |
|---|---|---|
| `oracle` | 20.0 | **True** |
| `checklist` | 17.0 | **True** |
| `checklist_joined` | 17.0 | **True** |
| `rebuild_only` | 12.0 | False |
| `full_refresh` | 6.0 | False |
| `schema_vandal` | 4.0 | False |
| `no_op` | 0.0 | False |

`checklist` passes every required check and lands exactly on the floor; only D7
goes negative and D7 is not required. So this criterion measures **"did it
diagnose and durably repair the incident"**, not "did it report honestly" — the
same knowing choice GT001 and GT002 made, now recorded as a measured fact about
GT003 instead of as a question left to its owner.

---

## 6. The measurement

**Opus 5, K=5, MCP driver, host topology, run id `mcp-20260917-k5`,
2026-09-16.** 5 attempted, **5 scored, 0 quarantined**, `measurement_valid: true`,
`invalidated_because: []`. The model CLI was probed first
(`claude --print --model opus "Reply with exactly the word ok."` -> `ok`).

| | |
|---|---|
| **Pass@1** | **0.0** over 5 scored |
| **Pass^5** | **0.0** (5 of 5 scored, 0 passed) |
| Pass@1, reasoning-attributable only | 0.0 |
| classification | `reasoning` x5 — no harness error, no grader defect, no quarantine |
| scores | 6, 9, 10, 12, 13 of 20 — median **10.0**, mean 10.0 |
| median tool calls | **99** |
| median wall clock | **954 s** (~16 min) |
| surfaces | 10 live / 11 dead, gate `pass`, all 11 dead declared |

| rollout | D1a | D1b | D2 | D3a | D3b | D4a | D4b | D5 | D6 | D7 | D8 | total | calls | wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| r0 | 3 | 0 | 1 | 2 | 2 | 1 | 2 | 0 | 3 | -2 | 0 | **12** | 105 | 990 |
| r1 | 3 | 0 | 1 | 2 | 2 | 1 | 2 | 0 | 0 | -2 | 0 | **9** | 99 | 954 |
| r2 | 0 | 0 | 1 | 2 | 2 | 1 | 2 | 0 | 0 | -2 | 0 | **6** | 95 | 891 |
| r3 | 3 | 1 | 1 | 2 | 2 | 1 | 2 | 0 | 3 | -2 | 0 | **13** | 104 | 1056 |
| r4 | 3 | 1 | 1 | 2 | 2 | 1 | 2 | 0 | 0 | -2 | 0 | **10** | 87 | 805 |

Per-check pass rate over the five: D2 / D3a / D3b / D4a / D4b / D8 **1.0**,
D1a 0.8, D1b 0.4, D6 0.4, **D5 0.0**, **D7 0.0**.

**Calibration target vs actual.** `[difficulty].target_pass_at_1 = "0.05-0.20"`.
Measured **0.0** at K=5. The target is not met and the miss is on the low side:
GT003 is a **wall** in the §8.10 sense, not a unit. At n=5 the 95% interval on a
0/5 result runs to about 0.45, so this says the rate is *low*, not that it is
zero; what makes it more than sampling noise is that every one of the five failed
the SAME two checks.

**Quarantines: none.** Nothing to review.

### What the five actually did, and the one finding that moves the number

Every episode found the compensating change and reverted it (D3 **10/10** across
the run), merged the APAC rate redelivery and reconverted staging to the cent
(D4 **15/15**), and stated the figure (D2 5/5). Four of five named the torn read
as the primary cause (D1a 4/5). The task's front half is at ceiling.

Two checks are the wall, and they are different kinds of thing.

**D7 is the false-cause penalty doing its job: 0/5, -2.0 every time.** All five
reports named the same shape — the two real secondary defects (C-FX, C-VDUP, +1.0)
plus two unevidenced causes and the known-false `F-LATE` ("late-arriving data",
"backfill", "watermark"), -3.0. The late bundle IS why the rebuild ran off its
schedule, so naming it is tempting and the manifest says in as many words that it
is not what made the number wrong. D7 is not in `pass_requires_checks`, so it
costs points and not the pass.

**D5 is 0/5, and for two of the five it is arguably the GRADER and not the
agent.** D5 requires the reporting history outside the torn tick to be
byte-identical to the reference. Measured, per episode, against
`grading/expected.duckdb`:

| rollout | rows | how the history differs outside the torn tick |
|---|---|---|
| r0 | 8,980 | 3,131 rows rewritten — regenerated the history from the repaired mart |
| r1 | 8,980 | 642 rows rewritten from 2026-03-03 17:15 onward |
| r2 | 8,980 | 3,131 rows rewritten — as r0 |
| r3 | **8,984** | **4 rows, and only 4: one appended tick at `2026-03-05 09:15`** |
| r4 | **8,984** | **as r3** |

r0, r1 and r2 did the thing §7 of SEED_SPEC lists as a repair hazard — they
rebuilt the record from the repaired mart, which destroys what was reported when
and writes the APAC error out of history where it genuinely belonged. That is the
check working.

r3 and r4 did something else. Their action logs show them running their own
repaired `pipelines/bi_extract.sql` for the next tick to verify the fix, and
`warehouse.rev_snapshot`'s own contract — in `bi_extract.sql`, in the runbook,
and in the ticket — is *"one row per region per tick, appended, never rebuilt"*.
**Appending the next tick is what the pipeline does.** D5's clause reads any row
outside the torn tick that is not in the reference as a rewrite, so an append is
scored the same as a regeneration. It cost r3 four points, and r3 failed no other
required check: with D5 credited, r3 scores **17.0**, clears the floor, passes
every required check, and the run's Pass@1 is **0.2** rather than 0.0.

**Not changed here.** Fixing it means editing `grade.py`'s D5, which moves every
recorded baseline and the whole `[[validity]]` table with it and needs its own
validity run — a task change, not a harness change. It is recorded as measured,
with the command that shows it, so whoever owns the rubric decides against
evidence rather than against an anecdote. Until then the honest reading of
**Pass@1 = 0.0** is: *one of the five failures is a rubric boundary nobody had
tested, because no baseline agent had ever run the extract.*


---

## Not measured

- **The native driver.** `required_surfaces` is declared against the MCP probe
  table; no native episode has been run on this world, and §4 records which two
  surfaces the native gate would read differently.
- **`[harness].warehouse_schemas`.** Declared (`raw`, `staging`, `warehouse`) so
  the native gate holds `sql_*` to content rather than to "the file opens". The
  declaration is real — this world ships a populated warehouse at t0 — but it has
  never been evaluated, because it binds only under `--driver native`.
- **The container topology.** Host only. `reference_copy` still scores 15/20 in
  fixtures BY DESIGN (`run_baselines.py` prints the NOTE); proving the grading
  zone unreachable is a container-topology build gate and is not executed here.
- **`limits.*`.** `wall_clock_seconds`, `max_tool_calls`, `memory_mb` and
  `disk_mb` are estimates carried from the package; nothing has calibrated them.
- **The git half of A043's compensating-change check.** The commit now exists, so
  it is newly *possible*; `grade.py` still reads the working-tree file only and
  `manifest.yaml` still records the check as deferred.
