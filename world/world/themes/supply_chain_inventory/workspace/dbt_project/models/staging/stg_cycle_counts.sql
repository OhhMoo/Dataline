select
  c.count_id,
  cast(c.count_date as date) as count_date,
  c.warehouse_id,
  c.sku_id,
  cast(c.book_quantity_each as integer) as book_quantity_each,
  cast(c.counted_quantity as integer) as counted_quantity,
  c.uom,
  cast(c.counted_quantity as integer) * case
    when c.uom = 'CASE' then u.units_per_case else 1 end as counted_each,
  cast(c.book_quantity_each as integer) - cast(c.counted_quantity as integer) * case
    when c.uom = 'CASE' then u.units_per_case else 1 end as shrinkage_each,
  c.count_method,
  c.counter_id
from {{ source('raw', 'cycle_counts') }} c
join {{ ref('stg_uom_conversions') }} u
  on c.warehouse_id = u.warehouse_id and c.sku_id = u.sku_id
