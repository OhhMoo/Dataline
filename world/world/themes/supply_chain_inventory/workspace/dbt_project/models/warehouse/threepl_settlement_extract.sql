select
  '3PL-' || replace(cast(p.position_date as varchar), '-', '') || '-' ||
    p.warehouse_id || '-' || p.sku_id as settlement_id,
  p.position_date as settlement_date,
  p.warehouse_id,
  p.sku_id,
  '3PL-ATLAS' as threepl_id,
  p.received_today_each,
  p.shipped_today_each,
  p.on_hand_each as storage_each,
  p.received_today_each * u.receipt_rate_cents as receipt_handling_cents,
  p.shipped_today_each * u.outbound_rate_cents as outbound_handling_cents,
  p.on_hand_each * u.storage_rate_cents as storage_charge_cents,
  p.received_today_each * u.receipt_rate_cents +
    p.shipped_today_each * u.outbound_rate_cents +
    p.on_hand_each * u.storage_rate_cents as total_charge_cents,
  'USD' as currency,
  'READY' as settlement_status
from {{ ref('fct_inventory_position') }} p
join {{ ref('stg_uom_conversions') }} u using (warehouse_id, sku_id)
order by settlement_date, warehouse_id, sku_id
