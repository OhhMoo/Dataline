select
  line_id,
  account,
  cast(post_date as date) as post_date,
  period,
  cast(amount as decimal(12,2)) as amount
from {{ source('raw', 'gl_lines') }}
