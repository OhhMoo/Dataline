-- The grain the controller reviews against. A second row for a period and
-- account means the workbook has been imported twice or the manual adjustment
-- join has fanned out, and either way the dashboard total is wrong.
select period, account, count(*) as rows_at_grain
from {{ ref('fct_gl_monthly') }}
group by period, account
having count(*) > 1
