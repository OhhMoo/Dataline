select
    c.claim_id,
    c.claim_version,
    c.frequency_code,
    c.transaction_control_number,
    c.provider_id,
    c.member_id,
    c.service_date,
    cast(date_trunc('month', c.service_date) as date) as service_month,
    c.received_date,
    c.service_code,
    c.diagnosis_code,
    c.other_payer_id,
    c.other_payer_id <> 'NONE' as cob_flag,
    r.remit_id,
    r.remit_date,
    r.adjudication_status as claim_status,
    c.billed_amount,
    r.allowed_amount,
    r.payer_paid_amount,
    r.member_responsibility_amount,
    r.contractual_adjustment_amount,
    r.cob_adjustment_amount,
    r.other_payer_paid_amount,
    r.denial_reason_code,
    c.landing_date
from {{ ref('stg_claims_current') }} as c
join {{ ref('stg_remits_current') }} as r
  on r.claim_id = c.claim_id
 and r.claim_version = c.claim_version
order by c.claim_id
