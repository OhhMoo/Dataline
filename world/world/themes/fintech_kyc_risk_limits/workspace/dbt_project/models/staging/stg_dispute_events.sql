select
    event_id,
    dispute_id,
    merchant_id,
    lower(event_type) as event_type,
    cast(event_at as timestamp) as event_at,
    cast(event_at as date) as event_date,
    cast(opened_at as timestamp) as opened_at,
    cast(dispute_amount as decimal(18, 2)) as dispute_amount,
    reason_code,
    nullif(representment_case_id, '') as representment_case_id,
    dt as landed_on
from {{ source('raw', 'dispute_events') }}
