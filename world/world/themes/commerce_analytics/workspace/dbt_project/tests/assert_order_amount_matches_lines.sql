-- Every order's amount is the sum of its lines, to the cent.
--
-- The shop computes the subtotal and the lines separately and ships them in
-- two files. If one file is short, duplicated or late, the two disagree, and
-- every revenue number built on fct_orders disagrees with every units / mix
-- number built on fct_order_lines. Error, not warn: a dashboard showing both
-- would be internally inconsistent.
select
    o.order_id,
    o.order_amount,
    coalesce(sum(l.line_amount), 0) as line_total
from {{ ref('fct_orders') }} as o
left join {{ ref('fct_order_lines') }} as l
    on l.order_id = o.order_id
group by o.order_id, o.order_amount
having o.order_amount <> coalesce(sum(l.line_amount), 0)
