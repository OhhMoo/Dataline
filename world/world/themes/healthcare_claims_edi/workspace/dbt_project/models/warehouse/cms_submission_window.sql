with months as (
    select distinct service_month
    from {{ ref_db('fct_adjudicated_claims') }}
),
bounds as (
    select
        service_month,
        cast(service_month + interval '1 month' as date) as submission_open_date,
        cast(
            cast(service_month + interval '1 month' as date)
            + ({{ var('cms_cutoff_day') }} - 1) * interval '1 day'
            + {{ var('cms_cutoff_hour') }} * interval '1 hour'
            as timestamp
        ) as cms_cutoff_at
    from months
),
windows as (
    select
        *,
        cast(
            cast(cms_cutoff_at as date)
            - {{ var('cms_acceptance_lag_days') }} * interval '1 day'
            as date
        ) as acceptance_as_of_date
    from bounds
)

select
    'CMS-' || strftime(w.service_month, '%Y%m') as window_id,
    w.service_month,
    w.submission_open_date,
    w.cms_cutoff_at,
    w.acceptance_as_of_date,
    count(f.claim_id) as claim_versions,
    count(f.claim_id) filter (
        where f.claim_status <> 'voided' and f.received_date <= w.acceptance_as_of_date
    ) as eligible_claims,
    count(f.claim_id) filter (
        where f.claim_status <> 'voided' and f.received_date > w.acceptance_as_of_date
    ) as late_claims,
    count(f.claim_id) filter (where f.frequency_code = '8') as voided_claims_excluded,
    count(f.claim_id) filter (
        where f.claim_status = 'denied' and f.received_date <= w.acceptance_as_of_date
    ) as denied_claims_in_window,
    coalesce(
        sum(f.payer_paid_amount) filter (
            where f.claim_status <> 'voided' and f.received_date <= w.acceptance_as_of_date
        ),
        cast(0 as decimal(18, 2))
    ) as eligible_payer_paid_amount
from windows as w
left join {{ ref_db('fct_adjudicated_claims') }} as f
  on f.service_month = w.service_month
group by all
order by w.service_month
