-- Board pack daily revenue publish.
--
--   job:   board_pack_publish
--   cron:  0 6 * * *
--   owner: Finance Systems (finance.board_pack_daily is their book; this job
--          writes it on their behalf and is the only thing that should)
--
-- One row per region per day from the revenue mart, for every reporting month
-- that is OPEN in finance.reporting_periods. Months already issued are outside
-- this statement. The contract for one is the wiki page finance/board-pack;
-- the calendar is docs/board-pack.md.

create schema if not exists finance;

delete from finance.board_pack_daily
where period_month in (select period_month from finance.reporting_periods
                       where status = 'open');

insert into finance.board_pack_daily (period_month, order_dt, region, revenue_usd, order_count)
select
    p.period_month,
    f.order_dt,
    d.region,
    round(sum(f.revenue_usd), 2)::decimal(16,2) as revenue_usd,
    sum(f.order_count)::bigint                  as order_count
from warehouse.mart_revenue_fact f
join warehouse.mart_revenue_dim  d on d.customer_id = f.customer_id
join finance.reporting_periods   p on p.period_month = date_trunc('month', f.order_dt)::date
where p.status = 'open'
group by 1, 2, 3;
