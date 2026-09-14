select claim_date, count(*) as claims, sum(claim_amt) as total_amount
from {{ ref('stg_claims') }}
group by claim_date
order by claim_date
