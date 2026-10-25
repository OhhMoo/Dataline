select f.conversion_id
from {{ ref('fct_attributed_conversions') }} as f
inner join {{ ref('stg_clicks') }} as c on c.click_id = f.click_id
where c.is_bot
