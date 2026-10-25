with calendar as (
    select merchant_id, business_date as exposure_date
    from {{ ref('stg_merchant_volume') }}
),
volume_window as (
    select
        c.merchant_id,
        c.exposure_date,
        sum(v.settled_transactions) as settled_transactions_30d,
        sum(v.settled_volume) as settled_volume_30d
    from calendar as c
    inner join {{ ref('stg_merchant_volume') }} as v
        on v.merchant_id = c.merchant_id
       and v.business_date between c.exposure_date - (
           select rolling_window_days - 1 from {{ ref('risk_policy') }})
           and c.exposure_date
    group by c.merchant_id, c.exposure_date
),
dispute_window as (
    select
        c.merchant_id,
        c.exposure_date,
        count(d.event_id) filter (where d.event_type = 'dispute_opened')
            as opened_disputes_30d,
        count(d.event_id) filter (where d.event_type = 'representment_won')
            as won_disputes_30d,
        count(d.event_id) filter (where d.event_type = 'representment_lost')
            as lost_disputes_30d,
        coalesce(sum(d.dispute_amount) filter (where d.event_type = 'dispute_opened'), 0)
            as dispute_amount_30d,
        coalesce(sum(d.dispute_amount) filter (where d.event_type = 'representment_won'), 0)
            as representment_reversal_30d
    from calendar as c
    left join {{ ref('stg_dispute_events') }} as d
        on d.merchant_id = c.merchant_id
       and d.event_date between c.exposure_date - (
           select rolling_window_days - 1 from {{ ref('risk_policy') }})
           and c.exposure_date
    group by c.merchant_id, c.exposure_date
),
latest_kyc as (
    select
        c.merchant_id,
        c.exposure_date,
        k.legal_name,
        k.merchant_category,
        k.country,
        k.kyc_status,
        k.risk_tier,
        k.reviewed_at as last_kyc_reviewed_at,
        row_number() over (
            partition by c.merchant_id, c.exposure_date
            order by k.reviewed_at desc nulls last, k.review_id desc nulls last
        ) as kyc_rank
    from calendar as c
    left join {{ ref('stg_kyc_reviews') }} as k
        on k.merchant_id = c.merchant_id and k.review_date <= c.exposure_date
),
dispute_state as (
    select
        c.merchant_id,
        c.exposure_date,
        count(d.event_id) filter (where d.event_type = 'dispute_opened')
            - count(d.event_id) filter (
                where d.event_type in ('representment_won', 'representment_lost'))
            as open_disputes,
        coalesce(sum(d.dispute_amount) filter (where d.event_type = 'dispute_opened'), 0)
            - coalesce(sum(d.dispute_amount) filter (
                where d.event_type in ('representment_won', 'representment_lost')), 0)
            as open_dispute_exposure
    from calendar as c
    left join {{ ref('stg_dispute_events') }} as d
        on d.merchant_id = c.merchant_id and d.event_date <= c.exposure_date
    group by c.merchant_id, c.exposure_date
),
reserve_state as (
    select
        c.merchant_id,
        c.exposure_date,
        coalesce(sum(case
            when p.event_type = 'hold_placed' then p.reserve_amount
            when p.event_type = 'release_approved' then -p.release_amount
            else 0 end), 0) as reserve_held_amount
    from calendar as c
    left join {{ ref('stg_payout_holds') }} as p
        on p.merchant_id = c.merchant_id and p.event_date <= c.exposure_date
    group by c.merchant_id, c.exposure_date
),
scored as (
    select
        v.merchant_id,
        v.exposure_date,
        k.legal_name,
        k.merchant_category,
        k.country,
        k.kyc_status,
        k.risk_tier,
        k.last_kyc_reviewed_at,
        v.settled_transactions_30d,
        v.settled_volume_30d,
        d.opened_disputes_30d,
        d.won_disputes_30d,
        d.lost_disputes_30d,
        d.dispute_amount_30d,
        d.representment_reversal_30d,
        s.open_disputes,
        s.open_dispute_exposure,
        r.reserve_held_amount,
        cast(d.opened_disputes_30d * 10000
             / nullif(v.settled_transactions_30d, 0) as integer) as chargeback_ratio_bps
    from volume_window as v
    inner join dispute_window as d using (merchant_id, exposure_date)
    inner join dispute_state as s using (merchant_id, exposure_date)
    inner join reserve_state as r using (merchant_id, exposure_date)
    inner join latest_kyc as k
        on k.merchant_id = v.merchant_id
       and k.exposure_date = v.exposure_date
       and k.kyc_rank = 1
),
priced as (
    select
        s.*,
        p.rolling_window_days,
        p.chargeback_warning_bps,
        p.chargeback_critical_bps,
        p.reserve_coverage_days,
        p.base_reserve_bps
            + case when s.chargeback_ratio_bps >= p.chargeback_critical_bps
                   then p.elevated_reserve_bps else 0 end
            + case when s.risk_tier = 'high' then 200 else 0 end
            + case when s.kyc_status <> 'approved' then 500 else 0 end
            as reserve_rate_bps
    from scored as s
    cross join {{ ref('risk_policy') }} as p
)
select
    merchant_id,
    exposure_date,
    legal_name,
    merchant_category,
    country,
    kyc_status,
    risk_tier,
    last_kyc_reviewed_at,
    rolling_window_days,
    settled_transactions_30d,
    settled_volume_30d,
    opened_disputes_30d,
    won_disputes_30d,
    lost_disputes_30d,
    dispute_amount_30d,
    representment_reversal_30d,
    open_disputes,
    open_dispute_exposure,
    chargeback_ratio_bps,
    chargeback_warning_bps,
    chargeback_critical_bps,
    reserve_rate_bps,
    round(open_dispute_exposure
          + settled_volume_30d * reserve_rate_bps
            / (10000 * rolling_window_days), 2) as reserve_required_amount,
    reserve_held_amount,
    greatest(reserve_held_amount - round(open_dispute_exposure
          + settled_volume_30d * reserve_rate_bps
            / (10000 * rolling_window_days), 2), 0) as available_release_amount,
    case
        when kyc_status <> 'approved' then 'kyc_review'
        when chargeback_ratio_bps >= chargeback_critical_bps then 'critical'
        when chargeback_ratio_bps >= chargeback_warning_bps then 'watch'
        else 'normal'
    end as exposure_status
from priced
order by exposure_date, merchant_id
