-- warn, not error. a returns-only day is legitimate: globex sends returns for
-- orders that landed days earlier, and if acme's file is late the order side
-- of that day is not there yet. it self-corrects on the next load.
{{ config(severity='warn') }}

select order_date, net_revenue
from {{ ref('fct_orders_daily') }}
where net_revenue < 0
