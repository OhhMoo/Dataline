with watermark as (
    select max(received_date) as data_through_date
    from {{ ref_db('fct_adjudicated_claims') }}
),
daily as (
    select
        service_date,
        count(*) filter (where claim_status <> 'voided') as adjudicated_claims,
        count(*) filter (where claim_status in ('paid', 'adjusted')) as paid_claims,
        count(*) filter (where claim_status = 'denied') as denied_claims,
        count(*) filter (where claim_status = 'voided') as voided_claims,
        coalesce(
            sum(allowed_amount) filter (where claim_status <> 'voided'),
            cast(0 as decimal(18, 2))
        ) as incurred_amount,
        coalesce(
            sum(payer_paid_amount) filter (where claim_status <> 'voided'),
            cast(0 as decimal(18, 2))
        ) as payer_paid_amount
    from {{ ref_db('fct_adjudicated_claims') }}
    group by service_date
),
aged as (
    select
        d.*,
        w.data_through_date,
        greatest(
            {{ var('reserve_window_days') }} - date_diff('day', d.service_date, w.data_through_date),
            0
        ) as reserve_open_days
    from daily as d
    cross join watermark as w
)

select
    'RSV-' || strftime(service_date, '%Y%m%d') as handoff_id,
    service_date,
    data_through_date,
    adjudicated_claims,
    paid_claims,
    denied_claims,
    voided_claims,
    incurred_amount,
    payer_paid_amount,
    reserve_open_days,
    cast(round(
        incurred_amount * reserve_open_days * {{ var('reserve_bps') }}
        / ({{ var('reserve_window_days') }} * 10000),
        2
    ) as decimal(18, 2)) as ibnr_reserve_amount,
    'ready' as handoff_status
from aged
order by service_date
