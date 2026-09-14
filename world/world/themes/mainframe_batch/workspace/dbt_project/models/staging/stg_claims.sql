select
  claim_id,
  cast(strptime(claim_date, '%Y%m%d') as date) as claim_date,
  cust_id,
  cast(claim_amt as decimal(11,2)) as claim_amt
from {{ source('raw', 'claims_extract') }}
