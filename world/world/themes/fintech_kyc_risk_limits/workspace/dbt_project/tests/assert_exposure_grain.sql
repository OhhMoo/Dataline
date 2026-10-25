select merchant_id, exposure_date, count(*) as rows_at_grain
from {{ ref('fct_merchant_exposure') }}
group by merchant_id, exposure_date
having count(*) <> 1
