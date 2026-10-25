select
    merchant_id,
    cast(business_date as date) as business_date,
    cast(settled_transactions as integer) as settled_transactions,
    cast(settled_volume as decimal(18, 2)) as settled_volume,
    currency,
    dt as landed_on
from {{ source('raw', 'merchant_volume') }}
