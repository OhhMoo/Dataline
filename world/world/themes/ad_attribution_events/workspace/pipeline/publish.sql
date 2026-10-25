create or replace table warehouse.partner_payout_handoff as
select conversion_date, partner_id, count(*) as conversions,
       sum(value_cents) as value_cents,
       cast(round(sum(value_cents) * 0.35) as bigint) as payout_cents
from warehouse.fct_attributed_conversions group by 1,2 order by 1,2;
create or replace table warehouse.billing_lock as
select conversion_date, bool_or(
  date_diff('day', conversion_date, (select max(dt) from raw.conversions)) >= 7
) as locked
from warehouse.fct_attributed_conversions group by 1 order by 1;
