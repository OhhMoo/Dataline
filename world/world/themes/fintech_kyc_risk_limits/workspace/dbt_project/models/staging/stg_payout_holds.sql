select
    event_id,
    hold_id,
    nullif(request_id, '') as request_id,
    merchant_id,
    lower(event_type) as event_type,
    cast(event_at as timestamp) as event_at,
    cast(event_at as date) as event_date,
    cast(reserve_amount as decimal(18, 2)) as reserve_amount,
    cast(release_amount as decimal(18, 2)) as release_amount,
    reason_code,
    nullif(risk_officer_id, '') as risk_officer_id,
    dt as landed_on
from {{ source('raw', 'payout_holds') }}
