# finance close workspace

- `finance/workbooks/CLOSE_YYYYMM.xlsx` — the monthly close workbook; the
  GL team closes the month in it and it is the source of record
- `loader/import_close.py` — imports the workbooks into `raw` (scheduler job
  `close_import`, day 1 08:00 UTC)
- `dbt_project/` — dbt Core project (staging view + warehouse table over `raw`)
- `docs/` — data dictionary and the close runbook

The warehouse is reachable only through the sql_* tools, never via fs_*.
