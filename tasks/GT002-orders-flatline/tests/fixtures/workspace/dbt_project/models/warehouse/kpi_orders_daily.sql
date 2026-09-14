{{ config(materialized='view') }}

-- Daily order counts and value by channel group. This is what the exec revenue
-- board reads.
select o.order_dt                            as dt,
       coalesce(c.channel_group, 'unmapped') as channel_group,
       count(*)                              as order_count,
       round(sum(o.order_amount), 2)         as order_value
from {{ source('warehouse', 'fct_orders') }} o
left join {{ source('warehouse', 'dim_channel') }} c
       on c.channel_code = o.channel_code
group by 1, 2
