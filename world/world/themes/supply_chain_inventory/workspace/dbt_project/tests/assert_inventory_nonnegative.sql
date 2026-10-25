select position_date, warehouse_id, sku_id, on_hand_each, in_transit_each
from {{ ref('fct_inventory_position') }}
where on_hand_each < 0 or in_transit_each < 0
