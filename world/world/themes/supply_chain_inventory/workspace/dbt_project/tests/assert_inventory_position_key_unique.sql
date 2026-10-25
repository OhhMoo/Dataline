select position_date, warehouse_id, sku_id, count(*) as row_count
from {{ ref('fct_inventory_position') }}
group by position_date, warehouse_id, sku_id
having count(*) <> 1
