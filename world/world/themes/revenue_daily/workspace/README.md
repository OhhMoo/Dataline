# revenue workspace

Daily order revenue: landing zone -> `raw` -> `staging` -> `warehouse`, one
dbt project, one Airflow DAG, one dashboard.

## Layout

- `dags/` — the Airflow DAG. Read `dags/README.md` before you edit it; the
  copy that runs is the deployed one, not this one.
- `dbt_project/` — dbt Core project (staging view + warehouse table over `raw`)
- `loader/load_daily_revenue.py` — full reload of `raw.daily_revenue` from the
  landing zone
- `data/landing/daily_revenue/dt=*/revenue.csv` — daily order landings
- `config/profiles.yml` — dbt targets (`dev`, `ci`, `prod`)
- `docs/` — data dictionary, runbooks, ADRs
- `run.sh` — every command in this README is a target

## Getting started

```sh
direnv allow             # or skip it: run.sh sources .envrc itself
./run.sh env              # check what it resolved to
./run.sh check            # dbt parse + dag-check, same as CI
./run.sh build            # dbt run + dbt test
```

`run.sh` sources `.envrc` and exports `DBT_PROFILES_DIR=config`, so it uses
the committed profile rather than your `~/.dbt/profiles.yml`. A bare `dbt`
will not. Personal overrides go in `.env`, which `.envrc` sources on top of
itself and which is gitignored.

## Where the warehouse is

`../var/warehouse/warehouse.duckdb`, outside the repo. Override with
`DBT_WAREHOUSE_PATH`. Reasoning in `docs/adr/0001-one-warehouse-per-environment.md`
— including why you must `ATTACH` it under an alias instead of opening it
directly.

## Schedule

`daily_load` at 01:00 UTC, `dbt_build` five minutes later. Both should be
green in `sched_state`; investigate the first non-success run, not the last.

## Contacts

`#data-platform`. The weekly revenue reconciliation against finance was
retired in January — the spreadsheet it wrote to is still on the share, and
still stale, and is not worth reading.
