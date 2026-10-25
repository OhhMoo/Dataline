{{ config(tags=['fold_subscriptions']) }}

select
  event_id,
  subscription_id,
  account_id,
  account_name,
  segment,
  event_type,
  cast(occurred_at as timestamp) as occurred_at,
  cast(effective_date as date) as effective_date,
  plan_code,
  cast(seats as integer) as seats,
  cast(unit_price_cents as bigint) as unit_price_cents,
  cast(revrec_days as integer) as revrec_days,
  reason,
  cast(source_date as date) as source_date
from {{ source('raw', 'subscription_events') }}
