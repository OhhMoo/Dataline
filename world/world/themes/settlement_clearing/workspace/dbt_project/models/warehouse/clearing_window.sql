-- Backstory of bank cutoffs already executed at build time. Later records are
-- appended only by pipelines/clearing_window_close.py.
select
    h.settlement_day as closed_on,
    h.settlement_day,
    cast('SETTLED' as varchar) as status,
    cast(sum(h.payments) as bigint) as payments,
    cast(sum(h.net_cents) as bigint) as net_cents
from {{ ref('settlement_handoff') }} as h
cross join {{ source('raw', 'settlement_config') }} as c
where h.settlement_day <= c.cutoff_day
group by h.settlement_day
order by h.settlement_day
