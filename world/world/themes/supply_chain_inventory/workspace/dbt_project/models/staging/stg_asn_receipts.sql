select
  a.event_id,
  a.asn_id,
  a.asn_line_id,
  a.edi_control_id,
  a.warehouse_id,
  a.sku_id,
  a.supplier_id,
  a.event_type,
  cast(a.event_date as date) as event_date,
  cast(a.quantity as integer) as quantity,
  a.uom,
  cast(a.quantity as integer) * case
    when a.uom = 'CASE' then u.units_per_case else 1 end as quantity_each,
  cast(a.expected_receipt_date as date) as expected_receipt_date,
  nullif(a.customs_declaration_id, '') as customs_declaration_id
from {{ source('raw', 'asn_receipts') }} a
join {{ ref('stg_uom_conversions') }} u
  on a.warehouse_id = u.warehouse_id and a.sku_id = u.sku_id
