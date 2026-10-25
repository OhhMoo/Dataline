select handoff_id, signoff_status, treasury_release_amount
from {{ ref('treasury_reserve_handoff') }}
where treasury_release_amount > 0
  and (signoff_status <> 'approved'
       or risk_officer_id is null
       or signed_off_at is null
       or handoff_status <> 'ready_for_treasury')
