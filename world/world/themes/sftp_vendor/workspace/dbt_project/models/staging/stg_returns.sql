select
  cast(return_id as integer) as return_id,
  cast(order_id as integer) as order_id,
  cast(return_date as date) as return_date,
  cast(amount as decimal(12,2)) as amount
from {{ source('raw', 'vendor_returns') }}
