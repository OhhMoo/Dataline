create or replace table warehouse.merchant_payout_handoff as
select settlement_date, merchant_id, payment_count, gross_cents, cutoff_day,
       interchange_cents, gross_cents - interchange_cents as net_cents,
       date_diff('day', settlement_date, (select max(dt) from raw.payments_events)) >= window_days
         as window_closed
from warehouse.fct_settlement_daily order by settlement_date, merchant_id;
create or replace table warehouse.clearing_window as
select settlement_date,
       bool_and(window_closed) and day(settlement_date) <= max(cutoff_day) as closed,
       sum(net_cents) as net_cents
from warehouse.merchant_payout_handoff group by settlement_date order by settlement_date;
