# Northstar IoT metering and billing

Northstar meters telemetry from enterprise device fleets and turns monthly
committed-tier contracts into ERP-ready invoices. The repository contains the
nightly loader, dbt models, operating docs, and orchestration definition.

## Nightly path (UTC)

1. `00:30` gateways land heartbeats, store-and-forward rollups, and contract
   commits under `data/landing/<entity>/dt=<usage day>/`.
2. `01:00` `load_usage` runs `loader/load_usage.py`, rebuilding all three raw
   tables from every landed file.
3. `01:10` `finalize_billing` runs `dbt build`, including data tests. It
   publishes `warehouse.fct_billable_usage`,
   `warehouse.billing_period_lock`, and `warehouse.invoice_run_handoff`.

An arrival on day N covers business day N-1. A late rollup is written into its
original usage-day partition, not the arrival-day partition, so a full reload
naturally picks up multi-day backfills.

## Local commands

```bash
./run.sh env
./run.sh load
./run.sh build
./run.sh test
```

Use `./run.sh` rather than a bare dbt invocation so the warehouse path and
single-threaded profile are consistent. See `docs/data_dictionary.md` for the
table grain and `docs/billing_rules.md` for counter, overage, and lock rules.
