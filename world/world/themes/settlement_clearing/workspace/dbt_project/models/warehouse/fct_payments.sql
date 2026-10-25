with versions as (
    select
        payment_id,
        payment_day,
        merchant_id,
        amount_cents,
        version,
        event_type,
        row_number() over (
            partition by payment_id
            order by version desc, delivered_at desc, event_id desc
        ) as version_rank
    from {{ ref('stg_payment_events') }}
)

select
    payment_id,
    payment_day,
    merchant_id,
    amount_cents,
    version
from versions
where version_rank = 1
  and event_type <> 'void'
order by payment_id
