select claim_id
from {{ ref('fct_adjudicated_claims') }}
where cob_adjustment_amount <> other_payer_paid_amount
