with gross as (
    select
        f.payment_day as settlement_day,
        f.merchant_id,
        m.interchange_bps,
        cast(sum(f.amount_cents) as bigint) as gross_cents
    from {{ ref('fct_payments') }} as f
    join {{ ref('dim_merchant') }} as m
      on m.merchant_id = f.merchant_id
    group by f.payment_day, f.merchant_id, m.interchange_bps
), fees as (
    select
        settlement_day,
        merchant_id,
        interchange_bps,
        gross_cents,
        cast((gross_cents * cast(interchange_bps as bigint)) // 10000 as bigint)
            as interchange_cents
    from gross
)

select
    settlement_day,
    merchant_id,
    interchange_bps,
    gross_cents,
    interchange_cents,
    gross_cents - interchange_cents as net_after_interchange_cents
from fees
order by settlement_day, merchant_id
