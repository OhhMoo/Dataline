-- BI revenue extract.
--
--   job:  bi_revenue_extract
--   cron: 0,15,30,45 * * * *
--
-- Appends one row per region to warehouse.rev_snapshot for the tick it is
-- producing.  The runner substitutes :snapshot_ts with that tick.
--
-- CONTRACT: warehouse.rev_snapshot is the reporting record.  One row per region
-- per tick, appended, never rebuilt; the board pack joins on the tick.  The
-- columns are fixed:
--     snapshot_ts, region, mtd_revenue_usd, mtd_order_count, active_accounts
-- mtd_* are month-to-date over the mart's fact as at the tick; active_accounts
-- is the number of accounts the mart's dimension carries for that region.

insert into warehouse.rev_snapshot
with rev as (
    select d.region,
           round(sum(f.revenue_usd), 2)::decimal(16,2) as mtd_revenue_usd,
           sum(f.order_count)::bigint                  as mtd_order_count
    from warehouse.mart_revenue_fact f
    join warehouse.mart_revenue_dim  d on d.customer_id = f.customer_id
    where f.order_dt >= date_trunc('month', cast(:snapshot_ts as date))
    group by 1
),
acct as (
    select region, count(*)::bigint as active_accounts
    from warehouse.mart_revenue_dim
    group by 1
)
select cast(:snapshot_ts as timestamp),
       a.region,
       coalesce(r.mtd_revenue_usd, 0)::decimal(16,2),
       coalesce(r.mtd_order_count, 0)::bigint,
       a.active_accounts
from acct a
left join rev r on r.region = a.region;
