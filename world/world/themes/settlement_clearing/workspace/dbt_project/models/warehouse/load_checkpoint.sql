select
    max(dt) as through_day,
    cast(max(delivered_at) as timestamp) as updated_at
from {{ source('raw', 'payments_events') }}
