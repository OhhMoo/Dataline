select
  declaration_id,
  asn_id,
  warehouse_id,
  entry_type,
  declaration_status,
  cast(declared_at as timestamp) as declared_at,
  cast(expected_arrival_date as date) as expected_arrival_date,
  cast(freeze_start_at as timestamp) as freeze_start_at,
  cast(freeze_end_at as timestamp) as freeze_end_at,
  cast(document_version as integer) as document_version
from {{ source('raw', 'customs_declarations') }}
