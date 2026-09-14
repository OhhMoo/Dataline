# Data dictionary (extract)

## warehouse.fct_orders

One row per order **revision**. The producer re-emits an order with an
incremented `revision` when a correction is issued. Both the original and the
correction are kept; reporting reads the highest revision per order. The
business key is therefore `(order_id, revision)`, not `order_id` on its own.

| column | type | note |
|---|---|---|
| order_id | BIGINT | business key part 1 |
| revision | INTEGER | business key part 2, `1` on first emission |
| order_dt | DATE | date of the original order; stable across revisions |
| event_ts | TIMESTAMP | when the producer emitted this revision |
| customer_id | BIGINT | |
| channel_code | VARCHAR | joins `warehouse.dim_channel` |
| sku | VARCHAR | |
| qty | INTEGER | |
| unit_price | DECIMAL(10,2) | |
| order_amount | DECIMAL(12,2) | `qty * unit_price` |
| load_ts | TIMESTAMP | audit only, not part of the record |
| batch_id | VARCHAR | audit only, not part of the record |

`fct_orders` holds ninety days. The topic behind it holds thirty. The table is
the only copy of anything older than the retention horizon.

## warehouse.dim_channel

Refreshed by `channel_dim_refresh` on the asset platform, not from this repo.
Rows with `is_curated = true` are hand-maintained mappings that the refresh does
not produce and must not be dropped.
