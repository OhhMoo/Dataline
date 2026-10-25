with requests as (
    select *
    from {{ ref('stg_payout_holds') }}
    where event_type = 'release_requested'
),
decisions as (
    select
        *,
        row_number() over (
            partition by request_id order by event_at desc, event_id desc
        ) as decision_rank
    from {{ ref('stg_payout_holds') }}
    where event_type in ('release_approved', 'release_rejected')
)
select
    r.request_id,
    r.hold_id,
    r.merchant_id,
    e.legal_name,
    r.event_at as requested_at,
    r.event_date as request_date,
    r.release_amount as requested_amount,
    case
        when d.event_id is null then 'pending'
        when d.event_type = 'release_approved' then 'approved'
        else 'rejected'
    end as signoff_status,
    d.event_id as decision_event_id,
    d.event_at as signed_off_at,
    d.risk_officer_id,
    e.kyc_status,
    e.chargeback_ratio_bps,
    e.chargeback_critical_bps,
    e.reserve_held_amount,
    e.reserve_required_amount,
    e.available_release_amount,
    case
        when d.event_id is null then 'awaiting_risk_officer'
        when d.event_type = 'release_approved' then 'officer_gate_passed'
        else 'officer_gate_rejected'
    end as gate_status
from requests as r
left join decisions as d
    on d.request_id = r.request_id and d.decision_rank = 1
inner join {{ ref('fct_merchant_exposure') }} as e
    on e.merchant_id = r.merchant_id and e.exposure_date = r.event_date
order by r.event_date, r.request_id
