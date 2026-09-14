# Runbook: vendor feeds (sftp_poll -> vendor_load -> dbt_build)

## Drop conventions

Vendors transfer one file per feed per day into their SFTP drop folder:

- `data/drop/acme/ORDERS_YYYYMMDD.csv` — ACME order feed
- `data/drop/globex/RETURNS_YYYYMMDD.csv` — Globex returns feed

Every data file is paired with a sentinel `<name>.done` written by the vendor
after the transfer completes. The loader only picks up a CSV whose sentinel
exists; a file without one is left for the next poll. Files are processed in
filename order.

## Schedule

`sftp_poll` runs at 05:30 UTC. `vendor_load` starts ten minutes after the
poll and loads everything that has fully landed; `dbt_build` starts five
minutes after the load and rebuilds staging and the warehouse. All three
should be green in `sched_state`; investigate the first non-success run, not
the last.

## Loader settings

`loader/vendor_load.py --drop data/drop --db <warehouse.duckdb>`

- `--require-sentinel` (default on; `--no-require-sentinel` to disable).
  Keep this on: it is the only guarantee a file is complete before it loads.
- `--on-redelivery overwrite|append` (default `overwrite`). When a vendor
  redelivers a correction under a filename that has already been loaded,
  `overwrite` replaces the rows previously loaded from that filename;
  `append` adds the new content alongside the earlier load.

## Redelivery procedure

Vendors resend corrections under the same filename with a fresh sentinel.
The default `overwrite` setting handles this: the next load replaces the
file's earlier rows. Confirm with the vendor that a redelivery is a full
replacement of that day's feed before letting the loader pick it up, and
check the row counts in `raw.*` against the sentinel after the run.
