# DW001 - live revenue (pilot dynamic world DW-1)

Nothing in this file is agent-visible. The episode sees `instruction.md` and
the world.

Design: `docs/implementation_plan/experimental_ideas/2026-09-30-dynamic-world-design.md`
(section 4.4); plan: `...-dynamic-world-plan.md`, Task 4.

## The world

Theme `world/world/themes/revenue_live/` is derived from `revenue_daily`. It keeps the same
arrival stream and schedule: day D lands at D+1 00:00, `daily_load` runs at 01:00, and
`dbt_build` runs at 01:05. The world builds to vnow `2026-03-03T00:00:00Z`. The first arrival
after the build is day_index 30 (`dt=2026-03-03`).

Lineage: landing `dt=*/revenue.csv` (with a new `terminal_ref` column) ->
`raw.daily_revenue` (+ `raw.load_checkpoint`) -> `staging.stg_revenue` (view) ->
`warehouse.fct_revenue_lines` (one row per line) -> `warehouse.fct_revenue` (one row per
day, read by the dashboard).

## Latent defects

| # | registry id | contract | where | shock |
|---|---|---|---|---|
| 1 | `retry_backfill_doubling` | exactly_once | `fct_revenue_lines.sql`: no dedup on `order_id` | `duplicate_arrival`: the loader's `*.csv` glob reads `revenue.dup.csv` too |
| 2 | `checkpoint_past_an_unread_partition` | liveness | `loader/load_daily_revenue.py`: incremental, and the checkpoint is set to the newest partition seen | `late_arrival`, 1560 min: lands D+2 02:00, after the D+2 01:00 load already loaded D+1 |
| 3 | `null_unsafe_not_in_quality_gate` | consumer_consistency | `stg_revenue.sql`: `terminal_ref NOT IN ('TEST-HARNESS','SMOKE-TEST')` | none: continuous from t0 |

The registry ids are recorded author-side only: here, in `task.toml`
`[[latent_defects]]`, and in the theme's `theme.yaml` / `gen.py`. They are not in any
workspace file, because that would name the defect to the agent.

`terminal_ref` is NULL in 2% of rows (card-not-present, legacy acquirer), `TEST-HARNESS` /
`SMOKE-TEST` in 2% (rows the gate is right to drop), and `T####` otherwise.

## Measured damage (seed 1, `tests/test_dw001.py`; direct SQL against `damage.yaml`'s tables)

| scenario | defect present | `solution/` fix for that defect applied |
|---|---|---|
| `duplicate_arrival` on day:30, +26h | exactly_once V = 0.0328 (49 duplicated order_ids = every line of 2026-03-03) | V = 0, 0 duplicated keys |
| `late_arrival` on day:30, +98h | liveness V = 0.03125 (1 of 32 due partitions; `dt=2026-03-03` on disk, never loaded) | V = 0 (loaded by the 2026-03-06 01:00 run) |
| no shock, at build | consumer_consistency V = 0.533 (16 of 30 days' orders/revenue differ from the reference) | V = 0 after `apply` + `restate`, and still 0 after the next nightly |

Without a shock, defects 1 and 2 cause no damage (0 / 0 after +74h). Defect 3 needs no
shock.

## Policies (`policies.py`)

All seven implement the plan's `Policy` protocol (`name`, `act(root, tick, vnow)`).
They act only through surfaces an agent has: workspace files, the warehouse attached
under an alias, and re-running the load or dbt by hand. None of them writes `var/sched`
or `var/world`.

| policy | does |
|---|---|
| noop | nothing |
| oracle | at tick -1: `solution.apply` (all three fixes) + `solution.restate` (loader + `dbt build`, outside the schedule, clock untouched) |
| root_late | the same fix, at the first tick boundary at or after t0 + horizon/2 |
| symptom | every tick: backfills landed partitions missing from raw, deletes duplicate raw rows, then rebuilds both facts by hand with correct SQL. It never edits code, so the 01:05 `dbt_build` brings the gate damage back every night, and each new duplicate comes back too. |
| partial | one fix (`defect=`, default `exactly_once`) + restate, at t0 |
| overreach | replaces the loader with a no-op that "succeeds" |
| vandal | truncates `warehouse.fct_revenue`, `warehouse.fct_revenue_lines` and `raw.daily_revenue` at t0. The incremental loader never re-reads history. |

## Ladder (one realisation: dup day:30, late day:31, dup day:33; ticks of 24h; J = sum_t sum_c V_c * 24)

| policy | J, T=168h | S | J, T=336h | S |
|---|---|---|---|---|
| oracle | 0.0 | 1 | 0.0 | 1 |
| root_late | 54.2 | 0.46 | 100.6 | 0.52 |
| symptom | 79.7 | 0.21 | 173.2 | 0.17 |
| partial (exactly_once) | 87.1 | 0.13 | 177.6 | 0.15 |
| noop | 100.6 | 0 | 209.2 | 0 |
| overreach | 96.9 | **+0.04** | 221.5 | -0.06 |
| vandal | 371.7 | -2.70 | 663.6 | -2.17 |

The oracle's damage is exactly 0 on every tick. `liveness.grace_hours: 50` is chosen for
that: a correct loader picks up a 26h-late partition 49h after its nominal arrival.

**Known gap against design spec section 6.** Overreach S < 0 holds only beyond a
horizon-dependent crossover, which falls between one and two weeks on this realisation.
Two things cause it:

- Stopped loads add nothing until the grace has passed, and then the damage grows by one
  partition per day.
- Each duplicated day counts immediately, and in two contracts: exactly_once, and
  consumer_consistency, whose reference de-duplicates.

So the more duplicates a run has (higher rho), the better overreach looks against noop.
Consumer consistency (about 0.53, because 64% of days carry at least one NULL
`terminal_ref`) is common to noop and overreach and cancels out in S. Task 7 should check
this ordering across seeds and rho rather than assume it.

## Files

- `damage.yaml`, `shock_spec.yaml`: inputs to `world.damage` / `world.hawkes`, written to
  the plan's shared schemas.
- `solution/apply.py` (`FIXES`, `apply`, `restate`), `solution/solution.sh`, and
  `solution/workspace/...`: the fixed files, laid out path for path like the workspace.
- `tests/test_dw001.py`: `PYTHONPATH=world gateway/.venv/bin/python -m pytest -q tasks/DW001-live-revenue/tests`
  (about 8-10 min; most of it is the seven-policy week).
