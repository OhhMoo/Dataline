select partner_id, partner_name, revshare_bps, currency
from {{ source('raw', 'partners') }}
