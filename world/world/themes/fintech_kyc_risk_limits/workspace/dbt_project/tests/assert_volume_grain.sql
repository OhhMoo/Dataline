select merchant_id, business_date, count(*) as rows_at_grain
from {{ ref('stg_merchant_volume') }}
group by merchant_id, business_date
having count(*) <> 1
