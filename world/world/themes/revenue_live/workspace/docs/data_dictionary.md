# Data dictionary

- `raw.daily_revenue` — as-landed order rows from the landing zone, one
  partition (`dt`) per landing day. `amount` is a string here; cast happens in
  staging. `terminal_ref` is the card terminal, empty for card-not-present
  orders from the legacy acquirer.
- `raw.load_checkpoint` — the loader's position: the newest `dt` loaded.
- `staging.stg_revenue` — typed order rows (`amount` decimal(12,2)), with
  payments test-harness traffic (`TEST-HARNESS`, `SMOKE-TEST`) removed.
- `warehouse.fct_revenue_lines` — one row per order line.
- `warehouse.fct_revenue` — one row per `order_date`: `orders`, `revenue`.

Dashboard `revenue_daily` reads `warehouse.fct_revenue`.
