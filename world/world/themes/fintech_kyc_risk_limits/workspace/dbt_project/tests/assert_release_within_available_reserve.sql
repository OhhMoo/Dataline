select handoff_id, treasury_release_amount, requested_amount, available_release_amount
from {{ ref('treasury_reserve_handoff') }}
where treasury_release_amount < 0
   or treasury_release_amount > requested_amount
   or treasury_release_amount > available_release_amount
