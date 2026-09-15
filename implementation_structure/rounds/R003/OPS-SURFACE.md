# OPS-SURFACE — the deployment and DevOps axis

**Agent:** OPS-SURFACE · **Date:** 2026-09-14 · **Scope:** `gateway/` only.
Horizon-SWE weights Deployment & DevOps at 30% and we had nothing on that axis: no
metric an agent could read, no alarm that could fire, no notion of a deploy that went
badly. Twelve tools now exist over one declarative definition file.

```
gateway tools    36 -> 48
gateway tests    51 -> 87        (51 existing untouched + 36 new)
ruff check .     clean
world tests      37 passed       (world/ not touched by me)
```

**Added:** `gateway/src/alkera_gateway/observability.py` (1066), `runlogs.py` (154),
`gateway/tests/test_observability.py` (1116), `gateway/OBSERVABILITY_CONTRACT.md` (286).
**Edited, additively:** `config.py` (+11, two paths), `tools.py` (+80, argument models and
registry), `operations.py` (+42, the twelve wrappers), `tests/conftest.py` (+5, an empty
`var/obs/` and a default `observability` path), `README.md`, `config.example.json`.
**No file under `world/`, `tasks/` or `gateway/docker/` was touched.**

---

## 1. What was built

| Tool | Reads | `op_class` |
|---|---|---|
| `metrics_list` | every metric, each measured now | read |
| `metrics_get` | one metric plus the raw reading behind it | read |
| `metrics_series` | derived history and this session's own samples, kept apart | read |
| `alarms_list` | every alarm evaluated live; `state` filter | read |
| `alarms_get` | one alarm plus its transition history and `since` | read |
| `alarms_history` | recorded transitions, seeded and observed | read |
| `deploy_list` | deploy targets and the canary each one checks | read |
| `deploy_run` | applies a change, re-measures, reports | write / destructive |
| `deploy_history` | recorded deploys and canary outcomes | read |
| `logs_list` | scheduler runs and whether each captured output | read |
| `logs_get` | one run's output with numbered evidence lines | read |
| `logs_search` | literal search across runs, with `skipped` accounting | read |

Six metric kinds — `freshness`, `row_count`, `null_rate`, `job_duration`,
`job_error_rate`, `sql_scalar`. Warehouse readings go through the B1 connector with
`read_only=True`, so they run on any backend and are classified by `sql.py` like any
other query. Definitions live in the world (`/var/obs/observability.yml`, seedable by a
theme); observed samples, alarm transitions and deploy records live in `obs_state_dir`
outside the world tree, under the same rule as `bi_state_dir`. Full spec:
`gateway/OBSERVABILITY_CONTRACT.md`.

---

## 2. Proof that every value traces to world state

This was the whole risk of the assignment, so it is proved three ways.

### 2.1 Every reading is checked against a query the agent could run itself

`test_every_metric_matches_a_query_the_agent_can_run_itself` asks each question twice —
once through the metric, once through `sql_query`/`sched_state` — and requires agreement.
Not `assert value > 0`; the same question, two routes.

```
count(*)                       = 20     == orders_rows
count(*) - count(amount)       = 2      == orders_null_rate.detail.null_rows, 2/20 = 0.1
sum(amount)                    = 237.00 == orders_revenue, detail.raw {"decimal","237.00"}
max(order_date)                = 2026-03-04
                                        == orders_freshness.detail.watermark
vnow 2026-03-05T09:00Z - that  = 118800 == orders_freshness.value
sched_state run statuses       = [success, success, failed, success]
                                        == daily_load_error_rate 0.25, detail {runs:4, failed:1}
```

Every reading also ships the components it was derived from (`detail.watermark`,
`detail.rows`, `detail.run_id`, `detail.raw`), so the agent and the grader can re-derive
it. That is not decoration: it is what makes the surface auditable after the fact.

### 2.2 Change the world, watch the number move

Standalone run through `Gateway`, not the test suite — one row inserted, dated one day
after the current watermark:

```
                         before      after       change
orders_freshness        118800.0    32400.0     -86400  exactly one day
orders_rows                 20.0       21.0          +1
orders_null_rate             0.1     0.09523…   2/20 -> 2/21
orders_revenue             237.0      287.0     +50.00  the row's amount
daily_load_duration        300.0      300.0          0  scheduler untouched
vendor_rows                 null       null      still insufficient_data
```

```
alarm                  before              after
orders_stale           firing              ok        the new row cleared it
orders_daily_volume    pending             firing    three consecutive low buckets now
orders_null_warning    firing              firing
vendor_feed_empty      insufficient_data   insufficient_data
```

`orders_daily_volume` crossing from `pending` to `firing` was not designed into the
fixture — it is what three consecutive sub-threshold buckets genuinely produce once a
fourth day exists. A hardcoded surface does not do that.

`test_changing_the_world_moves_every_metric` asserts the same deltas and then undoes the
change and requires every value back. `test_freshness_tracks_the_virtual_clock_not_wall_time`
moves `vnow` forward a day and requires freshness up by exactly 86400.

### 2.3 A reading that cannot be taken is blank, never zero

The single most dangerous failure a metrics surface can have is reporting a number for
something it did not read. `vendor_rows` points at a table that does not exist:
`value: null`, `status: "insufficient_data"`, `reason` naming the table — and the alarm
over it reports `insufficient_data`, not `ok` and not `firing`. Dropping the table under
a live metric does the same thing (`test_a_series_the_world_cannot_supply_blanks_one_alarm_not_the_page`)
and leaves every unrelated alarm still answering. A broken *definition* still fails the
whole call loudly (`invalid_observability`) — that is a broken surface, not world state,
and the two are kept apart deliberately.

---

## 3. The deploy and its canary

`deploy_run` measures the canary, applies the change, measures again, and reports the
difference. Real run of `prune_old_orders`, whose statement is a real `DELETE`:

```json
{"status": "breached", "blocked": false,
 "metrics": [{"metric": "orders_rows",      "before": 20.0, "after": 12.0, "delta_pct": -40.0},
             {"metric": "orders_null_rate", "before": 0.1,  "after": 0.1666…, "delta_pct": 66.67},
             {"metric": "orders_freshness", "before": 118800.0, "after": 118800.0}],
 "guards":  [{"metric": "orders_rows", "breached": true, "reason": "fell 40.00% (limit 5%)"}],
 "newly_firing": ["orders_row_floor"],
 "still_ok":     ["orders_null_critical"]}
```

and the action row for it:

```json
{"seq": 7, "tool": "deploy_run", "op_class": "destructive", "flags": ["delete"],
 "rows_affected": 8, "exit": "ok"}
```

The rows really are gone (`select count(*)` = 12 afterwards) and `orders_row_floor` is
still firing on the next `alarms_list`. **Ground rule 5 holds: flagged, not blocked.**
`blocked` is a constant `false` and there is no code path that sets it otherwise.

Classification, per the brief: a deploy is at least `write`; `full_refresh` on a dbt
deploy and a state-replacing statement on a SQL deploy both classify `destructive`, with
the statement's own flags carried through. A clean deploy is reachable too —
`fix_null_amounts` reports `status: "clean"` and `cleared: ["orders_null_warning"]`,
which matters because a canary that always says "breached" is worth nothing.

The dbt path executes for real: `deploy_run build_models` returns dbt's own return code
and `full_refresh: true` logs `op_class: destructive`, `flags: ["full_refresh"]`.

A `sql` deploy whose statement fails rolls the whole transaction back and **errors**
rather than reporting a comparison it could not make; the first statement's effect is
verified gone and no deploy record is written. dbt deploys behave the other way — a
non-zero return code still produces a full canary report, because dbt applies partially.
Each half is honest about its own semantics; the asymmetry is documented.

### It feeds the process axis

New surface prefixes are distinct under `signals.py`'s `tool.split("_")[0]`, so
consulting metrics/alarms/logs now raises `surfaces_before_write` — directly serving
agent.md §7.6's *"did it consult lineage, logs, docs — or just start typing SQL."*

```
metrics_list, alarms_list, logs_list, then deploy_run
    surfaces_before_write 3    ungated_destructive 0   (the metric read gated the deploy)
deploy_run with nothing before it
    surfaces_before_write 0    ungated_destructive 1   review_candidates[0] = ungated_destructive
```

A deploy's own canary reads do not self-gate it: `process_signals` folds `read_targets`
in *after* testing the row. Signals stay advisory and uncalibrated (S104).

---

## 4. Which alarm imprecision is deliberate

Declared author-side in `known_imprecision` on the alarm, and **stripped from every tool
response** — the same treatment the ingest connector's `sync` template gets.
`test_alarm_imprecision_is_author_side_and_never_reaches_the_agent` serialises
`alarms_list` + `alarms_history` + every `alarms_get` and requires the strings
`known_imprecision`, `blind_spot` and `false_positive` to appear nowhere. Telling the
agent which alarm was planted crooked would destroy the measurement (F202).

Three mechanisms, all genuinely computed rather than decorative:

**1. Collector lag (`evaluation_lag_s`).** The alarm evaluates the metric *as it
genuinely stood* at `vnow - lag`: the freshness watermark restricted to rows that existed
by then, run history truncated to then, series points later than then dropped. It is not
the current reading with a number subtracted. In the fixture `orders_stale` fires at
118 800 s while `orders_stale_lagged` — same metric, same threshold, 48 h lag — reports
`ok` at 32 400 s, which is what the reading actually was two days ago.

Because the reconstruction has to be derivable, a lag the surface cannot honour is
**refused at load time** rather than faked: a lag on a `row_count`, `null_rate` or
`sql_scalar` value fails with `no earlier value can be derived from current state`.

**2. Datapoint suppression (`datapoints_to_alarm`).** `orders_daily_volume` wants three
consecutive days under the floor and sits at `pending` with 2 of 3 breaching, while daily
volume has collapsed to a quarter for two days. Tuned to ignore one quiet day, it also
ignores a two-day outage.

**3. Scope mismatch.** `orders_null_critical` is `ok` at a whole-table null rate of 0.10
against a 0.2 threshold, while `metrics_series` shows the most recent bucket at **1.0** —
a day where every amount is missing. The evidence that the alarm is wrong is reachable
through the same surface, which is the point.

**One false positive**, needing no mechanism, only a threshold the world outgrew:
`daily_load_slow` fires on a 300 s run that **succeeded**, and was long only because it
backfilled two days after the previous night's failure. The reading's own
`detail.status_of_run` says `success` and the run it should have pointed at
(`run-000003`, `connection reset by peer`) is one `logs_get` away. That makes it
mechanically checkable as misleading rather than a matter of opinion.

`OBSERVABILITY_CONTRACT.md` §"Deliberate imprecision" is the catalogue a task author
plants from, and it also names the hazard an author will otherwise hit by accident: for
`row_count` and `null_rate`, a bucketed series point covers one bucket while the scalar
covers the whole table — same unit, different scope.

---

## 5. Logs

`logs_list` / `logs_get` / `logs_search` read the scheduler's run list and the same
per-run record `sched_logs` serves, and add enumeration, numbered evidence lines, and
search across runs with `skipped` accounting (the `docs_search` rule: a run is either
searched or named with a reason). They never synthesise log text, and every response
names its provenance — `run_output` (a real transcript), `run_result` (a structured
record), `scheduler_state` (the `detail` field and nothing more), `none`.

**Verified against a scheduler that is not `FileScheduler`.** LIVE-WORLD's
`world/world/gateway_scheduler.py` publishes its response shape in its own docstring;
`logs(run_id)` returns a structured record with `job/status/action/detail/executed/error`
and no transcript. A stub matching that shape is injected via `Gateway(scheduler=...)` in
`test_the_surface_reads_through_the_scheduler_protocol_not_one_implementation`, and the
whole surface works through the protocol: runs enumerate, `connection reset by peer` is
found in both the `error` and `detail` fields with separate line numbers, and the job
metrics read the injected history (`daily_load_error_rate` 1/3, `daily_load_duration`
120 s). This is a stub, not that class — `world` is not importable from the gateway venv,
so what is verified is protocol-independence, not the live integration.

---

## 6. Verified working vs. not exercised

**Verified on this machine, 2026-09-14:** all twelve tools through `Gateway`, on DuckDB.
Action rows follow §9.2.2 (`op_class` in the log and absent from responses, `args_hash ==
digest(args)`, contiguous `seq`, targets naming `metric:`/`alarm:`/`deploy:` plus the real
tables read). A six-call observability session including a destructive deploy **replays
byte-for-byte** into a restored clone with an identical world hash, so writing alarm
transitions beside the audit log does not break determinism. A second world shape with
different tables and different metrics works with no code change.

**Not exercised:**

- **Postgres and Snowflake backends.** Metric SQL goes through the B1 connector, but no
  metric has ever been evaluated against Postgres. `count(col)` was chosen over `FILTER`
  and `CAST(... AS TIMESTAMP)` over a bare literal specifically for portability; that
  reasoning is untested.
- **The real `WorldScheduler`** — a stub of its published shape, not the class (§5).
- **Any container.** Everything here ran in-process.
- **Scale.** The largest fixture has 20 rows, 7 metrics, 8 alarms. `metrics_list`
  evaluates every metric on every call, which is N queries per call; where that hits
  `result_too_large` or the SQL timeout is unmeasured.

---

## 7. The single biggest weakness in my own output

**Every alarm I built is optional evidence, and an agent that ignores the whole surface
can still reach the right answer — so as it stands this axis may add zero
discrimination.**

The three blind spots and the false positive are all discoverable by an agent that never
calls `alarms_list` and just runs `sql_query`. That is by construction: I required every
reading to be re-derivable from the data, because a surface that cannot be checked against
the world is a surface that cannot be trusted. But the consequence is that reading the
alarms is *strictly dominated* by reading the data. An agent that consults the dashboard
can be misled; an agent that ignores it cannot. On a 20-row fixture, ignoring it is also
cheap.

That inverts what the axis is supposed to measure. Horizon-SWE's claim is that *"the
ability to plan over long horizons and coordinate across tools"* discriminates — but a
tool whose output is redundant with a cheaper tool does not create coordination pressure,
it creates a trap that costs nothing to avoid. For this surface to discriminate, a task
has to make the alarm **load-bearing**, and none of the three routes to that exist yet:

1. a world large enough that re-deriving every reading by hand is genuinely expensive;
2. a grader check on whether the agent *noticed* a misleading alarm — which needs the
   scoring decision that was explicitly out of scope for me;
3. a required action gated on alarm state (don't deploy while the canary is red).

Only route 3 is partly built: `deploy_run` genuinely creates a decision point, the choice
is visible in the action log (`op_class: destructive`, `ungated_destructive`), and there
is no cheaper way to learn what a deploy did than to run it and read its canary. **The
deploy pillar is the one that can discriminate; metrics and alarms currently cannot, on
their own.** I did not realise that while designing and only saw it while writing this up,
which means the four-pillar shape I was given may be weighted wrong — the effort belongs
in deploys and in whatever makes alarms non-redundant, not in more metric kinds.

### Other gaps, in order

1. **No world seeds a definitions file.** `world/` was off-limits (LIVE-WORLD owns it), so
   every real world answers `no_observability` today and the surface is exercised only by
   test fixtures. Concretely: a theme needs a `bi/`-style `obs/observability.yml` copied
   into `var/obs/`, and the R003 fixture in `tests/test_observability.py::DEFINITIONS` is a
   ready template.
2. **No fingerprint coverage for `var/obs/`.** `world/world/fingerprint.py` has no `obs`
   section, so a world's observability definitions are outside the semantic hash. The fix
   is one conditional section, exactly like `ingest`; the patch is written out in
   `OBSERVABILITY_CONTRACT.md`. **Track A item.**
3. **Run logs are thin because the content is thin.** Seeded runs carry only `detail`, so
   `logs_get` mostly renders one line. I built enumeration, evidence lines and search over
   content that barely exists yet. This gets better as the scheduler genuinely executes —
   but note that even LIVE-WORLD's `WorldScheduler` returns a structured record, not a
   transcript, so `run_output` currently only appears for dbt runs through `FileScheduler`.
4. **The canary's before and after share a virtual timestamp.** `vnow` does not advance
   during a SQL deploy, so a clock-dependent metric like freshness cannot show time-based
   change across a deploy. Correct for a deploy that takes no virtual time; wrong the
   moment a deploy is supposed to consume a maintenance window.
5. **`sql_scalar` compares as a float.** The exact DECIMAL survives in `detail.raw`, but
   the threshold comparison is `float`, so a money metric sitting exactly on a boundary
   could flip. Small, real, and unmeasured.
6. **No `alarm_ack` or silence.** An agent cannot acknowledge, silence, or retune an
   alarm. Alarm fatigue and the operator who silences the pager are real behaviours and
   are not representable on this surface at all.

Nothing here is wired into any task's official score, per the brief.
