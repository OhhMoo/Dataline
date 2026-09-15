# Changelog

Newest first. One entry per change that someone on call could be woken by.

## 2026-02-18
- `fct_revenue` gained `not_null` + `unique` tests on `order_date`. The grain
  was only ever documented in the data dictionary; now it is enforced.

## 2026-01-27
- Moved the dbt profile into `config/profiles.yml` and had `run.sh` export
  `DBT_PROFILES_DIR`. Two people had different `~/.dbt/profiles.yml` files and
  got different row counts out of the same model for a week.

## 2025-12-03
- `loader/load_daily_revenue.py` extracted from the platform ingestion module
  so the load can be re-run by hand during an incident.

## 2025-11-11
- `run.sh` replaced the README's list of commands. CI runs `./run.sh check`.

## 2025-09-05
- Initial pipeline: daily landing -> `raw.daily_revenue` -> `stg_revenue` ->
  `fct_revenue`, plus the `revenue_daily` dashboard.
