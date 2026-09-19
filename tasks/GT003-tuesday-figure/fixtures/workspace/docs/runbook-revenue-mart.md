# Runbook — revenue mart and the board extract

Owner: Data Platform. Last reviewed 2026-01-19 (parts of this are out of date; the
`region_code` section was removed when the LATAM migration finished).

## What runs

| job | schedule | script |
|---|---|---|
| `daily_revenue_load` | `0 5 * * *` | loads yesterday's vendor bundle into `raw.*` |
| `mart_hourly_rebuild` | `7 * * * *`, plus on completion of `daily_revenue_load` | `pipelines/mart_rebuild.sql` |
| `bi_revenue_extract` | `0,15,30,45 * * * *` | `pipelines/bi_extract.sql` |

The rebuild is on `:07` and the extract on the quarter hours so that the two do
not normally overlap. `daily_revenue_load` also triggers a rebuild when it
finishes, so that a late bundle is published without waiting for the next hour.

## How the mart is published

`mart_rebuild.sql` does not replace the mart in place. It builds each relation
aside under a `__dbt_tmp` name, renames the live relation out to `__dbt_backup`,
renames the new one in, and drops the backup. That way the mart is never absent
from the catalog, which matters because the board pack and the extract both read
it by name on a schedule nobody coordinates with us.

The two relations are published one after the other, fact first.

If a rebuild is interrupted, a `__dbt_backup` relation can be left behind. The
next run will then fail on the rename with `Table with name "..." already
exists`. Drop the leftover and re-run.

## `warehouse.rev_snapshot`

This is the **reporting record**, not a cache. Finance quotes from it
historically and the quarterly pack is reconciled against it, so:

- rows are appended, one per region per tick, and are never rebuilt in bulk;
- the pack joins on the tick, so a tick must not be left with missing regions;
- if a tick is found to have recorded a figure the mart never actually held, it
  is restated in place and the restatement is noted in the incident record.

Do not regenerate `rev_snapshot` from the mart. The mart moves; the record of
what was reported at a point in time does not.

## FX

`raw.fx_rates_daily` is loaded from the vendor's daily file. Files land in
`data/landing/fx/` and the loader stages them in `raw.fx_rates_inbox` before
merging them into `raw.fx_rates_daily`. Redeliveries and backfills arrive in the
same place and are merged the same way.

`staging.stg_revenue_lines` resolves the rate AS OF the order date, so a date
with no published rate takes the last rate published before it. This is what
lets the weekend work without a calendar table.

## Known gaps

- Nothing checks that `raw.fx_rates_daily` is complete per currency per day.
- Nothing checks that a rebuild finished. A partially applied rebuild is only
  visible as the leftover relation and as the next run's failure.
