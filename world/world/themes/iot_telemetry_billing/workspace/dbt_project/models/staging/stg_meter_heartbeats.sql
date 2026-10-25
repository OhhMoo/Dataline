with ordered as (
    select h.*,
           case when event_kind = 'counter' then
               lag(counter_value) over (
                   partition by device_id order by observed_at, heartbeat_id
               )
           end as previous_counter
    from {{ source('raw', 'meter_heartbeats') }} as h
),
normalized as (
    select heartbeat_id as usage_id,
           device_id,
           customer_id,
           gateway_id,
           cast(observed_at as date) as usage_date,
           event_kind as source_kind,
           observed_at,
           collected_at,
           case
               when event_kind = 'delta' then delta_units
               when previous_counter is null then counter_value
               when counter_value < previous_counter then counter_value
               else counter_value - previous_counter
           end as reported_units,
           case
               when event_kind = 'counter' and previous_counter is not null
                    and counter_value < previous_counter then true
               else false
           end as counter_reset
    from ordered
)
select usage_id, device_id, customer_id, gateway_id, usage_date,
       'telemetry_units' as metric, source_kind, collected_at,
       reported_units, counter_reset
from normalized
where source_kind in ('counter', 'delta')
