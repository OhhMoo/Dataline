-- One row per calendar day from the first order to the last order the
-- warehouse holds - contiguous, no gaps, which is what Power BI's "mark as date
-- table" and MetricFlow's time spine both require. Data-driven bounds rather
-- than current_date: nothing in this project reads a wall clock, so a rebuild
-- of the same data is the same calendar.
--
-- Also the MetricFlow time spine (see _marts.yml). One calendar, one
-- definition of a week.
with bounds as (

    select min(order_date) as first_day, max(order_date) as last_day
    from {{ ref('stg_orders') }}

),

days as (

    select cast(gs as date) as date_day
    from bounds, generate_series(bounds.first_day, bounds.last_day, interval 1 day) as t(gs)

)

select
    date_day,
    cast(year(date_day) as integer)                  as year,
    cast(quarter(date_day) as integer)               as quarter,
    cast(month(date_day) as integer)                 as month,
    cast(monthname(date_day) as varchar)             as month_name,
    -- ISO weeks: Monday start
    cast(date_trunc('week', date_day) as date)       as week_start,
    isodow(date_day) >= 6                            as is_weekend
from days
order by date_day
