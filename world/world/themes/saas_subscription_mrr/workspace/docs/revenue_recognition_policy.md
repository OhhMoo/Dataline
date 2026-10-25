# Subscription MRR and revenue recognition policy

## Recurring metrics

MRR measures the active contractual run rate, not cash collected. Seat changes alter MRR on their effective date. Invoice amounts, taxes, payment status, and proration credits do not enter the MRR calculation. ARR is exactly `12 * MRR`.

Every subscription event carries a complete state snapshot. A backdated cancellation becomes effective on its `effective_date`, not on the later webhook receipt date. This allows the nightly rebuild to present the corrected subscription history while preserving the original source event.

## ASC-606 subledger

Subscription consideration is recognized daily on a straight-line basis over the contract's `revrec_days` service window. All calculations use integer cents. Daily division discards no source amount silently: unrecognised consideration remains in the cumulative deferred-revenue balance until later recognition or a credit.

The close ledger is produced at account and calendar-period grain. Positive invoices and proration charges increase net billing; signed proration credits reduce it. Recognition and billing are shown separately so Finance can reconcile run-rate changes to cash and deferred revenue without treating ARR as revenue.

Periods before the latest materialized period are marked `closed`; the latest period remains `open` for daily recognition. Corrections must arrive as source events or billing webhooks, never as direct warehouse edits.
