with orders as (
  select order_date, count(*) as orders, sum(amount) as gross_revenue
  from {{ ref('stg_orders') }}
  group by order_date
),
returns as (
  select return_date, count(*) as returns, sum(amount) as return_total
  from {{ ref('stg_returns') }}
  group by return_date
)
select
  o.order_date,
  o.orders,
  coalesce(r.returns, 0) as returns,
  o.gross_revenue,
  coalesce(r.return_total, cast(0 as decimal(12,2))) as return_total,
  o.gross_revenue - coalesce(r.return_total, cast(0 as decimal(12,2))) as net_revenue
from orders o
left join returns r on r.return_date = o.order_date
order by o.order_date
