-- One row per order line. A partition delivered twice (a vendor resend lands
-- as a second file beside the first) is read twice by the loader; order_id is
-- the business key, so the fact keeps one row per order_id.
select
  order_id,
  order_date,
  customer_id,
  amount,
  terminal_ref,
  dt as landed_dt
from {{ ref('stg_revenue') }}
qualify row_number() over (
  partition by order_id
  order by dt, order_date, customer_id, amount, terminal_ref
) = 1
