create or replace table warehouse.invoice_run_handoff as
select date_trunc('month',usage_date)::date as billing_month, account_id,
       sum(units) as usage_units, sum(overage_units) as overage_units,
       sum(overage_cents) as overage_cents
from warehouse.fct_billable_usage group by 1,2 order by 1,2;
create or replace table warehouse.billing_period_lock as
select billing_month,
       date_diff('day', billing_month, (select max(dt) from raw.usage_rollups)) >= 30 as locked
from warehouse.invoice_run_handoff group by 1 order by 1;
