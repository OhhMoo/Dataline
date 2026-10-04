-- One row per account, latest version (SCD type 1). A segment upgrade
-- rewrites the account's segment for ALL of its history - Enterprise revenue
-- "last year" includes accounts that were SMB last year. Deliberate: sales
-- comp is paid on the current book (docs/metrics.md).
with versions as (

    select
        cast(customer_id as integer)  as customer_id,
        trim(customer_name)           as customer_name,
        lower(trim(email))            as email,
        trim(segment)                 as segment,
        upper(trim(region))           as region,
        upper(trim(country))          as country,
        cast(signup_date as date)     as signup_date,
        cast(updated_at as timestamp) as updated_at,
        row_number() over (
            partition by cast(customer_id as integer)
            order by cast(updated_at as timestamp) desc, dt desc
        ) as version_rank
    from {{ source('shop', 'customers') }}

)

select customer_id, customer_name, email, segment, region, country, signup_date, updated_at
from versions
where version_rank = 1
