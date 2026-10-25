select
  replace(h.handoff_id, 'PO-', 'LOCK-') as lock_id,
  h.handoff_id,
  h.conversion_date,
  h.partner_id,
  h.partner_name,
  h.billing_cutoff_date,
  cast(h.conversion_date as timestamp)
    + cast(r.billing_lag_days + 1 as integer) * interval '1 day'
    + interval '90 minutes' as locked_at,
  h.currency,
  h.attributed_conversions,
  h.revenue_cents,
  h.payout_cents
from {{ ref('partner_payout_handoff').include(database=true) }} as h
cross join {{ ref('stg_attribution_rules') }} as r
where h.billing_status = 'locked'
order by h.conversion_date, h.partner_id
