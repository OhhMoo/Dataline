{{ config(tags=['fold_subscriptions']) }}

with events as (
  select * from {{ ref('stg_subscription_events') }}
),
calendar as (
  select cast(generate_series as date) as metric_date
  from generate_series(
    (select min(effective_date) from events),
    (select max(source_date) from events),
    interval 1 day
  )
),
accounts as (
  select distinct
    account_id, subscription_id, account_name, segment
  from events
),
account_days as (
  select a.*, c.metric_date
  from accounts a
  cross join calendar c
),
ranked as (
  select
    d.metric_date,
    d.account_id,
    d.subscription_id,
    d.account_name,
    d.segment,
    e.event_id,
    e.event_type,
    e.plan_code,
    e.seats,
    e.unit_price_cents,
    e.revrec_days,
    row_number() over (
      partition by d.account_id, d.metric_date
      order by e.effective_date desc, e.occurred_at desc, e.event_id desc
    ) as event_rank
  from account_days d
  join events e
    on e.account_id = d.account_id
   and e.effective_date <= d.metric_date
),
folded as (
  select
    metric_date,
    account_id,
    subscription_id,
    account_name,
    segment,
    event_id as source_event_id,
    event_type,
    plan_code,
    case when event_type = 'subscription_churned' then false else true end as is_active,
    seats,
    unit_price_cents,
    revrec_days
  from ranked
  where event_rank = 1
)
select
  metric_date,
  account_id,
  subscription_id,
  account_name,
  segment,
  source_event_id,
  plan_code,
  is_active,
  seats,
  unit_price_cents,
  case when is_active then seats * unit_price_cents else 0 end as mrr_cents,
  case when is_active then seats * unit_price_cents * 12 else 0 end as arr_cents,
  case when is_active
    then (seats * unit_price_cents) // revrec_days
    else 0
  end as recognized_revenue_cents
from folded
order by metric_date, account_id
