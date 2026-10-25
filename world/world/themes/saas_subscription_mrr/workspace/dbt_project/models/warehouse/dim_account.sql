{{ config(tags=['fold_subscriptions']) }}

with history as (
  select
    f.*,
    row_number() over (
      partition by account_id order by metric_date desc
    ) as recency_rank,
    min(metric_date) over (partition by account_id) as first_seen_date
  from {{ ref('fct_mrr_daily') }} f
)
select
  account_id,
  subscription_id,
  account_name,
  segment,
  first_seen_date as subscription_start_date,
  metric_date as as_of_date,
  plan_code,
  case when is_active then 'active' else 'churned' end as subscription_status,
  seats,
  unit_price_cents,
  mrr_cents,
  arr_cents
from history
where recency_rank = 1
order by account_id
