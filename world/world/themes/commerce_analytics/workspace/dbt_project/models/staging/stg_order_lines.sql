-- Order lines are immutable at the source (an edit is a cancel + a new order),
-- so there is nothing to deduplicate: a line_id seen twice is a delivery
-- problem, and the unique test below is what says so.
select
    cast(order_line_id as integer)      as order_line_id,
    cast(order_id as integer)           as order_id,
    cast(product_id as integer)         as product_id,
    cast(quantity as integer)           as quantity,
    cast(unit_price as decimal(12, 2))  as unit_price,
    cast(line_amount as decimal(12, 2)) as line_amount,
    cast(created_at as timestamp)       as created_at,
    dt                                  as extracted_on
from {{ source('shop', 'order_lines') }}
