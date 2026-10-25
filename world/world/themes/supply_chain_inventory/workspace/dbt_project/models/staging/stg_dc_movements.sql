select
  m.movement_id,
  cast(m.movement_date as date) as movement_date,
  m.warehouse_id,
  m.sku_id,
  m.movement_type,
  cast(m.quantity as integer) as quantity,
  m.uom,
  cast(m.quantity as integer) * case
    when m.uom = 'CASE' then u.units_per_case else 1 end as quantity_each,
  m.reference_id,
  m.threepl_id
from {{ source('raw', 'dc_movements') }} m
join {{ ref('stg_uom_conversions') }} u
  on m.warehouse_id = u.warehouse_id and m.sku_id = u.sku_id
