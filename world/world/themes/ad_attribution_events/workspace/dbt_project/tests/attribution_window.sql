select conversion_id
from {{ ref('fct_attributed_conversions') }}
where click_time > conversion_time
   or click_time < conversion_time - cast(lookback_days as integer) * interval '1 day'
