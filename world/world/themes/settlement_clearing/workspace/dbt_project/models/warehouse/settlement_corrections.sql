select
    settlement_day,
    ticket,
    approved_by,
    approved_at
from {{ source('raw', 'settlement_corrections') }}
order by settlement_day, approved_at, ticket
