-- One row per order line, with the order's date and customer carried down so
-- line-grain visuals (units by product by month) need no second join.
-- LEFT join on purpose: a line whose order is missing keeps its row and fails
-- the not_null tests on order_date / customer_id, instead of vanishing.
select
    cast(l.order_line_id as integer)       as order_line_id,
    cast(l.order_id as integer)            as order_id,
    cast(l.product_id as integer)          as product_id,
    cast(l.quantity as integer)            as quantity,
    cast(l.line_amount as decimal(12, 2))  as line_amount,
    cast(o.order_date as date)             as order_date,
    cast(o.customer_id as integer)         as customer_id
from {{ ref('stg_order_lines') }} as l
left join {{ ref('stg_orders') }} as o
    on o.order_id = l.order_id
order by l.order_line_id
