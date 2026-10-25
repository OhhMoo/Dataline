select
    'TRH-' || request_id as handoff_id,
    request_id,
    hold_id,
    merchant_id,
    legal_name,
    request_date as handoff_date,
    requested_at,
    requested_amount,
    signoff_status,
    risk_officer_id,
    signed_off_at,
    kyc_status,
    chargeback_ratio_bps,
    chargeback_critical_bps,
    reserve_held_amount,
    reserve_required_amount,
    available_release_amount,
    case
        when signoff_status = 'approved'
         and kyc_status = 'approved'
         and chargeback_ratio_bps < chargeback_critical_bps
         and available_release_amount > 0
        then least(requested_amount, available_release_amount)
        else 0
    end as treasury_release_amount,
    case
        when signoff_status = 'pending' then 'awaiting_risk_signoff'
        when signoff_status = 'rejected' then 'rejected_by_risk'
        when kyc_status <> 'approved' then 'blocked_by_kyc'
        when chargeback_ratio_bps >= chargeback_critical_bps then 'blocked_by_chargeback_ratio'
        when available_release_amount <= 0 then 'blocked_no_excess_reserve'
        else 'ready_for_treasury'
    end as handoff_status
from {{ ref('risk_committee_signoff') }}
order by handoff_date, request_id
