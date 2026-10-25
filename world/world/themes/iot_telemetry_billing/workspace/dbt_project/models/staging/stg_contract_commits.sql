with ranked as (
    select c.*,
           row_number() over (
               partition by customer_id, billing_month
               order by collected_at desc, commit_id desc
           ) as version_rank
    from {{ source('raw', 'contract_commits') }} as c
)
select commit_id,
       contract_id,
       customer_id,
       customer_name,
       billing_month,
       metric,
       committed_units,
       monthly_minimum_cents,
       overage_cents_per_1000,
       lock_grace_days,
       currency,
       effective_at,
       collected_at,
       cast(date_trunc('month', billing_month) as date) as period_start,
       cast(date_trunc('month', billing_month) + interval '1 month' as date) as period_end,
       cast(date_trunc('month', billing_month) + interval '1 month'
            + lock_grace_days * interval '1 day' as timestamp) as lock_cutoff
from ranked
where version_rank = 1
