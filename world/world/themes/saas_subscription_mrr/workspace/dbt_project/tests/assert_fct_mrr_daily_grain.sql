select metric_date, account_id, count(*) as rows_at_grain
from {{ ref('fct_mrr_daily') }}
group by 1, 2
having count(*) <> 1
