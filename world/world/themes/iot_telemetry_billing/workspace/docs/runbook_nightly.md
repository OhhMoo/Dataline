# Nightly metering and close runbook

## Normal checks

- Confirm `load_usage` and `finalize_billing` both succeeded, in that order.
- Check that `warehouse.fct_billable_usage` includes the latest business day.
- Reconcile new edge rollups by `rollup_id` and `window_start`, not by arrival
  partition alone.
- During close, compare `invoice_run_handoff` with the locked rows in
  `billing_period_lock` and review `late_units_excluded`.

## Late or disconnected gateways

The loader is a full reload. Once a gateway flush lands, the next scheduled
load and dbt build incorporate every older usage-day partition automatically.
Do not copy a rollup into the arrival-day partition and do not subtract its
units from a counter feed. Direct and edge devices are disjoint by contract.

If a flush crosses a lock cutoff, leave the locked invoice unchanged. The fact
and lock table expose the excluded units for Billing Operations to assess a
separate adjustment.

## Counter anomalies

A lower counter after reboot is expected and produces a reset row. A negative
`reported_units` or `billable_units` fails dbt and must be investigated from
the raw heartbeat sequence. Never replace a reset with zero merely to make a
dashboard look smooth.

## Manual recovery

From the workspace run `./run.sh load`, then `./run.sh build`. Both commands
are safe to rerun over unchanged landing files. Do not hand-edit raw tables,
warehouse tables, or lock rows.
