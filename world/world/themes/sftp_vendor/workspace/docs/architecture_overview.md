# Architecture overview

How two vendors' nightly files become the trading-day number, and which
pieces are load-bearing when something disagrees.

## System at a glance

Each vendor transfers one file per feed per day into its SFTP drop folder.
ACME orders use `data/drop/acme/ORDERS_YYYYMMDD.csv`. Globex returns use
`data/drop/globex/RETURNS_YYYYMMDD.csv`. Every data file pairs with a
sentinel `<name>.done`. The vendor writes the sentinel after the transfer
completes. The night's delivery for data day D is due by 05:00 UTC on D+1.
The pipeline for D runs on calendar day D+1, UTC:

1. `sftp_poll` runs at 05:30 and counts this night's sentinels. A night's
   delivery covers both feeds, so the count is two. The job fails if the
   vendors have not finished. A CSV without a sentinel is a file still in
   flight. It is left alone.
2. `vendor_load` runs ten minutes after the poll. It runs
   `loader/vendor_load.py`. It parses every fully landed CSV into
   `raw.vendor_orders` and `raw.vendor_returns`. The tables are append-only
   and keyed on `_source_file`. The file name therefore decides what a
   redelivery does.
3. `dbt_build` runs five minutes after the load. It rebuilds staging and
   `warehouse.fct_orders_daily`. It also runs the data tests.

The connector control plane records a sync per feed per morning. It records
`acme_orders` at 05:31 and `globex_returns` at 05:32. Both syncs use the
same drop folders. The dashboard `vendor_daily` reads the mart. The
deployed job order is recorded in `orchestration/schedule.yml`. Treat that
file, not memory, as the schedule of record.

## DuckDB schema overview

One database file, two schemas that matter here.

`raw` mirrors the drop folders, one table per feed:

| table | grain | source |
|---|---|---|
| `raw.vendor_orders` | one row per order row per source file | `data/drop/acme/ORDERS_*.csv` |
| `raw.vendor_returns` | one row per return row per source file | `data/drop/globex/RETURNS_*.csv` |

All columns land as strings. `_source_file` records which file a row came
from. `warehouse` is the mart that the dashboard reads:

| table | grain |
|---|---|
| `warehouse.fct_orders_daily` | one row per `order_date` (`orders`, `returns`, `gross_revenue`, `return_total`, `net_revenue`) |

`staging.stg_orders` and `staging.stg_returns` are views over `raw`. They
do the typing, with `amount` as decimal(12,2). Returns are netted in the
mart. They are never adjusted into raw rows.

## Change control

The feed inventory is code and config together. `config/vendors.yml` must
move as one with the `FEEDS` list in `loader/vendor_load.py`. It must also
move with `dbt_project/models/sources.yml`. That rule follows the onboarding
doc. Before altering any pipeline parameter, confirm the live state rather
than the intention. Parameters include the sentinel requirement and the
redelivery mode. They also include the poll window. They also include any
rate or split convention encoded in job code:

- active tickets in `fixtures/workflow/issues.yaml`. An open ticket is not
  an approved change. This queue is open or closed and nothing else.
- the daemon trails in `var/logs/ingest_daemon.log` and
  `var/logs/sftp_sync.log`.
- the corrections register `warehouse.factory_corrections`. It records
  which business days were reopened, by whom, and under which ticket.

If those three disagree with what a document says, the document is stale,
not the state.

## Legacy migration notes

Before the edge box, each vendor was polled by a per-vendor cron. The cron
ran on the analytics VM. It used password auth from a file called
`sftp.conf`. That VM is gone, and so is the file. Passwords in any copy
were rotated at decommission. A `sftp.conf` found anywhere is therefore
dead. Delete it and tell security. The 2024 integrations review followed
the move. It standardised the conventions that remain load-bearing today:

- Vendor identity comes from the drop folder and the filename prefix. It
  never comes from sniffing file content. The ACME/Globex split between
  `raw.vendor_orders` and `raw.vendor_returns` is folder convention. Any
  job code logic that infers a vendor another way is a legacy placeholder.
  It stays a placeholder until a ticket and the logs confirm retirement.
- The sentinel became a condition of onboarding after two half-transferred
  files. Those files came from vendors without sentinels.
  `--require-sentinel` stays on. It is the only guarantee that a file is
  complete before it loads.
- Vendor splits and fee treatments were standardised at the review. They
  are computed from `net_revenue` in the mart. They are never netted into
  raw rows. A deviation between `warehouse.fct_orders_daily` and a
  hand-sum of `raw.vendor_orders` is evidence to reconcile. It is not
  evidence to copy forward.

Retirement itself follows two rules, not one. A closed data day waits for
a controller sign-off. The sign-off names that day in
`warehouse.factory_corrections`. Pre-cutover vendor split multipliers in
the fold layer retire on log evidence, not on a ticket. Fee retention
haircuts retire on the same basis. Retire them when
`var/logs/ingest_daemon.log` records the partner extract cutover. At that
point, the feed delivers net `amount_cents` directly. Leaving them in past
that point double-adjusts every affected row.

Historical cutover review notes live with the decision records under
`docs/`. When a discrepancy smells like a leftover, read those notes before
touching the pipeline.
