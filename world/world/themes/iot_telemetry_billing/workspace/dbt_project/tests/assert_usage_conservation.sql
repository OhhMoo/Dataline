select usage_id, reported_units, billable_units
from {{ ref('fct_billable_usage') }}
where reported_units < 0
   or billable_units < 0
   or billable_units > reported_units
