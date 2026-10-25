# Architecture overview

How a night's merchant risk feeds become an exposure fact is the subject of
this document. It also covers the committee sign-off and the treasury reserve
handoff. It notes which pieces are load-bearing when something disagrees.

## System at a glance

The acquiring platform drops dispute, KYC, volume, and payout-hold extracts
as CSV events. The extracts land under
`data/landing/<feed>/dt=<business date>/events.csv`. The arrival
`merchant_risk_feeds` is due at 00:30 UTC. It covers the previous UTC
business date. The pipeline for business date D runs entirely on calendar
day D+1, UTC:

1. `risk_feed_load` (01:00) rebuilds `raw.dispute_events`, `raw.kyc_reviews`,
   `raw.merchant_volume`, and `raw.payout_holds` from every partition on
   disk. It is a full reload, not an upsert: whatever is landed is what `raw`
   shows, no more and no less.
2. `merchant_exposure_build` (starts 10 minutes after the load) materialises
   staging and `warehouse.fct_merchant_exposure`, and runs the data tests. A
   failed test fails the job and leaves the mart holding the previous night's
   rows.
3. `reserve_handoff` (5 minutes after the exposure build) builds
   `warehouse.risk_committee_signoff` for the risk committee and
   `warehouse.treasury_reserve_handoff` for treasury operations. Reserve
   release requires a risk officer approval and a chargeback ratio below the
   policy threshold; a payout request alone does not authorise release.

The deployed job order is recorded in `orchestration/schedule.yml`. Treat
that file, not memory, as the schedule of record.

## DuckDB schema overview

One database file, two schemas.

`raw` mirrors the platform extracts, one table per feed, partitioned by `dt`:

| table | grain | source partition |
|---|---|---|
| `raw.dispute_events` | one row per dispute event per `dt` | `data/landing/dispute_events/dt=D/` |
| `raw.kyc_reviews` | one row per merchant review per `dt` | `data/landing/kyc_reviews/dt=D/` |
| `raw.merchant_volume` | one row per merchant volume snapshot per `dt` | `data/landing/merchant_volume/dt=D/` |
| `raw.payout_holds` | one row per payout hold per `dt` | `data/landing/payout_holds/dt=D/` |

`warehouse` is what the risk committee and treasury read:

| table | grain |
|---|---|
| `warehouse.fct_merchant_exposure` | one row per merchant per business date |
| `warehouse.risk_committee_signoff` | one row per merchant per review cycle |
| `warehouse.treasury_reserve_handoff` | one row per merchant per reserve period |

Staging models (`stg_*`) are views over `raw`. The fact and handoffs are
tables. Exposure and reserve amounts are merchant-grain measures. Downstream
consumers aggregate those measures. Never pre-aggregate them in a report
extract.

## Change control

The fact and the handoffs are reviewed artefacts. A model change ships
through the pull request and CI before it reaches the committee. Confirm the
live state rather than the intention before altering any pipeline parameter.
Such parameters include rolling windows and chargeback warning and critical
thresholds. They also include reserve coverage days, basis-point rates, and
hold-cycle conventions encoded in job code:

- active tickets in `fixtures/workflow/issues.yaml` (a parked ticket is not
  an approved change),
- the daemon trails in `var/logs/ingest_daemon.log` and
  `var/logs/sftp_sync.log`,
- the corrections register `warehouse.factory_corrections`, which records
  which business dates were reopened, by whom, and under which ticket.

If those three disagree with what a document says, the document is stale,
not the state.

## Legacy migration notes

The exposure mart replaced the old processor risk extracts in the 2024
cutover. At that time, reserve calculation moved out of a treasury
spreadsheet into the nightly build. Two conventions from that programme are
still load-bearing:

- The pre-cutover extracts applied their own reserve uplift and hold-release
  conventions downstream of the warehouse. Those conventions were reviewed at
  cutover. Anything of that sort still present in job code is a legacy
  placeholder. Do not delete or "fix" such logic on the strength of a comment
  alone. A ticket and the logs must confirm retirement first. Do not assume
  the logic is active risk policy either. Check
  `fixtures/workflow/issues.yaml` and the audit lines in `var/logs/` first.
- Acquirer and processing-partner splits were standardised at cutover to be
  computed from exposure amounts in the mart, never multiplied into raw
  volume rows. Any deviation visible in `warehouse.fct_merchant_exposure`
  versus `raw.merchant_volume` is evidence to reconcile, not to copy forward.

Retirement evidence depends on the kind of legacy logic. Restating a closed
business date is governance. It waits for a risk officer sign-off in
`warehouse.factory_corrections` naming that date. Pre-cutover feed split
multipliers in the fold layer are engineering. Fee retention haircuts in the
fold layer are also engineering. They retire when
`var/logs/ingest_daemon.log` records the extract cutover active. At that
point, the feed delivers net amount_cents directly. A leftover multiplier or
haircut double-adjusts every affected row. Remove it on that log evidence.

Historical cutover review notes live with the architecture decision records
under `docs/`. When a discrepancy smells like a cutover leftover, read those
notes before touching the pipeline.
