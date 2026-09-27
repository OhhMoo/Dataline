# OBS-HISTORY — the observability record

**Agent:** OBS-HISTORY · **Date:** 2026-09-14 · **Scope:** the reader side of
`/var/obs/` — `gateway/src/alkera_gateway/observability.py` plus the per-theme
observability definitions.

OPS-SURFACE shipped twelve tools and then named their defect exactly:

> *"Every alarm I built is optional evidence. Because I required each reading to
> be re-derivable from the data, reading the alarms is strictly dominated by
> reading the data — an agent that consults the dashboard can be misled, one
> that ignores it cannot."*

That is a correctness problem, not a scoring one. **A real observability system
records what happened; ours computed what is.** Those are the same thing only in
a world that never moves, and the world now moves. This round makes history come
from the record.

```
gateway tests   87 -> 132 passed      (17 new here; the rest are other agents' this round)
ruff check .    clean
world           build + advance reproduce byte-identical samples.jsonl,
                and advance(72) == advance(24) x 3
```

**Edited:** `observability.py` (+~330: `SampleStore`, `SampleRow`, `Retention`,
recorded series, recorded alarm transitions, alias lifting, ambiguity guard),
`config.py` (+4, `obs_samples`), `OBSERVABILITY_CONTRACT.md` (v1 -> v2),
`README.md`, `config.example.json`, `OBS_STORE_CONTRACT.md` (appended
reader-side notes only; the seam itself untouched).
**Added:** `gateway/tests/test_observability_history.py` (17 tests),
`world/world/themes/*/obs/definitions.yaml` (four).
**Not touched:** any `world/world/*.py`, anything under `tasks/`.

---

## 1. The non-derivability test

`test_a_past_sample_cannot_be_reconstructed_from_the_world_that_replaced_it`.
The fixture records `orders_freshness = 118800.0` at `2026-03-05T09:00:00Z`,
then advances the world — the day's partition lands (eight orders dated
2026-03-05, arriving at 18:00) and a backfill completes two amounts the failed
night left blank. The gateway is constructed **after** the advance, so nothing
in the episode ever measured the old world. Output, verbatim:

```
  recorded at 2026-03-05T09:00:00Z:                       118800.0
  measured now (2026-03-05T18:00:00Z):       64800.0
  reconstructed for 2026-03-05T09:00:00Z from the world as it now stands: 32400.0
  '118800.0' anywhere in the 32 read responses the surface can produce:
      with    var/obs/samples.jsonl:  present
      without var/obs/samples.jsonl:  absent
```

The third line is the one that matters. The best reconstruction present state
allows is the same point-in-time restriction the lagged-collector machinery
runs — `max(order_date) where order_date <= TIMESTAMP '2026-03-05 09:00:00'` —
and it returns `2026-03-05`, because the partition that landed at 18:00 is
*dated* inside any window ending at 09:00. There is no other restriction
available. 118 800 is not recoverable by computing; it is only recoverable by
having written it down.

The fourth is the mechanical form: 32 read calls (every `metrics_get`, every
`metrics_series`, every `alarms_get`, plus `metrics_list`, `alarms_list`,
`alarms_history`, `deploy_list`, `deploy_history`, `logs_list`, `sched_state`
and two `sql_query`s) serialised and searched for the string. Delete
`samples.jsonl` and the number is gone from the entire surface while everything
else keeps answering. **If that test could be made to pass by deleting the
store, the store would be decoration.**

### The same thing on a real world, with the real writer

`sftp_vendor` built and advanced five days, its fault catalogue enabled, read
through a real `Gateway`:

```
recorded peak : 2026-03-05T05:41:00Z  279660.0  {'watermark': '2026-03-02T00:00:00Z'}
measured now  : 2026-03-08T00:00:00Z  172800.0  {'watermark': '2026-03-06T00:00:00Z'}
best reconstruction of the watermark at that instant: [['2026-03-05']]
orders_stale  : now = ok | recorded fired_in_window = True | last_fired = 2026-03-07T05:30:20Z
transitions   : [('2026-03-03T00:00:00Z', 'ok',     'recorded_samples'),
                 ('2026-03-04T05:30:08Z', 'firing', 'recorded_samples'),
                 ('2026-03-05T05:46:30Z', 'ok',     'recorded_samples'),
                 ('2026-03-06T05:30:20Z', 'firing', 'recorded_samples'),
                 ('2026-03-06T05:46:30Z', 'ok',     'recorded_samples'),
                 ('2026-03-07T05:30:20Z', 'firing', 'recorded_samples'),
                 ('2026-03-07T05:46:30Z', 'ok',     'recorded_samples')]
```

The feed was three days late on 2026-03-05 and freshness genuinely read 279 660.
Reconstructing the watermark at that instant from today's world gives
`2026-03-05` — about 20 000 seconds, wrong by a factor of thirteen, because the
late delivery has since arrived and is dated before the instant being asked
about. **This is a seeded fault becoming evidence**, which is the whole point of
the two halves of the seam existing.

`orders_stale` is `ok` right now and has flapped seven times inside the window.
`mainframe_batch`'s `batch_window_overrun` is the same shape: `ok` now,
`fired_in_window: True`, `last_fired_at: 2026-03-05T00:01:00Z`.

---

## 2. What changed in the reader

**`metrics_series` and `alarms_history` read the record.** When the world keeps
a store with rows for the metric, `points` are the recorded samples exactly as
written (`series_source: "recorded"`, nulls carried through as nulls) and the
derivation is not offered as history. When there is no store, everything behaves
exactly as it did in R003 — the 87 existing tests are untouched and still pass,
including the ones asserting `derived_from_data` and `scheduler_run_history`.
`metrics_get` still computes live. **Now is computed; then is remembered.**

**Alarm state and history derive from the record.** `recorded_transitions`
replays the alarm's comparison over the recorded samples for its metric and
emits one entry per state change; `alarms_get`/`alarms_history` merge three
labelled origins — `world_seed`, `recorded_samples`, `gateway_observed` — sorted
by `at`. Every alarm carries a `recorded` block with `fired_in_window` and
`last_fired_at`, which is the field the store exists for. `evaluate_on: series`
alarms take their **state** from the recorded series. `since` now walks back from
the end while the state holds, so a flapping alarm reports the start of the
current episode rather than the first time it was ever in that state (that was a
latent bug; it only became visible once history got rich).

**One deliberate exception:** `deploy_run`'s canary evaluates live on both
sides. No store row is written by a gateway deploy, so an alarm read from the
record would give the same answer before and after and could not see the deploy
at all. A canary asks "what is true now", twice. This is commented at the call
site and documented.

**Nothing is fabricated.** A recorded `null` is served as `null`, never `0.0`; a
`value` key that is absent makes the row malformed, not zero; an alarm whose
window contains a recorded null reports `insufficient_data` naming the
timestamp. `test_the_gateway_never_writes_to_the_world_s_sample_store` asserts
the file's bytes are unchanged across two full surface sweeps and a destructive
deploy. The §9.2.2 action-log contract is re-asserted over the new paths
(`seq` contiguous, `tool`, `args_hash == digest(args)`, `op_class: read` in the
log and absent from the response, `exit`), and the author-side
`known_imprecision` string still appears nowhere in any response.

---

## 3. Retention

Declared per world as `retention: {days: N}` (default 14), and applied on
**read**: the file is append-only and is never rewritten, so history stays intact
on disk and the window is what the surface will answer for. What the window
excludes is counted, not hidden:

| field | meaning |
|---|---|
| `retained` | inside the window and at or before the world clock |
| `aged_out` | older than the window — with a per-metric note naming the window |
| `not_yet_observed` | stamped **after** the world clock |
| `malformed` / `malformed_lines` | did not parse, with line numbers |

so "it aged out", "it was never recorded" and "there is no store" are three
different answers. With a two-day window over the fixture record:

```
window_start 2026-03-03T18:00:00Z   retained 3   aged_out 2
points       2026-03-04T01:02:00Z (null), 2026-03-05T01:05:00Z, 2026-03-05T09:00:00Z
orders_stale recorded transitions:  insufficient_data@03-04, firing@03-05
             (the firing on 2026-03-02 is outside the window and is not claimed)
```

Each theme declares its own, and the number says something about the world:
revenue_daily and sftp_vendor 14 days; **mainframe_batch 7** — the operations
team keeps a week of batch telemetry, so a month-end question genuinely has to
be answered from the run logs or not at all; **excel_close 45** — a close cycle
spans a month and gets reopened, so the record has to outlive the period it
describes.

---

## 4. What each theme now defines

Every world was dead before this (`no_observability`); all four are live. Each
file is **one** `obs/definitions.yaml`, read by both `world/world/observe.py`
(writer) and the gateway (reader). Verified by building and advancing each world
for real and reading it through a real `Gateway` — every metric `ok`, every alarm
evaluating, no author-side leak.

| theme | pathology the definitions exist to notice | metrics | alarms | retention |
|---|---|---|---|---|
| `revenue_daily` | the daily partition is late, short or malformed and `fct_revenue` goes quietly stale | 8 | 5 | 14 d |
| `sftp_vendor` | the data arrives because someone else dropped a file; the returns feed is thin enough that a missing day looks quiet | 10 | 5 | 14 d |
| `mainframe_batch` | the batch window — extract 23:00, load +45 min, dbt +10; the failure mode is *late*, visible only against a clock | 10 | 6 | 7 d |
| `excel_close` | the integration layer is a spreadsheet; the number that matters is how much of the open period is a manual adjustment, and nothing upstream fails when that goes wrong | 9 | 6 | 45 d |

Each carries job-side *and* data-side alarms for the same pathology (a missed
delivery is both `sftp_poll_error_rate` stepping off zero and `orders_freshness`
climbing), a canaried dbt deploy with guards, and at least one declared
author-side `known_imprecision`. Freshness thresholds are 180 000 s rather than
a flat two days because freshness legitimately peaks around 176 400 s in the
hour before each load — a flat two-day threshold flickers every night on a
healthy world, and the arithmetic is written into the file.

Three metric sets were already seeded by FAULTS mid-round. I merged rather than
duplicated: FAULTS's metric ids are preserved verbatim, its explanatory comments
kept, and my series/null-rate/scalar metrics, alarms, deploys and retention added
on top. `excel_close` had none and is entirely mine.

---

## 5. Seam problems found and fixed

Building against the real writer found four things a fixture never would.

**1. Two definitions files.** This document's seam names `definitions.yaml`;
`OBSERVABILITY_CONTRACT.md` named `observability.yml` first. Both existed in
three themes, and the writer and the reader would each have picked a different
one — different metric sets, samples for metrics the gateway does not know,
gateway metrics with no history, and nothing looking wrong anywhere. The gateway
now reads either and **refuses a world that ships both**; every theme ships
exactly one.

**2. Definition spelling.** `observe.py` reads a job metric as `metric.job` or
`metric.source.job` and reads `window_runs` only at the top level; the gateway's
model wanted them under `source` and `extra="forbid"` would have failed the whole
surface with `invalid_observability`. The gateway now lifts the top-level
spelling into `source`. Anything genuinely unrecognised still fails loudly.

**3. A recorded sample is always a scalar — and that changes what
`evaluate_on: series` means.** Two named hazards, both now in the contract:

- **Scope.** A derived point covers one bucket; a recorded sample covers the
  whole table. A `lt 20` floor over a cumulative `row_count` can never fire from
  the record. **Three of my four series alarms were wrong this way** and one
  (`excel_close`'s `adjustment_count_spike`) fired permanently on a healthy
  world. Fixed by adding per-period `sql_scalar` metrics and pointing the series
  alarms at those.
- **Cadence.** `datapoints_to_alarm` counts *readings*, not days, and the world
  records one per executed run — two datapoints can be six minutes apart. Every
  theme series alarm declares this in `known_imprecision`.

**4. The world's staging views are unreadable through the gateway.** dbt writes
a view whose stored body names the `warehouse` catalog; the gateway attaches the
database as `wh`, so `select count(*) from staging.stg_revenue` returns
`Binder Error: Catalog "warehouse" does not exist` while `warehouse.fct_revenue`
and `raw.daily_revenue` work. This is not an observability bug — it affects
`sql_query` identically — but every theme's definitions had to route around it,
so it is recorded in the contract. **Somebody should own this**; it is a whole
layer of the world the agent cannot see.

---

## 6. Verified vs. integration-unverified

**Verified on this machine, 2026-09-14:**

- All 132 gateway tests, `ruff` clean.
- All four themes built and advanced five days for real, then read through a
  real `Gateway`: every metric `ok`, every alarm evaluating, series sourced
  `recorded`, no `known_imprecision` leak.
- End-to-end with the **real writer**, not a fixture: `orders_stale` and
  `returns_feed_thin` (sftp_vendor) and `batch_window_overrun`
  (mainframe_batch) each fired on a seeded fault and cleared, and still report
  `fired_in_window: True` with a `last_fired_at`.
- Determinism holds with the new definitions: two builds plus `advance 72`
  produce byte-identical `samples.jsonl` and identical semantic hashes, and
  `advance(72) == advance(24) x 3`.

**Not verified:**

- **Postgres and Snowflake.** Unchanged from R003. The new per-period
  `sql_scalar` metrics use `order by … limit 1` and a scalar subquery, which are
  portable in principle and untested in fact.
- **Any container.** Everything ran in-process on DuckDB.
- **Scale.** The largest record read here is 160 lines. The reader parses the
  whole file on each call and `metric_status` re-walks the parsed rows per
  metric, so `metrics_list` over a world with months of history is O(file ×
  metrics). Where that hits the payload cap or gets slow is unmeasured.
- **Concurrent append while reading.** The reader does one `read_text`; a writer
  appending during that read is not something either side has been tested for.
  It cannot happen today (the world writes during `advance`, not during a tool
  call) but nothing enforces it.
- **`not_yet_observed`.** Ten rows of `mainframe_batch`'s five-day advance carry
  a `vts` after the resulting `vnow` — the documented consequence of appending in
  measurement order when `job_overrun` pushes a run past the clock. The reader
  refuses to serve them and reports the count. I believe that is right; it has
  not been agreed with FAULTS.

---

## 7. The single biggest weakness in my own output

**I made history unfakeable and left the present exactly as redundant as
OPS-SURFACE found it — and the present is what almost every alarm on the four
themes actually compares.**

The invariant holds: a past sample cannot be reconstructed, and
`fired_in_window` is genuinely unreachable from present state. But count what
that buys on a real world. Of the 22 alarms across the four themes, **19 are
`evaluate_on: value`** — their state is computed live from a reading the agent
could take itself with one `sql_query`. For those, exactly the thing OPS-SURFACE
described is still true: consulting the alarm is dominated by consulting the
data. What the record adds for them is a *second* question — "did this ever fire
while I wasn't looking" — which is genuinely load-bearing, and which no task asks
yet. Only **three** alarms are `evaluate_on: series`; they take their state from
the record and are the only ones whose *current* answer is unreachable any other
way.

So the honest accounting is: the surface now has a non-redundant capability, and
it is concentrated in three alarms out of twenty-two and in a question nobody has
written a task around. I did not narrow the redundancy gap; I opened a door beside it.

The mechanism that would close it is one I can see and did not build: **an alarm
whose current state legitimately depends on history that has aged out.** If the
window excludes the sample, and the alarm needs it, then the correct answer is
"I cannot know" and the agent has to go to the run logs — a genuinely different
tool, a genuinely more expensive route, and the shape of an actual incident.
Every ingredient exists (retention is declared, `aged_out` is reported,
`insufficient_data` is a first-class state) and no theme uses it. That is the
next thing to build, and it belongs to whoever writes the first task on this
axis rather than to another round of surface work.

Two smaller ones, in order:

1. **Retention is enforced on read, not on disk.** The row is still in the file.
   That is safe only because `/var/obs/` is outside `workspace` and no gateway
   file tool reaches it. A task that gives the agent a shell hands it the whole
   record, window or no window — and "the data aged out" stops being true.
2. **Run logs are still thin**, the second handoff OPS-SURFACE named. Seeded and
   executed runs carry `detail`, not a transcript, so `logs_get` still renders
   one line for most runs. The record now makes the *numbers* of an incident
   legible; the *narrative* of one still is not. I added metrics that make an
   overrun and a missed delivery visible, which raises the value of the logs
   they point at, and did nothing about the logs themselves.

Nothing here is wired into any task's official score, per the brief.
