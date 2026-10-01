select
  cast(order_id as integer) as order_id,
  cast(order_date as date) as order_date,
  customer_id,
  cast(amount as decimal(12,2)) as amount,
  terminal_ref,
  dt
from {{ source('raw', 'daily_revenue') }}
-- synthetic orders from the payments test harness are not revenue
where terminal_ref not in ('TEST-HARNESS', 'SMOKE-TEST')
