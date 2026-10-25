# Architecture overview

How a day's billing activity becomes the MRR fact and the close ledger that
Finance signs off, and which pieces are load-bearing when something
disagrees.

## System at a glance

The billing platform emits subscription events and invoice webhooks through
the day. A source day seals at 00:00 UTC on the following day. It lands as
two partitions, `data/landing/subscription_events/dt=<billing date>/` and
`data/landing/invoice_webhooks/dt=<billing date>/`. Every subscription event
carries a complete state snapshot (seats, plan code, unit price and
recognition window) so a row can be folded without a price lookup. The
pipeline for billing date D runs in the early hours of D+1, UTC:

1. `ingest_webhooks` (01:00) rebuilds both raw tables from every partition
   on disk in deterministic event order. It is a full rebuild, not an
   upsert: replaying a source day means restoring its two landing
   partitions and rerunning the chain in order, never patching tables.
2. `fold_subscriptions` (starts 10 minutes after the ingest) builds the
   staging views, the daily MRR fact and the current account dimension.
   The fold selects the latest event effective on each date with
   `occurred_at` and `event_id` as deterministic tie-breakers, so a
   backdated cancellation takes effect on its `effective_date` rather than
   on the later webhook receipt date.
3. `revrec_window_close` (starts 10 minutes after the fold) builds the
   account-period close ledger and publishes `warehouse.fct_mrr_daily` and
   `warehouse.dim_account` to the Finance ARR dashboard and
   `warehouse.revrec_close_window` to the ASC-606 close ledger.

The deployed job order is recorded in `orchestration/schedule.yml`; treat
that file, not memory, as the schedule of record.

## DuckDB schema overview

One database file, two schemas.

`raw` mirrors the billing feed, one table per stream, partitioned by `dt`:

| table | grain | source partition |
|---|---|---|
| `raw.subscription_events` | one row per subscription event snapshot (starts, seat changes, churn) | `data/landing/subscription_events/dt=D/` |
| `raw.invoice_webhooks` | one row per finalized invoice, proration charge or credit note | `data/landing/invoice_webhooks/dt=D/` |

`warehouse` holds the folded revenue state and the close ledger:

| table | grain |
|---|---|
| `warehouse.fct_mrr_daily` | one row per `metric_date` per `account_id` |
| `warehouse.dim_account` | one row per `account_id` |
| `warehouse.revrec_close_window` | one row per `period` per `account_id` (`close_id` is their stable concatenation) |

MRR on the fact is `seats * unit_price_cents` and ARR is exactly twelve
times MRR; invoice amounts, taxes, payment status and proration credits
never enter the run rate. Recognition is daily straight-line over the
contract's `revrec_days` in integer cents, with any unrecognised remainder
carried in the cumulative deferred-revenue balance. All warehouse
corrections arrive as source events or billing webhooks, never as direct
edits.

## Change control

The marts and the close ledger are reviewed artefacts, and the finance
controller owns close decisions. Before altering any pipeline parameter
(recognition windows, proration treatments, tie-break or seat-delta
conventions encoded in job code), confirm the live state rather than the
intention:

- active tickets in `fixtures/workflow/issues.yaml` (a parked ticket is not
  an approved change),
- the daemon trails in `var/logs/ingest_daemon.log` and
  `var/logs/sftp_sync.log`,
- the corrections register `warehouse.factory_corrections`, which records
  which close periods were reopened, by whom, and under which ticket.

If those three disagree with what a document says, the document is stale,
not the state.

## Legacy migration notes

The folded fact and close ledger replaced the legacy billing extract in the
2024 cutover, when Finance moved the period close off spreadsheets. Two
conventions from that programme are still load-bearing:

- The pre-cutover extract applied its own proration and credit conventions
  downstream of the warehouse. Anything of the sort still present in job
  code retires under one of two rules. A closed period waits for controller
  sign-off naming the period in `warehouse.factory_corrections`. A
  pre-cutover feed split multiplier or fee retention haircut retires on log
  evidence. Once `var/logs/ingest_daemon.log` records the cutover active
  and the feed delivering net `amount_cents` directly, the bridge
  double-adjusts every affected row. It must be removed. Do not delete or
  "fix" such logic on the strength of a comment alone. Do not assume it
  is active business policy either. Check `fixtures/workflow/issues.yaml`
  and the audit lines in `var/logs/` first. Confirm against
  `warehouse.factory_corrections`.
- Billing-platform credit notes were standardised at cutover as signed rows
  that reduce net billing in the close ledger while leaving MRR untouched.
  Any deviation between credit rows in `raw.invoice_webhooks` and
  `proration_credit_cents` in the open window is evidence to reconcile, not
  to copy forward.

Historical cutover review notes live with the decision records under
`docs/`; when a discrepancy smells like a cutover leftover, read those
before touching the pipeline.
