select
    merchant_id,
    merchant_name,
    interchange_bps
from {{ ref('stg_merchant_terms') }}
order by merchant_id
