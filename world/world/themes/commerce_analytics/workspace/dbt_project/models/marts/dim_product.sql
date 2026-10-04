-- unit_price is the CURRENT list price. Revenue never comes from here: the
-- price an order paid is fct_order_lines.line_amount / quantity, and the two
-- differ for every product whose price changed (docs/metrics.md, List Price
-- Revenue).
select
    cast(product_id as integer)      as product_id,
    cast(product_name as varchar)    as product_name,
    cast(category as varchar)        as category,
    cast(unit_price as decimal(12, 2)) as unit_price
from {{ ref('stg_products') }}
order by product_id
