select coalesce(h.handoff_id, l.handoff_id) as handoff_id
from {{ ref('partner_payout_handoff') }} as h
full outer join {{ ref('billing_lock') }} as l
  on l.handoff_id = h.handoff_id
where (h.billing_status = 'locked' and l.lock_id is null)
   or (h.billing_status = 'open' and l.lock_id is not null)
   or (l.lock_id is not null and h.handoff_id is not null
       and (h.payout_cents <> l.payout_cents
            or h.attributed_conversions <> l.attributed_conversions))
