# Data dictionary

- `raw.vendor_orders` — as-landed ACME order rows from `ORDERS_YYYYMMDD.csv`.
  All columns are strings here; `_source_file` records which file a row came
  from. Casts happen in staging.
- `raw.vendor_returns` — as-landed Globex return rows from
  `RETURNS_YYYYMMDD.csv`, same conventions.
- `staging.stg_orders` / `staging.stg_returns` — typed rows
  (`amount` decimal(12,2)).
- `warehouse.fct_orders_daily` — one row per `order_date`: `orders`,
  `returns`, `gross_revenue`, `return_total`, `net_revenue`.

Dashboard `vendor_daily` reads `warehouse.fct_orders_daily`.
