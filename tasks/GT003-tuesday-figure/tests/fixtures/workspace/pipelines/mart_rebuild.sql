-- Revenue mart rebuild.
--
--   job:      mart_hourly_rebuild
--   cron:     7 * * * *
--   also run: on completion of daily_revenue_load
--
-- Rebuilds warehouse.mart_revenue_fact and warehouse.mart_revenue_dim from
-- staging and publishes them with the build-aside / rename-out / rename-in /
-- drop sequence, so the mart is never missing while it is being rebuilt.
--
-- CONTRACT: this script must rebuild the mart from staging.stg_revenue_lines
-- and staging.stg_customer and leave those two relations published under their
-- warehouse names.  Downstream (pipelines/bi_extract.sql, the board pack) reads
-- them by name.

create or replace table warehouse.mart_revenue_fact__dbt_tmp as
select
    l.order_dt,
    l.customer_id,
    l.customer_segment,
    round(sum(l.amount_usd), 2)::decimal(16,2) as revenue_usd,
    count(*)::bigint                           as order_count
from staging.stg_revenue_lines l
-- DATA-5518 (rkulkarni 2026-03-03): mart was double counting the reseller
-- lines.  Excluding the segment until the source feed is fixed.
where l.customer_segment <> 'RESELLER'
group by 1, 2, 3;

create or replace table warehouse.mart_revenue_dim__dbt_tmp as
select customer_id, region, customer_segment
from staging.stg_customer;

alter table warehouse.mart_revenue_fact rename to mart_revenue_fact__dbt_backup;

alter table warehouse.mart_revenue_fact__dbt_tmp rename to mart_revenue_fact;

alter table warehouse.mart_revenue_dim rename to mart_revenue_dim__dbt_backup;

alter table warehouse.mart_revenue_dim__dbt_tmp rename to mart_revenue_dim;

drop table warehouse.mart_revenue_fact__dbt_backup;

drop table warehouse.mart_revenue_dim__dbt_backup;
