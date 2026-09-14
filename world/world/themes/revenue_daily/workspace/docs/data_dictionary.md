# Data dictionary

- `raw.daily_revenue` — as-landed order rows from the landing zone.
  `amount` is a string here; cast happens in staging.
- `staging.stg_revenue` — typed order rows (`amount` decimal(12,2)).
- `warehouse.fct_revenue` — one row per `order_date`: `orders`, `revenue`.

Dashboard `revenue_daily` reads `warehouse.fct_revenue`.
