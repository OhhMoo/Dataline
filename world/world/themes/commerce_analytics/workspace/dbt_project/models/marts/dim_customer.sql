-- Column list and types are a contract with the BI layer (bi/README.md): the
-- Power BI model, the Tableau data source and the Sigma workbook all bind to
-- these names. Add columns at the end; never rename one in place.
select
    cast(customer_id as integer)   as customer_id,
    cast(customer_name as varchar) as customer_name,
    cast(segment as varchar)       as segment,
    cast(region as varchar)        as region,
    cast(signup_date as date)      as signup_date
from {{ ref('stg_customers') }}
order by customer_id
