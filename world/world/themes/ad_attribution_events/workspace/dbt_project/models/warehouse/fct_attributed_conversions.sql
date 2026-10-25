with candidates as (
  select
    v.conversion_id,
    v.event_time as conversion_time,
    v.user_id,
    v.order_id,
    v.revenue_cents,
    v.currency,
    c.click_id,
    c.click_time,
    c.partner_id,
    c.campaign_id,
    p.partner_name,
    p.revshare_bps,
    r.lookback_days,
    row_number() over (
      partition by v.conversion_id
      order by c.click_time desc, c.click_id desc
    ) as attribution_rank
  from {{ ref('stg_conversions') }} as v
  cross join {{ ref('stg_attribution_rules') }} as r
  inner join {{ ref('stg_clicks') }} as c
    on c.user_id = v.user_id
   and c.click_time <= v.event_time
   and c.click_time >= v.event_time - cast(r.lookback_days as integer) * interval '1 day'
  inner join {{ ref('stg_partners') }} as p
    on p.partner_id = c.partner_id
  where not c.is_bot
)
select
  conversion_id,
  conversion_time,
  cast(conversion_time as date) as conversion_date,
  user_id,
  order_id,
  click_id,
  click_time,
  partner_id,
  partner_name,
  campaign_id,
  revenue_cents,
  currency,
  lookback_days,
  cast(floor(revenue_cents * revshare_bps / 10000) as integer) as payout_cents
from candidates
where attribution_rank = 1
order by conversion_id
