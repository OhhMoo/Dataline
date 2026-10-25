select exclusion_id, exclusion_type, exclusion_value, reason, active
from {{ source('raw', 'bot_exclusions') }}
