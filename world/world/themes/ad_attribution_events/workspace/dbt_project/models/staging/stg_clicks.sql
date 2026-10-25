select
  c.*,
  exists (
    select 1
    from {{ ref('stg_bot_exclusions') }} as b
    where b.active
      and (
        (b.exclusion_type = 'ip_prefix' and b.exclusion_value = c.ip_prefix)
        or (b.exclusion_type = 'user_agent'
            and lower(c.user_agent) like '%' || lower(b.exclusion_value) || '%')
      )
  ) as is_bot
from {{ source('raw', 'clicks') }} as c
