# Data dictionary

## raw (as landed, every column VARCHAR except `dt`)

- `raw.orders` - one row per order per status change, from
  `orders/dt=*/orders.csv`. An order appears in the partition of the day it
  was placed and again in the partition of the day it was cancelled or
  refunded.
- `raw.order_lines` - lines of each day's new orders. Written once.
- `raw.customers` - the cutover snapshot, then signups and account changes.
- `raw.products` - the cutover catalogue, then launches, price changes and
  retirements.
- `raw.bi_refresh_history` - one row per dashboard per refresh: `refresh_id`,
  `dashboard`, `data_through` (the newest business date it showed),
  `panels`, `panel_rows`. Written by the refresh job, not by the loader.

`dt` is the business date of the extract the row arrived in.

## staging (views)

`stg_orders`, `stg_order_lines`, `stg_customers`, `stg_products`: typed, and
for orders / customers / products the latest version of each record.

## warehouse (tables - what the dashboards read)

| table | grain | columns |
|---|---|---|
| `fct_orders` | order | order_id, order_date, customer_id, order_status, order_amount, shipping_amount |
| `fct_order_lines` | order line | order_line_id, order_id, product_id, quantity, line_amount, order_date, customer_id |
| `dim_customer` | account | customer_id, customer_name, segment, region, signup_date |
| `dim_product` | product | product_id, product_name, category, unit_price |
| `dim_date` | day | date_day, year, quarter, month, month_name, week_start, is_weekend |

Money is DECIMAL(12,2), USD. `order_amount` is the sum of the order's
`line_amount` - `tests/assert_order_amount_matches_lines.sql` fails the build
when it is not. `dim_date` runs from the first order to the last with no gaps,
and is also the semantic layer's time spine.
