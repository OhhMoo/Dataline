select
  order_id,
  order_date,
  customer_id,
  amount,
  terminal_ref,
  dt as landed_dt
from {{ ref('stg_revenue') }}
