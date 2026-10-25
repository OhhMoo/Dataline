# Healthcare claims EDI workspace

Synthetic payer 837/835 deliveries flow through a full raw reload into dbt
staging views and warehouse tables for adjudication, actuarial reserving,
provider capitation, and CMS encounter cutoff reconciliation.

All member identifiers are generated tokens. The project contains no real
patient information or real EDI transactions.

## Layout

- `data/landing/edi_837_claims/dt=*/` - claim versions with frequency 1, 7, or 8
- `data/landing/edi_835_remits/dt=*/` - one remittance per claim version
- `data/landing/provider_roster/dt=*/` - synthetic contract and capitation roster
- `loader/load_edi.py` - deterministic full reload of all three raw tables
- `dbt_project/` - current-version staging, warehouse marts, and data tests
- `dags/` - source for the four-task nightly schedule
- `docs/` - data dictionary, EDI rules, and operating runbook
- `config/profiles.yml` - single-threaded DuckDB development and CI targets

## Commands

```sh
./run.sh env
./run.sh load
./run.sh build
./run.sh test
```

`run.sh` sources `.envrc`, uses `config/profiles.yml`, and resolves the warehouse
to `../var/warehouse/warehouse.duckdb`. Override `DBT_WAREHOUSE_PATH` for an
isolated local build. dbt target and log output under `dbt_project/` is ignored.

## Nightly cycle

The delivery lands at 00:30 UTC. `nightly_edi_load` starts at 01:10;
`adjudication_build`, `settlement_handoffs`, and `cms_window_close` follow on
scheduler dependencies. All raw loads are full reloads, so rerunning the chain
after replacing a landed partition is idempotent.
