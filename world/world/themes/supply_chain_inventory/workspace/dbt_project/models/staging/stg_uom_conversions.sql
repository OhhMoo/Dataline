select distinct
  warehouse_id,
  sku_id,
  product_family,
  case_uom,
  each_uom,
  cast(units_per_case as integer) as units_per_case,
  cast(receipt_rate_cents as integer) as receipt_rate_cents,
  cast(outbound_rate_cents as integer) as outbound_rate_cents,
  cast(storage_rate_cents as integer) as storage_rate_cents
from {{ source('raw', 'uom_conversions') }}
