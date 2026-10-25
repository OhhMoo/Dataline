select
  count_id,
  count_date,
  warehouse_id,
  sku_id,
  book_quantity_each,
  counted_quantity,
  uom,
  counted_each,
  shrinkage_each,
  counted_each as reconciled_on_hand_each,
  case when shrinkage_each = 0 then 'MATCHED' else 'SHRINKAGE' end as reconciliation_status,
  count_method,
  counter_id
from {{ ref('stg_cycle_counts') }}
order by count_date, warehouse_id, sku_id, count_id
