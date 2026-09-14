# vendor feeds workspace

- `data/drop/<vendor>/` — SFTP drop folders; one `<FEED>_<YYYYMMDD>.csv` plus a
  `<name>.done` sentinel per feed per day
- `loader/vendor_load.py` — drop-folder loader into `raw.*`, run by the
  `vendor_load` cron job after the morning poll
- `dbt_project/` — dbt Core project (staging views + warehouse table over `raw`)
- `docs/` — data dictionary and runbooks

The warehouse is reachable only through the sql_* tools, never via fs_*.
