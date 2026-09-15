# FAULTS — the world misbehaves on its own, and still rebuilds

**Agent:** FAULTS · **Round:** R003 · **Scope:** `world/` only.
**Date:** 2026-09-14.

Continues LIVE-WORLD, which named its own gap precisely:

> *"The world only breaks when someone breaks it. Every failure mode this
> simulator can produce is one an author planted or an agent caused — the
> environment itself is incapable of misbehaving, and that is exactly the class
> of incident the benchmark sells."*

`advance` can now produce, on its own and reproducibly: a partition that
arrives late, one that never arrives, one delivered twice, a file read while it
is still being written, a job that times out, one that retries, one still
running when its next occurrence is due, two jobs whose windows overlap, and a
partially applied write.

Three new files in `world/world/`: `faults.py` (the layer), `observe.py` (the
observability writer, per `OBS_STORE_CONTRACT.md`), and a fault catalogue in
each of the four `theme.yaml` files. Plus one defect fix the coordinator
handed over mid-task (§8).

> **STATUS.** `test_advance_is_deterministic` failed inside the full suite
> while passing in isolation. **Cause found: a concurrent edit to a
> fingerprinted theme input, not a defect in the fault layer** — another agent
> saved all four `obs/definitions.yaml` files mid-run, and a double-build gate
> cannot tell a save from a divergence. Ruled out by reproduction: test
> pollution. Reproduced directly: the concurrent-edit mechanism. The suite is
> **green on a clean window, twice.** Full working in §7.1 — including one real
> defect of mine found on the way (`observe._scalar` turned any engine error
> into a recorded `null`), which is fixed.

---

## 1. The catalogue

Declared per theme under `live.faults`, applied by `advance`, overridable by
seed file. Eight kinds:

| kind | fires on | what it leaves behind |
|---|---|---|
| `late_arrival` | an arrival | the partition lands `delay_minutes` late; the consumer that ran at its own slot found nothing |
| `missing_arrival` | an arrival | the partition never lands: a gap that does not heal |
| `duplicate_arrival` | an arrival | a byte copy under a sibling name the loader's glob picks up |
| `torn_write` | an arrival | the payload cut mid-line, the `.done` sentinel withheld — a read landing inside a write |
| `job_timeout` | a job occurrence | one `failed` run, `executed: false`, killed at the timeout; no work ran |
| `job_retry` | a job occurrence | a short `failed` attempt, then a real one `retry_delay_minutes` later, interleaved at its real time |
| `job_overrun` | a job occurrence | the work really runs; the recorded window stretches over the next occurrence, or past `vnow` |
| `partial_write` | a job occurrence | the work runs, the tail of the newest partition is gone, and the run is `failed` |

```yaml
live:
  faults:
    - id: vendor_late_delivery     # unique; the draw is keyed on it
      kind: late_arrival
      enabled: true
      rate: 0.20                   # per-occurrence probability
      params: {delay_minutes: 150}
    - id: sftp_connection_reset
      kind: job_retry
      enabled: true
      jobs: [sftp_poll]            # job faults may be scoped to named jobs
      only: ["sftp_poll@2026-03-05T05:30:00Z"]   # exact occurrences, no draw
      params: {attempts: 2, retry_delay_minutes: 7}
```

`rate` draws; `only` names occurrences outright and wins over `rate`, which is
how an author plants one specific incident rather than a distribution. At most
one fault applies per occurrence — the catalogue is scanned in declaration
order and the first that fires wins, so it reads top to bottom like a rule
table. A seed file may re-aim a declared fault (`enabled`, `rate`, `only`,
`jobs`, `params`, merged key by key) but may **not** change its `kind` or `id`:
that would make the theme's catalogue a lie about the world it describes, and
it is refused at load time. So is an override naming a fault the theme does not
declare — a typo that silently disables a planted defect is precisely the
silent-wrong failure this project calls disqualifying.

### Shipped defaults

| theme | state | why |
|---|---|---|
| `revenue_daily` | 7 declared, **all off** | it is the seed the acceptance battery, the cross-host gate and every worked example build. Its default behaviour stays the clean metronome it was. |
| `sftp_vendor` | 4 of 5 **on** | a vendor drop folder is the least reliable integration in any real stack and this theme exists to model one. "This world has a flaky vendor feed" is an authoring decision made in `theme.yaml`. |
| `mainframe_batch` | 3 of 5 **on** | a batch window that is never missed is not a batch window. |
| `excel_close` | 3 declared, **all off** | see §5 — two arrival kinds are structurally meaningless here and are deliberately not offered. |

---

## 2. Determinism — the design, then the evidence

Determinism is the whole game and this is where it is hardest, so the design
is worth stating before the hashes.

**One draw, keyed on `(fault_seed, fault_id, occurrence)` and nothing else.**
Not iteration order, not accumulated PRNG state, not the wall clock, and — the
property that makes a catalogue composable — **not which other faults are
declared or enabled**. Enabling `vendor_late_delivery` shifts no draw belonging
to `sftp_connection_reset`. `random.Random(str)` seeds from SHA-512 of the
encoded string, the same construction `gen.day_rows` already uses and which the
cross-host gate has proven. Asserted in
`test_faults.py::test_one_fault_does_not_perturb_another`.

**Anything that has to happen after the span is parked by ABSOLUTE virtual
time.** A late arrival whose new instant falls past `target` goes into
`live.json`'s `deferred_arrivals` as an iso timestamp; a retry attempt whose
delay straddles the boundary goes into `deferred_runs` the same way. Both are
replayed from the stored instant, never recomputed. This is what makes
`advance(1)` × N land the identical world `advance(N)` does — and it is the
one piece of the design that would have been easy to get wrong and hard to
notice, because without it a retry that crosses a boundary is simply **lost**
and the occurrence never completes.

**Two cursors, not one.** `days_landed` counts days whose data actually landed;
`days_scheduled` is the arrival stream's cursor. They diverge the moment a
partition goes missing. Conflating them is how a gap silently heals itself on
the next pass — which would make `missing_arrival` a fault that fires and then
un-fires.

**Nothing races.** LIVE-WORLD's predecessor established the rule: on DuckDB
appends never conflict, so a live race produces no error, forces no winner, and
does not rebuild. Every overlap here is **overlapping recorded windows over
strictly sequential execution**, plus the data state that interleaving
genuinely produces (a partial table, a truncated file). We build the evidence
of a race; we never run one.

### Gate 1 — baseline accounting: what moved, and why

`world/` at HEAD does not contain LIVE-WORLD's work at all (`executor.py`,
`gateway_scheduler.py` and `test_live_world.py` are still untracked), so "the
committed baseline" is pre-LIVE-WORLD and not a meaningful comparison. The
meaningful one is **the tree as LIVE-WORLD left it**, and LIVE-WORLD.md records
its hashes for `seed.example.yaml`, so it can be checked exactly: strip the
three things this agent added to the theme tree — the fault catalogue, the obs
definitions, the relations macro — rebuild, and see whether the recorded hash
comes back.

```
  LIVE-WORLD recorded build   : 76e3062b379c385385b093374eac1947c03585b86646b3023842f0378ece07f6
  stripped tree, build        : 76e3062b379c385385b093374eac1947c03585b86646b3023842f0378ece07f6
  stripped tree, advance(30)  : 1b9e498773f5f15897621086399527bdd8b774e6d93ccd68b986e8a6d63b90a5
  LIVE-WORLD recorded advance : 1b9e498773f5f15897621086399527bdd8b774e6d93ccd68b986e8a6d63b90a5
  FAULTS tree,   build        : cdb4ae8349aee1e98679301f7987c50f983ebde7442ac67f366c34e8c6614e83
  FAULTS tree,   advance(30)  : 0cd3b79ba2e2929e819efe81a5f13ae3251821f9655b4824084d504850eb543b

  section-by-section (faults DISABLED):
    bi         87fe81ee59e9f0fc   87fe81ee59e9f0fc   same
    live       88458bc27e3854c5   2dda921c605e06c0   MOVED
    obs        None               3b0b58d98078bf62   MOVED
    scheduler  12bc81b4e2919db7   12bc81b4e2919db7   same
    warehouse  3756b4e936f345e0   3756b4e936f345e0   same
    workspace  79e6485dfe7c22c3   e3c4d343f06c5064   MOVED
```

**Strip the three additions and LIVE-WORLD's recorded hashes come back, digit
for digit, for both the build and the advance.** Nothing else in `world/`
changed behaviour. With faults *disabled*, `warehouse`, `scheduler` and `bi`
are byte-identical — the data, the run history and the dashboards are the ones
LIVE-WORLD left. The three that moved are the three additions and nothing more:

| section | why it moved | scope |
|---|---|---|
| `live` | `live.json` gains `faults`, `fault_seed`, `days_scheduled`, `deferred_arrivals`, `deferred_runs`, `fault_log` | every theme that declares a catalogue — now all four |
| `obs` | new conditional section: `var/obs/` definitions + samples | every theme that seeds definitions — now all four |
| `workspace` | §8's `macros/relations.sql`, one new file per theme | all four |

**So yes, the fingerprint of all four themes moved, including with faults
switched off.** Task fixtures are unaffected because none of GT001/2/3 is built
from a theme (§7), but any *future* fixture pinned to a theme hash must be
re-baselined against the numbers in Gate 4 and §8.

### Gate 2 — faults declared and DISABLED: two builds, one world

`revenue_daily`, all seven faults declared, none enabled, `advance(30)`:

```
  A after advance(30)         : 0cd3b79ba2e2929e819efe81a5f13ae3251821f9655b4824084d504850eb543b
  B after advance(30)         : 0cd3b79ba2e2929e819efe81a5f13ae3251821f9655b4824084d504850eb543b
  C after advance(1) x 30     : 0cd3b79ba2e2929e819efe81a5f13ae3251821f9655b4824084d504850eb543b
  equal A==B                  : True
  equal A==C (path indep.)    : True
  state.json bytes equal      : True
  samples.jsonl bytes equal   : True
  faults fired                : 0
  deferred arrivals / runs    : {} / {}
```

### Gate 3 — faults FIRING: two builds, one world, two paths

Same seed, **all seven enabled**, rate-driven (not placed), `advance(120)` — and
`load_retry` given a 95-minute delay on purpose, so retry attempts straddle
advance boundaries and the deferral path is exercised rather than assumed:

```
  A after advance(120)        : 2b95a34ad5f21b13a046cd4d92190bd009fe72d9959868b1045918f172ad1b88
  B after advance(120)        : 2b95a34ad5f21b13a046cd4d92190bd009fe72d9959868b1045918f172ad1b88
  C after advance(1) x 120    : 2b95a34ad5f21b13a046cd4d92190bd009fe72d9959868b1045918f172ad1b88
  equal A==B                  : True
  equal A==C (path indep.)    : True
  state.json bytes equal      : True
  samples.jsonl bytes equal   : True
  faults fired                : 7
  deferred arrivals / runs    : {'34': {'fault': 'late_partition',
                                        'vts': '2026-03-08T04:00:00Z'}} / {}
```

Seven faults fired, and one arrival is still parked past the end of the span at
an absolute instant — so the two walks agreed *while* carrying deferred state,
which is the case that would have broken had the deferral been recomputed
rather than stored. `state.json` and `samples.jsonl` are byte-identical, not
merely semantically equal.

### Gate 4 — all four themes at their shipped defaults

Built twice from each theme's own `seed.example.yaml`, then `advance(120)`.
`sftp_vendor` and `mainframe_batch` ship their catalogues enabled, so these are
worlds that misbehaved on their own.

```
--- revenue_daily (advance 120h) ---
  built     A 668227dc680ced36f4f327b65a614c4dc6f41faa3f630df045bdf56f49b8b9bb
  built     B 668227dc680ced36f4f327b65a614c4dc6f41faa3f630df045bdf56f49b8b9bb   equal=True
  advanced  A f7fbe0b8d7d421eec43f00a3cae1397be7551c160c2cb5a169560fa5c2945529
  advanced  B f7fbe0b8d7d421eec43f00a3cae1397be7551c160c2cb5a169560fa5c2945529   equal=True
  moved     True
  faults fired: 0  {}
  failed runs: 0 of 68
  samples: 72 lines, byte-identical=True

--- sftp_vendor (advance 120h) ---
  built     A 9923b81483a8ef2d193e3f2d637d12859c638177db7b457d92a11fe6d51e054e
  built     B 9923b81483a8ef2d193e3f2d637d12859c638177db7b457d92a11fe6d51e054e   equal=True
  advanced  A 6ab31b644f189821d46c11da2211431bc842a841af853ca0a6afd28388bed810
  advanced  B 6ab31b644f189821d46c11da2211431bc842a841af853ca0a6afd28388bed810   equal=True
  moved     True
  faults fired: 3  {'job_retry': 1, 'late_arrival': 1, 'duplicate_arrival': 1}
  failed runs: 1 of 103
    2026-03-04T05:30:00Z sftp_poll        paramiko.ssh_exception.SSHException: Connection reset by peer
  samples: 140 lines, byte-identical=True

--- mainframe_batch (advance 120h) ---
  built     A 4aa45be32c71943eae13a4ff4c0b6482d4c5648c1097a568778f4b1724afc420
  built     B 4aa45be32c71943eae13a4ff4c0b6482d4c5648c1097a568778f4b1724afc420   equal=True
  advanced  A 3c1bc130bddebf05991d3f530c46f8de8532ef2ec1f5d2318bbfbbda493bf434
  advanced  B 3c1bc130bddebf05991d3f530c46f8de8532ef2ec1f5d2318bbfbbda493bf434   equal=True
  moved     True
  faults fired: 2  {'job_overrun': 2}
  failed runs: 0 of 105
  samples: 160 lines, byte-identical=True

--- excel_close (advance 120h) ---
  built     A 6d5a9978af8c7cb9adb2c21db7c29e425b2a787b62ea33f6044292f1a34a17f8
  built     B 6d5a9978af8c7cb9adb2c21db7c29e425b2a787b62ea33f6044292f1a34a17f8   equal=True
  advanced  A a2f57940c023c51a9dc662ca4e8e3005f51dc5d0e6ae3eea12e8aa901300e3aa
  advanced  B a2f57940c023c51a9dc662ca4e8e3005f51dc5d0e6ae3eea12e8aa901300e3aa   equal=True
  moved     True
  faults fired: 0  {}
  failed runs: 0 of 68
  samples: 81 lines, byte-identical=True
```

**These four are pre-§8 numbers**: they were taken before the view-catalog fix
added `macros/relations.sql`, which moves the `workspace` section of every
theme. §8 gives the exact before/after for all four, and Gates 1–3 above are
post-§8 (the clean `advance(30)` hash `0cd3b79b…` appears in both Gate 1 and
Gate 2, which is the cross-check that they were taken against the same tree).
The *equality* is what Gate 4 asserts, and that is unaffected.

### One false alarm, recorded because it looked exactly like a determinism bug

An earlier run of this gate reported `sftp_vendor` **not** reproducing at build
time, with a different sample count between the two builds. It was not a
defect: OBS-HISTORY was rewriting `world/world/themes/*/obs/definitions.yaml`
at that moment, so build A and build B read different definitions. It is worth
recording because the symptom of a concurrent edit to a fingerprinted input is
indistinguishable from real nondeterminism, and the next person to see it will
lose the same twenty minutes. The re-run after the files settled was equal, and
has stayed equal across every run since.

---

## 3. Each fault, demonstrated

One fault at a time, at `rate: 1.0`, on `revenue_daily` unless noted, `advance
30` from `vnow = 2026-03-03T00:00:00Z`. Output verbatim. (Captured before §8's
fix; nothing shown here is a view body or a fingerprint, so none of it moved.
Every assertion below is also a test in `tests/test_faults.py::FaultArtifactTest`,
which runs against the current tree.)

### `late_arrival` — the partition lands after the job that consumes it

```
{"day": "2026-03-03", "kind": "arrival", "late": true, "name": "daily_revenue",
 "produced": {"partition": "dt=2026-03-03", "rows": 50}, "vts": "2026-03-04T04:00:00Z"}

  2026-03-04T01:00:00Z -> 2026-03-04T01:00:45Z  daily_load   success  executed=True
  2026-03-04T01:05:00Z -> 2026-03-04T01:06:30Z  dbt_build    success  executed=True

  2026-03-03T00:00:00Z revenue_freshness        86400.0    build
  2026-03-04T01:00:45Z revenue_freshness        176445.0   advance  daily_load-0061
  2026-03-04T01:00:45Z raw_revenue_rows         1504       advance  daily_load-0061
```

The load ran at 01:00 and **succeeded** while being a day behind — the quiet
failure, not the loud one. Raw row count unchanged at 1504; freshness doubles.

### `missing_arrival` — a gap that does not heal

```
{"day": "2026-03-03", "kind": "arrival_missed", "name": "daily_revenue",
 "produced": null, "vts": "2026-03-04T00:00:00Z"}

  partitions: 30   newest: dt=2026-03-02
  days_landed: 30  days_scheduled: 31  deferred: {}
```

The two cursors have separated. A later arrival will not backfill day 30.

### `duplicate_arrival` — the same delivery twice

```
{"fault_effect": {"duplicated": ["landing/daily_revenue/dt=2026-03-03/revenue.dup.csv"]}, ...}

  files: ['revenue.csv', 'revenue.dup.csv']
  2026-03-04T01:00:45Z raw_revenue_rows   1604   (was 1504; the day carried 50 rows)
  2026-03-04T01:06:30Z revenue_rows       31
```

The loader's `dt=*/*.csv` glob picks the copy up. 100 rows land where 50 were
delivered.

### `job_timeout` — a failure that claims no output

```json
{
  "action": null,
  "detail": "dt=2026-03-03",
  "effects": {},
  "error": "job exceeded its 45s timeout and was killed (SIGKILL) before it produced any output",
  "executed": false,
  "execution_date": "2026-03-03",
  "job": "daily_load",
  "status": "failed",
  "trigger": "schedule",
  "vts_end": "2026-03-04T01:00:45Z",
  "vts_start": "2026-03-04T01:00:00Z"
}
```

```
  2026-03-04T01:00:45Z daily_load_error_rate    0.1   advance  daily_load-0061
  2026-03-04T01:00:45Z raw_revenue_rows         1504  (unchanged — nothing ran)
```

`executed: false`, `action: null`, `effects: {}`. Nothing in the record implies
work that did not happen. The dbt build downstream still runs and still
succeeds, on yesterday's data.

### `job_retry` — every attempt recorded, interleaved honestly

```
  2026-03-04T01:00:00Z -> 2026-03-04T01:00:05Z  daily_load   failed   try=1/2 executed=False
       error: duckdb: could not open database: resource temporarily unavailable
  2026-03-04T01:05:00Z -> 2026-03-04T01:06:30Z  dbt_build    success  executed=True
  2026-03-04T01:10:00Z -> 2026-03-04T01:10:45Z  daily_load   success  try=2/2 executed=True
```

The downstream build was scheduled **inside** the retry window, so it ran
against data the load had not yet written. That is emergent, not scripted: the
retry attempt goes back into the event queue at its own instant rather than
running in place. It is also the reason the run list is in completion order
rather than clock order, which `observe._finished` sorts for rather than
assumes.

### `job_overrun` — two overlapping windows

```
  2026-03-04T01:00:00Z -> 2026-03-04T01:09:00Z  daily_load   success  executed=True
  2026-03-04T01:05:00Z -> 2026-03-04T01:06:30Z  dbt_build    success  executed=True
  overlap: daily_load [01:00:00, 01:09:00) contains dbt_build start 01:05:00 -> True

  2026-03-04T01:09:00Z daily_load_duration  540.0  advance  daily_load-0061
  2026-03-04T01:06:30Z daily_load_duration   45.0  advance  dbt_build-0062
```

45 s nominal × 12. Note the second sample: at 01:06:30 the overrunning load had
not finished, so the latest *finished* run is still the previous day's 45 s one.
A run that has not finished by the reading's instant is not history and
contributes to nothing — the `OBSERVABILITY_CONTRACT` rule, falling out of the
implementation rather than being special-cased.

### `partial_write` — partial data, and a failed run

```json
{
  "action": "theme:load_raw",
  "effects": {"raw.daily_revenue": {"rows_after": 1524, "rows_before": 1504}},
  "error": "loader process exited with code 137 (killed)",
  "executed": true,
  "job": "daily_load",
  "status": "failed"
}
```

```
ledger: {"trimmed": {"partition": "2026-03-03", "rows_kept": 20, "rows_lost": 30,
                     "table": "raw.daily_revenue"}}
```

The work really ran and really wrote; 20 of the 50 delivered rows survive. The
landing CSV is intact — the *write* was partial, not the delivery — so the
evidence that something is missing is reachable by comparing the file to the
table. The error message says the process was killed and nothing more; the
agent has to find the partial rows itself.

### `torn_write` — a read landing inside a write (`sftp_vendor`)

```
{"fault_effect": {"sentinels_withheld": ["drop/acme/ORDERS_20260303.done"],
                  "truncated": [{"bytes_kept": 834, "bytes_lost": 683,
                                 "file": "drop/acme/ORDERS_20260303.csv"}]}, ...}

  acme tail: ['ORDERS_20260302.csv', 'ORDERS_20260302.done', 'ORDERS_20260303.csv']
  ORDERS_20260303.csv last 80 bytes:
    b',2026-03-03,C0456,7.55\n30025,2026-03-03,C0315,82.13\n30026,2026-03-03,C0010,123.8'
  sentinel present: False
  2026-03-04T05:30:20Z vendor_order_rows   1504   (the day did not load)
```

Cut mid-line at 55 % — deliberately mid-line, because that is what a
half-written file is. The loader skips unsentinelled files, so the orders for
that day never load while the *returns* for the same day do: a referential
pathology nobody planted. The byte count is a pure function of the file's own
contents, so it reproduces exactly.

---

## 4. Faults disabled → the world is clean

A fault that fires when it is switched off is worse than no fault. Four
assertions, all in `tests/test_faults.py::CleanWorldTest`, against a
`revenue_daily` world that declares seven faults and enables none, advanced 120
hours:

- `fault_log == []`, `deferred_arrivals == {}`, `deferred_runs == {}`;
- `days_scheduled == days_landed` — every day scheduled also landed;
- no run carries `try_number`, no run error mentions a timeout, every run with
  an `executed` key has `executed: true`;
- all five arrivals landed on their nominal instants, none late, none missed,
  and every one of the 35 landing partitions holds exactly `['revenue.csv']` —
  no duplicate, no truncation.

Gate 2 above adds the fingerprint half of the same claim.

---

## 5. What I could not make deterministic, and left out

Named rather than faked. Each of these is a thing a real stack does that this
layer does not.

**True concurrency.** By design, per §2. On DuckDB appends never conflict, so a
live race produces no error and forces no winner. The staged interleave is the
deliberate substitute and it is weaker in one specific way: the *data* state is
the one sequential execution produced, so an agent cannot discover an ordering
artefact that only a real race would create (a lost update, a half-visible
transaction). It can discover everything the committed artefacts show.

**Measured timeouts and durations.** `timeout_s` and the overrun multiplier are
declared. Elapsed wall time is the most host-dependent number available and
`state.json` is fingerprinted. This is LIVE-WORLD's existing trade, extended.

**Arrival faults in the seeded backstory.** `faults_in_history` is implemented
and opt-in, but covers **job** faults only (timeout, retry, overrun). A late or
missing *historical* partition would need every theme's `write_landing`
restructured to skip or corrupt a day, and `partial_write` is excluded from
history for a different reason: it would leave the built warehouse disagreeing
with the loader that produced it. No shipped theme turns it on, so
`initial_state` is byte-identical to the function GT001 calls directly —
asserted.

**`duplicate_arrival` and `torn_write` on `mainframe_batch` and
`excel_close`.** Declared and off on `mainframe_batch` (`CLAIMS.DAT` is
fixed-block, so truncation leaves a partial 31-byte record and the copybook
reader is entitled to raise rather than round-trip it — real, but a rougher
edge than the CSV themes), and **not offered at all** on `excel_close`, where
they are structurally meaningless: a "partition" there is a month, `write_day`
rewrites the whole workbook from every day landed so far, so a missing day
heals itself on the next write and a duplicate file is not a redelivery because
the loader reads one workbook per period. A fault that silently does not happen
is worse than no fault, so only the ones that genuinely bite are declared.

**Per-row load stamps.** The brief offered them as one way to stage an
interleave. Not done: adding a `_loaded_at` column changes every table's schema,
every dbt model and every fingerprint. The interleave evidence is the run
history plus the observability record instead.

---

## 6. The observability store

`world/world/observe.py` writes `/var/obs/samples.jsonl` per
`OBS_STORE_CONTRACT.md`; the gateway (OBS-HISTORY) reads it. `build`, `advance`
and `trigger` each append one sample per defined metric after every executed
run. All six declared kinds are implemented (`freshness`, `row_count`,
`null_rate`, `job_duration`, `job_error_rate`, `sql_scalar`); `null` means
insufficient data and is never `0.0`.

**Faults are visible in the record without any fault-specific metric existing.**
That is the design: a late partition shows up as freshness climbing, a timeout
or retry storm as an error rate stepping off zero, an overrun as a duration
jumping. Asserted in `ObservabilityStoreTest::test_faults_are_visible_in_the_record`.
The contract's own invariant — a reader must not be able to reconstruct a past
sample from present state — is asserted directly by taking the build-time
freshness sample and showing the live reading differs after `advance`.

**One honest deviation from "ordered by vts", and it is forced.** Samples are
appended in *measurement* order (the order jobs start), not strictly by `vts`,
because measurement order is the path-independent one: `advance(1)` × 30 walks
the same event sequence as `advance(30)`. Stamping by `vts_end` instead would
be vts-sorted within one call and **not** path-independent, because a
`job_overrun` run finishes after events a later, smaller `advance` has not
simulated yet. With `job_overrun` active a line can therefore carry an earlier
`vts` than the line above it. Determinism won the tie, as the brief requires.
**OBS-HISTORY should sort by `vts` if it needs clock order** — this is the one
thing in the seam that differs from the contract's wording.

Also worth stating: `OBS_STORE_CONTRACT.md` names `definitions.yaml` and
`gateway/OBSERVABILITY_CONTRACT.md` named `observability.yml`. The writer reads
either, preferring the frozen seam's name.

**Fingerprint.** `world/world/fingerprint.py` gains a conditional `obs`
section. OPS-SURFACE's patch shape hashed the parsed definitions YAML; this
hashes the whole `var/obs/` directory byte-exact instead, because the samples
file is generated here and must reproduce byte for byte anyway, and because an
append-only record whose bytes are not pinned is not evidence. Conditional like
the rest: a world seeding no definitions writes no samples, has no `var/obs/`,
and keeps its recorded hash.

**Ownership note.** I seeded `themes/*/obs/definitions.yaml` for three themes;
OBS-HISTORY has since rewritten all four, extended them and added `excel_close`.
Their versions parse, declare only kinds this writer implements, and are what
the evidence above was taken against. Those files are now effectively
jointly owned and `world/world/build.py` copies whichever is present, verbatim.

---

## 7. What broke

| suite | before | after |
|---|---|---|
| `world/tests` | 37 | **76** — 35 new in `test_faults.py`, 4 new view-readability tests (§8), 1 existing test extended. Two full-suite runs green (`75 passed in 455.87s`, then `75 passed in 865.40s` with the §7.1 fix in place); the 76th test was added after that second collection and passes in its class (`8 passed`). One earlier full-suite run failed the determinism gate; cause found and it was external — §7.1. |
| GT001 `run_baselines.py` | TASK VALID | **TASK VALID** (oracle 19/19, no_op 0) |
| GT002 `run_baselines.py` | TASK VALID | **TASK VALID** |
| GT003 `run_baselines.py` | TASK VALID | **TASK VALID** |

All three baselines were re-run **after** §8's view fix as well as after the
fault layer, and all three still print TASK VALID.

### 7.1 RESOLVED — the suite failure was a concurrent edit, not a defect

**Cause found and named. The gate now passes under the full suite.** The
sub-sections below are kept in the order they were established, because the
process of ruling things out is the useful part.

```
75 passed in 455.87s (0:07:35)      # full suite, nothing being edited underneath it
75 passed in 865.40s (0:14:25)      # again, with the observe._scalar fix in place
```

**What it was.** `world/world/themes/*/obs/definitions.yaml` is read by every
build and hashed into the `obs` fingerprint section. OBS-HISTORY saved all four
of those files — one bulk write, mtime `12:38:12` on every one — inside the
window the failing suite run occupied. A double-build determinism gate reads
that file twice; if somebody saves between the two reads, the two worlds are
genuinely different and the gate is genuinely right to fail.

**Proof of mechanism, not just of opportunity.** Build a world, append a single
comment line to `definitions.yaml`, build a second world, compare:

```
equal: False
differing sections: ['obs']
unittest message tail: Diff is 813 characters long. ...
```

One differing section, `obs`, and a diff of the same order as the 898 characters
the coordinator saw. Nothing else in the tree moves.

**Test pollution is ruled out, independently.** A harness ran the whole of
`test_faults.py` in-process (34 tests, 0 failures) — pytest orders it before
`test_live_world.py`, so it is the only module that can pollute it — and then
did exactly what `LiveWorldTest.setUpClass` does:

```
[clean]    built equal: True    advanced equal: True
[polluted] built equal: True    advanced equal: True
```

Equal before and after, at build and after `advance`, and that run happened
concurrently with a full suite so it was under load as well.

**The finding is about how this repo is being worked on, not about the code.**
Nothing currently stops one agent saving a fingerprinted theme input while
another agent's determinism gate is mid-flight, and the failure it produces is
indistinguishable from a real reproducibility defect — I lost time to the same
thing earlier in this task (§2, "One false alarm"). A double-build gate is a
*measuring instrument*, and this repo lets people move the thing being measured.
Worth a rule: no edits under `world/world/themes/` while `world/tests` or
`scripts/acceptance.sh` is running.

---

The rest of this section is what was written while the cause was still open.
It is kept because one real defect was found on the way to the answer, and
fixed.

The coordinator ran the suite and got:

```
FAILED tests/test_live_world.py::LiveWorldTest::test_advance_is_deterministic
1 failed, 74 passed in 455.09s
```

The same test, run alone, passes:

```
Ran 1 test in 47.476s ... OK
built    semantic_hash: cdb4ae8349aee1e98679301f7987c50f983ebde7442ac67f366c34e8c6614e83
advanced semantic_hash: 0cd3b79ba2e2929e819efe81a5f13ae3251821f9655b4824084d504850eb543b
```

Those two hashes are the ones in Gates 1 and 2 above, so **every determinism
number in this report was gathered under the conditions where the gate passes.**
The honest statement is therefore: *`advance` is deterministic in isolation, and
something that only happens when the suite runs together perturbs it.* Until
that something is named, **the reproducible-rebuild property cannot be claimed**
— it is a Phase-0 go/no-go gate and this is exactly the shape of failure that
lets you talk yourself into either answer depending on how you invoke the test.

The failing assertion compares whole fingerprint dicts and pytest truncated the
diff, so `LiveWorldTest.maxDiff = None` is now set — whoever hits it next gets
the section name instead of `Diff is 898 characters long`.

**Leading hypothesis, and it is in my code, not someone else's.**
`observe._scalar` catches `duckdb.Error` and turns *any* engine failure into a
recorded sample of `value: null` with a `reason`. A missing table is genuinely
insufficient data and belongs there. A transient — an IO or lock error when the
reading attaches to `warehouse.duckdb` moments after a dbt subprocess exited,
which is far likelier under a loaded full-suite run than an isolated one — is
**not world state**, and swallowing it writes a hiccup permanently into
`samples.jsonl`, which is inside the fingerprint. That mechanism is
load-dependent by construction, which is precisely the "alone versus together"
signature. `executor.table_rows` has the identical shape (`duckdb.Error` →
`None` → run `effects` → `state.json`) and is pre-existing.

**This is fixed as of now, confirmed cause or not**, because the behaviour was
wrong on its own terms: `CatalogException` / `BinderException` (the object is
not there) stay `insufficient_data`; every other `duckdb.Error` now raises
`observe.ReadingFailed`, so a failed measurement fails the build or the advance
loudly instead of freezing a wrong reading into evidence. `executor.table_rows`
got the same split. Determinism over realism means a loud stop, not a quiet
null. The fix landed *after* the evidence in §2 was gathered; the suite was
re-run against it afterwards, and a test that forces a reading to fail
(`test_a_reading_that_fails_is_loud_and_never_recorded_as_null`) now pins the
distinction, because a transient that does not happen leaves no trace and
nothing else in the suite would ever have caught this.

**Competing hypothesis, and I have direct evidence for this one already.** A
concurrent edit to a fingerprinted input. `world/world/themes/*/obs/definitions.yaml`
is read by every build and hashed into the `obs` section, and **OBS-HISTORY was
still editing those four files during this task** — that is exactly what caused
the false alarm recorded in §2, where `sftp_vendor` appeared not to reproduce
and turned out to be two builds reading two different definitions files. The
suite's failing window is 455 s; the isolated run's is 47 s. A ten-times-longer
window is ten times likelier to straddle somebody's save.

**And there was a save.** All four `definitions.yaml` files carry an mtime of
`12:38:12` — the same second, i.e. one agent writing all four at once — which
is *after* the evidence batch in §2 and inside the window the failing suite run
occupied. That is circumstantial, not proof: it establishes means and
opportunity, not that this particular build pair straddled it. But it is a
concrete, checkable fact rather than a theory, and it is the first thing to rule
out.

*(It was the cause. The reproduction at the top of this section closed it: test
pollution ruled out, the concurrent-edit mechanism reproduced directly, and the
suite green on a clean window.)*

**And it is still happening.** While this report was being written,
`revenue_daily/obs/definitions.yaml` was written again — mtime `12:38:12` →
`12:51:48`. That second save happened to be byte-identical (content hash
`79f45019…` before and after), so it was harmless; the first was not. Nobody
doing this is doing anything wrong, and that is the point: the repo offers no
signal that a determinism gate is mid-flight and no way for an agent to know it
is about to invalidate somebody's measurement. Anyone re-running these gates
should check the mtimes of `world/world/themes/*/obs/definitions.yaml` and
`theme.yaml` before trusting a failure.

**One existing test had to change, and it is the right one.** LIVE-WORLD wrote
`test_world.py::test_conditional_sections_track_what_the_theme_actually_seeds`
to assert the fingerprint's section set *honestly*, so adding the `obs` section
made it fail by design. It now asserts
`["bi", "live", "obs", "scheduler", "warehouse", "workspace"]`, and additionally
checks that the `obs` section really covers the appended record rather than only
the definitions.

**No task fixture broke.** GT001 calls `initial_state(seed, jobs)` directly with
two positional arguments; the new `faults=` parameter defaults to `None` and the
output is byte-identical (asserted). GT002 and GT003 write their own
`sched_state.json` and never import the scheduler. None of the three has a
`var/world/live.json`, so none has a fault catalogue.

**One pre-existing hazard fixed in passing.** `executor._kind_theme` caught
`Exception`, but three of the four themes' `load_raw` hooks raise `SystemExit`
when their loader subprocess fails — a `BaseException`. It escaped and killed
`advance` rather than failing the job. Harmless while nothing made loaders fail;
the fault layer makes them fail routinely. Now caught.

### The fingerprint moved, and here is the accounting

Every theme's semantic hash moves. Three separate causes, all explainable:

1. **`live.json` gains keys** (`faults`, `fault_seed`, `days_scheduled`,
   `deferred_arrivals`, `deferred_runs`, `fault_log`) → the `live` section
   moves. Only for themes that declare faults; all four now do.
2. **A new conditional `obs` section** appears.
3. **§8's `macros/relations.sql`** → the `workspace` section moves.

The `warehouse` section — the data itself — is **unchanged by all three, and
that is measured, not argued**: Gate 1's section table for (1) and (2), §8's
before/after table for (3). So is `scheduler`, and so is `bi`. With faults
disabled nothing that runs changes, and the obs writer only ever reads.

---

## 8. The view-catalog defect (handed over mid-task) — FIXED

The coordinator reported that every dbt-created **view** in a theme world was
unreadable through the gateway. Reproduced, fixed, gated.

**The defect.** A view's body is *stored*, so it can be created successfully and
be unreadable afterwards. dbt opens `var/warehouse/warehouse.duckdb` directly —
DuckDB names the catalog after the file stem, so the catalog genuinely *is*
`warehouse` at creation time — and rendered:

```sql
CREATE VIEW staging.stg_revenue AS SELECT ... FROM warehouse.raw.daily_revenue;
```

Every reader is required to `ATTACH` the same file under an alias instead
(agent.md §9.2.1), because opening it directly makes the catalog collide with
the `warehouse` *schema*. Under that alias the baked name does not resolve:

```
Binder Error: Catalog "warehouse" does not exist!
```

This is the §9.2.1 collision in a third form: open directly → ambiguous; attach
as `wh` → unambiguous; **but dbt still writes as if it were case 1**.

**The fix.** Each theme's `workspace/dbt_project/macros/relations.sql`
overrides `ref()` and `source()` to render **without** the database qualifier:

```jinja
{% macro ref() %}
  {% do return(builtins.ref(*varargs, **kwargs).include(database=false)) %}
{% endmacro %}
```

Bodies then say `FROM raw.daily_revenue`, which binds against whatever catalog
the view finds itself in — verified under `ATTACH … AS wh` **with and without**
`USE wh`, and when the file is opened directly. `this` is untouched, so the
CREATE statement's target still renders three-part and resolves fine.

**Why not the profile route.** Making dbt and the reader agree on one alias
(dbt-duckdb's `attach:` block with `database: wh`) was the coordinator's
first-choice direction and it does work. I rejected it: agreement is a
convention, and the next tool to run dbt is the gateway's own `dbt_run` with a
`profiles.yml` this repo does not write (`config.profiles_dir`, default
`/etc/alkera/dbt`). One profile out of step and every view silently breaks
again. Suppressing the qualifier needs no agreement from anyone.

**Verification — every view, all four themes, both connection styles:**

```
--- revenue_daily ---   USE=True/False  staging.stg_revenue     OK
    body: CREATE VIEW staging.stg_revenue AS SELECT ... FROM raw.daily_revenue;
--- sftp_vendor ---     USE=True/False  staging.stg_orders      OK
                        USE=True/False  staging.stg_returns     OK
--- mainframe_batch --- USE=True/False  staging.stg_claims      OK
--- excel_close ---     USE=True/False  staging.stg_gl_lines    OK
ALL VIEWS READABLE: True
```

The gate is `executor.unreadable_views(root)`, asserted in all four theme
suites. It reads **every** view `duckdb_views()` reports, not the one someone
happened to look at — GT001's hand-written `warehouse.kpi_daily` passed
throughout and proved nothing, which is exactly why this survived.

**Fingerprint movement, measured before and after in one run:**

```
theme             section     before            after             moved
revenue_daily     semantic    668227dc680ced36  cdb4ae8349aee1e9  True
                  warehouse   3756b4e936f345e0  3756b4e936f345e0  False
                  workspace   79e6485dfe7c22c3  e3c4d343f06c5064  True
sftp_vendor       semantic    9923b81483a8ef2d  e4e56cbd26a896bc  True
                  warehouse   b81e6b60b10a6b92  b81e6b60b10a6b92  False
                  workspace   2f66fec0999a5e13  2d5a25f7df70fc7b  True
mainframe_batch   semantic    4aa45be32c71943e  baabf1d156b20b4e  True
                  warehouse   4ab62267f607fcc2  4ab62267f607fcc2  False
                  workspace   9a97b6fc4a9d8d98  e877dc2f52fbae02  True
excel_close       semantic    6d5a9978af8c7cb9  f354b378664c1aa2  True
                  warehouse   6432efc53f0e0eef  6432efc53f0e0eef  False
                  workspace   e72ac4821405dea3  2816a19fa3e41ace  True
```

Exactly one section moved — `workspace`, the one new macro file per theme. The
data is byte-identical. `fingerprint.py` hashes BASE TABLES only, so view
bodies are not themselves in the fingerprint; the movement is the file, not its
effect.

**Not covered, and someone should own it.** `world/world/executor.write_profiles`
is the only place in this repo that writes a duckdb `profiles.yml`, and it
covers the initial build and the scheduler's dbt jobs. The episode container's
`profiles_dir` is provisioned elsewhere. The fix above is robust to that
*because* it needs no agreement — but if anyone later adds a dbt profile that
attaches under an alias, `unreadable_views` is the test that will catch the
consequences.

---

## 9. The single biggest weakness in my own output

**The faults are all in the future, and every episode starts from a spotless
past.**

`initial_state` still writes 30 or 180 consecutive green nights. A task author
who opens a world built today finds a pipeline that has never once been late,
never retried, never overrun its window — and then, the moment `advance` runs,
a world that does all three. That discontinuity is visible to an agent and it
is backwards: the *history* is where an operator looks first when diagnosing an
incident, and ours says the system has been perfect since the day it was
switched on. An agent asked "is this normal?" has no baseline to answer from,
and the honest answer under this build is "nothing has ever been abnormal,"
which is not a fact about any real stack.

I built `faults_in_history` and then shipped it off by default in every theme,
for a defensible reason — `initial_state`'s byte-identical output is what keeps
GT001 green, and I was not willing to move a task fixture's ground truth as a
side effect of a feature nobody asked me for. But defensible is not the same as
right, and the effect is that the deliverable's headline claim ("the world
misbehaves on its own") is true only of the part of the world that has not
happened yet. It also covers **job** faults only: a backstory with a timed-out
load but no corresponding gap in the landing zone is coherent here purely
because all four themes reload every partition every night, which is luck about
these four themes rather than a property of the design.

What it would take: give each theme's `write_landing` the same per-day fault
consultation `advance` already does, so a historical partition can be late,
missing or duplicated, and have `initial_state` derive its history from the
arrival record rather than from the calendar. That is a day's work and it moves
every fixture built on a theme, which is why it belongs in a round where
someone owns re-baselining the tasks.

Three smaller ones, in order:

- **I shipped a silent-wrong path into the evidence module, and found it by
  accident.** `observe._scalar` caught *any* `duckdb.Error` and recorded
  `value: null` — turning a lock or an IO error, which is a property of the
  machine, into permanent content of `samples.jsonl`, which is inside the
  semantic fingerprint. That is the exact failure mode this project calls
  disqualifying, written into the one module whose whole purpose is to be the
  record you can still trust after the world has moved. It is fixed (§7.1), and
  `executor.table_rows` had the identical shape and is fixed too. But I did not
  find it by reviewing my own code; I found it while hunting a *different* bug
  that turned out not to be mine. Nothing in my tests would have caught it,
  because a transient that does not happen leaves no trace, and I wrote no test
  that forces a reading to fail. There is one now
  (`test_a_reading_that_fails_is_loud_and_never_recorded_as_null`), added after
  the fact, which is the wrong order.
- **I validated determinism beside the suite rather than inside it.** Every gate
  in §2 was a script I wrote that started a clean process and measured one thing.
  That is why, when the suite failed, I could not immediately say whether the
  fault layer was at fault — I had no result from the environment the failure
  came from. The cause turned out to be external (§7.1), so the conclusion
  survives; the method that produced it was still weaker than it looked, and
  `FaultDeterminismTest` covering the same ground inside the suite is the reason
  the answer was recoverable at all.
- **The staged interleave cannot produce an ordering artefact.** §5 says this
  plainly but it is worth repeating as a weakness, not just a design note:
  GT003's premise is a read landing inside a rebuild, and what this layer
  delivers is a run history that *says* the read landed inside the rebuild plus
  data that is consistent with one sequential ordering. An agent that reasons
  only from timestamps and row counts is fully served. An agent that could have
  been caught out by a genuinely torn read of a table — half-old, half-new —
  is not, because no such state is ever committed. `torn_write` gives that for
  *files*; there is no table equivalent.
- **The fault ledger is unreachable by tool, not by topology** — the same
  caveat LIVE-WORLD recorded for `var/world/live.json`, now carrying more. It
  names every fault that fired, with its id and parameters. It is fingerprinted,
  so tampering is detectable; reading is not. If the workspace-terminal work in
  flight gives the agent a filesystem view wider than `/workspace`, an agent can
  read the answer key to its own incident. Whoever lands that terminal must
  treat `/var/world` as needing the same containment as `/grading`.

---

## How to reproduce

**Before anything else, read §7.1.** Nobody in this repo may edit
`world/world/themes/*/obs/definitions.yaml`, `theme.yaml`, or anything else
inside a theme's fingerprinted tree while a determinism gate is running — a
double-build gate reads those files twice and cannot tell a save from a defect.

```bash
cd world
PYTHONPATH=. ../gateway/.venv/Scripts/python -m pytest tests          # 76
bash scripts/acceptance.sh        # gate 2c is the new faults-on gate
PYTHONPATH=. ../gateway/.venv/Scripts/python -m world.observe <root>  # the record
PYTHONPATH=. ../gateway/.venv/Scripts/python -m world.scheduler <root> live
```

A world with one fault forced on, from a seed file:

```yaml
theme: revenue_daily
seed: 1
# ... the usual keys ...
faults:
  load_partial: {enabled: true, rate: 1.0}
```
