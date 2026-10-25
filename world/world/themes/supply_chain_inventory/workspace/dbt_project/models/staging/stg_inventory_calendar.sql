select distinct dt as position_date
from {{ source('raw', 'asn_receipts') }}
union
select distinct dt from {{ source('raw', 'dc_movements') }}
union
select distinct dt from {{ source('raw', 'cycle_counts') }}
union
select distinct dt from {{ source('raw', 'uom_conversions') }}
union
select distinct dt from {{ source('raw', 'customs_declarations') }}
