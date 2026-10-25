create or replace table warehouse."3pl_settlement_extract" as
select position_date, dc_id, sum(received_each) as inbound_each,
       sum(-moved_each) as outbound_each, sum(on_hand_each) as closing_each
from warehouse.fct_inventory_position group by 1,2 order by 1,2;
create or replace table warehouse.customs_cutoff as
select position_date,
       date_diff('day', position_date, (select max(dt) from raw.asn_receipts)) >= 3 as frozen,
       sum(received_each) as declared_each
from warehouse.fct_inventory_position group by 1 order by 1;
create or replace table warehouse.cycle_count_reconciliation as
select c.count_date, c.sku, c.dc_id, c.counted_each,
       p.on_hand_each as system_each,
       c.counted_each - p.on_hand_each as shrinkage_each
from raw.cycle_counts c
join warehouse.fct_inventory_position p
  on c.count_date=p.position_date and c.sku=p.sku and c.dc_id=p.dc_id
order by c.count_date, c.sku, c.dc_id;
