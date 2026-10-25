with latest as (
  select max(position_date) as position_date from {{ ref('fct_inventory_position') }}
)
select
  c.declaration_id,
  c.asn_id,
  c.warehouse_id,
  c.entry_type,
  c.declaration_status,
  c.declared_at,
  c.expected_arrival_date,
  c.freeze_start_at,
  c.freeze_end_at,
  date_diff('hour', c.freeze_start_at, c.freeze_end_at) as freeze_window_hours,
  date_diff('day', latest.position_date, cast(c.freeze_end_at as date)) as days_to_cutoff,
  c.freeze_end_at <= cast(latest.position_date as timestamp) + interval '1 day'
    as documents_frozen,
  case
    when c.freeze_end_at <= cast(latest.position_date as timestamp) + interval '1 day'
      then 'FROZEN'
    else 'OPEN'
  end as window_status,
  c.document_version
from {{ ref('stg_customs_declarations') }} c
cross join latest
order by c.freeze_end_at, c.declaration_id
