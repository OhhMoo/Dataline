with usage as (
    select customer_id, billing_month,
           sum(reported_units) as received_units,
           sum(billable_units) as billable_units,
           sum(reported_units - billable_units) as late_units_excluded
    from {{ ref('fct_billable_usage') }}
    group by customer_id, billing_month
),
priced as (
    select c.customer_id,
           c.customer_name,
           c.billing_month,
           c.period_start,
           c.period_end,
           l.locked_at,
           l.source_watermark,
           c.committed_units,
           c.monthly_minimum_cents,
           c.overage_cents_per_1000,
           c.currency,
           coalesce(u.received_units, 0) as received_units,
           coalesce(u.billable_units, 0) as billable_units,
           coalesce(u.late_units_excluded, 0) as late_units_excluded,
           greatest(coalesce(u.billable_units, 0) - c.committed_units, 0) as overage_units
    from {{ ref('stg_contract_commits') }} as c
    join {{ ref('billing_period_lock') }} as l
      on l.customer_id = c.customer_id
     and l.billing_month = c.billing_month
    left join usage as u
      on u.customer_id = c.customer_id
     and u.billing_month = c.billing_month
    where l.is_locked
),
amounts as (
    select p.*,
           (overage_units * overage_cents_per_1000 + 999) // 1000
               as overage_amount_cents
    from priced as p
)
select 'INV-' || strftime(billing_month, '%Y%m') || '-' || customer_id as invoice_run_id,
       customer_id,
       customer_name,
       billing_month,
       period_start,
       period_end,
       locked_at,
       source_watermark as generated_from_watermark,
       'telemetry_units' as metric,
       committed_units,
       received_units,
       billable_units,
       overage_units,
       late_units_excluded,
       monthly_minimum_cents,
       overage_cents_per_1000,
       overage_amount_cents,
       monthly_minimum_cents + overage_amount_cents as invoice_total_cents,
       cast(monthly_minimum_cents + overage_amount_cents as decimal(18, 2)) / 100
           as invoice_total,
       currency,
       'ready_for_erp' as handoff_status
from amounts
order by billing_month, customer_id
