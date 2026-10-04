# Runbook: the nightly run

All UTC. The pipeline for business date D runs on D+1.

| time | job | does |
|---|---|---|
| 02:00 | (the platform) | drops `data/landing/<entity>/dt=D/` |
| 02:30 | `daily_load` | `loader/load_extracts.py`: rebuilds `raw.*` from every partition |
| 02:40 | `dbt_build` | `dbt build`: staging, marts, tests, semantic layer |
| 06:00 | `bi_refresh` | `scripts/refresh_dashboards.py`: every dashboard re-reads the marts. Its own DAG - it does not wait for dbt |

Check `sched_state`: all three green for the night. Investigate the first
failure, not the last.

## The extract is late or missing

The load is a full reload, so a late partition needs no backfill:

1. Wait for `data/landing/*/dt=D/` to appear.
2. Trigger `daily_load`, then `dbt_build`.
3. Check `warehouse.fct_orders` has D.

The dashboards pick it up at their next scheduled refresh.

## dbt_build failed

The first failing node is in the run's error. A failing test on
`stg_order_lines` (unique `order_line_id`) or on
`assert_order_amount_matches_lines` almost always means the night's
`order_lines` file is short or arrived twice - look at
`data/landing/order_lines/dt=D/` before touching a model. Until the build
passes, the marts keep the previous night's rows.

## The load failed

Usually a malformed CSV: the loader reads every `.csv` under every `dt=`
folder, so one bad file fails the whole load, every night, until it is fixed
and re-landed.

## Never

- hand-edit `raw.*` or `warehouse.*`
- delete a landed partition without saying so in #data-incidents
- change a metric or a published dashboard during the month-end freeze
