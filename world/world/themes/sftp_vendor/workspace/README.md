# vendor feeds

Three vendors drop files on SFTP, we mirror them to `data/drop/<vendor>/`,
load them into `raw.*`, and dbt does the rest.

(Two vendors. Initech offboarded in November. `config/vendors.yml` still
carries them because the sweeper needs the folder.)

## layout

- `data/drop/<vendor>/` — mirrored drop folders. one `<FEED>_<YYYYMMDD>.csv`
  plus a `<name>.done` sentinel per feed per day
- `loader/vendor_load.py` — drop folder -> `raw.*`
- `dbt_project/` — staging views + `fct_orders_daily`
- `config/vendors.yml` — per-vendor connection + feed conventions
- `config/profiles.yml` — dbt targets. prod is snowflake and does not work
  from here on purpose
- `dags/vendor_feeds.py` — the airflow dag
- `docs/` — data dictionary + the runbook you actually want
- `NOTES.md` — what changed and why
- `run.sh` — everything

## quick

```sh
direnv allow       # optional, run.sh sources .envrc anyway
./run.sh poll      # what has landed
./run.sh load      # drop folder -> raw
./run.sh build     # dbt run + test
```

## the sftp pull itself is not in this repo

A cron on the edge box does the actual `sftp get` into `data/drop/`. It reads
`config/vendors.yml` and the vault. If files stop arriving, that is where to
look, and it is not something you can fix from here.

## gotchas

- The loader only picks up a CSV whose `.done` sentinel exists. A CSV without
  one is a transfer in flight. Do not "fix" this by passing
  `--no-require-sentinel`.
- `--on-redelivery` defaults to `overwrite`. Leave it. See `NOTES.md` 2026-02-02.
- `warehouse.duckdb` is outside the repo at `../var/warehouse/`. Attach it
  under an alias if you open it yourself — the file stem and the `warehouse`
  schema have the same name and two-part references go ambiguous otherwise.
