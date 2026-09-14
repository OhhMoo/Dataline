select
  cast(order_id as integer) as order_id,
  cast(order_date as date) as order_date,
  customer_id,
  cast(amount as decimal(12,2)) as amount
from {{ source('raw', 'vendor_orders') }}
