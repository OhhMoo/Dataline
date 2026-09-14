# GT003 — seed spec (FROZEN CONTRACT for Track A)

**Task:** "The Tuesday revenue figure" — a reported number captured mid-publish, and the
compensating change it provoked. Built from archetype **A043**.
**Frozen 2026-09-13.** Track A builds the world from this. Track B's `sql_*` and BI tools
address these names. Track C owns everything under `tests/`.

Amendments go through Track C. Do not rename anything below without telling me — the grader
addresses these identifiers literally.

---

## 0. The property this task exists to protect

**By the time the agent arrives, the number it is asked to explain cannot be produced by any
relation in the warehouse.** The mart is correct. The dimension is correct. The raw
duplication that caused the figure was removed by the same delivery that exposed it, and a
delete leaves nothing behind. The agent must argue about a state that no longer exists, from
timestamp alignment, one leftover relation, and the internal inconsistency of a single
recorded row.

Every decision below is downstream of that. Anything that would let the agent re-derive the
figure from live data — a build-id column on the snapshot, a retained copy of the pre-delivery
customer master, a loader audit row naming the deleted duplicates — **must not be added**,
however much easier it would make grading.

**Measured, 2026-09-13.** The property is checked by ablation rather than asserted:

| join | 2026-03-01..03-02 total | |
|---|---:|---|
| `mart_revenue_fact` x `mart_revenue_dim` (today) | 9,755,143.07 | |
| `stg_revenue_lines` x `mart_revenue_dim` (today) | 13,482,810.23 | |
| `mart_revenue_fact` x `mart_revenue_dim__dbt_backup` | 9,755,143.07 | |
| **`stg_revenue_lines` x `mart_revenue_dim__dbt_backup`** | **15,385,785.19** | **the reported figure** |

Only the leftover relation reproduces it, and only against staging — the mart still carries
the analyst's predicate, so the reconstruction is impossible until that has been reverted
too. `raw.customer_master`, `staging.stg_customer` and `warehouse.mart_revenue_dim` all hold
exactly one row per customer; the duplication exists in one place in the whole world.

Two shortcuts were found by ablation and closed:

1. The launch cohort originally occupied its own id block above 900000. That was a fixture
   artefact — `ORDER BY customer_id DESC` separated it at a glance with nothing in the world
   to justify it. The cohort is now chosen by hash and scattered through one id space.
2. The duplicated set originally **equalled** the launch cohort, so the tick's excess equalled
   that cohort's launch-day revenue and three live predicates (`source_batch`, `valid_from`,
   and "customers whose first order was 2026-03-02") each reproduced it without the leftover
   relation. The duplication now covers **883 of the 1,180** — the vendor packaged one chunk
   of the onboarding file twice, which has no reason to cover the whole batch. Re-measured:
   all three predicates now return 2,496,922.15 against an excess of 1,902,974.96.

## 1. The causal chain

```
2026-03-02 23:50  the 2026-03-02 vendor bundle does not arrive
2026-03-03 05:00  daily_revenue_load fails: source file not present
          06:00  fails again (retry 1)            <- DECOY: looks like the textbook
          07:00  fails again (retry 2)               "retry duplicated the window"
          07:07  mart_hourly_rebuild succeeds     <- BUILD A published here
          08:00:12 daily_revenue_load starts (retry 3)
          08:14:49 ...succeeds. The bundle carries BOTH the 2026-03-02 revenue lines
                   AND the customer-master correction that removes the duplicate rows
                   for 883 of the reseller accounts onboarded on 2026-02-25
          08:14:58 mart_hourly_rebuild fires, triggered downstream_of the load
                        |  create fact aside
                        |  create dim aside
                        |  rename fact out -> __dbt_backup
                        |  rename new fact in            <-- BUILD B's fact is now live
          08:15:00 ---- bi_revenue_extract fires on its quarter-hour tick ------------
                        |      reads warehouse.mart_revenue_fact  (BUILD B)
                        |      joins warehouse.mart_revenue_dim   (BUILD A - duplicated)
                        |      writes one rev_snapshot row per region
                        |  rename dim out -> __dbt_backup
                        |  rename new dim in
                        |  drop fact __dbt_backup
          08:15:39 X   the run dies before its final DROP
                        -> warehouse.mart_revenue_dim__dbt_backup SURVIVES
                        -> every later rebuild fails on that rename; the DIM IS FROZEN
                           from here on, the FACT keeps rebuilding hourly
          16:40  rkulkarni pushes DATA-5518: `customer_segment <> 'RESELLER'`
          17:07  the next rebuild picks it up -> the whole segment leaves the mart
```

The duplicated dimension rows had been present since 2026-02-25 and were **harmless**: those
883 accounts had no revenue until the channel launched on 2026-03-02, so they joined to
nothing. The only instant at which they could ever have been observed is the fraction of a
second between the two rename pairs.

**The arithmetic that closes it:**

```
rev_snapshot 2026-03-03 08:15  total = 15,385,785.19   (reported, escalated)
rev_snapshot 2026-03-03 08:30  total = 13,482,810.23   (first self-consistent read)
                              excess =  1,902,974.96
SUM(amount_usd) on 2026-03-02 for the 883 accounts the backup dim carries twice
                                                     = 1,902,974.96  <- identical
```

## 2. Virtual clock

- World "now": **2026-03-05T09:00:00Z** (a Thursday)
- Order-date history: **2025-11-03 → 2026-03-04** (122 days)
- `rev_snapshot` retention: **2026-02-10T00:00:00Z → 2026-03-05T09:00:00Z**, every 15 minutes
  (2,245 ticks x 4 regions = 8,980 rows)
- The incident instant: **2026-03-03T08:15:00Z**

## 3. Schema

Warehouse file `/var/warehouse/warehouse.duckdb`, schemas `raw`, `staging`, `warehouse`.

**Mandatory access rule.** Never open the file directly. At its contract name DuckDB calls
the catalog `warehouse` as well, so every `warehouse.<table>` reference raises
`Ambiguous reference to catalog or schema "warehouse"`. This is not theoretical: GT001's
oracle was broken at the deployed path for weeks while its baselines, which used a
differently-stemmed fixture file, printed TASK VALID (F622). Everything in this task —
generator, oracle, grader, probe, baselines — uses:

```python
con = duckdb.connect(":memory:", config={"threads": 1})
con.execute("ATTACH '<path>' AS wh")
con.execute("USE wh.warehouse")
```

and `run_baselines.py` stages every world at `var/warehouse/warehouse.duckdb` so the
collision cannot hide.

| relation | rows | note |
|---|---|---|
| `raw.customer_master` | 10,180 | `customer_id, region, customer_segment, source_batch, valid_from`. **One row per customer.** The 883 duplicates are gone — removed by the 2026-03-03 bundle. |
| `raw.revenue_lines` | 160,501 | `order_id, line_nbr, order_dt, customer_id, currency, amount_local, load_batch, loaded_at` |
| `raw.fx_rates_daily` | 479 | `rate_date, currency, rate_to_usd DECIMAL(12,8), source_file`. **JPY is missing for 2026-02-16..2026-03-04 (17 rows).** |
| `raw.fx_rates_inbox` | 38 | the loader's landing table: 7 days of already-merged drops plus 17 unmerged JPY rows under `source_file = 'fx_jpy_20260216_20260304.csv'` |
| `staging.stg_revenue_lines` | 160,501 | dbt model; AS OF join to the rate, `amount_usd = round(amount_local * fx_rate, 2)` |
| `staging.stg_customer` | 10,180 | dbt model; **no deduplication** — a latent defect that only ever mattered for 0.4 seconds |
| `warehouse.mart_revenue_fact` | 131,730 | `order_dt, customer_id, customer_segment, revenue_usd, order_count`. RESELLER absent — the compensating change. Reference is **149,203** with **17,473** RESELLER rows. |
| `warehouse.mart_revenue_dim` | 10,180 | `customer_id, region, customer_segment`. Correct. Frozen since 2026-03-03 08:14:58. |
| `warehouse.mart_revenue_dim__dbt_backup` | **11,063** | **THE EVIDENCE.** Build A's dim: 10,180 + a second row for **883** accounts — three quarters of onboarding batch `cm-20260225-0001`, and no live predicate picks that set out. |
| `warehouse.rev_snapshot` | 8,980 | `snapshot_ts, region, mtd_revenue_usd DECIMAL(16,2), mtd_order_count, active_accounts`. Append-only. |

Customers: ids **500001..510180, one contiguous space**. 4 regions (AMER 4,340 / EMEA 3,212 /
APAC 1,779 / LATAM 849), 3 segments (ENTERPRISE 3,123 / MIDMARKET 4,904 / RESELLER 2,153 —
11.87% of all-time revenue). The 1,180 accounts of the launch cohort are **chosen by hash and
scattered through that id space**, marked only by `source_batch = 'cm-20260225-0001'` and
`valid_from = 2026-02-25`. An earlier build gave them their own id block above 900000; that was
a fixture artefact that separated the cohort at a glance with nothing in the world to justify
it, and it is gone.
Currency follows region: AMER→USD, EMEA→EUR, APAC→JPY, LATAM→BRL.

### Workspace

```
/workspace/
  pipelines/mart_rebuild.sql      the hourly publish. Self-contained SQL, run by the
                                  scheduler. Carries the eight-statement swap AND the
                                  DATA-5518 predicate.
  pipelines/bi_extract.sql        the 15-minute extract. `:snapshot_ts` is substituted
                                  by the runner with the tick being produced.
  dbt_project/models/staging/     stg_revenue_lines.sql, stg_customer.sql
  docs/runbook-revenue-mart.md    documents the swap, the leftover-relation failure mode,
                                  and that rev_snapshot is a record and not a cache
  data/landing/fx/                where rate redeliveries land
```

`pipelines/*.sql` are plain SQL on purpose. A043 specifies the mart as a dbt model and the
grading check as "compiled SQL plus the git diff"; the ordering probe has to **execute the
agent's own publish script statement by statement**, which is not possible through `dbt run`
inside a grading zone with no dbt invocation budget. The business logic therefore lives in
the publish script and dbt keeps the staging layer. **Deviation from A043, recorded.**

### `/var/sched/state.json`

777 runs over 2026-02-27 → 2026-03-05 for three jobs. Track A must reproduce these exactly:

- `daily_revenue_load`, cron `0 5 * * *`, 3 retries. Normal days 05:00:00–05:04:18 success.
  **2026-03-03: failed 05:00, 06:00, 07:00 with `source file revenue_20260302.csv.gz not
  present in landing`, succeeded 08:00:12–08:14:49** with detail
  `dt=2026-03-02 + customer_master bundle cm-20260303-0800`.
- `mart_hourly_rebuild`, cron `7 * * * *`, `also_triggered_by: [daily_revenue_load]`.
  Success 40s at `HH:07:00` up to and including 2026-03-03 08:07. **One extra run
  08:14:58–08:15:39 on 2026-03-03, `trigger: downstream_of:daily_revenue_load`, status
  failed**, detail `Catalog Error: Table with name "mart_revenue_dim__dbt_backup" already
  exists!`. **Every run from 2026-03-03 09:07 onward fails with the same message.**
- `bi_revenue_extract`, cron `0,15,30,45 * * * *`, 6s, always success.

The alignment of `08:14:58–08:15:39` against `08:15:00–08:15:06` is the ordinal evidence.
Nothing else in the log overlaps: the rebuild is on `:07` and the extract on the quarter
hours precisely so that this collision is a one-off caused by the late bundle.

### Git

Branch `fix/DATA-5518-reseller-double-count`, merged to `main`. One commit, author
`rkulkarni`, authored and committed `2026-03-03T16:40:00Z`, touching only
`pipelines/mart_rebuild.sql`, message:

```
DATA-5518: exclude reseller lines from the revenue mart

Mart was double counting the reseller segment on 03-03. Filtering it out
until the source feed is fixed.
```

**Track A owes this.** The fixture world has no git history, so `grade.py` checks the
working-tree file only and the git-diff half of A043's check is **deferred** until the repo
build lands. Recorded in `tests/manifest.yaml` under `compensating_change.note`.

## 4. Scale

160,501 revenue lines, 10,180 customers, 8,980 snapshot rows. The fixture builds in **1.7 s**
and the two databases are **7.6 MB** and **7.9 MB**. Small on purpose — must build under 60 s
and sit inside ~2 GB.

## 5. The planted defects

**P1 — the torn read (the primary cause).** One `rev_snapshot` tick,
**2026-03-03 08:15:00**, AMER and EMEA only, whose month-to-date columns come from build B
and whose `active_accounts` comes from build A. Overstated by **1,902,974.96** on a reported
total of **15,385,785.19**. APAC and LATAM at that tick are already self-consistent because
no account of theirs was duplicated.

**P2 — the compensating change (a defect, not a fix).** `customer_segment <> 'RESELLER'` in
`pipelines/mart_rebuild.sql`, live in the snapshot from 2026-03-03 17:15. Removes **17,473**
rows and **11.87%** of all-time revenue from the mart, across all history, silently.

**P3 — the late APAC rate (a genuine, independently fixable defect).** `raw.fx_rates_daily`
has no JPY rows for 2026-02-16..2026-03-04. `stg_revenue_lines` resolves the rate with an
AS OF join, so every APAC line from 2026-02-16 converts at the 2026-02-15 rate of
`0.00712115` instead of the true `0.00653…0.00657`. APAC is **overstated 8.72%**; the
window's JPY revenue reads `18,781,469.65` against a true `17,274,683.95`. The vendor's
redelivery is sitting unmerged in `raw.fx_rates_inbox`.

**P4 — the leftover relation.** `warehouse.mart_revenue_dim__dbt_backup`, 11,063 rows. The
only physical trace of the interrupted publish and the only surviving copy of the duplicated
dimension.

## 6. Decoys — real, and each surviving a shallow check

**Decoy 1 — the retried load (C-RETRY).** `daily_revenue_load` really did fail three times
and succeed on the fourth attempt, on exactly the right night. This is the model's favourite
hypothesis and it is GT001's own mechanism: *a retry re-ran over an already-loaded window and
duplicated it.* It is wrong here — the loader is delete+insert by date and `raw.revenue_lines`
holds no second copy of 2026-03-02 — but it survives "were there retries?" and it is why the
rebuild ran off-schedule, so it is genuinely part of the setup. Naming it as a **cause** takes
the false-cause penalty.

**Decoy 2 — the vendor re-send (C-VDUP).** **31** order lines dated **2026-02-24** are
present twice in `raw.revenue_lines` under batches `rev-20260225-0500` and
`rev-20260225-0500-resend`. Real duplication, which is exactly what an analyst checking for
double counting finds first. All **AMER/USD** — deliberately, so that an agent who fixes it
does not shift the APAC figure the currency check grades to the cent. Worth ~0.04% of one
day. Present in **both** the broken and the reference world, so fixing it is optional and
leaving it costs nothing.

## 7. Repair hazards — what the obvious fix destroys

| action | what it takes with it |
|---|---|
| full refresh of the mart | drops `mart_revenue_dim__dbt_backup` — the only physical evidence |
| rebuilding `rev_snapshot` from the mart | destroys the record of what was reported when, *and* rewrites the APAC error out of history where it genuinely belonged |
| deleting the torn tick | the board pack joins on the tick; a region must not go missing |
| treating the DATA-5518 predicate as the existing fix | leaves an entire customer segment out of every number Finance sees |
| restating the record "correctly" from the repaired mart | matches neither accepted restatement — the record is what was reported, not what is true now |

The `full_refresh` baseline does the first two and the suite asserts it: it must come back
`DESTROYED` / `REWRITTEN` on both, and still score above zero.

## 8. The two accepted restatements

The torn tick may be restated to **either**:

- **published** — the values the 2026-03-03 08:00 tick recorded, i.e. what a reader of the
  build that was still published would have returned; or
- **landing** — the values the 08:30 tick recorded, i.e. what the build that was landing
  would have returned.

Both are defensible and **the world does not disambiguate them**. Grading one would be
grading a coin flip, so `grade.py` accepts either, applied consistently across all four
regions of the tick. Mixing them, recomputing from the repaired mart, or deleting the tick
all fail.

## 9. What the agent must do

1. Account for the 2026-03-03 08:15 figure as a read landing inside the publish window.
2. State the figure: **15,385,785.19** (±0.2%).
3. Revert the DATA-5518 predicate and republish the mart with the segment restored.
4. Merge the APAC rate redelivery and reconvert staging.
5. Restate the torn tick, and nothing else in the record.
6. Make a read incapable of observing a partial publish, without disabling the extract.
7. Submit the structured final artifact (`agent.md` §9.2.5).

## 10. Hidden — grading zone only, never mounted in the episode

`/grading/expected.duckdb`, `/grading/manifest.yaml`, `/grading/grade.py`,
`/grading/solution.sh`.

**Track A must verify** that a reference-copy agent cannot `ATTACH` anything under `/grading`
from inside the episode. `reference_copy` scores **15/20** in fixtures *by design* — the
grader honestly reporting that a reachable reference can be copied. That check is a build
gate, not a test.

## 11. Determinism

Everything above generates from `seed = 4211`. Two from-scratch builds are **byte-identical**
(verified 2026-09-13: sha256 of `world.duckdb`, `expected.duckdb` and `sched_state.json` all
match across separate from-scratch runs). Every value derives from `hash(seed, row_id)` in SQL; no wall clock,
no Python-side RNG ordering, no thread-count dependence.

**Nothing races at world-build time.** On DuckDB appends never conflict (S611), so a live
race produces no error and nothing forces a particular winner — the world would not rebuild
identically from the seed. Every interleave is staged as **committed artifacts**: the
leftover relation, the scheduler log with explicit start/end stamps, and one snapshot row
whose fact-derived and dim-derived columns belong to different builds. The only thing that
actually runs concurrently is the scoring probe, at fixed statement offsets, driven in order
on two connections to one database.

**The mechanism is executed, not asserted.** `fixture_contract.py` reproduces the planted
interleave at the contract filename on every run. If a future engine makes renames
transactional, the fixture contract fails and the task is invalid — which is where we want to
find that out.

## 12. Evidence grounding

- **S605** *(repo, open GitHub issue, firsthand_anonymous, high confidence)* — the direct
  mechanism: a concurrent reader mid-swap on a create / rename-out / rename-in / commit
  rebuild sees a state the transaction was supposed to hide, in a real pipeline with an
  independently scheduled reader on a sub-minute cadence. The `__dbt_backup` relation and the
  final DROP sitting outside the transaction come from the same source's swap sequence.
- **S608** *(CircleCI public postmortem, firsthand_named, high confidence)* — the human
  sequence that makes the compensating change plausible rather than contrived: a report
  arrives, the first investigator cannot reproduce it, a wrong local theory is adopted, and
  the correction ships. Also the detection texture: first report 5h20m in, misread as one
  customer's own automation, metrics normal again before anyone looked.
- **S611** *(DuckDB docs)* — the buildability constraint. The engine will not raise anything,
  so the interleave must be staged as committed artifacts rather than produced live.

**S609 is excluded from this task's provenance.** It is the only G1 failure in the corpus
(`community.databricks.com` returns 403 to the gate's user agent) and A043's own author
recorded that A043 does not depend on it for mechanism. Excluding it is a local choice for
this task, not a corpus disposition — the record is not withdrawn.

**Honest status:** the *mechanism* rests on audit-clean sources and is executed against the
real engine in `fixture_contract.py`. No frequency claim is made or derivable. S605 is a
Redshift adapter issue; the DuckDB behaviour this task is built on was **measured here**
(DuckDB 1.5.5) rather than carried over from it.
