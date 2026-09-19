{{ config(materialized='table', schema='staging') }}

-- Order lines converted to USD.
--
-- The rate is resolved AS OF the order date, so a day with no published rate
-- carries the last rate that was published before it. That is deliberate: the
-- vendor does not publish on weekends or on local holidays, and without the
-- carry-forward every Saturday would convert to NULL.

select
    l.order_id,
    l.line_nbr,
    l.order_dt,
    l.customer_id,
    c.customer_segment,
    l.currency,
    l.amount_local,
    f.rate_to_usd                                          as fx_rate,
    round(l.amount_local * f.rate_to_usd, 2)               as amount_usd,
    l.loaded_at
from {{ source('raw', 'revenue_lines') }} l
join {{ source('raw', 'customer_master') }} c using (customer_id)
asof join {{ source('raw', 'fx_rates_daily') }} f
  on l.currency = f.currency and l.order_dt >= f.rate_date
