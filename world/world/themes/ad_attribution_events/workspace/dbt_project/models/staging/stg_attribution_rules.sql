select rule_id, lookback_days, billing_lag_days
from {{ source('raw', 'attribution_rules') }}
where rule_id = 'network_default'
