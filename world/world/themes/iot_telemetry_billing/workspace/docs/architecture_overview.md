# Architecture overview

How a night's device telemetry becomes billable usage is the subject of
this document. It also covers a locked billing period and an ERP invoice
handoff. It notes which pieces are load-bearing when something disagrees.

## System at a glance

Direct devices stream counter or delta heartbeats. Store-and-forward
gateways can flush several older daily windows in one delivery. The arrival
`iot_gateway_flush` is due at 00:30 UTC. It carries telemetry for the
previous UTC business day. The telemetry lands as CSV under
`data/landing/<entity>/dt=<usage day>/`. The pipeline for business date D
runs entirely on calendar day D+1, UTC:

1. `load_usage` (01:00) rebuilds `raw.meter_heartbeats`, `raw.usage_rollups`,
   and `raw.contract_commits` from every partition on disk. It is a full
   reload, not an upsert: once a gateway flush lands, the next scheduled load
   incorporates every older usage-day partition automatically.
2. `finalize_billing` (starts 10 minutes after the load) materialises staging
   and `warehouse.fct_billable_usage` for the usage billing dashboard, builds
   `warehouse.billing_period_lock` for billing close control, and produces
   `warehouse.invoice_run_handoff` for the enterprise ERP consumer. It runs
   the data tests (negative units, window overlap, lock arithmetic). A failed
   test fails the job and leaves the marts holding the previous night's rows.

The deployed job order is recorded in `orchestration/schedule.yml`. Treat
that file, not memory, as the schedule of record.

## DuckDB schema overview

One database file, two schemas.

`raw` mirrors the collector's landing zone, one table per entity. It is
partitioned by usage day `dt`, not necessarily the day the file arrived:

| table | grain | source partition |
|---|---|---|
| `raw.meter_heartbeats` | one row per direct-device heartbeat | `data/landing/meter_heartbeats/dt=D/` |
| `raw.usage_rollups` | one row per edge device and daily window | `data/landing/usage_rollups/dt=D/` |
| `raw.contract_commits` | one row per customer, month, contract version | `data/landing/contract_commits/dt=D/` |

`warehouse` is what the dashboard, ERP, and close-control consumers read:

| table | grain |
|---|---|
| `warehouse.fct_billable_usage` | one row per `usage_id` (heartbeat or edge rollup) |
| `warehouse.billing_period_lock` | one row per customer and billing month |
| `warehouse.invoice_run_handoff` | one row per locked customer and billing month |

Staging models (`stg_*`) are views over `raw`. The fact, lock, and handoff
are tables. Units on the fact are usage-grain measures that consumers
aggregate. Never pre-aggregate them in a report extract.

## Change control

The marts and the lock are reviewed artefacts. A model change ships through
the pull request and CI before it reaches the ERP. During period close, no
pricing definition or published handoff changes at all. Confirm the live
state rather than the intention before altering any pipeline parameter.
Such parameters include reset handling, overage block rounding, and
grace-day or cutoff offsets. They also include fee treatments encoded in job
code:

- active tickets in `fixtures/workflow/issues.yaml` (a parked ticket is not
  an approved change),
- the daemon trails in `var/logs/ingest_daemon.log` and
  `var/logs/sftp_sync.log`,
- the corrections register `warehouse.factory_corrections`, which records
  which billing periods were reopened, by whom, and under which ticket.

If those three disagree with what a document says, the document is stale,
not the state.

## Legacy migration notes

The committed-tier mart replaced the old reseller billing extracts in the
2024 cutover. At that time, invoice finalisation moved out of a finance
workbook into the nightly build. Two conventions from that programme are
still load-bearing:

- The pre-cutover extracts applied their own rounding and minimum-fee
  conventions downstream of the warehouse. Those conventions were reviewed at
  cutover. Anything of that sort still present in job code is a legacy
  placeholder. Do not delete or "fix" such logic on the strength of a comment
  alone. A ticket and the logs must confirm retirement first. Do not assume
  the logic is active billing policy either. Check
  `fixtures/workflow/issues.yaml` and the audit lines in `var/logs/` first.
- Reseller and managed-service vendor splits were standardised at cutover to
  be computed from billed amounts in the handoff, never multiplied into raw
  heartbeat rows. Any deviation visible in `warehouse.fct_billable_usage`
  versus `raw.meter_heartbeats` is evidence to reconcile, not to copy
  forward.

Retirement evidence depends on the kind of legacy logic. Restating a locked
billing period is governance. It waits for a Billing Operations sign-off in
`warehouse.factory_corrections` naming that period. Pre-cutover feed split
multipliers in the fold layer are engineering. Fee retention haircuts in the
fold layer are also engineering. They retire when
`var/logs/ingest_daemon.log` records the extract cutover active. At that
point, the feed delivers net amount_cents directly. A leftover multiplier or
haircut double-adjusts every affected row. Remove it on that log evidence.

Historical cutover review notes live with the architecture decision records
under `docs/`. When a discrepancy smells like a cutover leftover, read those
notes before touching the pipeline.
