# Metering and billing rules

## Counter and delta normalization

A `counter` heartbeat is cumulative since device boot. Usage is the current
counter minus the previous counter for that device. If the counter decreases,
the device reset: usage for that heartbeat is the current counter, never a
negative delta. `boot_id` helps operators identify the reboot, but a decrease
is treated as a reset even if a device failed to update its boot identifier.

A `delta` heartbeat already reports usage for its window. Use `delta_units`
directly; never subtract consecutive delta events. Edge rollups are likewise
pre-aggregated daily deltas. Their stable `rollup_id` is the idempotency key.

## Committed-tier pricing

Each customer/month has one current contract commit. For a locked period:

1. Sum `billable_units` across all three source kinds.
2. `overage_units = max(billable_units - committed_units, 0)`.
3. Divide overage into blocks of 1,000 units, rounding a partial block up.
4. `invoice_total_cents = monthly_minimum_cents + blocks *
   overage_cents_per_1000`.

All money calculations use integer cents. No exchange-rate conversion is
applied; the seed contracts are USD.

## Period close and late telemetry

A month's cutoff is the first instant of the next month plus the contract's
`lock_grace_days`. Usage collected before the cutoff is billable. Usage
collected at or after it stays in the fact with its reported units but has zero
billable units. This keeps late gateway flushes auditable without changing a
locked invoice.

The lock is evaluated against the advancing source collection watermark, not a
wall clock in a SQL session. The ERP handoff contains locked periods only.
Corrections to a locked period require a separate credit or debit memo approved
by Billing Operations; do not rewrite the handoff row.
