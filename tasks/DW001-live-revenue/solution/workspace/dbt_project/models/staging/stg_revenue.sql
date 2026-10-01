select
  cast(order_id as integer) as order_id,
  cast(order_date as date) as order_date,
  customer_id,
  cast(amount as decimal(12,2)) as amount,
  terminal_ref,
  dt
from {{ source('raw', 'daily_revenue') }}
-- synthetic orders from the payments test harness are not revenue.
-- NULL-safe: `x NOT IN (...)` is NULL (so: dropped) when x is NULL, and
-- card-not-present orders carry no terminal_ref at all.
where terminal_ref is distinct from 'TEST-HARNESS'
  and terminal_ref is distinct from 'SMOKE-TEST'
