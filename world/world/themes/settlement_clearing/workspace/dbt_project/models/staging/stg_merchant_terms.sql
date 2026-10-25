select
    merchant_id,
    merchant_name,
    cast(interchange_bps as integer) as interchange_bps
from {{ source('raw', 'merchant_terms') }}
