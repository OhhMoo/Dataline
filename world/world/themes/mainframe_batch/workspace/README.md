# claims workspace

- `copybooks/CLAIMS.CPY` — COBOL copybook for the mainframe claims extract
- `loader/load_extract.py` — batch-window loader: landing -> `raw.claims_extract`
- `dbt_project/` — dbt Core project (staging view + warehouse table over `raw`)
- `data/landing/claims_extract/dt=*/CLAIMS.DAT` — nightly extract landings
- `docs/` — data dictionary and runbooks

The warehouse is reachable only through the sql_* tools, never via fs_*.
