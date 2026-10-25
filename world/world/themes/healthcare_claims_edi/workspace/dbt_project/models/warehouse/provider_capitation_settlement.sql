with months as (
    select distinct service_month
    from {{ ref_db('fct_adjudicated_claims') }}
),
schedule as (
    select
        m.service_month,
        p.provider_id,
        p.capitated_members as member_months,
        p.capitation_pmpm,
        cast(p.capitated_members * p.capitation_pmpm as decimal(18, 2)) as gross_capitation_amount
    from months as m
    cross join {{ ref_db('dim_provider') }} as p
)

select
    'CAP-' || strftime(service_month, '%Y%m') || '-' || provider_id as settlement_id,
    service_month,
    provider_id,
    member_months,
    capitation_pmpm,
    gross_capitation_amount,
    cast(round(
        gross_capitation_amount * {{ var('capitation_withhold_bps') }} / 10000,
        2
    ) as decimal(18, 2)) as quality_withhold_amount,
    cast(gross_capitation_amount - round(
        gross_capitation_amount * {{ var('capitation_withhold_bps') }} / 10000,
        2
    ) as decimal(18, 2)) as capitation_settlement_amount,
    cast(
        cast(service_month + interval '1 month' as date)
        + ({{ var('capitation_payment_day') }} - 1) * interval '1 day'
        as date
    ) as payment_date,
    'scheduled' as settlement_status
from schedule
order by service_month, provider_id
