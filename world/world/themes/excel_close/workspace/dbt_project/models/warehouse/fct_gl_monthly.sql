select
  g.period,
  g.account,
  count(*) as lines,
  sum(g.amount) as net_amount,
  coalesce(a.adj_total, 0) as adj_total
from {{ ref('stg_gl_lines') }} g
left join (
  select period, account, sum(cast(amount as decimal(12,2))) as adj_total
  from {{ source('raw', 'manual_adj') }}
  group by period, account
) a using (period, account)
group by g.period, g.account, a.adj_total
order by g.period, g.account
