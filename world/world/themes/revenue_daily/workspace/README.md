# revenue workspace

- `dbt_project/` — dbt Core project (staging view + warehouse table over `raw`)
- `data/landing/daily_revenue/dt=*/revenue.csv` — daily order landings
- `docs/` — data dictionary and runbooks

The warehouse is reachable only through the sql_* tools, never via fs_*.
