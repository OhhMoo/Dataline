with ranked_postbacks as (
  select
    c.*,
    row_number() over (
      partition by conversion_id
      order by received_at asc, postback_id asc
    ) as postback_rank
  from {{ source('raw', 'conversions') }} as c
)
select
  postback_id,
  conversion_id,
  event_time,
  user_id,
  order_id,
  revenue_cents,
  currency,
  received_at,
  dt
from ranked_postbacks
where postback_rank = 1
