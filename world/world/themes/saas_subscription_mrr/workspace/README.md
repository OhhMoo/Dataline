# Subscription revenue data platform

This repository turns billing webhook extracts into the Finance MRR/ARR model and the monthly ASC-606 recognition ledger. The DuckDB warehouse lives outside the git checkout at `../var/warehouse/warehouse.duckdb` by default.

## Nightly path

1. `ingest_webhooks` rebuilds `raw.subscription_events` and `raw.invoice_webhooks` from `data/landing/`.
2. `fold_subscriptions` builds the typed staging views, `warehouse.fct_mrr_daily`, and `warehouse.dim_account`.
3. `revrec_window_close` builds `warehouse.revrec_close_window`, the account-period close ledger consumed by Finance.

## Local commands

```bash
./run.sh ingest
./run.sh fold
./run.sh close
./run.sh test
```

`./run.sh build` runs the complete dbt build. Override `DBT_WAREHOUSE_PATH` to use a scratch warehouse. The scheduled deployment renders its own single-threaded dbt profile.

Start with `docs/data_dictionary.md` and `docs/revenue_recognition_policy.md` before changing a model.
