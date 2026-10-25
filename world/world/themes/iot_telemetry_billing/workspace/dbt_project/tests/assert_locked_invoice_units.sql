with expected as (
    select customer_id, billing_month, sum(billable_units) as expected_units
    from {{ ref('fct_billable_usage') }}
    group by customer_id, billing_month
)
select i.invoice_run_id, i.billable_units, e.expected_units
from {{ ref('invoice_run_handoff') }} as i
join expected as e
  on e.customer_id = i.customer_id
 and e.billing_month = i.billing_month
where i.billable_units <> e.expected_units
