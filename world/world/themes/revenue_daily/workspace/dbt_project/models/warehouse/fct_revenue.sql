select order_date, count(*) as orders, sum(amount) as revenue
from {{ ref('stg_revenue') }}
group by order_date
order by order_date
