-- Initial handoff. The nightly publisher owns later writes so closed windows
-- are not silently rebuilt by dbt.
select
    payment_day as settlement_day,
    merchant_id,
    cast(count(*) as bigint) as payments,
    cast(sum(amount_cents) as bigint) as net_cents,
    cast(null as varchar) as correction_ref
from {{ ref('fct_payments') }}
group by payment_day, merchant_id
order by payment_day, merchant_id
