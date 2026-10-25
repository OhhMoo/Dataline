with positions as (
  select c.position_date, u.warehouse_id, u.sku_id
  from {{ ref('stg_inventory_calendar') }} c
  cross join (
    select distinct warehouse_id, sku_id from {{ ref('stg_uom_conversions') }}
  ) u
),
receipts as (
  select event_date as position_date, warehouse_id, sku_id,
         sum(quantity_each) as received_today_each
  from {{ ref('stg_asn_receipts') }}
  where event_type = 'RECEIVED'
  group by event_date, warehouse_id, sku_id
),
movements as (
  select movement_date as position_date, warehouse_id, sku_id,
         sum(case when movement_type = 'OUTBOUND_SHIP' then quantity_each else 0 end)
           as shipped_today_each,
         sum(case when movement_type = 'RETURN_RECEIPT' then quantity_each else 0 end)
           as returns_today_each
  from {{ ref('stg_dc_movements') }}
  group by movement_date, warehouse_id, sku_id
),
counts as (
  select count_date as position_date, warehouse_id, sku_id,
         sum(shrinkage_each) as shrinkage_today_each
  from {{ ref('stg_cycle_counts') }}
  group by count_date, warehouse_id, sku_id
),
transit_changes as (
  select event_date as position_date, warehouse_id, sku_id,
         sum(quantity_each) as transit_delta_each
  from {{ ref('stg_asn_receipts') }}
  where event_type = 'SHIPPED'
  group by event_date, warehouse_id, sku_id
  union all
  select expected_receipt_date as position_date, warehouse_id, sku_id,
         -sum(quantity_each) as transit_delta_each
  from {{ ref('stg_asn_receipts') }}
  where event_type = 'SHIPPED'
  group by expected_receipt_date, warehouse_id, sku_id
),
transit as (
  select position_date, warehouse_id, sku_id, sum(transit_delta_each) as transit_delta_each
  from transit_changes
  group by position_date, warehouse_id, sku_id
),
daily as (
  select
    p.position_date,
    p.warehouse_id,
    p.sku_id,
    coalesce(r.received_today_each, 0) as received_today_each,
    coalesce(m.shipped_today_each, 0) as shipped_today_each,
    coalesce(m.returns_today_each, 0) as returns_today_each,
    coalesce(c.shrinkage_today_each, 0) as shrinkage_today_each,
    coalesce(t.transit_delta_each, 0) as transit_delta_each
  from positions p
  left join receipts r using (position_date, warehouse_id, sku_id)
  left join movements m using (position_date, warehouse_id, sku_id)
  left join counts c using (position_date, warehouse_id, sku_id)
  left join transit t using (position_date, warehouse_id, sku_id)
)
select
  position_date,
  warehouse_id,
  sku_id,
  received_today_each,
  shipped_today_each,
  returns_today_each,
  sum(received_today_each + returns_today_each - shipped_today_each - shrinkage_today_each)
    over (partition by warehouse_id, sku_id order by position_date
          rows between unbounded preceding and current row) as on_hand_each,
  sum(transit_delta_each) over (
    partition by warehouse_id, sku_id order by position_date
    rows between unbounded preceding and current row) as in_transit_each,
  sum(shrinkage_today_each) over (
    partition by warehouse_id, sku_id order by position_date
    rows between unbounded preceding and current row) as cumulative_shrinkage_each
from daily
order by position_date, warehouse_id, sku_id
