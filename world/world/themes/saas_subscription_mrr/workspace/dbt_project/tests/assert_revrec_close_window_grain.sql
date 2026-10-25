select period, account_id, count(*) as rows_at_grain
from {{ ref('revrec_close_window') }}
group by 1, 2
having count(*) <> 1
