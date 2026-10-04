-- One row per order, at its LATEST status.
--
-- The extract is change-data-capture: an order placed on the 20th and refunded
-- on the 9th of the next month is in two partitions, once per status. This
-- keeps the most recent version; raw.orders keeps them all.
with versions as (

    select
        cast(order_id as integer)              as order_id,
        cast(customer_id as integer)           as customer_id,
        cast(ordered_at as timestamp)          as ordered_at,
        lower(trim(status))                    as order_status,
        cast(subtotal as decimal(12, 2))       as order_amount,
        cast(shipping as decimal(12, 2))       as shipping_amount,
        currency,
        cast(updated_at as timestamp)          as updated_at,
        dt                                     as extracted_on,
        row_number() over (
            partition by cast(order_id as integer)
            order by cast(updated_at as timestamp) desc, dt desc
        ) as version_rank
    from {{ source('shop', 'orders') }}

)

select
    order_id,
    customer_id,
    ordered_at,
    -- business date is the UTC calendar date the order was placed on
    cast(ordered_at as date) as order_date,
    order_status,
    order_amount,
    shipping_amount,
    currency,
    updated_at,
    extracted_on
from versions
where version_rank = 1
