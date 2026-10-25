with collection_watermark as (
    select max(collected_at) as source_watermark
    from (
        select collected_at from {{ source('raw', 'meter_heartbeats') }}
        union all
        select collected_at from {{ source('raw', 'usage_rollups') }}
    ) as collection_events
),
late_usage as (
    select customer_id, billing_month,
           sum(reported_units - billable_units) as late_units
    from {{ ref('fct_billable_usage') }}
    group by customer_id, billing_month
)
select 'LOCK-' || strftime(c.billing_month, '%Y%m') || '-' || c.customer_id as lock_id,
       c.customer_id,
       c.billing_month,
       c.period_start,
       c.period_end,
       c.lock_cutoff,
       w.source_watermark,
       w.source_watermark >= c.lock_cutoff as is_locked,
       case when w.source_watermark >= c.lock_cutoff then 'locked'
            else 'open' end as lock_status,
       case when w.source_watermark >= c.lock_cutoff then c.lock_cutoff
            else null end as locked_at,
       coalesce(l.late_units, 0) as late_units_excluded
from {{ ref('stg_contract_commits') }} as c
cross join collection_watermark as w
left join late_usage as l
  on l.customer_id = c.customer_id
 and l.billing_month = c.billing_month
order by c.billing_month, c.customer_id
