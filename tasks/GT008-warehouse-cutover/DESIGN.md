# GT008 - design contract: the cross-warehouse cutover

**Candidate 1 of `implementation_structure/TASK_DESIGN_2026-09-15.md` ("a repair
that crosses a moving cutoff"), built as a migration.** The backfill is the
migration, the moving cutoff is the dual-run window. The task is: *migrate a
revenue mart from the legacy warehouse to a new one while the source keeps
delivering* - build the target, dual-run, reconcile, cut the consumer over,
retire the old writer. Not "write the models"; run the migration.

Everything below marked **measured** was executed on this host (DuckDB 1.5.5,
Python 3.11.15, `gateway/.venv`) on 2026-09-15 or, where the line says so, on
2026-09-16. Everything marked **designed** was not. `tests/VALIDITY.md` carries
every 2026-09-16 measurement with the command that produced it.

## Why this shape

Five difficulty hypotheses have been tested and four falsified (agent.md 8.10):
conjunction, prompt vagueness, concealment, an authoritative wrong claim. The
only two non-ceiling tasks share one property that is not informational: **a
second obligation that appears after the first looks discharged**. GT008 has
that property structurally, not narratively:

* Obligation 1 - build nova, reconcile it with legacy, cut Finance over, retire
  legacy - is fully dischargeable at t0, and every check an agent can run at t0
  says it is done. `EXCEPT ALL` both ways returns zero. The equality canary is
  clean.
* Obligation 2 - keep nova right as the source keeps delivering - is only
  observable **after the world moves**, and the world moves in the grader: four
  planted nights, one `world.scheduler.advance` each, the agent's own schedule
  executed for real.

The obvious build - port `pipelines/load_legacy.py` to nova unchanged, which
the tracker recommends in so many words (DATA-8104) - discharges obligation 1
exactly and fails obligation 2 on the second morning. **Measured**
(`tests/run_baselines.py`, baseline `port_legacy_loader`, 2026-09-16): C0 and
C1 full, C2 C3 C4 zero, OPERATIONS full, EVIDENCE full, **total 12/20**, does
not pass. (An earlier draft of this line said 10/20; 12.0 is what the grader
prints.)

## The engine pair, and why

**v1 is DuckDB -> DuckDB.** Primary `nova` at `var/warehouse/warehouse.duckdb`
(`warehouse_name: "nova"`); peer `legacy` at `var/warehouse/legacy.duckdb`,
`attach: true`, `read_only: false`.

Why not DuckDB -> Postgres or DuckDB -> Snowflake today, **measured**:

* no Postgres server is running on this host (`docker ps`: only `minio` and an
  unrelated container; `ALKERA_POSTGRES_DSN` empty);
* `fakesnow` is not importable from `gateway/.venv`;
* `duckdb_extensions()` reports `postgres_scanner installed=True loaded=False`,
  so a Postgres peer *could* attach here once a server exists.

So the instinct in the brief - design for Snowflake, build and validate on
something that exists - is right, with one correction: **the pair that can be
built *and validated* today is DuckDB -> DuckDB**, not DuckDB -> Postgres. What
DuckDB -> DuckDB buys: the consumer can be a **view in nova over the attached
legacy catalog** (measured: a persistent view whose body names
`legacy.warehouse.rpt_revenue_daily` is created, survives
`enable_external_access=false; lock_configuration=true`, and binds
`Catalog "legacy" does not exist!` when legacy is not attached), so "repoint the
consumer" is a one-statement deploy with an equality canary and the grader can
read the pointer out of `duckdb_views()`. Cross-catalog reconciliation is one
`sql_query`.

Why nova is the primary and legacy the peer, not the other way round: BI,
observability and the deploy surface all bind to the primary. The consumer
(Finance's dashboard), the canary that watches it and the deploy that moves it
must live where the platform's control planes already point - the new
warehouse - and reach the old one through the attached catalog. The
alternative (legacy primary, nova peer) leaves the deploy surface unable to
touch anything in nova.

**What changes for a Snowflake target (designed, not built).** The primary
must stay DuckDB to attach anything, so nova-as-Snowflake means the *legacy*
warehouse is the primary and nova is a non-attached peer. Then: the consumer
cannot be a cross-catalog view; it has to be a scheduled export script that
reads a warehouse named in a config file, and "repointed" is read from that
file plus the export's output after the advance. The canary has to watch the
export's output table in the primary. Reconciliation cannot be one statement:
`fct_orders` is 1,131 rows at t0 and 1,219 after night 2, above `max_rows`
(1000), so the agent has to compare **group-by-day sums (31-33 rows) plus a
per-day `hash_agg`/count of order_ids**, or keyed batches of at most 1000. That
bound is real even in v1: a full-table pull of `fct_orders` through
`sql_query` is refused at 1000 rows today (`fixture_contract._problems_legacy`
requires `fct_orders > 1000` so the argument stays true). And the engine
divergences the brief measured on fakesnow become live: `COUNT_IF` over an
empty set is NULL on DuckDB and 0 on Snowflake (measured here: `count_if` over
`WHERE false` -> `None`), so the empty night 4 is where a target-side aggregate
written with `COUNT_IF` diverges from a legacy-side one. In v1 there is **no
engine divergence** - both ends are DuckDB - and I say so rather than plant a
fake one. The divergences v1 grades are semantic: version precedence,
deletion, adjustments, staleness.

## The world

`workspace/` is the `revenue-mart` repo:

| path | what |
|---|---|
| `pipelines/load_legacy.py` | the old writer. Full reload nightly. **Current = most recently delivered event** (`ORDER BY delivered_dt DESC, emitted_at DESC`). Handles deletes. Applies approved adjustments. |
| `pipelines/load_nova.py` | a placeholder that exits 0 |
| `orchestration/schedule.yml` | `legacy_nightly` 02:00, `nova_nightly` 02:30; `enabled:`, `script:`, `args:` with `{db}` `{workspace}` `{root}` tokens; the agent may add, retire or move jobs. Both jobs also carry `publishes:` - what the job hands to Finance - from which the WORLD writes the receipts ledger C5 audits |
| `data/orders_feed/dt=*/events.csv` | 30 partitions, 1,209 events, 58 in-order corrections, 10 deletes, **0 out-of-order arrivals** |
| `data/finance/adjustments.csv` | 18 rows, 13 approved, one approved against a deleted order |
| `docs/source_contract_orders_v2.md` | section 3: highest version wins; delete at the highest version removes; re-deliveries are byte-identical and may land late; consumers must apply version precedence. Section 4: approved adjustments only, against current orders only |
| `docs/migration_nova.md` | the spec: both tables, columns, the six phases, "exactly one writer per warehouse" |

Legacy holds `raw.order_events`, `raw.finance_adjustments`,
`warehouse.fct_orders` (1,131), `warehouse.rpt_revenue_daily` (31 days, net
228,237.92) - **built by running `load_legacy.py`**, so the shipped mart cannot
disagree with its loader. Nova holds one object: the view
`warehouse.v_revenue_daily` over `legacy.warehouse.rpt_revenue_daily`.

The human corpus (`fixtures/workflow`): DATA-8110 the migration ticket with
acceptance criteria; **DATA-8104 the interference** ("port load_legacy.py to
nova unchanged; it has been right for a year; change one thing at a time" -
every sentence true, the conclusion wrong by the contract); DATA-8102 the order
service's re-delivery notice, open and unassigned; DATA-8095 the legacy host's
decommission. The wiki's cutover how-to states the six phases and no figure
from this world.

The deployment plane (`fixtures/obs/definitions.yaml`): metrics over the
consumer view (days, orders, net) and the nova mart; alarms `consumer_empty`,
`consumer_thin`; deploys `revenue_consumer_cutover` (view -> nova, **equality
canary**: `max_decrease_* 0` and `max_increase_* 0` on days, orders, net),
`revenue_consumer_rollback` (view -> legacy, decrease-only canary so restoring
does not breach), `revenue_consumer_cutover_legacy` (same statement, no canary
- the `permanent_skip` tool). The equality canary is why a mart with
resurrected orders or missing adjustments breaches on the way in: measured,
`ignored_deletion` and `forgot_adjustments` both come back `breached`.

## The event ledger and the oracle

The feed is an immutable ledger: `event_id, order_id, version, event_type
(upsert|delete), order_day, customer_id, amount, emitted_at`, one partition
per day, never rewritten. History is generated by `make_fixture_world.Ledger`
with one PRNG per `(seed, day_index)`; every history event arrives in version
order, which is the precondition of the trap and is verified
(`history_out_of_order == 0`).

**The oracle** (`make_fixture_world.fold` / `apply_adjustments` /
`oracle_mart`) is pure Python, uses no SQL, and is independent of both loaders:

1. for each `order_id` keep the highest-`version` event; two events with the
   same version must be identical (else `ContractViolation`);
2. an order whose highest version is a `delete` is absent;
3. adjustments: `status == approved` and the order is current, else ignored;
4. `rpt_revenue_daily`: per `order_day` of the *current* version, `orders =
   count`, `gross = sum(amount)`, `adjustments = sum(delta)`, `net = gross +
   adjustments`, all `Decimal`, quantised to the cent.

Its answer is frozen into the grading zone at five checkpoints
(`expected/checkpoint_k.json`) and `fixture_contract._problems_oracle` re-folds
the ledger on every `verify()` and refuses a reference that is not what the
fold says.

**The four planted nights** (grading zone only; `verify()` refuses a fixture
where one has leaked into the feed):

| night | dt | delivers | who gets it wrong |
|---|---|---|---|
| 1 | 05-13 | 45 new orders; v2 of A (amount x1.30); v2 of B moving `order_day` 05-11 -> 05-09; Finance approves a pending row and a new one | a snapshot; a loader that does not re-read the adjustments file; a per-day incremental aggregate that does not recompute the day B left |
| 2 | 05-14 | 45 new orders; **a byte-identical replay of A's v1**; delete of C; **v3 of D** | `load_legacy.py` ported (A regresses to v1); a loader that ignores deletes |
| 3 | 05-15 | one event: **v2 of D**, after its v3 | anything that takes the last-delivered event as current |
| 4 | 05-16 | header only | a loader that fails on an empty partition; on Snowflake, a `COUNT_IF` aggregate |

**Measured** (`fixture_contract._problems_mechanism`, on every `verify()`): the
shipped legacy loader, run over the plants, agrees with the oracle at t0 and
after night 1, and disagrees on `[0, 1, 2, 2]` days after nights 1-4 - A on
night 2 (`201.80` vs `262.34`), D on night 3 (`52.89` vs `33.06`). Checkpoints
2, 3 and 4 are identical *by the contract* (night 3 must change nothing, night
4 is empty) and not under the legacy loader.

## The phase gates and the rubric

| phase | gate | what it catches |
|---|---|---|
| baseline | S1: the report's `baseline` equals legacy at t0 (31 / 1,131 / 228,237.92 +/- 0.01) | cannot prove losslessness later |
| build + reconcile @t0 | **C0**: nova's two tables equal the oracle's checkpoint 0, row for row, to the cent | rows match, decimals do not (`forgot_adjustments`); resurrected deletes (`ignored_deletion`) |
| advance | the grader lands one partition, advances 24h, reads nova; four times | - |
| divergence | **C1 C2 C3 C4**: nova equals the oracle after each night | reconciled once, called it done (`snapshot_only`); the ported loader (`port_legacy_loader`); ignored deletion; a loader that dies on an empty file |
| cutover | **D2.repointed**: the view reads nova and after the advance the dashboard shows what nova holds; **D2.canaried**: the last canaried `deploy_run` is a cutover deploy, `clean`, live | repointed by hand; through the uncanaried deploy; rollback left standing; breach left standing |
| decommission | **D2.retired**: legacy's logical hash (`world.warehouse_seed.duckdb_logical`) is unchanged across the four nights | "forgot to turn it off" (`cutover_without_decommission`, `mirror_legacy_nightly`) |

Points: CORRECTNESS C0 2, C1 2, C2 3, C3 3, C4 2 = **12 (60%)**; OPERATIONS D2
= **6 (30%)**, 2 per clause; EVIDENCE S1 1, S2 1 = **2 (10%)**, excluded from
pass/fail; HYGIENE H1 penalty -2 for `reconciled_at_cutover: true` while C0
fails; AUDITABILITY C5 penalty -2, **not evaluable yet** (see below). Pass =
C0..C4 and D2 all passed and total >= 18 (exactly CORRECTNESS + OPERATIONS).
Adding C5 changed no total: it carries 0 points and 20 is still the maximum.

**Every check-shaped clause has a liveness pair.** D2 is gated to zero unless
the nova mart *changed* across night 1 (which delivers new orders) and a
nova-writing job ran with `executed: true, status: success` every night; that
is what stops `disabled_load` and `snapshot_only` from collecting `retired`.
The `permanent_skip` gate is GT007's, reused verbatim: a standing canary whose
guards compared nothing zeroes the axis (`blind_canary`). C1..C4 are judged on
content, never on a job's reported status. Nothing an idle agent satisfies
pays: `no_op` scores 0.0. The rubric floor is below no_op: `checklist` scores
**-1.0** (measured 2026-09-16: H1's -2 for a false reconciliation claim, plus
the +1 of S2, which it earns honestly by naming the right deploy and the right
job - an earlier draft of this file said -2.0, which was never what the grader
produced).

**Axis separation is measured, not asserted.** Six baselines make the same
correct loader and differ only in how they operated the cutover; all six score
CORRECTNESS 12 and only OPERATIONS moves (0, 2, 2, 4, 4, 4). Conversely
`port_legacy_loader` scores OPERATIONS 6 with CORRECTNESS 4. `repointed`
compares the dashboard to *what nova holds* after the advance, not to the
oracle, precisely so a wrong-but-live target does not bleed into D2.

## Baselines (measured 2026-09-16, seed 8008)

Every baseline builds its own world through `tests/build_world.py`, acts through
a **real two-warehouse gateway** (`tests/probes/dual_gateway.py`: `warehouses`
declared, `WorldScheduler` injected, so `sched_trigger` really runs the loader
and `deploy_run` really canaries), and is scored by `tests/grade.py` as a
subprocess with the argv `task.toml` declares.

| baseline | total | C0 C1 C2 C3 C4 | D2 | pass | what the row proves |
|---|---|---|---|---|---|
| `oracle` | **20.0** | 2 2 3 3 2 | 6.0 | **PASS** | the reference solution reaches max through the tool surface |
| `rolled_back_then_fixed` | **20.0** | 2 2 3 3 2 | 6.0 | **PASS** | the positive control: acting on a breached canary is not penalised |
| `cutover_without_decommission` | 18.0 | 2 2 3 3 2 | 4.0 | fail | exactly AT `pass_min_total`, and still fails - D2 is required, so EVIDENCE cannot buy a pass |
| `cutover_by_hand` | 18.0 | 2 2 3 3 2 | 4.0 | fail | `sql_execute` is not a rollout |
| `cutover_uncanaried` | 17.0 | 2 2 3 3 2 | 4.0 | fail | the deploy that declares no canary reports `clean` having measured nothing |
| `decommission_before_repoint` | 16.0 | 2 2 3 3 2 | 2.0 | fail | Finance is left on a warehouse nobody writes |
| `blind_rollback` | 16.0 | 2 2 3 3 2 | 2.0 | fail | cut over, then rolled back "to be safe": same outcome, from the other direction |
| `blind_canary` | 14.0 | 2 2 3 3 2 | 0.0 | fail | `permanent_skip`: the axis is ZERO, not reduced |
| `cutover_breached` | 14.0 | 0 2 3 3 2 | 4.0 | fail | a breach left standing does not pay `canaried` |
| `port_legacy_loader` | 12.0 | **2 2** 0 0 0 | 6.0 | fail | **THE mechanism.** DATA-8104's advice, followed exactly. Right on the day, right on the first morning, wrong from the second |
| `mirror_legacy_nightly` | 10.0 | 2 2 0 0 0 | 4.0 | fail | copying legacy's tables nightly is a double-write, and from night 2 it copies legacy's wrong answer |
| `snapshot_only` | 4.0 | 2 0 0 0 0 | 0.0 | fail | `reconciled_once_then_cut_over` in its purest form: exact at t0, frozen after |
| `full_refresh` | 4.0 | 2 0 0 0 0 | 0.0 | fail | the reload IS full and the precedence rule IS right - the SOURCE is the old warehouse's raw layer. Its job reports success every night |
| `disabled_load` | 4.0 | 2 0 0 0 0 | 0.0 | fail | switch everything off: liveness gates D2 to zero |
| `ignored_deletion` | 4.0 | 0 0 0 0 0 | 4.0 | fail | ten resurrected orders; the equality canary breaches on the way in |
| `forgot_adjustments` | 4.0 | 0 0 0 0 0 | 4.0 | fail | same days, same order counts, different net |
| `report_only` | 2.0 | 0 0 0 0 0 | 0.0 | fail | writing down the right answer is not doing the work |
| `no_op` | 0.0 | 0 0 0 0 0 | 0.0 | fail | nothing an idle agent satisfies pays |
| `checklist` | **-1.0** | 0 0 0 0 0 | 0.0 | fail | below `no_op`: H1's -2 for a false reconciliation claim, plus the +1 of S2 |

Two baselines reach max and seventeen do not. Nothing sits between 18 and 20:
`cutover_without_decommission` and `cutover_by_hand` are at the floor and fail
on the required check, which is the property `pass_min_total = 18.0` exists to
keep honest.

## The reconciliation bound

`fct_orders` is 1,131 rows at t0 and `max_rows` is 1000: a full-table pull
through `sql_query` is not possible in one call even here, and the harness's
1 MiB payload ceiling is the other bound. In v1 the agent does not need one:
DuckDB -> DuckDB attaches, so `SELECT * FROM warehouse.fct_orders EXCEPT ALL
SELECT * FROM legacy.warehouse.fct_orders` is one statement and returns the
diff, not the tables (the oracle solution does exactly this, both ways, both
tables). For a non-attached target the feasible reconciliation is: 31-33
group-by-day rows of `(orders, gross, adjustments, net)` from each side plus a
per-day order-id digest, or keyed batches of <= 1000 `order_id`s. A task that
expects 1,131 rows to be diffed through the tool surface is not solvable, so
the Snowflake version must ship that recipe in its runbook.

## Determinism

* one PRNG per `(seed, day_index)`; **measured**: two independent builds
  produce identical content digests for both warehouses, the workspace, the
  feed, the plants, the reference and the corpus;
* money is `DECIMAL(12,2)` in every table and `Decimal` in the oracle; the
  grader quantises the agent's values to the cent before comparing, so a
  `DOUBLE` column that rounds correctly is not marked wrong;
* every text file is written with `newline=""`; the workflow assembler refuses
  a corpus with a timestamp at or after the clock or a message out of order
  (it did, once, during authoring);
* warehouses are compared by per-table content digest, never by `.duckdb`
  bytes; `world/world/fingerprint.py` hashes `legacy.duckdb` in its
  `warehouses` section;
* no gate reads a wall clock; the grader's advance is
  `CONTRACT.advance_hours_per_night` x `CONTRACT.plant_nights` of virtual time,
  one partition landed before each;
* `fixture_contract.py --falsify` plants 33 damages and requires `verify()` to
  catch every one, 23 of them by a **named clause** - **measured 33/33** (the
  33rd, added 2026-09-16, strips the `publishes:` annotation off the nova job)
  and 32/32 on 2026-09-15 after
  the leaked-plant mutation's crash was fixed (it had been 31/32: the mechanism
  check crashed on a pre-existing partition instead of letting the F121 clause
  report it).

## Anti-exploit rules carried over

* **F121** - the reference and the plants live in the grading zone only;
  `write_grading_zone` refuses a zone inside the world root;
  `run_baselines.structural_gates` rglobs the world root for `expected`,
  `next_nights`, `manifest.yaml`.
* **F622** - both warehouses are staged at their contract filenames, where the
  DuckDB catalog name and the `warehouse` schema name collide; every read is an
  aliased ATTACH.
* **Do not pay for a fatal clause.** The definitions digest, the deploy
  record's hash, the canary's liveness and the target's liveness all gate.
* **Do not make the cutover canary one-sided.** `verify()` refuses it: a
  decrease-only guard lets a mart with ten resurrected orders through.
* **Do not put an out-of-order arrival into history.** `verify()` refuses it:
  the legacy mart would be wrong at t0, reconciliation could not be exact, and
  the second obligation would be visible before the world moves.
* **Do not "fix" `load_legacy.py`.** `verify()` refuses a legacy loader that
  applies version precedence: the mechanism is that the world's working code
  is wrong by the contract.

## The substrate this task needed - LANDED (measured 2026-09-16)

`tests/build_world.py` publishes `warehouse_name` and `warehouses` in its
manifest, and when this package was written `tasks/harness/episode.py`'s
`write_gateway_config` passed only `WORLD_PLANE_KEYS` plus four fixed keys, so
the harness would have stood a **one-warehouse** gateway up over this world:
`sql_query` with no `warehouse` field and `bi_render` binding
`Catalog "legacy" does not exist!`. Both are in `required_surfaces`, so the
harness would have refused the rollout. DESIGN.md carried the required patch
and the task declared `harness_patch_required`.

**That substrate has since landed and this task no longer patches anything.**
`episode.py` now carries
`WORLD_WAREHOUSE_KEYS = ("warehouse_name", "warehouses", "initial_world_fp")`
into the gateway config and re-roots a DuckDB peer's `path` onto the probe copy
(`reroot_warehouses`), which is exactly the two hunks this file used to ask for.
**Measured 2026-09-16, `tests/surface_check.py`** - the harness's own pipeline
with no model: `build_world.py` -> `episode.probe_surfaces` (real gateway, real
throwaway copy) -> `surface_audit.all_probes` over the whole toolbelt ->
`surface_audit.audit` -> `gate_error`. 63 tools in the toolbelt, 40 probes,
**16 live and 24 dead**, `sql_legacy` and `bi_render` among the live, every
dead *declared* probe declared `dead_by_design`, and **`gate_error` clean**.
`task.toml` now records `harness_patch_required = false`. No `GT008.patch`
exists and none is needed.

`GT007`'s `tests/probes/deploy_probe.open_gateway` still has the
one-warehouse shape; `dual_gateway.py` beside it is the two-warehouse version
and is what this task's suite and solution use.

## The composition, declared (`task.toml [composition]`)

GT008 is the first task written against the four-layer generator contract in
`implementation_structure/TASK_DESIGN_2026-09-15.md`. The declaration is in
`task.toml` under `[composition]` and is **declarative only** - `tests/grade.py`
is still the grader - so that when the generator exists, GT008 can be re-derived
from it and the derivation checked against a package that already works.

* **Contracts (layer 1):** `version_precedence`, `liveness`,
  `consumer_consistency`. Three, not four: `exactly_once` is *not* claimed
  (a full reload of an immutable feed absorbs a duplicate through version
  precedence - claiming both would count one mechanism twice, which is the
  error agent.md 8.10 records as G4 restating G1c), and `cutoff_integrity` is
  *not* claimed (there is no watermark an agent can get wrong by one night;
  `D2.retired` is a writer-count clause, not a cutoff). `auditability` is
  **declared and not yet conjoined** - see C5 below.
* **Stressors (layer 2):** seven, each naming the contract it stresses and the
  night it fires - `late_redelivery`, `out_of_order_arrival`,
  `deletion_at_head`, `dimension_move`, `empty_partition`,
  `consumer_cutover_under_canary`, `authoritative_wrong_advice` (DATA-8104,
  ablatable).
* **Coupling (layer 3): measured, not asserted.** The rule is *perturb defect
  A; if the correct repair of B changes, they are coupled.* It does:
  `ignored_deletion` and `forgot_adjustments` change what the correct cutover
  IS - the same deploy sequence that comes back `clean` for the oracle comes
  back `breached`, and the correct operation becomes roll back, repair, re-run
  (`rolled_back_then_fixed`, which scores max). In the other direction the six
  correct-loader baselines all hold CORRECTNESS 12 while OPERATIONS moves
  0 / 2 / 2 / 4 / 4 / 4, so the coupling is a real one-way dependency and not a
  smeared axis. Two easy tasks in a trench coat would show neither.
* **Live time (layer 4):** the five checkpoints, one per `[[composition.schedule]]`
  entry, and the checkpoint ledger below.

## C5 and the publication ledger (declared; NOT EVALUABLE)

Layer 4 of the framework asks for grading *along the trajectory, in a protected
ledger, so a good final state cannot erase a bad intermediate publication.*
GT008 declares both halves of that and grades neither yet, on purpose.

* **The declaration.** Both consumer-facing jobs in `orchestration/schedule.yml`
  carry a `publishes:` block naming the mart they hand Finance
  (`legacy:warehouse.rpt_revenue_daily` and `nova:warehouse.rpt_revenue_daily`,
  `kind: table`, `consumer: finance_revenue`). `schedule_model.derive_jobs`
  validates it and carries it into the job spec the scheduler executes, and
  `fixture_contract._problems_schedule` refuses a fixture that has lost it -
  **measured**: the falsifier now plants 33 damages and catches 33, the new one
  being "the publishes annotation stripped off the nova job".
* **The writer is not ours.** `world/README.md#publication-ledger` (frozen 2026-09-16):
  `world/world/ledger.py` plus `WorldScheduler` append one receipt per entry
  per run, at the three sites where the job's command actually ran;
  `tasks/harness/ledger.py` is the reader. The reader is on disk; the
  scheduler's publish hook is in `LEDGER.patch` and **is not applied to this
  tree** while rollouts run. Nothing in this package writes either.
* **The check.** `grade.py`'s **C5**, axis `AUDITABILITY`, `points: 0`,
  `penalty: -2`, outside the pass criterion: *no receipt ever published a state
  the ledger-fold oracle could not have produced at that vnow.* It compares
  `row_count` against the fold and each `sample` row - the contract spells a
  sample as up to five rows **as lists** in canonical order - against the
  fold's rows in declared column order. `canonical_hash` is **not** recomputed:
  reproducing `fingerprint._warehouse_hash`'s typed-value spelling from the
  frozen JSON reference is not something this task has measured, and a check
  that might fail for a formatting reason is worse than one that says what it
  did not compare.
* **The boundary is the contract's, not taste.** `trigger()` writes no receipt:
  a manual trigger runs inside the episode container where `var/ledger` is an
  empty read-only tmpfs. So **C5 audits only the four nights this grader
  advances** - never a publication the agent caused by hand (every receipt at
  or before t0 is dropped), and never the thirty history nights, whose partial
  folds are not frozen in the grading zone. Night *k* is the half-open vnow
  span `[t0 + H(k-1), t0 + Hk)` read through `ledger.receipts_between`, and the
  receipts come out of `replay()`'s own throwaway copy - the four nights happen
  there, not in the episode - read before the copy is torn down.
* **Not evaluable, and it says so.** With no `receipts.jsonl`, C5 records
  `not_evaluable: the advanced world published no receipts ...`, scores 0 and
  decides nothing - **measured**: the oracle still scores 20/20, the rubric
  total is still 20 and no baseline moved. The scoring branches are not dead
  code: eleven synthetic receipt cases were run against the real reader
  (`tests/VALIDITY.md`), including the two exclusions above and a
  mapping-shaped `sample`, which is reported as an uncompared shape change
  rather than silently passed.
* **When it lands**, `auditability` becomes the fourth conjoined contract and
  C5 is re-weighted **then, on evidence**. It is not weighted now.

## What is proven, what is designed

**Proven (executed; `tests/VALIDITY.md` has the commands and the numbers):**
the fixture builds deterministically; the legacy loader is right through night
1 and wrong from night 2; the oracle solution reaches **20/20** through the
tool surface, five runs out of five; every adversary lands where the design
says (table above); the falsifier catches **33/33**; the task package loads
under `tasks/harness/taskspec.py` with all three variants locked; a real
two-warehouse gateway serves `warehouse in {nova, legacy}` and renders the
shipped dashboard; and - new on 2026-09-16 - **the rollout harness itself can
serve this world**: `tests/surface_check.py` drives `episode.probe_surfaces`
and `surface_audit.gate_error` to a clean gate with `sql_legacy` and
`bi_render` live.

**Designed, not built:** the Snowflake / Postgres target (the consumer as an
export script, the aggregate reconciliation recipe, the `COUNT_IF` night); the
receipts ledger C5 audits (LEDGER owns it; C5 is written, exercised against
synthetic receipts, and `not_evaluable` until it lands); the
`[composition]` table's use as a GENERATOR input (it is declarative - nothing
reads it yet); any model rollout (none was run - the brief forbade it).

**Not measured:** `target_pass_at_1`. The design bets that the ported loader is
the modal build and that reconciliation-to-zero at t0 makes agents stop; the
interference ticket (DATA-8104) is the world lever agent.md 8.10 found to be
the only one that moved a model, and it is here **on purpose and ablatable**:
a matched control replaces its body with a neutral proposal (`fixture_contract`
names the clause to relax).

## Ownership

Everything under `tasks/GT008-warehouse-cutover/` was written by GT008. Nothing
under `gateway/`, `world/`, `tasks/harness/` or any other task was touched, and
**no `GT008.patch` was needed**: the one substrate change this task depended on
landed independently (SUBSTRATE) before this pass.
`tests/probes/deploy_probe.py` and `prompts/variant.py` are verbatim copies of
GT007's (the latter with the task name substituted).
