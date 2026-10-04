# commerce-analytics

Harrow & Finch's analytics repo: the shop platform's nightly extracts, the
warehouse, the dbt star schema and semantic layer, and the dashboards built on
them.

```
shop platform --02:00--> data/landing/ --02:30--> raw.* --02:40--> staging, warehouse (dbt)
                                                                        |
                                    Power BI / Tableau / Sigma <--06:00-+
```

## layout

- `data/landing/<entity>/dt=YYYY-MM-DD/<entity>.csv` - the nightly
  change-data-capture extract (orders, order_lines, customers, products),
  written by the platform's export job. Not by anything in this repo.
  `dt=2024-12-31` is the cutover snapshot: accounts and catalogue, no orders.
- `loader/load_extracts.py` - landing -> `raw.*`. Full reload of every
  partition, every night.
- `dbt_project/` - staging views, the star schema in `warehouse`
  (`models/marts`), data tests, the semantic layer (`models/semantic`) and the
  exposures (`models/exposures.yml`).
- `bi/` - the dashboards as code. See `bi/README.md`.
- `dags/` - the Airflow DAGs: `commerce_nightly` (load, then dbt) and
  `bi_refresh` (the 06:00 dashboard refresh). See `dags/README.md`.
- `scripts/` - `refresh_dashboards.py` (what `bi_refresh` runs) and
  `dag_check.py` (does `dags/` still match what the scheduler runs?).
- `docs/` - metric definitions, data dictionary, the nightly runbook.
- `config/profiles.yml` - dbt targets for this repo.
- `run.sh` - everything below.

## quick

```sh
direnv allow        # optional, run.sh sources .envrc itself
./run.sh load       # landing -> raw
./run.sh build      # dbt build: models, tests, semantic-layer validation
./run.sh sl         # list the semantic layer's metrics and saved queries
./run.sh refresh    # recompute every dashboard now (what the 06:00 job does)
./run.sh check      # dbt parse + dag-check
```

## things that surprise people

- **The dashboards refresh at 06:00 whether or not dbt has run.**
  `bi_refresh` is its own DAG on its own clock, not downstream of the build.
  ANA-219.
- **Net Revenue is by order date.** An order belongs to the day it was placed,
  in UTC, whatever happens to it afterwards. Details in `docs/metrics.md`.
- **Segments are current.** `dim_customer` is SCD1; an account upgraded to
  Enterprise is Enterprise for all of its history.
- `warehouse.duckdb` lives outside the repo at `../var/warehouse/`. If you open
  it yourself, ATTACH it under an alias: the file stem and the `warehouse`
  schema share a name and two-part references go ambiguous otherwise.
