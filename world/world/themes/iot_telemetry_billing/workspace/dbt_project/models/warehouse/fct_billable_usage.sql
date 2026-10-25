with usage_events as (
    select * from {{ ref('stg_meter_heartbeats') }}
    union all
    select * from {{ ref('stg_usage_rollups') }}
)
select e.usage_id,
       e.device_id,
       e.customer_id,
       e.gateway_id,
       e.usage_date,
       c.billing_month,
       c.period_start,
       c.period_end,
       c.lock_cutoff,
       e.metric,
       e.source_kind,
       e.collected_at,
       e.reported_units,
       e.counter_reset,
       e.collected_at >= c.lock_cutoff as after_lock_cutoff,
       case when e.collected_at >= c.lock_cutoff then 0
            else e.reported_units end as billable_units
from usage_events as e
join {{ ref('stg_contract_commits') }} as c
  on c.customer_id = e.customer_id
 and c.billing_month = cast(date_trunc('month', e.usage_date) as date)
 and c.metric = e.metric
order by e.usage_id
