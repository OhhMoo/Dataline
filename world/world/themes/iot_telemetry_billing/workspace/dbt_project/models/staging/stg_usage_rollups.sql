select rollup_id as usage_id,
       device_id,
       customer_id,
       gateway_id,
       cast(window_start as date) as usage_date,
       'telemetry_units' as metric,
       'edge_rollup' as source_kind,
       collected_at,
       units as reported_units,
       false as counter_reset
from {{ source('raw', 'usage_rollups') }}
where window_end > window_start
  and units >= 0
