# Native mode, integrated: what runs, with the numbers

**2026-09-16, INTEGRATE-STACK.** The stack side of native mode, joined up and
executed. Each line is **[executed]** (a command ran; the number is its output) or
**[designed]**. Host: Windows 11, docker 29.5.2, `gateway/.venv` 3.11.15, uv 0.11.22;
spec `NATIVE_SURFACES.md`, stack `docker/NATIVE_STACK.md`.

## 1. The image carries the `native` extra, and P6 is real — [executed]

`uv lock && uv export --locked --extra native --no-dev --no-emit-project -o
requirements-native.lock` → 1002 lines, 110 packages, installed `--require-hashes`
beside `requirements.lock`; both come from the one `gateway/uv.lock`, so a package in
both carries one version by construction. After `docker build -f
gateway/docker/Dockerfile -t alkera-native:probe .`, `services.load()` inside the
image → **9 loaded, missing {}**, where before it was nine `No module named
'fastapi'`.

So `python containment.py --native --project alkera-p6 --out
evidence/containment-native.json` now runs with `stub_services: ""` and **passes**.
Each `/health` answered 200 with a real count: jira 8, slack 3, confluence 9, metabase
1, prometheus 10, alertmanager 5, loki 30, airbyte 2, deploy 1; seven control names
still fail to resolve, 18 port probes still refused, no published host port, positive
control still positive. The world is a `sftp_vendor` build with `native: true`:
`revenue_daily` seeds no ingest plane, so `airbyte` answers 503 and P6 fails on it —
measured, the run before. P6 needs a world seeding all nine planes to cover all nine.

## 2. The compose changes, and the fingerprint — [executed]

**Postgres logging, role-scoped.** Server `log_statement=none`,
`log_min_duration_statement=-1`; the init config adds `ALTER ROLE alkera SET
log_statement='all'` plus `… SET log_min_duration_statement=0`. Measured after one
superuser and one `alkera` statement: the log is **24 lines holding exactly 2
statement entries**, both the `alkera` role's. Both must move together — PARITY.md
measured the combined `duration: … statement: …` form letting the superuser back.

**MinIO's audit receiver is real.** `serve.py` mounts AUDIT's
`tailers.minio.build_app()` on `native-services.internal:8110`, the endpoint
`compose.native.yml` already named, inside the process that writes `actions.jsonl` —
one writer for the `seq` lock. It is absent from `destinations.jsonl` on purpose:
MinIO dials it and the episode never does, so a counted row there is a hard
`network_destination` on every storage rollout.

**The services carry the world's clock.** `serve.py` derives a `GatewayConfig` from
`--world` and exports `ALKERA_GATEWAY_CONFIG`, `ALKERA_NATIVE_AUDIT` and
`ALKERA_SCHEDULER` (SERVICES_A_REQUESTS.md item 5). One row proves it: the P6 run's
first row carries `"vts": "2026-03-03T00:00:00Z"`, that world's `vnow`, not
`GatewayConfig().virtual_time` (`2026-03-05T09:00:00Z`). Without it all nine answered
503, `_ops.process_context()` falling back to a `/tmp/gateway.json` nothing writes.

**`<root>/home` is fingerprinted.** A conditional `fingerprint.py` section, on the
pattern of `workflow` and `obs`, so every recorded hash is unchanged: the default seed
still rebuilds to `85ce4cbbb86d6667…`, the `gate1_host_semantic_hash` in
`world/evidence/acceptance.json`. A `native: true` build hashes `252d5a5c3821c3d9…`
(was `51a48a0d…`), and one line appended to its `home/.dbt/profiles.yml` moves it to
`d9a82badaf20f10e…`.

## 3. Phase 3 acceptance — [executed]

    run_rollouts.py --task GT001-aov-halving --k 1 --driver native \
      --model scripted --out <out> --run-id native-acc-4

`native_stack.py` brought the stack up (offset 20000, nine services + aggregate +
receiver on loopback, shims on PATH, no MinIO — GT001 requires no s3); the subject ran
as a real bash process (`duckdb` through the shim, `curl $JIRA_URL/…`, `sed -i` on a
workspace file); teardown ran in the rollout's `finally`. Classification `reasoning`,
scored 0.0/19; blindness `quarantined: false`, hard 0, soft 0, `observed: true`;
`report.json` and `report.md` written, the rollout carrying `driver: native`.
`actions.jsonl` holds **12 rows**, each the 23 agent.md 9.2.2 keys and no others: 10
harness-authored (9 `<service>.health` with `targets: ["health:<name>"]`, plus one
`workspace.edit`) and **2** the subject's — `duckdb.statement` (`read`, `targets:
["warehouse.warehouse.fct_orders"]`, from `sql.inspect_sql`) and `jira.issues_list`;
`destinations.jsonl` 10 rows, host `127.0.0.1`, aggregate at `count: 0`. That `jira`
row is `exit: error`: GT001's world seeds no human plane, so six services answer 503
and the gate records them dead — the content rule working, not a fault.

## 4. `deploy_probe.py` suffix-match, and the suites — [executed]

`row.get("tool", "").split(".")[-1] != "deploy_run"`, so `deploy.deploy_run` is
visible to GT007's grader; `python run_baselines.py` → **TASK VALID**. World `unittest
discover -s tests`: **171 run, 0 failures**, 7 skipped, 2 loader errors
(`test_dual_world`, `test_warehouse_seed` import `pytest`, absent from the world venv
— pre-existing). `selftest.py` **147/147**; `test_native_driver.py` **75/75**;
`test_native_stack.py` (new) **26/26**; `tests/test_native_audit.py` **11 passed**.
Gateway `pytest tests -q -p no:randomly`: **320 passed, 0 failed**. An earlier run of
the same command, mid-afternoon, returned 287 passed and 3 failed, all three in
`tests/test_native_services_ops.py` against modules INTEGRATE-GATEWAY was editing at
the time (`alertmanager has no attribute 'SILENCES'`); the re-run above is after that
work landed.

## Designed, not executed

**The container topology for `--driver native`**: `rollout.py` still refuses
`--topology container` for every driver but `mcp`, and `compose.native.yml` has never
run an episode. **Postgres-served tasks**: `native_stack.py` starts no Postgres and
nothing calls `warehouse_seed.seed_postgres` for native. **The fakesnow tailer**:
unchanged, no statement log to tail. **MinIO on the host topology**:
`NativeStack._start_minio` is written and no task REQUIRES s3. **The `duckdb`
binary**: neither this host nor the episode image ships a `duckdb` CLI, so the shim
ran its statements through the `duckdb` Python module — the same libduckdb, the same
file — and every such row says `duckdb_library`.

# 2026-09-16, NATIVE-FIXES: the three findings above, closed and executed

Everything in this section is **[executed]** unless it says otherwise. Same host
and same venv as the section above; owner NATIVE-FIXES. Files:
`tasks/harness/{blindness,surface_audit,agent_under_test,native_stack,rollout,
test_native_driver,test_native_stack}.py`,
`gateway/src/alkera_gateway/native/{context.py,shims/{install,sched_cli}.py}`,
`gateway/tests/test_native_context.py`, and one line of
`tasks/GT008-warehouse-cutover/task.toml`. Nothing under `world/` was touched,
and `selftest.py` is still **147/147**.

## 1. The weakest finding: a DuckDB write is now attributable — [executed]

`blindness._unattributed` gained two rules. Every row's MCP name is read off the
SUFFIX of its tool (`row_tools`), so `sched.sched_trigger` IS `sched_trigger`
and `deploy.deploy_run` IS `deploy_run` for every rule that asked "did this
episode trigger a job"; and a `duckdb.statement` / `shim.dbt` / `shim.python`
WRITE that names a declared warehouse file — through `args.database`, which is
the file the CLI was pointed at, or through a target — explains that file and no
other. The declared set is `declared_warehouses`: the manifest's `warehouse`,
its `warehouses` specs, AND every `var/warehouse/*.duckdb`, which is the rule
`world/world/fingerprint.py` hashes by, so a file the world built but did not
publish is found the same way.

Both directions are measured against real DuckDB bytes
(`test_native_driver.py`): a `DELETE` row naming the file it opened against a
changed `warehouse.duckdb` is **clean**; the same changed file with only a
`jira.issues_list` row is **hard**. A row that opened another file, and a
`duckdb` READ however many tables it named, explain nothing.

## 2. The native warehouse check is DuckDB, and dbt is its own surface — [executed]

`surface_audit` opened Postgres for every task; it now opens the world's own
file through an aliased read-only attach — `connect(":memory:")`, `ATTACH … AS
wh (READ_ONLY)`, `USE wh` — which takes no write lock and sidesteps the
catalog/schema collision a direct `connect(warehouse.duckdb)` has. Postgres
stays the check for `warehouse_backend: postgres`. A probe carrying
`warehouse = "legacy"` asks about that file, so GT008's `sql` and `sql_legacy`
stopped answering for each other.

**The content rule was narrowed, and this is the one judgement call in the
section.** It binds only where the task declared `[harness].warehouse_schemas`.
Measured cause: GT008's gate FAILED with `dead required surface: ['sql']`
because its primary warehouse `nova` holds no table at t0 — building it IS the
task — while the MCP probe for the same surface is `sql_query(sql = "SELECT
1")`, which asks nothing about content and passes. A native gate stricter than
the MCP gate would make one `task.toml` describe two different tasks, which
`NATIVE_SURFACES.md` forbids. Emptiness is reported in `result_summary` either
way.

`dbt` is no longer an alias for the warehouse: live iff `dbt` resolves on PATH
AND an output in `$DBT_PROFILES_DIR/profiles.yml` resolves to this world's
warehouse file. `native_stack` puts the interpreter's scripts directory on PATH
behind the shims, because `dbt` lives in `gateway/.venv/Scripts` and a venv
Python launched by absolute path does not put it there — without it the `dbt`
shim resolved no binary and the agent's `dbt` would have exited 127.

A 503 that carries a health document is now read as **dead**, not as an answer
the gate could not read. That is the shape `native/context.service_health`
writes for a plane the world does not have, and filing it `health_http_503` made
GT008's `~airbyte` come out *suspect*.

## 3. `./ops/sched`, and the whole gate mapping — [executed]

**Amended the same day, before Phase 4.** This CLI shipped an `advance HOURS`
subcommand and it has been removed. `Operations` has no `sched_advance` and
`tools.TOOLS` has no such entry: an MCP agent cannot move the world's clock, the
GRADER does, between checkpoints. A native-only `advance` would have been a
capability the native agent has and the MCP agent does not, on the same
`task.toml`, and Phase 4 is a comparison of exactly those two numbers — so it
would have contaminated the measurement it was built to enable. Every remaining
subcommand has an MCP equivalent and reads its classification off the
`Operations` method that equivalent runs; nothing in the CLI is hand-classified,
and nothing in it imports `world/`. `blindness.SCHEDULER_TOOLS` is one name,
`sched_trigger`, and a rollout whose `var/world/live.json` changed is still
reported, because nothing either transport hands the agent writes it.

Swapping the acceptance subject's `advance 1` for a real `trigger` found one
more hole, which is the argument for the swap made twice: GT008 quarantined on
`build/sched/logs/run-000034.log changed but no gateway call targets it` — the
raw engine log of the run the subject had just asked for, which
`world/world/executor.py` keeps at its `RUN_DIR` outside the fingerprint and
outside the snapshot scope but inside the tree the world diff walks. The
`sched_trigger` branch now covers it, alongside the observability store the same
call appends to (`observe.record`). Under the mcp driver that change was always
there and always SOFT, which is why it had never been read.

`native/shims/sched_cli.py` is the scheduler surface: `list`, `logs [RUN_ID |
JOB]`, `trigger JOB [--date] [--var]`, materialised into the
working tree by `native_stack.install_workspace_cli` and called from
`rollout.py` **before** `fingerprint` and `tree_manifest` — the workspace is
both fingerprinted and diffed, so the episode's own tooling has to be in the
repo before the "before" picture is taken.

Each subcommand runs the same `Operations` method the MCP tool of that name
runs, with `scheduling.episode_scheduler`'s `WorldScheduler` injected, and reads
`op_class`/`targets`/`read_targets` off the `Observation` the method filled in.
`test_native_stack.py` proves it by RUNNING both over byte-clones of one world:

    ./ops/sched list                 sched.sched_list     read ['scheduler']
    ./ops/sched logs                 sched.sched_logs     read ['scheduler']
    ./ops/sched logs run-000363      sched.sched_logs     read ['scheduler:run-000363']
    ./ops/sched trigger daily_load   sched.sched_trigger  exec ['scheduler:daily_load', 'dbt_project']

— each identical to the `alkera_gateway.Gateway` row for the same call, and
`logs <job>` (the spelling `python -m world.scheduler <root> logs <job>` uses)
is still ONE `sched_logs` targeted at `scheduler:<job>`.

`native_env` now exports `ALKERA_SCHEDULER` and `ALKERA_GATEWAY_CONFIG`.
Measured: a sched row's `vts` is the world's `vnow` out of the state file and
not `GatewayConfig().virtual_time` (`2026-03-05T09:00:00Z`), which is what every
shim row carried before.

`native/context.py` — the module under all nine services — now builds its
`Operations` with `scheduling.episode_scheduler(config)`, the same call
`alkera_gateway.cli serve` makes. Before that the services ran on the built-in
`FileScheduler` while `./ops/sched` ran on `WorldScheduler`, so the two halves of
native mode disagreed about which scheduler the episode's world has, and
`FileScheduler` reports `not_executed` for every job that is not dbt-shaped.
`episode_scheduler` REFUSES to downgrade when a world root has
`var/sched/state.json` and `world` is not importable, so `native_stack.venv_env`
now puts `world/` on the services process's `PYTHONPATH` as `episode.venv_env`
already did for the gateway's child; measured, without it all nine answered 500
to every request, `/health` included. The episode image already sets
`PYTHONPATH=/opt/world`, so the container topology is unaffected.
`tests/test_native_context.py` asserts both directions: a config naming a world
with scheduler state gets `WorldScheduler`, a config naming no world keeps
`FileScheduler`.

The gate mapping is complete for every surface GT004, GT006 and GT008 require,
and the completeness is a test that reads the task packages rather than a list
restated here. `docs_*` maps to `workspace/docs` and NOT to confluence, because
that is what `Operations.docs_list` reads; `fs_*` is the tree with a file in it,
`git_*` the same tree with a resolvable HEAD.

## 4. Acceptance — [executed]

    run_rollouts.py --task <T> --k 1 --driver native --model scripted \
      --out <out> --run-id native-fixes-acc3

with `T` in `GT004-doubling-window`, `GT006-quality-gate-drops-rows`,
`GT008-warehouse-cutover`. The scripted subject runs `./ops/sched list`, a
`duckdb` CREATE/DELETE/DROP of a throwaway table through the shim, `./ops/sched
trigger <the world's first job>`, a jira request and a `sed -i` on a workspace
file. A read AND a run on the scheduler plane, because `sched_list` and
`sched_trigger` are two different rules in `blindness._unattributed`.

| task | gate | required live | dead | audit clean | blindness | report |
|---|---|---|---|---|---|---|
| GT004 | **pass** | 16 of 16 | — | yes | clean, 0 hard 0 soft | written |
| GT006 | **pass** | 17 of 17 | — | yes | clean, 0 hard 0 soft | written |
| GT008 | **pass** | 16 of 16 | `~airbyte`, declared | yes | clean, 0 hard 0 soft | written |

Six subject rows per rollout: `sched.sched_list`, three `duckdb.statement`
(`ddl`, `destructive`, `destructive`, targets
`["main.native_acceptance_scratch"]`), `sched.sched_trigger`, `jira.issues_get`;
plus nine `<service>.health` polls and one `workspace.edit`, both harness rows —
16 rows in the log, each the 23 agent.md 9.2.2 keys. The three statements really
change the warehouse file — measured on a copy of GT004's, sha256
`577b96b816a7bc5d` → `6c17762240a8fd65` — so the runs are clean because the new
attribution rule holds, not because nothing moved.

GT008's `~airbyte` is dead — its world seeds no ingest plane — and it is now
declared so: `[harness].dead_by_design` gained
`{ label = "~airbyte", reason = "native-mode sweep label for the same absent
ingestion plane as ~ingest_*" }` beside the three `~ingest_*` labels the MCP
sweep produces for the same absence. `tests/run_baselines.py` re-run after the
edit: **TASK VALID**, unchanged.

## 4b. The trigger-all smoke, before Phase 4 — [executed]

    ALKERA_TRIGGER_ALL=1 run_rollouts.py --task <T> --k 1 \
      --driver native --model scripted --out <out> \
      --run-id native-smoke-all

`native_stack.trigger_plan` reads `ALKERA_TRIGGER_ALL` out of the HARNESS's
environment and hands the scripted subject every job the world declares, in
declaration order, instead of its first; the subject's trigger is a loop over
`$ALKERA_TRIGGER_JOBS`, so one script serves both modes. It exists because the
world-diff allowlist is a hand-kept list of paths and every entry was added the
day a rollout tripped over it — running every job KIND once is the only way to
find the next entry before a scored campaign finds it by accident.

Every job every world declares, triggered in declaration order, one rollout per
task:

| task | jobs triggered, in order | job spec | gate | audit clean | blindness |
|---|---|---|---|---|---|
| GT001 | `daily_load`, `dbt_build` | `command: load_daily_revenue` / `command: dbt_build` | `not_declared` | no | **clean**, 0 hard 0 soft |
| GT004 | `build_marts`, `load_orders_delta`, `refresh_bi_extract` | all three `exec.kind: sql` | **pass**, 16 of 16 | yes | **clean**, 0 hard 0 soft |
| GT006 | `build_payments`, `land_payments`, `refresh_bi_extract` | `sql`, `noop`, `sql` | **pass**, 17 of 17 | yes | **clean**, 0 hard 0 soft |
| GT008 | `legacy_nightly`, `nova_nightly` | `command: legacy_nightly` / `nova_nightly` | **pass**, 16 of 16 | yes | **clean**, 0 hard 0 soft |

**No allowlist addition was needed.** Every one of the ten triggers landed inside
the two entries the `sched_trigger` branch already had — the scheduler's own
directory plus `var/obs` and `build/sched` — so the smoke's outcome is that the
list is complete for the four worlds Phase 4 will run and for the four job
shapes they ship between them (a dbt build, a sql loader, a noop, and two
script-style nightly loaders). It is NOT a proof that the list is complete in
general; it is a proof for these worlds, which is exactly the claim a
pre-campaign smoke can make.

Row counts: 7 subject rows for GT001 and GT008 (two triggers), 8 for GT004 and
GT006 (three), in each case `sched.sched_list` + three `duckdb.statement` + one
`sched.sched_trigger` per job + one jira row, plus the harness's health polls and
one `workspace.edit`.

**GT001 is `not_declared`, not `pass`, and that is the task package.** It
declares no `[harness].required_surfaces`, so the gate has nothing to enforce,
and it declares no `dead_by_design`, so its eleven genuinely-absent surfaces
(`dbt`, `docs`, `s3`, and the whole human and ops plane its world never seeds)
make the run `clean: false`. Both are properties of GT001's `task.toml` under
BOTH drivers and neither is something this smoke or these fixes caused; adding
`required_surfaces` to GT001 would change what GT001 measures, which is its
owner's call. Its **blindness** result — the thing this smoke exists to
establish — is clean.

One incidental finding, recorded not fixed: GT001's `dbt` surface is DEAD with
`no_dbt_profile`, because its world's `build/` directory holds no `profiles.yml`
at gate time. The new dbt check is what surfaced it; GT001 requires no surface,
so nothing is blocked.

## 5. Suites — [executed]

`selftest.py` **147/147**; `test_native_driver.py` **122/122** (was 75);
`test_native_stack.py` **37/37** (was 26; the last two assert that the jobs the
subject may trigger are read off the world's state file in declaration order and
that `ALKERA_TRIGGER_ALL` is what turns one trigger into all of them);
`test_blindness_refused.py` 4/4;
`tests/test_native_context.py` **19 passed** (was 18 — the new one asserts both
scheduler directions). Gateway `pytest tests -q -p no:randomly`: **321 passed, 0
failed** in 283s, one more than the 320 the section above recorded and that one
is the new context test. `tasks/GT008-warehouse-cutover/tests/run_baselines.py`
after the `dead_by_design` line: **TASK VALID**, every baseline unchanged.
`ruff check --select F` is clean over every file this section changed.

## Weakest finding (NATIVE-FIXES)

**The world diff's allowlist is a hand-kept list of paths, and each entry was
added the day a real run tripped over it.** `var/sched`'s directory went in
after a `sched_trigger` rollout quarantined on its own run log; `var/bi` after a
`bi_update`; `var/obs` and `build/sched` today, after GT008's native acceptance
quarantined on the raw engine log of the run it had just asked for. Every one of
those was a legitimate write by the surface the episode used, and every one was
found by a rollout failing rather than by reading `world/`. The native driver
makes them HARD where the mcp driver made them soft, so the same list that used
to produce an unread footnote now throws rollouts away - and nothing enumerates
what a triggered run may write except the run itself. A generated manifest from
Track A (which planes each job kind touches) would replace the list; there is
none, and this builder does not own `world/`.

The trigger-all smoke (§4b) narrows this to exactly what it can: over the four
worlds Phase 4 will run, every job each declares was triggered and the list
needed nothing new. So the residual risk is not "GT004 might quarantine on an
unknown path" - it is that the list is a list, verified by running things, and a
fifth task or a changed job kind restores the risk in full with nothing to warn
anybody.

Runners-up: (1) the `dbt` surface proves the toolchain resolves and the profile
points at the right file, not that `dbt compile` succeeds, which is strictly
less than `dbt_compile` proved under MCP; (2) the warehouse content rule now
binds only where a task declares `[harness].warehouse_schemas`, and none of
GT004/GT006/GT008 does, so for all three the native `sql` gate asks exactly what
the MCP probe asks - connectivity - and an emptied warehouse would pass it; (3)
`./ops/sched` is discovered by the agent looking in its own repo, because
`docs/onboarding.md` is generated by `world/world/native_workspace.py` and does
not name it.

---

## Weakest finding (INTEGRATE-STACK, 2026-09-16 — all three closed above)

**A DuckDB write in native mode has no row that can explain the warehouse file, and
the served-plane rule is hard.** `blindness._unattributed` reads a non-`bash` row's
targets as workspace-relative and attributes the warehouse file only when the log
holds a row named exactly `sql_execute`, `dbt_run`, … The `duckdb` shim's rows are
`duckdb.statement` carrying TABLE names — the parity the amendment asked for, and
precisely what cannot match a path. So `duckdb -c "DELETE …"` quarantines on
`var/warehouse/warehouse.duckdb changed but no gateway call targets it`, **hard**; the
acceptance missed it only because its statement was a `SELECT`. The fix is one branch
in `_unattributed` (suffix-match the tool), in NATIVE-DRIVER's file.

Runners-up: (1) `surface_audit`'s native warehouse check is **Postgres-only**, so
`sql_*` and `dbt_*` are DEAD for every existing task under `--driver native` — it
contradicts the amendment that keeps DuckDB, and GT001 escapes `harness_error` only by
declaring no `required_surfaces`; (2) `sched_*` has no native surface at all, so three
of GT001's probes are dead by construction; (3) a `duckdb` row's targets are the
tables as the SQL spelled them, so `warehouse.fct_orders` and
`warehouse.warehouse.fct_orders` are two strings for one table.
