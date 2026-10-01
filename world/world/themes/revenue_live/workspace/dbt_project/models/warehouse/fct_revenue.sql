select order_date, count(*) as orders, sum(amount) as revenue
from {{ ref('fct_revenue_lines') }}
group by order_date
order by order_date
