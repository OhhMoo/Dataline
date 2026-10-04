-- One row per order. order_amount is the merchandise subtotal (lines only);
-- shipping is carried separately and is NOT revenue in any metric we publish.
select
    cast(order_id as integer)              as order_id,
    cast(order_date as date)               as order_date,
    cast(customer_id as integer)           as customer_id,
    cast(order_status as varchar)          as order_status,
    cast(order_amount as decimal(12, 2))   as order_amount,
    cast(shipping_amount as decimal(12, 2)) as shipping_amount
from {{ ref('stg_orders') }}
order by order_id
