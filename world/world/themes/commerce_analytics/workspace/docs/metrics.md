# Metrics

The definitions of record are the dbt metrics in
`dbt_project/models/semantic/metrics.yml`. This file and the wiki page
ae/metrics/net-revenue are the prose versions; when either disagrees with the
YAML, the YAML wins.

## Revenue

| metric | definition |
|---|---|
| Net Revenue | `order_amount` of orders whose status is `completed`, on `order_date`. Excludes refunded and cancelled orders. Excludes shipping. |
| Gross Revenue | `order_amount` of every order, whatever its status. |
| Shipping Revenue | `shipping_amount` of completed orders. |
| Revenue YTD | Net Revenue from 1 January to the date in context. |
| Revenue LY | Net Revenue for the same period one year earlier. |
| YoY Growth | (Net Revenue - Revenue LY) / Revenue LY. Empty where there is no prior year. |

`order_amount` is the merchandise subtotal - the sum of the order's lines at
the price paid. Finance's revenue line in the close pack is Net Revenue +
Shipping Revenue. Ours is not, and that is on purpose (ANA-205, ANA-229).

`order_date` is the UTC calendar date the order was placed on. It is set in
`stg_orders` and has been UTC since cutover.

## Orders and units

| metric | definition |
|---|---|
| Order Count | orders, whatever their status |
| Completed Orders | orders whose status is `completed` |
| AOV | Net Revenue / Completed Orders |
| Units | quantity on order lines, whatever the order's status |

## Only in the Power BI model

MetricFlow cannot express these, so they live as DAX measures in
`bi/powerbi` and nowhere else:

- **Segment Share** - Net Revenue / Net Revenue with the segment filter
  removed.
- **List Price Revenue** - quantity x the product's CURRENT list price
  (`dim_product.unit_price`). It differs from Net Revenue's basis for every
  product whose price has changed (the July 2025 and January 2026 price
  lists) and for refunded and cancelled orders.

## Dimensions

- **Segment** - the account's current segment. Accounts get upgraded (Consumer
  -> SMB -> Enterprise) and `dim_customer` is SCD1, so the upgrade applies to
  the account's whole history. Sales comp is paid on the current book, which
  is why.
- **Region** - NA, EMEA, APAC, from the account's billing country.
- **Category** - Desks, Seating, Accessories, Lighting, Services.
