with watermark as (
  select max(conversion_date) as max_conversion_date
  from {{ ref('fct_attributed_conversions').include(database=true) }}
),
aggregated as (
  select
    f.conversion_date,
    f.partner_id,
    f.partner_name,
    f.currency,
    count(*) as attributed_conversions,
    sum(f.revenue_cents) as revenue_cents,
    sum(f.payout_cents) as payout_cents
  from {{ ref('fct_attributed_conversions').include(database=true) }} as f
  group by f.conversion_date, f.partner_id, f.partner_name, f.currency
)
select
  'PO-' || strftime(a.conversion_date, '%Y%m%d') || '-' || a.partner_id as handoff_id,
  a.conversion_date,
  a.partner_id,
  a.partner_name,
  a.currency,
  a.attributed_conversions,
  a.revenue_cents,
  a.payout_cents,
  cast(w.max_conversion_date - r.billing_lag_days as date) as billing_cutoff_date,
  case
    when a.conversion_date <= w.max_conversion_date - r.billing_lag_days
      then 'locked'
    else 'open'
  end as billing_status
from aggregated as a
cross join watermark as w
cross join {{ ref('stg_attribution_rules') }} as r
order by a.conversion_date, a.partner_id
