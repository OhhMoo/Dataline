select claim_id
from {{ ref('fct_adjudicated_claims') }}
where billed_amount <> round(
    payer_paid_amount
    + member_responsibility_amount
    + contractual_adjustment_amount
    + cob_adjustment_amount,
    2
)
