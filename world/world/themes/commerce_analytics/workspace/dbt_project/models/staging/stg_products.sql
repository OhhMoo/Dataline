-- One row per product, latest version: the CURRENT list price. The price an
-- order actually paid is on the order line (stg_order_lines.unit_price).
with versions as (

    select
        cast(product_id as integer)        as product_id,
        trim(sku)                          as sku,
        trim(product_name)                 as product_name,
        trim(category)                     as category,
        cast(unit_price as decimal(12, 2)) as unit_price,
        cast(is_active as boolean)         as is_active,
        cast(updated_at as timestamp)      as updated_at,
        row_number() over (
            partition by cast(product_id as integer)
            order by cast(updated_at as timestamp) desc, dt desc
        ) as version_rank
    from {{ source('shop', 'products') }}

)

select product_id, sku, product_name, category, unit_price, is_active, updated_at
from versions
where version_rank = 1
