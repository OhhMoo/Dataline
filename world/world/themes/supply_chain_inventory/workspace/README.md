# Multi-DC inventory and customs cutoff

Only received ASNs add on-hand stock. Cases convert to eaches through the UOM table. Movements reduce stock. Cycle counts are a separate control and customs declarations freeze after three days.

Landing files: data/landing/inventory_edi/dt=YYYY-MM-DD/<feed>.csv. Raw feeds: raw.asn_receipts, raw.dc_movements, raw.cycle_counts, raw.uom_conversions, raw.customs_declarations. Daily fact: warehouse.fct_inventory_position. Publications: warehouse.threepl_settlement_extract, warehouse.customs_cutoff.

The scheduler polls the landing zone, reloads the raw feeds, builds the dbt warehouse models, exports the 3PL settlement (scripts/export_threepl.py), and closes customs windows (scripts/customs_close.sql) in that order.
