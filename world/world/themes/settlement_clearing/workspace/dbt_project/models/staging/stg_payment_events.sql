with deliveries as (
    select
        event_id,
        cast(delivered_at as timestamp) as delivered_at,
        cast(payment_day as date) as payment_day,
        payment_id,
        cast(version as integer) as version,
        lower(event_type) as event_type,
        merchant_id,
        cast(amount_cents as bigint) as amount_cents,
        cast(dt as date) as source_day,
        source_partition,
        row_number() over (
            partition by event_id
            order by delivered_at desc, source_partition desc
        ) as delivery_rank
    from {{ source('raw', 'payments_events') }}
)

select
    event_id,
    delivered_at,
    payment_day,
    payment_id,
    version,
    event_type,
    merchant_id,
    amount_cents,
    source_day,
    source_partition
from deliveries
where delivery_rank = 1
