# Architecture overview

How a night's order activity becomes the daily revenue number, and which
pieces are load-bearing when something disagrees.

## System at a glance

The order platform writes one CSV partition per business day and drops it at
`data/landing/daily_revenue/dt=<business date>/revenue.csv`. The partition
for day D lands at 00:00 UTC on D+1. That is an hour before the load that
consumes it. The pipeline for D runs entirely on calendar day D+1, UTC:

1. `daily_load` (01:00) rebuilds `raw.daily_revenue` from every partition on
   disk. It is a full reload, not an upsert: whatever is landed is what
   `raw` shows. The landing zone is immutable, so re-running the load is
   safe at any point in the day. That is the reason it is a reload rather
   than an append.
2. `dbt_build` (01:05, five minutes after the load) materialises staging
   and `warehouse.fct_revenue` and runs the data tests. A failed test fails
   the job and leaves the mart holding the previous night's rows.

The dashboard `revenue_daily` reads the mart directly; there is no separate
refresh job, so a stale mart is a stale dashboard with nothing in between to
blame. The pipeline's source is `dags/revenue_daily.py`, but the scheduler
runs a deployed copy. Treat the run history in `var/sched/state.json`, not
the checkout, as the schedule of record. Use `./run.sh dag-check` to find
drift between the two.

## DuckDB schema overview

One database file per environment (placed per ADR 0001), two schemas that
matter here.

`raw` mirrors the landing zone, partitioned by `dt`:

| table | grain | source partition |
|---|---|---|
| `raw.daily_revenue` | one row per `order_id` per `dt` | `data/landing/daily_revenue/dt=D/revenue.csv` |

`warehouse` is the mart the dashboard reads:

| table | grain |
|---|---|
| `warehouse.fct_revenue` | one row per `order_date` (`orders`, `revenue`) |

`staging.stg_revenue` is a view over `raw` that does the typing: `amount`
lands as a string and becomes decimal(12,2) there, never earlier. The mart
aggregates those typed rows to the day; never pre-aggregate them downstream
of it.

## Change control

The pipeline is a reviewed artefact. Changes ship through the DAG source
and land on the deployed copy. Drift between the two is a finding rather
than a convenience. Confirm the live state rather than the intention before
altering any pipeline parameter (the 01:00 load time, the five-minute gap,
the staging cast, or any rate or split convention in job code):

- active tickets in `fixtures/workflow/issues.yaml` (a parked ticket is not
  an approved change),
- the daemon trails in `var/logs/ingest_daemon.log` and
  `var/logs/sftp_sync.log`,
- the corrections register `warehouse.factory_corrections`, which records
  which business days were reopened, by whom, and under which ticket.

If those three disagree with what a document says, the document is stale,
not the state.

## Legacy migration notes

The dbt mart replaced the platform's pre-dbt reporting mart in the 2024
cutover. The old loader typed `amount` as decimal at load time. It also
precomputed the daily fact at write time. The cutover review kept the rows
identical. It moved typing into staging and aggregation into the mart. The
pre-dbt mart survives in some environments as a read-only attachment, the
decommissioned writer, kept for reconciliation and never a dbt target.

Two conventions from that programme are still load-bearing:

- The pre-cutover finance extracts applied their own fee and split
  conventions downstream of the warehouse. Anything of the sort still
  present in job code is a legacy placeholder. It stays until a ticket
  and the logs confirm it was retired. Do not delete or "fix" such logic
  on the strength of a comment alone. Do not assume it is active business
  policy either. Check `fixtures/workflow/issues.yaml` first. Check the
  audit lines in `var/logs/` too.
- Partner and reseller splits were standardised at cutover to be computed
  from the day's net revenue in the mart, never multiplied into raw order
  rows. Any deviation visible in `warehouse.fct_revenue` versus a
  hand-summed `raw.daily_revenue` is evidence to reconcile, not to copy
  forward.

Retirement itself follows two rules, not one. A closed business day waits
for a controller sign-off naming that day in
`warehouse.factory_corrections`. Pre-cutover split multipliers or fee
retention haircuts in the fold layer retire on log evidence, not a ticket.
They retire when `var/logs/ingest_daemon.log` records the partner extract
cutover delivering net `amount_cents` directly. Leaving them in past that
point double-adjusts every affected row.

Cutover review notes live with the decision records under `docs/adr/`; when
a discrepancy smells like a cutover leftover, read those before touching the
pipeline.
