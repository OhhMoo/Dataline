# Data dictionary

All timestamps and billing dates are UTC. `dt` on a raw table is the usage-day
landing partition, not necessarily the day the file arrived.

## Raw sources

### raw.meter_heartbeats

One row per direct-device heartbeat. `event_kind` is `counter` or `delta`.
Counter rows carry `boot_id` and a cumulative `counter_value`; delta rows carry
`delta_units`. The unused numeric field is null. `observed_at` is device time
normalized to UTC; `collected_at` is when the cloud collector accepted it.

### raw.usage_rollups

One row per edge device and non-overlapping daily window. `rollup_id` is stable
across retries. `window_start` is inclusive and `window_end` exclusive. `units`
is already a window total, not a cumulative counter. A gateway that reconnects
writes several older partitions in one flush, all with the flush's
`collected_at`.

### raw.contract_commits

One row per customer, billing month, and contract version. It contains the
committed telemetry units, monthly minimum in cents, overage cents per 1,000
units, lock grace days, and currency. The latest collected version wins.

## Warehouse

### warehouse.fct_billable_usage

One row per `usage_id`: a direct heartbeat or an edge daily rollup. Important
columns:

- `reported_units`: normalized usage before the billing lock.
- `counter_reset`: true where a monotonic counter decreased and was rebased.
- `billable_units`: reported units accepted for invoice calculation; zero for
  usage collected at or after `lock_cutoff`.
- `billing_month`, `period_start`, `period_end`, `lock_cutoff`: contract
  calendar carried onto every usage row.

Direct and edge devices are disjoint, so their windows do not double count.

### warehouse.billing_period_lock

One row per customer and billing month. `source_watermark` is the latest raw
telemetry collection timestamp. A period is locked once that watermark reaches
`lock_cutoff`. `late_units_excluded` remains visible for close reconciliation.

### warehouse.invoice_run_handoff

One row per locked customer and billing month, ready for the ERP consumer.
`received_units` includes all reported usage; `billable_units` excludes
post-cutoff usage. The invoice is the monthly minimum plus rounded-up overage
blocks. Both integer-cent amounts and a decimal `invoice_total` are provided.
